import asyncio
import json

import httpx
import pytest
from fakes import FakeAdb

from android_debloater.app_info import CONCURRENCY, AppInfoUpdates, download, parse_play, parse_uad
from android_debloater.catalog import Catalog
from android_debloater.models import AppInfo, Device, Override, Package
from android_debloater.server import create_app
from android_debloater.storage import Store


def play_html(package, **fields):
    return (
        '<script type="application/ld+json">'
        + json.dumps(
            {
                "@type": "SoftwareApplication",
                "url": f"https://play.google.com/store/apps/details?id={package}",
                "name": "Example app",
                "description": "A useful app.",
                "applicationCategory": "PRODUCTIVITY",
                **fields,
            }
        )
        + "</script>"
    )


def test_parse_exact_listing_full_description_and_category():
    html = (
        play_html("com.example.app")
        + '<div data-g-id="description">First &amp; second.<br>More <b>detail</b>.</div>'
    )
    info = parse_play("com.example.app", html)
    assert info.description == "First & second.\nMore detail."
    assert info.name == "Example app" and info.purposes == ["tools"]
    assert info.category == "Productivity" and info.source == "Google Play"
    assert parse_play(
        "com.example.app", play_html("com.example.app", applicationCategory="GAME_PUZZLE")
    ).purposes == ["entertainment"]
    assert (
        parse_play(
            "com.example.app", play_html("com.example.app", applicationCategory="NEW_CATEGORY")
        ).purposes
        is None
    )
    for html in [
        play_html("com.somewhere.else"),
        "Consent required",
        play_html("com.example.app", **{"@type": []}),
    ]:
        with pytest.raises(ValueError, match="matching"):
            parse_play("com.example.app", html)


def test_online_descriptions_never_import_removal_advice_or_replace_edits():
    package = "com.android.chrome"
    info = parse_uad(
        package,
        {package: {"description": "Community description", "removal": "Recommended", "critical": False}},
    )
    catalog = Catalog()
    before = catalog.describe(package, {}, {})
    online = catalog.describe(package, {}, {}, {package: info})
    assert online["purpose"] == "Community description"
    for field in ["usefulness", "advice", "impact", "confidence", "purposes"]:
        assert online[field] == before[field]
    info = AppInfo(name="Internet name", description="Description", purposes=["media"], source="Google Play")
    row = catalog.describe(
        package,
        {package: Override(name="My name", purposes=["utilities"])},
        {package: Override(notes="My notes")},
        {package: info},
    )
    assert row["name"] == "My name" and row["purposes"] == ["tools"] and row["notes"] == "My notes"
    assert row["purpose"] == "Description" and row["info_url"].endswith("id=" + package)


