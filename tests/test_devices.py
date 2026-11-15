from concurrent.futures import ThreadPoolExecutor
from threading import Event

import pytest
from fakes import FakeAdb

from android_debloater.adb import AdbError
from android_debloater.devices import Devices, probe
from android_debloater.storage import Store


@pytest.mark.parametrize(
    "state", ["recovery", "sideload", "bootloader", "rescue", "authorizing", "connecting"]
)
def test_unready_phone_does_not_hide_another_connected_phone(tmp_path, state):
    adb = FakeAdb()
    adb.devices_map["usb-two"] = "two"
    adb.phone["two"]["state"] = state
    store = Store(tmp_path)
    store.open()
    try:
        devices = Devices(store, adb)
        devices.refresh(force=True)
        assert not devices.error
        device = store.all()[0]
        assert devices.connected(device.id) and device.inventory
        assert len(devices.pending) == 1
        assert devices.pending[0]["state"] != "Connected"
        assert devices.pending[0]["reason"]
        assert not any(call[:2] == ["-s", "usb-two"] for call in adb.calls)
    finally:
        store.close()


def test_inventory_remains_browsable_when_capability_check_fails(tmp_path, monkeypatch):
    from android_debloater.models import ReviewRequest
    from android_debloater.operations import Operations

    adb = FakeAdb()
    shell = adb.shell

    def without_help(transport, *args):
        if args == ("pm", "help"):
            raise AdbError("Package-manager help is unavailable")
        return shell(transport, *args)

    monkeypatch.setattr(adb, "shell", without_help)
    store = Store(tmp_path)
    store.open()
    try:
        devices = Devices(store, adb)
        devices.refresh(force=True)
        device = store.all()[0]
        assert len(device.inventory) == 5 and device.scanned_at
        assert device.blockers and not device.capabilities
        with pytest.raises(AdbError, match="help is unavailable"):
            Operations(devices).review(
                device.id, ReviewRequest(action="uninstall", packages=["com.example.extra"])
            )
        assert not adb.mutations
    finally:
        store.close()


@pytest.mark.parametrize(
    ("android_id", "valid"),
    [
        ("0", True),
        ("123456789abcdef", True),
        ("0123456789abcdef", True),
        ("", False),
        ("null", False),
        ("123456789abcdef01", False),
        ("123456789abcdeg", False),
    ],
)
def test_android_identity_accepts_short_hex_values_and_rejects_invalid_values(android_id, valid):
    adb = FakeAdb()
    adb.phone["one"]["android_id"] = android_id
    info = probe(adb, "usb-one")
    assert bool(info["reset_key"]) == valid
    assert bool(info["blockers"]) != valid


def test_android_11_oem_inventory_is_saved(tmp_path, monkeypatch):
    adb = FakeAdb()
    adb.phone["one"]["packages"]["oplus"] = {"system": True, "installed": True, "enabled": 0, "version": 80}
    run = adb._run

    def android_11_help(args, timeout=30):
        result = run(args, timeout)
        if args[3:] == ["pm", "help"]:
            result.code = 255
            result.output = "Package manager (package) commands:\n" + result.output
        return result

    monkeypatch.setattr(adb, "_run", android_11_help)
    store = Store(tmp_path)
    store.open()
    try:
        devices = Devices(store, adb)
        devices.refresh(force=True)
        device = store.all()[0]
        assert not device.blockers
        assert len(device.inventory) == 6
        assert device.inventory["oplus"].installed
        assert device.capabilities["uninstall"]
        assert not adb.mutations
    finally:
        store.close()
    store.open()
    try:
        assert store.all()[0].inventory["oplus"].installed
    finally:
        store.close()


@pytest.mark.parametrize("change", ["version", "firmware", "profile"])
def test_offline_alias_before_usb_does_not_skip_refresh(tmp_path, monkeypatch, change):
    store = Store(tmp_path)
    store.open()
    try:
        adb = FakeAdb()
        devices = Devices(store, adb)
        devices.refresh(force=True)
        device = store.all()[0]
        usb, wifi = adb.devices()
        monkeypatch.setattr(adb, "devices", lambda: [{**wifi, "state": "Offline"}, usb])
        adb.phone["one"]["packages"]["com.example.system"]["version"] = 99
        if change == "firmware":
            adb.phone["one"]["fingerprint"] = "test/firmware/2"
        elif change == "profile":
            adb.phone["one"]["android_id"] = "fedcba9876543210"
        devices.refresh(force=True)
        refreshed = store.get(device.id)
        assert refreshed.inventory["com.example.system"].version == 99
        assert refreshed.fingerprint == adb.phone["one"]["fingerprint"]
        assert refreshed.reconciliation_required == (change == "profile")
        assert not refreshed.blockers and not devices.pending
        assert devices.transport(device.id) == "usb-one"
        assert len(devices.transports[device.id]) == 2
        assert not adb.mutations
    finally:
        store.close()


