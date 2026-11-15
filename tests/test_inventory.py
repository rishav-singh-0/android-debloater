import pytest
from fakes import FakeAdb

from android_debloater.adb import (
    AdbError,
    main_profile,
    package_list,
    package_states,
    parse_devices,
    parse_profiles,
)
from android_debloater.devices import same_hardware
from android_debloater.inventory import scan
from android_debloater.models import Device, Package, ReviewRequest
from android_debloater.operations import read_package

DUMP = """Packages:
  Package [com.example.updated] (123):
    codePath=/data/app/updated
    versionCode=42 minSdk=29
    pkgFlags=[ SYSTEM HAS_CODE UPDATED_SYSTEM_APP ]
    User 10: installed=true hidden=false suspended=false enabled=3
Hidden system packages:
  Package [com.example.updated] (456):
    codePath=/system/app/old
    versionCode=1
    flags=[ SYSTEM ]
    User 10: installed=true enabled=0
"""


def test_updated_system_and_disabled_are_independent():
    p = package_states(DUMP, 10)["com.example.updated"]
    assert p.installation == "system"
    assert p.version == 42
    assert p.state == "installed/disabled"
    assert p.enabled == 3
    assert not package_states(DUMP, 0)


@pytest.mark.parametrize(
    "missing",
    [
        "User 10: installed=true hidden=false suspended=false enabled=3",
        "pkgFlags=[ SYSTEM HAS_CODE UPDATED_SYSTEM_APP ]",
    ],
)
def test_unreadable_current_package_never_uses_hidden_factory_metadata(missing):
    assert not package_states(DUMP.replace(missing, "unrecognized metadata"), 10)


def test_package_metadata_cannot_leak_from_another_dump_section():
    dump = (
        DUMP.split("Hidden system packages:")[0].replace(
            "User 10: installed=true hidden=false suspended=false enabled=3",
            "User 0: installed=true enabled=0",
        )
        + "Shared users:\n  SharedUser [android.uid.system]:\n    User 10: installed=true enabled=0\n"
    )
    assert not package_states(dump, 10)


@pytest.mark.parametrize("user", [0, 10])
def test_archived_state_is_scoped_to_its_user(user):
    dump = DUMP.split("Hidden system packages:")[0] + (
        "      archiveTime=2025-01-01 10:00:00\n"
        "      unarchiveInstallerTitle=Store\n"
        "    User 0: installed=true hidden=false suspended=false enabled=0\n"
    )
    package = package_states(dump, user)["com.example.updated"]
    assert package.unusual == (user == 10)


@pytest.mark.parametrize("reader", [scan, read_package])
def test_duplicate_current_package_records_are_rejected(reader, monkeypatch):
    adb = FakeAdb()
    dump = adb.dump
    monkeypatch.setattr(adb, "dump", lambda *args: dump(*args) + dump(*args).split("\nPackages:\n", 1)[1])
    with pytest.raises(AdbError, match="Duplicate"):
        if reader is scan:
            reader(adb, "usb-one", 10)
        else:
            reader(adb, "usb-one", 10, "com.example.third")


@pytest.mark.parametrize("reader", [scan, read_package])
def test_retained_data_does_not_make_a_removed_app_installed(reader):
    adb = FakeAdb()
    name = "com.example.third"
    adb.phone["one"]["packages"][name].update(installed=False, dump_installed=True)
    for _ in range(2):
        package = reader(adb, "usb-one", 10)[name] if reader is scan else reader(adb, "usb-one", 10, name)
        assert not package.installed and not package.available
        assert package.state == "unavailable"
        assert package.installation == "third-party" and package.version == 4
    assert not adb.mutations


@pytest.mark.parametrize("reader", [scan, read_package])
@pytest.mark.parametrize("installed", [True, False])
def test_installation_changes_while_reading_still_block(reader, installed, monkeypatch):
    adb = FakeAdb()
    name = "com.example.third"
    package = adb.phone["one"]["packages"][name]
    package.update(installed=installed, dump_installed=True)
    shell = adb.shell

    def change_during_dump(transport, *args):
        output = shell(transport, *args)
        if args[:2] == ("dumpsys", "package"):
            package["installed"] = not installed
        return output

    monkeypatch.setattr(adb, "shell", change_during_dump)
    with pytest.raises(AdbError, match="changed"):
        if reader is scan:
            reader(adb, "usb-one", 10)
        else:
            reader(adb, "usb-one", 10, name)
    assert not adb.mutations


@pytest.mark.parametrize("reader", [scan, read_package])
@pytest.mark.parametrize("mismatch", ["system", "hidden", "installed"])
def test_other_installation_disagreements_still_block(reader, mismatch, monkeypatch):
    adb = FakeAdb()
    name = "com.example.system" if mismatch == "system" else "com.example.third"
    adb.phone["one"]["packages"][name].update(
        installed=mismatch == "installed", dump_installed=mismatch != "installed"
    )
    dump = adb.dump
    if mismatch == "hidden":
        monkeypatch.setattr(adb, "dump", lambda *args: dump(*args).replace("hidden=false", "hidden=true"))
    with pytest.raises(AdbError, match="changed"):
        if reader is scan:
            reader(adb, "usb-one", 10)
        else:
            reader(adb, "usb-one", 10, name)
    assert not adb.mutations


