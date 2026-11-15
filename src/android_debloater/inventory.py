from .adb import AdbError, package_list, read_package_states
from .models import Device, Package, now


def scan(adb, transport: str, user: int) -> dict[str, Package]:
    installed = package_list(adb.shell(transport, "pm", "list", "packages", "--user", str(user)))
    system = package_list(adb.shell(transport, "pm", "list", "packages", "-s", "-u", "--user", str(user)))
    third = package_list(adb.shell(transport, "pm", "list", "packages", "-3", "--user", str(user)))
    all_packages = package_list(adb.shell(transport, "pm", "list", "packages", "-u", "--user", str(user)))
    disabled = package_list(adb.shell(transport, "pm", "list", "packages", "-d", "--user", str(user)))
    states = read_package_states(adb, transport, user, installed)
    result = {}
    for name in all_packages.keys() | installed.keys():
        p = states.get(name, Package(id=name, installed=name in installed, available=False))
        if (
            p.installed != (name in installed)
            or name in system
            and p.installation == "third-party"
            or name in third
            and p.installation == "system"
        ):
            raise AdbError("Package inventory changed during the scan. Scan again.")
        p.installation = "system" if name in system else "third-party" if name in third else p.installation
        if name in disabled and p.installed and p.state != "unverified":
            p.state = "installed/disabled"
        result[name] = p
    return result


def reconcile(device: Device, inventory: dict[str, Package]):
    for name, previous in device.inventory.items():
        if name not in inventory:
            inventory[name] = previous.model_copy(
                update={"installed": False, "available": False, "state": "unavailable"}
            )
    for event in device.history:
        if event.user_key != device.user_key:
            continue
        p = inventory.get(event.package)
        if event.status in {"attempting", "pending"}:
            event.status = "unverified" if event.status == "attempting" else "unattempted"
            event.verification = "Interrupted operation. Current state was observed; the cause is unknown."
        if p:
            event.observed_state = p.state
            event.observed_at = now()
    device.inventory = inventory
    device.scanned_at = now()


def removed_entries(device: Device, connected: bool):
    latest = {}
    for event in device.history:
        if event.action == "uninstall" and event.status in {"succeeded", "unverified"}:
            latest[event.package] = event
    result = []
    for name, event in latest.items():
        p = device.inventory.get(name)
        eligible = False
        if event.user_key != device.user_key or device.reconciliation_required:
            recovery = "Previous profile history. Recovery is blocked."
        elif not connected or device.blockers or not device.scanned_at:
            recovery = "Connect to check restore availability"
        elif p and p.installed:
            recovery = "Installed again"
        elif event.status != "succeeded":
            recovery = "Removal was not verified. Review the observed state in History."
        elif event.before.installation != "system":
            recovery = "Manual reinstall required"
        elif not p or not p.available or p.installation != "system":
            recovery = "Unavailable for restore"
        else:
            recovery = "Available for restore"
            eligible = True
        result.append(
            {
                "package": name,
                "removed_at": event.timestamp,
                "recovery": recovery,
                "eligible": eligible,
                "previous_enabled": event.before.enabled,
                "installation": event.before.installation,
                "event_id": event.id,
            }
        )
    return result