def test_two_transports_one_history_and_same_model_different_device(tmp_path):
    store = Store(tmp_path)
    store.open()
    adb = FakeAdb()
    devices = Devices(store, adb)
    devices.refresh()
    assert not devices.error
    assert len(store.all()) == 1
    first = store.all()[0]
    assert first.managed_user == 10
    assert len(devices.transports[first.id]) == 2
    assert not first.blockers
    adb.devices_map["usb-two"] = "two"
    devices.refresh()
    assert len(store.all()) == 2
    adb.devices_map = {}
    devices.refresh()
    assert all(d["connection_state"] == "Disconnected" for d in devices.summaries())
    assert store.get(first.id).inventory
    assert not adb.mutations
    store.close()


@pytest.mark.parametrize("result", ["connected", "disconnected", "error"])
def test_refresh_keeps_connections_visible_until_discovery_finishes(tmp_path, monkeypatch, result):
    store = Store(tmp_path)
    store.open()
    try:
        adb = FakeAdb()
        devices = Devices(store, adb)
        devices.refresh()
        device_id = store.all()[0].id
        previous = devices.transports.copy()
        discover = adb.devices
        entered, release = Event(), Event()

        def slow_discovery():
            entered.set()
            assert release.wait(3)
            if result == "error":
                raise AdbError("Discovery failed")
            return discover() if result == "connected" else []

        monkeypatch.setattr(adb, "devices", slow_discovery)
        with ThreadPoolExecutor(max_workers=1) as executor:
            refresh = executor.submit(devices.refresh)
            try:
                assert entered.wait(3)
                assert devices.connected(device_id)
                assert devices.transports == previous
                assert devices.summaries()[0]["connection_state"] == "Connected"
            finally:
                release.set()
                refresh.result(timeout=3)
        assert devices.connected(device_id) == (result == "connected")
        assert bool(devices.error) == (result == "error")
        assert not adb.mutations
    finally:
        store.close()


def test_ambiguous_identity_needs_explicit_matching(tmp_path):
    store = Store(tmp_path)
    store.open()
    adb = FakeAdb()
    adb.phone["one"]["serial"] = ""
    adb.devices_map = {"10.0.0.1:5555": "one"}
    devices = Devices(store, adb)
    devices.refresh()
    assert not store.all()
    assert devices.pending[0]["match_required"]
    device_id = devices.match("10.0.0.1:5555")
    assert len(store.all()) == 1
    assert devices.connected(device_id)
    store.close()


def test_reset_needs_reconciliation_and_role_uncertainty_blocks(tmp_path):
    store = Store(tmp_path)
    store.open()
    adb = FakeAdb()
    devices = Devices(store, adb)
    devices.refresh()
    device = store.all()[0]
    adb.phone["one"]["android_id"] = "fedcba9876543210"
    devices.refresh(force=True)
    assert store.get(device.id).reconciliation_required
    devices.acknowledge(device.id)
    assert not store.get(device.id).reconciliation_required
    adb.bad_policy = True
    devices.refresh(force=True)
    assert "administrators" in ";".join(store.get(device.id).blockers)
    store.close()


def test_all_transport_aliases_persist_and_probe_failures_stay_read_only(tmp_path):
    store = Store(tmp_path)
    store.open()
    adb = FakeAdb()
    devices = Devices(store, adb)
    devices.refresh()
    device = store.all()[0]
    assert set(device.aliases) == set(adb.devices_map)
    adb.bad_policy = True
    devices.refresh(force=True)
    assert store.get(device.id).blockers
    devices.refresh()
    assert store.get(device.id).blockers
    store.close()


def test_ambiguous_stable_serial_can_be_matched_explicitly(tmp_path):
    store = Store(tmp_path)
    store.open()
    adb = FakeAdb()
    adb.devices_map = {"usb-one": "one"}
    devices = Devices(store, adb)
    devices.refresh()
    first = store.all()[0]
    duplicate = first.model_copy(deep=True)
    from android_debloater.models import new_id

    duplicate.id = new_id()
    store.save(duplicate)
    devices.refresh()
    assert devices.pending[0]["match_required"]
    devices.match("usb-one", first.id)
    assert devices.connected(first.id)
    assert not devices.connected(duplicate.id)
    store.close()


def test_conflicting_transports_never_share_a_mutation_target(tmp_path):
    store = Store(tmp_path)
    store.open()
    adb = FakeAdb()
    adb.phone["two"]["serial"] = "one"
    adb.phone["two"]["android_id"] = "fedcba9876543210"
    adb.devices_map = {"usb-one": "one", "10.0.0.2:5555": "two"}
    devices = Devices(store, adb)
    devices.refresh()
    assert len(store.all()) == 1
    device = store.all()[0]
    assert len(devices.transports[device.id]) == 1
    assert device.blockers
    assert devices.pending[0]["match_required"]
    store.close()