def test_async_api_returns_immediately_parallel_requests_do_not_block_apps_or_review(tmp_path):
    async def scenario():
        release, four_started = asyncio.Event(), asyncio.Event()
        active, peak = 0, 0

        async def handler(request):
            nonlocal active, peak
            if request.url.host == "raw.githubusercontent.com":
                return httpx.Response(200, json={"com.example.system": {"description": "System app details"}})
            active += 1
            peak = max(peak, active)
            if active == CONCURRENCY:
                four_started.set()
            try:
                await release.wait()
                package = request.url.params["id"]
                return (
                    httpx.Response(404)
                    if package == "com.example.system"
                    else httpx.Response(200, text=play_html(package))
                )
            finally:
                active -= 1

        adb = FakeAdb(read_only=True)
        app = create_app(tmp_path, adb=adb, info_transport=httpx.MockTransport(handler))
        async with (
            app.router.lifespan_context(app),
            httpx.AsyncClient(transport=httpx.ASGITransport(app), base_url="http://127.0.0.1:8765") as client,
        ):
            headers = {"Origin": "http://127.0.0.1:8765", "X-Session-Token": app.state.token}
            response = await client.post("/api/refresh", json={}, headers=headers)
            device = response.json()["devices"][0]["id"]
            url = f"/api/devices/{device}/app-info"
            adb.calls.clear()
            assert (await client.post(url, json={})).status_code == 403
            response = await asyncio.wait_for(client.post(url, json={}, headers=headers), 1)
            assert response.status_code == 202 and response.json()["running"]
            await asyncio.wait_for(four_started.wait(), 2)
            assert (await client.post(url, json={}, headers=headers)).json()["id"] == response.json()["id"]
            assert (await asyncio.wait_for(client.get("/api/health"), 1)).status_code == 200
            detail = (await asyncio.wait_for(client.get(f"/api/devices/{device}"), 1)).json()
            assert len(detail["packages"]) == 5
            review = await asyncio.wait_for(
                client.post(
                    f"/api/devices/{device}/review",
                    json={"action": "uninstall", "packages": ["com.example.extra"]},
                    headers=headers,
                ),
                1,
            )
            assert review.status_code == 200
            assert (await client.get("/api/app-info")).json()["running"]
            release.set()
            await asyncio.wait_for(app.state.app_info.task, 3)
            status = (await client.get("/api/app-info")).json()
            assert peak == CONCURRENCY and status["checked"] == status["updated"] == 5
            assert not status["running"] and not status["error"] and not status["failed"]
            assert not adb.calls and not adb.mutations
            rows = (await client.get(f"/api/devices/{device}")).json()["packages"]
            system = next(row for row in rows if row["id"] == "com.example.system")
            assert system["purpose"] == "System app details" and system["info_source"] == "UAD-ng"
            assert next(row for row in rows if row["id"] == "com.android.systemui")["protection"]
        reopened = Store(tmp_path)
        reopened.open()
        try:
            assert len(reopened.app_info.packages) == 5
            assert not reopened.get(device).history
        finally:
            reopened.close()

    asyncio.run(scenario())


def make_store(path, count=8):
    store = Store(path)
    store.open()
    device = Device(
        inventory={f"com.example.app{i}": Package(id=f"com.example.app{i}") for i in range(count)}
    )
    store.save(device)
    return store, device


def test_malformed_listing_does_not_abort_other_app_updates(tmp_path):
    async def scenario():
        store, device = make_store(tmp_path, 2)

        async def handler(request):
            if request.url.host == "raw.githubusercontent.com":
                return httpx.Response(200, json={})
            package = request.url.params["id"]
            html = (
                '<script type="application/ld+json">{"@graph": null}</script>'
                if package == "com.example.app0"
                else play_html(package)
            )
            return httpx.Response(200, text=html)

        updates = AppInfoUpdates(store, transport=httpx.MockTransport(handler))
        try:
            await updates.start(device.id)
            await asyncio.wait_for(updates.task, 2)
            job = updates.status()
            assert not job["error"] and job["checked"] == 2
            assert job["updated"] == job["failed"] == 1
            assert "com.example.app1" in store.app_info.packages
        finally:
            await updates.close()
            store.close()

    asyncio.run(scenario())


def test_stop_preserves_completed_results_and_cancels_pending_requests(tmp_path):
    async def scenario():
        store, device = make_store(tmp_path)
        blocked = asyncio.Event()
        active = 0

        async def handler(request):
            nonlocal active
            if request.url.host == "raw.githubusercontent.com":
                return httpx.Response(200, json={})
            package = request.url.params["id"]
            if int(package[-1]) >= 4:
                active += 1
                blocked.set()
                try:
                    await asyncio.Event().wait()
                finally:
                    active -= 1
            return httpx.Response(200, text=play_html(package))

        updates = AppInfoUpdates(store, transport=httpx.MockTransport(handler))
        try:
            await updates.start(device.id)
            await asyncio.wait_for(blocked.wait(), 2)
            async with asyncio.timeout(2):
                while len(store.app_info.packages) < 4:
                    await asyncio.sleep(0.01)
            updates.stop()
            await asyncio.wait_for(updates.close(), 2)
            assert not updates.status()["running"] and updates.status()["stop_requested"]
            assert updates.status()["updated"] == 4 and active == 0
            assert len(store.app_info.packages) == 4
            assert len(store.get(device.id).inventory) == 8
        finally:
            await updates.close()
            store.close()

    asyncio.run(scenario())


