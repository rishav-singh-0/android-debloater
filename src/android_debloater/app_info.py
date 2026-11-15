"""User-triggered public metadata lookups. No ADB calls or removal recommendations."""

import asyncio
import json
import logging
import re
from html import unescape
from html.parser import HTMLParser
from urllib.parse import parse_qs, urlsplit

import httpx

from .models import AppInfo, new_id
from .storage import StorageError

logger = logging.getLogger(__name__)

UAD_URL = (
    "https://raw.githubusercontent.com/Universal-Debloater-Alliance/"
    "universal-android-debloater-next-generation/main/resources/assets/uad_lists.json"
)
CONCURRENCY = 4
CATEGORIES = {
    "COMMUNICATION": "communication",
    "SOCIAL": "communication",
    "FINANCE": "money",
    "SHOPPING": "money",
    "PRODUCTIVITY": "tools",
    "BUSINESS": "tools",
    "HEALTH_AND_FITNESS": "tools",
    "MEDICAL": "tools",
    "MAPS_AND_NAVIGATION": "tools",
    "TRAVEL_AND_LOCAL": "tools",
    "MUSIC_AND_AUDIO": "entertainment",
    "VIDEO_PLAYERS": "entertainment",
    "PHOTOGRAPHY": "entertainment",
    "ENTERTAINMENT": "entertainment",
    "NEWS_AND_MAGAZINES": "entertainment",
    "BOOKS_AND_REFERENCE": "entertainment",
    "TOOLS": "tools",
    "PERSONALIZATION": "tools",
    "WEATHER": "tools",
}
VOID_TAGS = {"area", "base", "br", "col", "embed", "hr", "img", "input", "link", "meta", "source", "wbr"}


class PlayPage(HTMLParser):
    def __init__(self):
        super().__init__()
        self.documents = []
        self.script = None
        self.description = []
        self.depth = 0

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        if tag == "script" and attrs.get("type") == "application/ld+json":
            self.script = []
        if attrs.get("data-g-id") == "description":
            self.depth = 1
        elif self.depth:
            if tag not in VOID_TAGS:
                self.depth += 1
            if tag in {"br", "p", "div", "li"}:
                self.description.append("\n")

    def handle_endtag(self, tag):
        if tag == "script" and self.script is not None:
            self.documents.append("".join(self.script))
            self.script = None
        if self.depth and tag not in VOID_TAGS:
            self.depth -= 1

    def handle_data(self, data):
        if self.script is not None:
            self.script.append(data)
        elif self.depth:
            self.description.append(data)


def clean_text(value, limit):
    if not isinstance(value, str):
        return ""
    return re.sub(r"\n{3,}", "\n\n", unescape(value)).strip()[:limit]


def parse_play(package, html):
    page = PlayPage()
    page.feed(html)
    for document in page.documents:
        try:
            data = json.loads(document)
        except ValueError:
            continue
        items = (
            data if isinstance(data, list) else data.get("@graph", [data]) if isinstance(data, dict) else []
        )
        if not isinstance(items, list):
            continue
        for item in items:
            if not isinstance(item, dict) or item.get("@type") not in (
                "SoftwareApplication",
                "MobileApplication",
            ):
                continue
            url = item.get("url")
            if not isinstance(url, str) or parse_qs(urlsplit(url).query).get("id") != [package]:
                continue
            description = clean_text("".join(page.description) or item.get("description"), 12000)
            name = clean_text(item.get("name"), 120)
            if not description or not name:
                continue
            category = clean_text(item.get("applicationCategory"), 80)
            purpose = (
                "entertainment"
                if category.startswith("GAME_") or category == "GAME"
                else CATEGORIES.get(category)
            )
            return AppInfo(
                name=name,
                description=description,
                purposes=[purpose] if purpose else None,
                category=category.replace("_", " ").capitalize(),
                source="Google Play",
            )
    raise ValueError("Google Play did not return a matching app listing.")


def parse_uad(package, catalog):
    entry = catalog.get(package)
    description = clean_text(entry.get("description"), 12000) if isinstance(entry, dict) else ""
    return AppInfo(description=description, source="UAD-ng") if description else None


async def download(client, url, *, limit=3_000_000, missing_ok=False):
    async with asyncio.timeout(15):
        async with client.stream("GET", url) as response:
            if missing_ok and response.status_code in {404, 410}:
                return None
            response.raise_for_status()
            content = bytearray()
            async for chunk in response.aiter_bytes():
                content.extend(chunk)
                if len(content) > limit:
                    raise ValueError("App information exceeded the download limit.")
            return content.decode("utf-8")


