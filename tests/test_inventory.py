import pytest

from android_debloater.adb import (
    AdbError,
    main_profile,
    package_list,
    package_states,
    parse_devices,
    parse_profiles,
)
from android_debloater.devices import same_hardware
from android_debloater.models import Device

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