def test_network_failure_is_bounded_and_keeps_previous_details(tmp_path):
    async def scenario():
        store, device = make_store(tmp_path, 20)
        old = AppInfo(name="Saved name", description="Saved description", source="Google Play")
        store.save_app_info({"com.example.app0": old})
        calls = []

        async def offline(request):
            calls.append(request)
            raise httpx.ConnectError("Offline", request=request)

        updates = AppInfoUpdates(store, transport=httpx.MockTransport(offline))
        try:
            await updates.start(device.id)
            await asyncio.wait_for(updates.task, 3)
            assert updates.status()["failed"] == 20 and updates.status()["updated"] == 0
            assert len(calls) <= CONCURRENCY + 1 and len(updates.status()["warnings"]) == 2
            assert store.app_info.packages["com.example.app0"] == old
        finally:
            await updates.close()
            store.close()

    asyncio.run(scenario())


def test_download_limits_and_missing_listings():
    async def scenario():
        async with httpx.AsyncClient(
            transport=httpx.MockTransport(lambda request: httpx.Response(200, text="x" * 100))
        ) as client:
            with pytest.raises(ValueError, match="limit"):
                await download(client, "https://play.google.com", limit=10)
        async with httpx.AsyncClient(
            transport=httpx.MockTransport(lambda request: httpx.Response(404))
        ) as client:
            assert await download(client, "https://play.google.com", missing_ok=True) is None

    asyncio.run(scenario())


@pytest.mark.parametrize("status_code", [429, 404, 410])
def test_unavailable_play_does_not_replace_richer_saved_details_with_fallback(tmp_path, status_code):
    async def scenario():
        store, device = make_store(tmp_path, 1)
        old = AppInfo(
            name="Saved name", description="Saved description", purposes=["games"], source="Google Play"
        )
        store.save_app_info({"com.example.app0": old})

        async def handler(request):
            if request.url.host == "raw.githubusercontent.com":
                return httpx.Response(200, json={"com.example.app0": {"description": "Fallback description"}})
            return httpx.Response(status_code)

        updates = AppInfoUpdates(store, transport=httpx.MockTransport(handler))
        try:
            await updates.start(device.id)
            await asyncio.wait_for(updates.task, 2)
            assert updates.status()["failed" if status_code == 429 else "not_found"] == 1
            assert updates.status()["updated"] == 0
            assert store.app_info.packages["com.example.app0"] == old
        finally:
            await updates.close()
            store.close()

    asyncio.run(scenario())


def test_failed_cache_save_keeps_last_good_copy_and_reports_error(tmp_path, monkeypatch):
    async def scenario():
        store, device = make_store(tmp_path, 1)
        old = AppInfo(name="Saved name", description="Saved description", source="Google Play")
        store.save_app_info({"com.example.app0": old})

        async def handler(request):
            if request.url.host == "raw.githubusercontent.com":
                return httpx.Response(200, json={})
            return httpx.Response(200, text=play_html(request.url.params["id"]))

        def disk_full(*args):
            raise OSError("disk full")

        monkeypatch.setattr(store, "_replace", disk_full)
        updates = AppInfoUpdates(store, transport=httpx.MockTransport(handler))
        try:
            await updates.start(device.id)
            await asyncio.wait_for(updates.task, 2)
            assert updates.status()["error"] and not updates.status()["running"]
            assert updates.status()["updated"] == 0
            assert store.app_info.packages["com.example.app0"] == old
            assert (
                json.loads((tmp_path / "app-info.json").read_text())["packages"]["com.example.app0"]["name"]
                == "Saved name"
            )
        finally:
            await updates.close()
            store.close()

    asyncio.run(scenario())


def test_corrupt_info_cache_does_not_overwrite_it_or_hide_phone(tmp_path):
    (tmp_path / "app-info.json").write_text("broken")
    from fastapi.testclient import TestClient

    with TestClient(create_app(tmp_path, adb=FakeAdb()), base_url="http://127.0.0.1:8765") as client:
        headers = {"Origin": "http://127.0.0.1:8765", "X-Session-Token": client.app.state.token}
        devices = client.post("/api/refresh", json={}, headers=headers).json()["devices"]
        assert len(devices) == 1
        assert (
            client.post(f"/api/devices/{devices[0]['id']}/app-info", json={}, headers=headers).status_code
            == 409
        )
    assert (tmp_path / "app-info.json").read_text() == "broken"
