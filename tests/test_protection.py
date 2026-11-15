import re
from pathlib import Path

import pytest
from fakes import FakeAdb

from android_debloater.adb import AdbError, Result
from android_debloater.devices import Devices
from android_debloater.models import ReviewRequest
from android_debloater.operations import Operations
from android_debloater.protection import live_protection
from android_debloater.storage import Store

ROLE_DUMP = (Path(__file__).parent / "fixtures" / "android11-roles.txt").read_text()
LEGACY_DEPENDENCIES = """Verifiers:
  Required: com.example.verifier (uid=10100)

Intent Filter Verifier:
  Using: com.example.intentverifier (uid=10101)

Libraries:
  android.test.base ->  (jar) /system/framework/android.test.base.jar
  android.ext.shared ->  (apk) com.example.shared

"""


class LegacyRoleAdb(FakeAdb):
    role_dump = ROLE_DUMP
    role_error = "Unknown command: get-role-holders"
    dependencies = LEGACY_DEPENDENCIES
    sdk = "30"

    def _run(self, args, timeout=30):
        if args[3:6] == ["cmd", "role", "get-role-holders"]:
            self.calls.append(args)
            return Result(args, 255, self.role_error)
        if args[3:] == ["dumpsys", "role"]:
            self.calls.append(args)
            return Result(args, 0, self.role_dump.replace("com.example.home", self.phone["one"]["role"]))
        result = super()._run(args, timeout)
        if args[3:] == ["dumpsys", "package"]:
            result.output = result.output.replace(
                "Known Packages:\n  System:\n    android\n\n", self.dependencies
            )
        elif args[3:] == ["getprop", "ro.build.version.sdk"]:
            result.output = self.sdk
        return result


@pytest.mark.parametrize("change", ["before_confirm", "between_apps"])
def test_android_11_defaults_allow_review_and_are_rechecked_before_changes(tmp_path, change):
    store = Store(tmp_path)
    store.open()
    adb = LegacyRoleAdb()
    devices = Devices(store, adb)
    operations = Operations(devices)
    try:
        devices.refresh(force=True)
        device = store.all()[0]
        assert not device.blockers
        assert {
            "com.example.verifier",
            "com.example.intentverifier",
            "com.example.shared",
        } <= device.protection.keys()
        plan = operations.review(
            device.id,
            ReviewRequest(
                action="uninstall", packages=["com.example.system", "com.example.extra", "com.example.home"]
            ),
        )
        assert [p.id for p in plan.packages] == ["com.example.system", "com.example.extra"]
        assert "Current default home" in plan.excluded["com.example.home"]
        assert not adb.mutations
        if change == "before_confirm":
            adb.phone["one"]["role"] = "com.example.system"
            with pytest.raises(AdbError, match="Current default home"):
                operations.confirm(plan.id)
            assert not adb.mutations
        else:
            adb.before_mutation = lambda _: adb.phone["one"].update(role="com.example.extra")
            batch = operations.confirm(plan.id)
            operations.worker.join(5)
            assert not operations.worker.is_alive()
            entries = operations.get_batch(batch["id"])["entries"]
            assert [entry.status for entry in entries] == ["succeeded", "failed"]
            assert len(adb.mutations) == 1
    finally:
        operations.close()
        store.close()


def test_legacy_roles_support_multiple_profiles_and_holders():
    adb = LegacyRoleAdb()
    state = ROLE_DUMP.split("  user_states=", 1)[1].rsplit("\n}", 1)[0]
    other = state.replace("user_id=10", "user_id=0").replace("com.example.home", "com.example.other")
    adb.role_dump = (
        "ROLE MANAGER STATE (dumpsys role):\n{\n  user_states=[\n" + other + "\n" + state + "\n  ]\n}"
    )
    adb.role_dump = adb.role_dump.replace(
        "holders=com.example.sms", "holders=[\n          com.example.sms\n          oplus\n        ]"
    )
    protected = live_protection(adb, "usb-one", 10)
    assert protected["com.example.home"] == "Current default home app."
    assert protected["com.example.sms"] == "Current default sms app."
    assert protected["oplus"] == "Current default sms app."
    assert "com.example.other" not in protected
    assert sum(call[3:] == ["dumpsys", "role"] for call in adb.calls) == 1