class AppInfoUpdates:
    def __init__(self, store, *, transport=None):
        self.store = store
        self.transport = transport
        self.task = None
        self.job = None
        self.lock = asyncio.Lock()

    def status(self):
        if self.task and self.task.done():
            self.job["running"] = False
        return {**self.job, "warnings": list(self.job["warnings"])} if self.job else None

    async def start(self, device_id):
        async with self.lock:
            if self.task and not self.task.done():
                if self.job["device_id"] != device_id:
                    raise StorageError("App information is already updating for another phone.")
                return self.status()
            device = await asyncio.to_thread(self.store.get, device_id)
            if "app-info.json" in self.store.errors:
                raise StorageError(self.store.errors["app-info.json"])
            self.job = {
                "id": new_id(),
                "device_id": device_id,
                "device_name": device.nickname or device.model,
                "running": True,
                "stop_requested": False,
                "total": len(device.inventory),
                "checked": 0,
                "updated": 0,
                "not_found": 0,
                "failed": 0,
                "warnings": [],
                "error": "",
            }
            self.task = asyncio.create_task(self._run(list(device.inventory)), name="app-info-update")
            return self.status()

    def stop(self):
        if self.task and not self.task.done() and not self.job["stop_requested"]:
            self.job["stop_requested"] = True
            self.task.cancel()
        return self.status()

    async def close(self):
        self.stop()
        if self.task:
            try:
                await self.task
            except asyncio.CancelledError:
                pass

    async def _run(self, packages):
        pending = {}

        async def save():
            if not pending:
                return
            # Finish an atomic save before shutdown closes the store.
            write = asyncio.create_task(asyncio.to_thread(self.store.save_app_info, dict(pending)))
            try:
                await asyncio.shield(write)
            except asyncio.CancelledError:
                await write
                raise
            else:
                self.job["updated"] += len(pending)
                pending.clear()

        try:
            async with httpx.AsyncClient(
                timeout=8,
                limits=httpx.Limits(max_connections=CONCURRENCY),
                headers={"User-Agent": "Android-Debloater/0.1", "Accept-Language": "en"},
                transport=self.transport,
                follow_redirects=False,
            ) as client:
                uad, uad_failed = {}, False
                try:
                    data = await download(client, UAD_URL, limit=8_000_000)
                    uad = await asyncio.to_thread(json.loads, data)
                    if not isinstance(uad, dict):
                        raise TypeError("Unexpected catalog format")
                except httpx.HTTPError, TimeoutError, ValueError, TypeError:
                    uad, uad_failed = {}, True
                    self.job["warnings"].append("The system-app catalog could not be reached.")
                play_unavailable = False
                semaphore = asyncio.Semaphore(CONCURRENCY)

                async def lookup(package):
                    nonlocal play_unavailable
                    async with semaphore:
                        failed = play_unavailable
                        if not play_unavailable:
                            try:
                                html = await download(
                                    client,
                                    f"https://play.google.com/store/apps/details?id={package}&hl=en&gl=US",
                                    missing_ok=True,
                                )
                                if html is not None:
                                    return package, await asyncio.to_thread(parse_play, package, html), False
                            except httpx.HTTPError, TimeoutError:
                                failed = True
                                if not play_unavailable:
                                    play_unavailable = True
                                    self.job["warnings"].append(
                                        "Google Play is unavailable. Try updating again later."
                                    )
                            except ValueError:
                                failed = True
                        cached = self.store.app_info.packages.get(package)
                        if cached and cached.source == "Google Play":
                            return package, None, failed
                        return package, parse_uad(package, uad), failed or uad_failed

                async with asyncio.TaskGroup() as group:
                    tasks = [group.create_task(lookup(package)) for package in packages]
                    for completed in asyncio.as_completed(tasks):
                        package, info, failed = await completed
                        self.job["checked"] += 1
                        if info:
                            pending[package] = info
                        else:
                            self.job["failed" if failed else "not_found"] += 1
                        if len(pending) >= CONCURRENCY or self.job["checked"] == len(packages):
                            await save()
        except asyncio.CancelledError:
            self.job["stop_requested"] = True
        except Exception:
            logger.exception("App information update failed")
            self.job["error"] = (
                "Could not finish updating app information. Previously saved details are kept."
            )
        finally:
            try:
                await save()
            except Exception:
                logger.exception("Could not save app information")
                self.job["error"] = "Could not save app information. Check the saved-data folder."
            self.job["running"] = False
