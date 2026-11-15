"""Separate durable hardware identity from disposable USB/Wi-Fi addresses."""

import hashlib
import re
from threading import RLock

from .adb import AdbError, main_profile, parse_profiles
from .inventory import reconcile, scan
from .models import Device, now
from .protection import live_protection
from .storage import StorageError


def probe(adb, transport):
    prop = lambda name: adb.shell(transport, "getprop", name).strip()
    serial = prop("ro.serialno") or prop("ro.boot.serialno")
    if serial.lower() in {"unknown", "null", "0123456789abcdef"}:
        serial = ""
    info = {
        "serial": serial,
        "manufacturer": prop("ro.product.manufacturer"),
        "model": prop("ro.product.model"),
        "android": prop("ro.build.version.release"),
        "fingerprint": prop("ro.build.fingerprint"),
    }
    sdk = prop("ro.build.version.sdk")
    info["sdk"] = int(sdk) if sdk.isdigit() else 0
    blockers = []
    if info["sdk"] < 29:
        blockers.append("Android 10 or later is required for phone changes.")
    if not info["fingerprint"] or not info["manufacturer"] or not info["model"]:
        blockers.append("Device identity or firmware metadata is incomplete.")
    current = adb.shell(transport, "am", "get-current-user").strip()
    if not current.isdigit():
        raise AdbError("Cannot identify the current Android user.")
    current = int(current)
    try:
        profiles = parse_profiles(adb.shell(transport, "dumpsys", "user"))
    except AdbError:
        profiles = parse_profiles(adb.shell(transport, "pm", "list", "users"))
    try:
        reported = adb.shell(transport, "cmd", "user", "get-main-user").strip()
        reported = int(reported) if reported.isdigit() else None
    except AdbError:
        reported = None
    profile, issue = main_profile(profiles, current, reported)
    if issue:
        blockers.append(issue)
    user = profile.id if profile else current
    reset_key = ""
    try:
        android_id = adb.shell(transport, "settings", "--user", str(user), "get", "secure", "android_id")
        if not re.fullmatch(r"[0-9a-fA-F]{1,16}", android_id):
            raise AdbError("Android user identity is unavailable.")
        reset_key = hashlib.sha256(android_id.encode()).hexdigest()
    except AdbError:
        blockers.append("Cannot read the Android user identity needed to detect a device reset.")
    info.update(
        profiles=profiles,
        managed_user=profile.id if profile else None,
        user_key=f"{user}:{profile.serial}:{reset_key}" if profile else "",
        reset_key=reset_key,
        blockers=blockers,
        scan_user=user,
    )
    return info


def same_hardware(device, info):
    return bool(
        device.serial
        and info["serial"] == device.serial
        and info["manufacturer"] == device.manufacturer
        and info["model"] == device.model
    )