def test_legacy_devices_without_telephony_roles_still_protect_available_defaults():
    adb = LegacyRoleAdb()
    adb.role_dump = re.sub(
        r"      \{\n        name=android.app.role.(?:DIALER|SMS|EMERGENCY)\n.*?      \}\n",
        "",
        ROLE_DUMP,
        flags=re.DOTALL,
    )
    protected = live_protection(adb, "usb-one", 10)
    assert protected["com.example.home"] == "Current default home app."
    assert protected["com.example.assistant"] == "Current default assistant app."
    assert protected["com.example.dialer"] == "Current default call_screening app."


@pytest.mark.parametrize(
    "known",
    [
        "",
        "  System:\n",
        "  System:\n    android\n  Installer:\n",
        "  System:\n    android\n  Installer:\n    bad;command\n",
    ],
)
def test_incomplete_modern_dependencies_block_changes(known, monkeypatch):
    adb = FakeAdb()
    dump = adb.dump
    monkeypatch.setattr(
        adb,
        "dump",
        lambda *args: dump(*args).replace(
            "Known Packages:\n  System:\n    android\n\n", "Known Packages:\n" + known + "\n"
        ),
    )
    with pytest.raises(AdbError, match="core dependencies"):
        live_protection(adb, "usb-one", 10)


def test_modern_dependencies_accept_empty_optional_categories(monkeypatch):
    adb = FakeAdb()
    dump = adb.dump
    monkeypatch.setattr(
        adb,
        "dump",
        lambda *args: dump(*args).replace(
            "    android\n",
            "    android\n  OEM installer:\n    com.vendor.installer\n  Retail Demo:\n    none\n",
        ),
    )
    protected = live_protection(adb, "usb-one", 10)
    assert "com.vendor.installer" in protected
    assert "none" not in protected


@pytest.mark.parametrize(
    "dump",
    [
        "Permission denied",
        ROLE_DUMP.replace("user_id=10", "user_id=11"),
        ROLE_DUMP.replace("name=android.app.role.HOME", "name=android.app.role.OTHER"),
        ROLE_DUMP.replace("name=android.app.role.SMS", "name=android.app.role.HOME"),
        ROLE_DUMP.replace("holders=com.example.home", "holders=com.example.home;reboot"),
        ROLE_DUMP.replace("holders=com.example.home", "holder=com.example.home"),
        ROLE_DUMP.rstrip()[:-1],
    ],
)
def test_unverified_legacy_roles_still_block_changes(dump):
    adb = LegacyRoleAdb()
    adb.role_dump = dump
    with pytest.raises(AdbError):
        live_protection(adb, "usb-one", 10)


def test_role_permission_errors_do_not_trigger_fallback():
    adb = LegacyRoleAdb()
    adb.role_error = "Error: permission denied"
    with pytest.raises(AdbError, match="permission denied"):
        live_protection(adb, "usb-one", 10)
    assert not any(call[3:] == ["dumpsys", "role"] for call in adb.calls)


def test_legacy_dependencies_accept_explicitly_absent_optional_verifiers():
    adb = LegacyRoleAdb()
    adb.sdk = "29"
    adb.dependencies = LEGACY_DEPENDENCIES.replace("com.example.verifier (uid=10100)", "null (uid=-1)")
    adb.dependencies = adb.dependencies.replace(
        "Intent Filter Verifier:\n  Using: com.example.intentverifier (uid=10101)",
        "No Intent Filter Verifier available!",
    )
    protected = live_protection(adb, "usb-one", 10)
    assert "com.example.shared" in protected
    assert "null" not in protected and "com.example.intentverifier" not in protected


@pytest.mark.parametrize(
    ("sdk", "dependencies"),
    [
        ("36", LEGACY_DEPENDENCIES),
        ("", LEGACY_DEPENDENCIES),
        ("30", ""),
        ("30", LEGACY_DEPENDENCIES.replace("Verifiers:", "Unknown:")),
        ("30", LEGACY_DEPENDENCIES.replace("com.example.verifier", "bad;command")),
        ("30", LEGACY_DEPENDENCIES.replace("com.example.shared", "bad;command")),
        ("30", LEGACY_DEPENDENCIES.replace("Libraries:", "Unknown:")),
    ],
)
def test_unverified_package_dependencies_still_block_changes(sdk, dependencies):
    adb = LegacyRoleAdb()
    adb.sdk, adb.dependencies = sdk, dependencies
    with pytest.raises(AdbError, match="core dependencies"):
        live_protection(adb, "usb-one", 10)
