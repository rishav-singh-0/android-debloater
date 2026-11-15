import json
from datetime import UTC, datetime, timedelta
from threading import Event
from unittest.mock import patch

import pytest
from fakes import FakeAdb

from android_debloater.adb import AdbError
from android_debloater.devices import Devices
from android_debloater.inventory import removed_entries
from android_debloater.models import Operation, ReviewRequest
from android_debloater.operations import Operations
from android_debloater.storage import StorageError, Store


@pytest.fixture
def rig(tmp_path):
    store = Store(tmp_path)
    store.open()
    adb = FakeAdb()
    devices = Devices(store, adb)
    devices.refresh(force=True)
    operations = Operations(devices)
    device_id = store.all()[0].id
    yield store, adb, devices, operations, device_id
    operations.close()
    store.close()


def review(rig, packages=None, action="uninstall"):
    return rig[3].review(rig[4], ReviewRequest(action=action, packages=packages or ["com.example.system"]))


def run(rig, plan):
    batch = rig[3].confirm(plan.id)
    rig[3].worker.join(5)
    assert not rig[3].worker.is_alive()
    return rig[3].get_batch(batch["id"])


def test_review_protection_and_durable_attempt_before_mutation(rig):
    store, adb, _, ops, device_id = rig
    plan = review(
        rig, ["com.example.system", "com.example.third", "com.android.systemui", "com.example.home"]
    )
    assert set(plan.excluded) == {"com.android.systemui", "com.example.home"}
    assert plan.recovery["com.example.third"] == "Manual reinstall required"
    assert not adb.mutations

    def before(package):
        path = store.directory / "devices" / f"{device_id}.json"
        record = json.loads(path.read_text())
        entry = next(e for e in record["history"] if e["package"] == package)
        assert entry["status"] == "attempting"
        assert entry["user_id"] == 10
        assert entry["command"]

    adb.before_mutation = before
    batch = run(rig, plan)
    assert all(entry.status == "succeeded" for entry in batch["entries"])
    assert len(adb.mutations) == 2
    assert ops.confirm(plan.id)["id"] == batch["id"]
    assert len(adb.mutations) == 2
    assert all(e.before.installed for e in store.get(device_id).history)


@pytest.mark.parametrize("change", ["version", "firmware", "profile", "role", "expired"])
def test_stale_reviews_and_new_protections_block_without_dispatch(rig, change):
    _, adb, _, ops, _ = rig
    plan = review(rig)
    if change == "version":
        adb.phone["one"]["packages"]["com.example.system"]["version"] += 1
    elif change == "firmware":
        adb.phone["one"]["fingerprint"] = "changed"
    elif change == "profile":
        adb.phone["one"]["user_serial"] += 1
    elif change == "role":
        adb.phone["one"]["role"] = "com.example.system"
    else:
        ops.plans[plan.id].expires_at = (datetime.now(UTC) - timedelta(seconds=1)).isoformat()
    with pytest.raises(AdbError):
        ops.confirm(plan.id)
    assert not adb.mutations


def test_confirmed_batch_outlives_review_deadline(rig):
    _, adb, _, ops, _ = rig
    plan = review(rig, ["com.example.system", "com.example.extra"])
    deadline = datetime.fromisoformat(plan.expires_at)
    with patch("android_debloater.operations.datetime", wraps=datetime) as clock:
        clock.now.return_value = deadline - timedelta(seconds=1)

        def cross_deadline(_):
            clock.now.return_value = deadline + timedelta(seconds=1)

        adb.before_mutation = cross_deadline
        batch = run(rig, plan)
        assert [entry.status for entry in batch["entries"]] == ["succeeded", "succeeded"]
        assert not batch["error"]
        assert ops.confirm(plan.id)["id"] == batch["id"]
        assert len(adb.mutations) == 2


@pytest.mark.parametrize("failure", ["exit_zero", "timeout", "disconnect", "false_success"])
def test_failure_stops_batch_and_never_retries(rig, failure):
    store, adb, _, _, device_id = rig
    plan = review(rig, ["com.example.system", "com.example.extra"])
    if failure == "exit_zero":
        adb.failure = "Failure [DELETE_FAILED_INTERNAL_ERROR]"
    elif failure == "false_success":
        adb.failure = "Success"
    elif failure == "timeout":
        adb.timeout_on = "com.example.system"
    else:
        adb.before_mutation = lambda _: adb.devices_map.clear()
    batch = run(rig, plan)
    assert batch["entries"][0].status in {"failed", "unverified"}
    assert batch["entries"][1].status == "unattempted"
    assert len(adb.mutations) == 1
    assert store.get(device_id).history[1].command == []