class Devices:
    def __init__(self, store, adb):
        self.store, self.adb = store, adb
        self.lock = RLock()
        self.transports: dict[str, list[dict]] = {}
        self.pending: list[dict] = []
        self.explicit_matches: dict[str, str] = {}
        self.error = ""
        self.active = False

    def connected(self, device_id):
        return any(t["state"] == "Connected" for t in self.transports.get(device_id, []))

    def transport(self, device_id):
        live = [t for t in self.transports.get(device_id, []) if t["state"] == "Connected"]
        if not live:
            raise AdbError("Connect and authorize this device, then scan again.")
        return min(live, key=lambda t: t["connection"] != "USB")["transport"]

    def refresh(self, force=False):
        with self.lock:
            if self.active:
                raise AdbError("A batch is running. You can browse saved inventory; scan after it finishes.")
            was_connected = {key for key in self.transports if self.connected(key)}
            # Keep the last connection snapshot visible while ADB queries run.
            transports, pending, error = {}, [], ""
            try:
                discovered = self.adb.devices()
            except AdbError as exc:
                self.transports, self.pending, self.error = {}, [], str(exc)
                return
            for transport in discovered:
                address = transport["transport"]
                if transport["state"] != "Connected":
                    # Unverified transport addresses can show status, but cannot authorize any action.
                    matches = [d for d in self.store.all() if address in d.aliases]
                    if len(matches) == 1:
                        transports.setdefault(matches[0].id, []).append(transport)
                    else:
                        pending.append(transport)
                    continue
                try:
                    info = probe(self.adb, address)
                    matches = [d for d in self.store.all() if same_hardware(d, info)]
                    if address in self.explicit_matches:
                        matched = self.store.get(self.explicit_matches[address])
                        if (
                            matched.manufacturer == info["manufacturer"]
                            and matched.model == info["model"]
                            and (not info["serial"] or matched.serial == info["serial"])
                        ):
                            matches = [matched]
                    if len(matches) > 1 or not info["serial"] and not matches:
                        pending.append(
                            {
                                **transport,
                                "reason": "Stable serial unavailable or ambiguous. Match explicitly.",
                                "match_required": True,
                                "manufacturer": info["manufacturer"],
                                "model": info["model"],
                            }
                        )
                        continue
                    if not matches and any(
                        k not in {"settings.json", "category-overrides.json", "app-info.json"}
                        for k in self.store.errors
                    ):
                        raise StorageError(
                            "Resolve unreadable device records before creating a new device history."
                        )
                    device = (
                        matches[0]
                        if matches
                        else Device(
                            serial=info["serial"], manufacturer=info["manufacturer"], model=info["model"]
                        )
                    )
                    if any(t["state"] == "Connected" for t in transports.get(device.id, [])):
                        if device.user_key != info["user_key"] or device.fingerprint != info["fingerprint"]:
                            issue = "Transports report conflicting profile or firmware identities. Match them explicitly or create separate records."
                            device.blockers.append(issue)
                            self.store.save(device)
                            pending.append({**transport, "reason": issue, "match_required": True})
                            continue
                        transports[device.id].append(transport)
                        if address not in device.aliases:
                            device.aliases.append(address)
                            self.store.save(device)
                        continue
                    changed = bool(device.user_key and device.user_key != info["user_key"])
                    if changed:
                        device.reconciliation_required = True
                    rescan = (
                        force
                        or bool(device.blockers)
                        or device.id not in was_connected
                        or device.fingerprint != info["fingerprint"]
                        or changed
                    )
                    for key in [
                        "android",
                        "sdk",
                        "fingerprint",
                        "profiles",
                        "managed_user",
                        "user_key",
                        "reset_key",
                        "blockers",
                    ]:
                        setattr(device, key, info[key])
                    if device.fingerprint not in device.firmware_history:
                        device.firmware_history.append(device.fingerprint)
                    if address not in device.aliases:
                        device.aliases.append(address)
                    device.last_seen = now()
                    transports.setdefault(device.id, []).append(transport)
                    if rescan:
                        self.scan_device(device, address, info["scan_user"])
                    self.store.save(device)
                except (AdbError, StorageError) as exc:
                    pending.append({**transport, "reason": str(exc)})
                    error = str(exc)
            self.transports, self.pending, self.error = transports, pending, error

    def scan_device(self, device, address, user):
        device.capabilities = {}
        try:
            reconcile(device, scan(self.adb, address, user))
            help_text = self.adb.shell(address, "pm", "help")
            uninstall = re.search(r"uninstall \[-k\].*?(?=\n\s*\n|\Z)", help_text, re.DOTALL)
            device.capabilities = {
                "uninstall": bool(uninstall and "--user" in uninstall[0]),
                "version_check": bool(uninstall and "--versionCode" in uninstall[0]),
                "restore": bool(re.search(r"install-existing \[--user", help_text)),
                "enabled_state": all(
                    command in help_text
                    for command in ["default-state", "disable-user", "disable-until-used", "enable"]
                ),
            }
            try:
                device.protection = live_protection(self.adb, address, user)
            except AdbError as exc:
                device.blockers.append(str(exc))
        except AdbError as exc:
            device.blockers.append(str(exc))

    def match(self, address, device_id=None):
        with self.lock:
            if self.active:
                raise AdbError("Wait for the current batch to finish.")
            if not any(t["transport"] == address and t.get("match_required") for t in self.pending):
                raise AdbError("Refresh and choose an ambiguous connected transport.")
            info = probe(self.adb, address)
            if device_id:
                device = self.store.get(device_id)
                if info["manufacturer"] != device.manufacturer or info["model"] != device.model:
                    raise AdbError("Manufacturer and model differ. Keep these device histories separate.")
                if device.serial and info["serial"] and info["serial"] != device.serial:
                    raise AdbError("Hardware serials differ. These histories cannot be matched.")
            else:
                device = Device(serial=info["serial"], manufacturer=info["manufacturer"], model=info["model"])
                self.store.save(device)
            self.explicit_matches[address] = device.id
            self.refresh(force=True)
            return device.id

    def validate(self, device):
        if device.reconciliation_required:
            raise AdbError(
                "Profile identity changed or the device was reset. Review and reconcile before changing apps."
            )
        if device.blockers or device.managed_user is None:
            raise AdbError("; ".join(device.blockers) or "Main personal profile is unknown.")
        address = self.transport(device.id)
        info = probe(self.adb, address)
        identity_ok = same_hardware(device, info) or (
            not info["serial"]
            and self.explicit_matches.get(address) == device.id
            and info["manufacturer"] == device.manufacturer
            and info["model"] == device.model
        )
        if not identity_ok or any(
            info[key] != getattr(device, key)
            for key in ["fingerprint", "user_key", "reset_key", "managed_user"]
        ):
            raise AdbError("Device, firmware, or personal profile changed. Scan and review again.")
        if info["blockers"]:
            raise AdbError("; ".join(info["blockers"]))
        return address

    def acknowledge(self, device_id):
        with self.lock:
            if self.active:
                raise AdbError("Wait for the current batch to finish.")
            self.refresh(force=True)
            device = self.store.get(device_id)
            if not self.connected(device_id) or device.blockers or not device.scanned_at:
                raise AdbError("A successful connected scan is required before reconciliation.")
            device.reconciliation_required = False
            self.store.save(device)

    def summaries(self):
        return [
            {
                **d.model_dump(exclude={"inventory", "history", "overrides"}),
                "transports": self.transports.get(d.id, []),
                "connection_state": "Connected"
                if self.connected(d.id)
                else next((t["state"] for t in self.transports.get(d.id, [])), "Disconnected"),
                "package_count": sum(p.installed for p in d.inventory.values()),
            }
            for d in self.store.all()
        ]