def test_single_segment_framework_survives_inventory_and_storage():
    from android_debloater.protection import reason

    listing = package_list("package:oplus\npackage:com.example.app")
    assert set(listing) == {"oplus", "com.example.app"}
    dump = """Packages:
  Package [oplus] (123):
    sharedUser=SharedUserSetting{abc android.uid.system/1000}
    codePath=/system/system_ext/framework/oplus-framework-res.apk
    versionCode=80 minSdk=30 targetSdk=30
    pkgFlags=[ SYSTEM PERSISTENT ALLOW_BACKUP ]
    User 0: installed=true hidden=false suspended=false enabled=0
"""
    packages = package_states(dump, 0)
    framework = packages["oplus"]
    assert framework.installed and framework.critical
    assert reason(framework, {})
    device = Device(inventory=packages)
    assert Device.model_validate_json(device.model_dump_json()).inventory == packages
    assert ReviewRequest(action="uninstall", packages=["oplus"]).packages == ["oplus"]


def test_single_segment_roles_and_administrators_remain_protected(monkeypatch):
    from fakes import FakeAdb

    from android_debloater.protection import live_protection

    adb = FakeAdb()
    adb.phone["one"]["role"] = "oplus"
    assert live_protection(adb, "usb-one", 10)["oplus"] == "Current default home app."
    shell = adb.shell

    def with_administrator(transport, *args):
        output = shell(transport, *args)
        return output + "\n  oplus/.Admin\n" if args == ("dumpsys", "device_policy") else output

    monkeypatch.setattr(adb, "shell", with_administrator)
    assert "administrator" in live_protection(adb, "usb-one", 10)["oplus"]


@pytest.mark.parametrize("package", ["oplus;reboot", "oplus/path", "-oplus", ".oplus", "oplus..core"])
def test_package_validation_still_rejects_unsafe_or_malformed_names(package):
    from pydantic import ValidationError

    from android_debloater.adb import Adb

    with pytest.raises(AdbError):
        package_list("package:" + package)
    with pytest.raises(ValidationError):
        Package(id=package)
    with pytest.raises(AdbError):
        Adb().mutation_args("device", "uninstall", 0, package)


def test_main_user_is_not_assumed_zero():
    profiles = parse_profiles("""Users:
  UserInfo{0:System:c10} serialNo=0
    Type: android.os.usertype.system.HEADLESS
  UserInfo{10:Owner:4403} serialNo=10
    Type: android.os.usertype.full.SECONDARY
  UserInfo{11:Work:1030} serialNo=11
    Type: android.os.usertype.profile.MANAGED
""")
    p, problem = main_profile(profiles, 10, 10)
    assert p.id == 10 and not problem
    assert main_profile(profiles, 11, 10)[1]
    assert main_profile(profiles, 11, 11)[0] is None
    assert main_profile(parse_profiles("UserInfo{0:Owner:13}"), 0, None)[0] is None


def test_transport_is_not_identity_and_unknown_formats_fail_closed():
    device = Device(serial="stable", manufacturer="maker", model="phone", aliases=["192.168.1.2:5555"])
    assert same_hardware(device, {"serial": "stable", "manufacturer": "maker", "model": "phone"})
    assert not same_hardware(device, {"serial": "different", "manufacturer": "maker", "model": "phone"})
    assert not same_hardware(device, {"serial": "", "manufacturer": "maker", "model": "phone"})
    transports = parse_devices(
        "List of devices attached\nusb1 device usb:1 model:My_Phone\nip:5555 offline\nother unauthorized"
    )
    assert transports[0]["connection"] == "USB"
    assert transports[1]["connection"] == "Wi-Fi"
    assert transports[2]["state"] == "Needs authorization"
    with pytest.raises(AdbError):
        package_list("Error: service not found")
    with pytest.raises(AdbError):
        package_list("package:com.foo;reboot")
    assert (
        package_list(
            "package:/data/app/~~hash==/base.apk=com.example.app versionCode:42 installer=com.android.vending"
        )["com.example.app"]["version"]
        == 42
    )


@pytest.mark.parametrize(
    ("filename", "current", "reported"), [("android10-users.txt", 0, None), ("android16-users.txt", 10, 10)]
)
def test_profile_format_fixtures(filename, current, reported):
    from pathlib import Path

    profiles = parse_profiles((Path(__file__).parent / "fixtures" / filename).read_text())
    profile, problem = main_profile(profiles, current, reported)
    assert profile.id == current and not problem


def test_oem_system_uid_and_apex_hosted_apks_are_protected():
    from android_debloater.protection import reason

    for metadata in [
        "    sharedUser=SharedUserSetting{abc android.uid.phone/1001}\n",
        "    apexModuleName=com.android.tethering\n",
    ]:
        dump = DUMP.replace("    codePath=/data/app/updated\n", "    codePath=/data/app/updated\n" + metadata)
        p = package_states(dump, 10)["com.example.updated"]
        assert reason(p, {})
