import subprocess
from unittest.mock import Mock

import pytest

from android_debloater.adb import Adb, AdbError


def test_module_listing_android_success_exit_one():
    runner = Mock(
        return_value=subprocess.CompletedProcess(
            [], 1, "ModuleInfo{abc Module} packageName: com.example.module\n", ""
        )
    )
    adb = Adb("/usr/bin/true", runner=runner)
    assert "com.example.module" in adb.shell("device", "pm", "get-moduleinfo", "--all")


@pytest.mark.parametrize(
    "output", ["Error: permission denied", "", "ModuleInfo{abc Module} packageName: bad;command"]
)
def test_module_exit_one_still_rejects_invalid_output(output):
    runner = Mock(return_value=subprocess.CompletedProcess([], 1, output, ""))
    with pytest.raises(AdbError):
        Adb("/usr/bin/true", runner=runner).shell("device", "pm", "get-moduleinfo", "--all")


def test_adb_rejects_shell_injection_and_read_only_mutations():
    runner = Mock()
    adb = Adb("/usr/bin/true", read_only=True, runner=runner)
    for command in [("pm", "uninstall", "com.example.app"), ("dumpsys", "package", "com.foo;reboot")]:
        with pytest.raises(AdbError):
            adb.shell("device", *command)
    with pytest.raises(AdbError, match="read-only"):
        adb.mutate("device", "uninstall", 10, "com.example.app")
    assert not runner.called