def test_storage_failure_prevents_dispatch(rig):
    store, adb, _, ops, _ = rig
    plan = review(rig)
    with patch.object(store, "save", side_effect=StorageError("disk full")), pytest.raises(StorageError):
        ops.confirm(plan.id)
    assert not adb.mutations


def test_attempt_write_failure_prevents_dispatch(rig):
    store, adb, _, _, _ = rig
    plan = review(rig)
    save = store.save

    def fail_attempt(device):
        if any(e.status == "attempting" for e in device.history):
            raise StorageError("disk full")
        save(device)

    with patch.object(store, "save", side_effect=fail_attempt):
        batch = run(rig, plan)
    assert not adb.mutations
    assert batch["error"]


def test_stop_after_current_and_one_global_batch(rig):
    _, adb, _, ops, _ = rig
    plan = review(rig, ["com.example.system", "com.example.extra"])
    entered, release = Event(), Event()

    def hold(_):
        entered.set()
        assert release.wait(3)

    adb.before_mutation = hold
    batch = ops.confirm(plan.id)
    assert entered.wait(3)
    # Cancellation must not wait for the ADB command or acquire the device lock.
    stopped = ops.cancel(batch["id"])
    assert stopped["stop_requested"]
    release.set()
    ops.worker.join(5)
    batch = ops.get_batch(batch["id"])
    assert [e.status for e in batch["entries"]] == ["succeeded", "cancelled"]
    assert len(adb.mutations) == 1


def test_default_change_between_apps_stops_next_removal(rig):
    _, adb, _, _, _ = rig
    plan = review(rig, ["com.example.system", "com.example.extra"])
    adb.before_mutation = lambda _: adb.phone["one"].update(role="com.example.extra")
    batch = run(rig, plan)
    assert [e.status for e in batch["entries"]] == ["succeeded", "failed"]
    assert len(adb.mutations) == 1


def test_restore_disabled_system_and_manual_third_party(rig):
    store, adb, devices, _, device_id = rig
    run(rig, review(rig, ["com.example.system", "com.example.third"]))
    devices.refresh(force=True)
    removed = {row["package"]: row for row in removed_entries(store.get(device_id), True)}
    assert removed["com.example.system"]["eligible"]
    assert removed["com.example.third"]["recovery"] == "Manual reinstall required"
    plan = review(rig, ["com.example.system", "com.example.third"], action="restore")
    assert plan.previous_enabled["com.example.system"] == 3
    assert "com.example.third" in plan.excluded
    batch = run(rig, plan)
    assert batch["entries"][0].status == "succeeded"
    assert adb.phone["one"]["packages"]["com.example.system"]["enabled"] == 3
    assert len(store.get(device_id).history) == 3
    assert removed_entries(store.get(device_id), True)[0]["recovery"] == "Installed again"
    # Another cycle appends events rather than replacing the old removal.
    run(rig, review(rig))
    assert len(store.get(device_id).history) == 4


def test_uninstall_with_retained_data_verifies_and_allows_future_reviews(rig):
    store, adb, devices, _, device_id = rig
    adb.phone["one"]["packages"]["com.example.third"]["dump_installed"] = True
    batch = run(rig, review(rig, ["com.example.third", "com.example.extra"]))
    assert [entry.status for entry in batch["entries"]] == ["succeeded", "succeeded"]
    assert len(adb.mutations) == 2
    devices.refresh(force=True)
    device = store.get(device_id)
    assert not device.blockers
    assert not device.inventory["com.example.third"].installed
    removed = {row["package"]: row for row in removed_entries(device, True)}
    assert removed["com.example.third"]["recovery"] == "Manual reinstall required"
    assert not removed["com.example.third"]["eligible"]
    plan = review(rig, ["com.example.system", "com.example.third"])
    assert [package.id for package in plan.packages] == ["com.example.system"]
    assert "com.example.third" in plan.excluded


def test_restore_partial_recovery_keeps_installation_result(rig):
    store, adb, _, _, device_id = rig
    run(rig, review(rig))
    plan = review(rig, action="restore")

    def fail_enabled(_):
        if adb.phone["one"]["packages"]["com.example.system"]["installed"]:
            adb.failure = "Failure [state denied]"

    adb.before_mutation = fail_enabled
    batch = run(rig, plan)
    assert batch["entries"][0].status == "partial"
    assert "Installation succeeded" in batch["entries"][0].verification
    assert store.get(device_id).inventory["com.example.system"].installed


def test_unavailable_firmware_and_disconnected_recovery(rig):
    store, adb, devices, _, device_id = rig
    run(rig, review(rig))
    adb.phone["one"]["packages"]["com.example.system"]["available"] = False
    adb.phone["one"]["fingerprint"] = "firmware-v2"
    devices.refresh(force=True)
    assert removed_entries(store.get(device_id), True)[0]["recovery"] == "Unavailable for restore"
    assert (
        removed_entries(store.get(device_id), False)[0]["recovery"] == "Connect to check restore availability"
    )


def test_restart_reconciles_attempt_without_inventing_cause(rig):
    store, adb, devices, _, device_id = rig
    d = store.get(device_id)
    p = d.inventory["com.example.system"]
    d.history.append(
        Operation(
            batch_id="interrupted",
            action="uninstall",
            package=p.id,
            user_id=10,
            user_key=d.user_key,
            before=p,
            recovery="system",
            status="attempting",
        )
    )
    store.save(d)
    adb.phone["one"]["packages"][p.id]["installed"] = False
    devices.refresh(force=True)
    entry = store.get(device_id).history[0]
    assert entry.status == "unverified"
    assert entry.observed_state == "removed"
    assert "cause is unknown" in entry.verification
    assert not adb.mutations


def test_read_only_server_can_review_but_never_confirm(rig):
    _, adb, _, ops, _ = rig
    adb.read_only = True
    plan = review(rig)
    with pytest.raises(AdbError, match="read-only"):
        ops.confirm(plan.id)
    assert not adb.mutations


def test_review_uses_displayed_inventory_without_adb_or_writes(rig):
    store, adb, _, _, device_id = rig
    before = store.get(device_id)
    adb.calls.clear()
    plan = review(rig)
    assert [p.id for p in plan.packages] == ["com.example.system"]
    assert not adb.calls
    assert store.get(device_id) == before


@pytest.mark.parametrize("change", ["role", "version", "profile", "firmware", "disconnect", "unreadable"])
def test_changes_since_scan_are_rejected_on_confirmation(rig, change):
    _, adb, _, ops, _ = rig
    phone = adb.phone["one"]
    if change == "role":
        phone["role"] = "com.example.system"
    elif change == "version":
        phone["packages"]["com.example.system"]["version"] += 1
    elif change == "profile":
        phone["user_serial"] += 1
    elif change == "firmware":
        phone["fingerprint"] = "changed"
    elif change == "disconnect":
        adb.devices_map.clear()
    else:
        adb.bad_policy = True
    plan = review(rig)
    with pytest.raises(AdbError):
        ops.confirm(plan.id)
    assert not adb.mutations


def test_review_does_not_wait_for_background_scan(rig):
    from concurrent.futures import ThreadPoolExecutor

    _, adb, devices, ops, _ = rig
    entered, release = Event(), Event()

    def scanning():
        with devices.lock:
            devices.transports = {}  # Refresh rebuilds connection state while its queries run.
            entered.set()
            assert release.wait(3)

    with ThreadPoolExecutor(max_workers=2) as executor:
        scan = executor.submit(scanning)
        assert entered.wait(3)
        preview = executor.submit(review, rig)
        try:
            plan = preview.result(timeout=0.5)
            assert plan.packages
        finally:
            release.set()
            scan.result(timeout=3)
    with pytest.raises(AdbError, match="Connect"):
        ops.confirm(plan.id)
    assert not adb.mutations


def test_duplicate_confirmation_returns_while_command_is_running(rig):
    from concurrent.futures import ThreadPoolExecutor

    _, adb, _, ops, _ = rig
    plan = review(rig)
    entered, release, returned = Event(), Event(), Event()

    def hold(_):
        entered.set()
        assert release.wait(3)

    adb.before_mutation = hold
    batch = ops.confirm(plan.id)
    assert entered.wait(3)

    def duplicate():
        result = ops.confirm(plan.id)
        returned.set()
        return result

    with ThreadPoolExecutor(max_workers=1) as executor:
        duplicate_result = executor.submit(duplicate)
        responsive = returned.wait(0.2)
        release.set()
        assert duplicate_result.result(timeout=3)["id"] == batch["id"]
    assert responsive, "Duplicate confirmation must not hold up cancellation behind an ADB command"
