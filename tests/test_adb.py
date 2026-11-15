import subprocess
from unittest.mock import Mock

import pytest

from android_debloater.adb import Adb, AdbError


@pytest.mark.parametrize("code", [0, 255])
def test_package_help_accepts_android_11_exit_status(code):
    output = "Package manager (package) commands:\n  help\n    Print this help text.\n"
    runner = Mock(return_value=subprocess.CompletedProcess([], code, output, ""))
    assert Adb("/usr/bin/true", runner=runner).shell("device", "pm", "help") == output.strip()


@pytest.mark.parametrize(
    ("code", "output", "command"),
    [
        (255, "", ("pm", "help")),
        (255, "Error: permission denied", ("pm", "help")),
        (1, "Package manager (package) commands:\n  help", ("pm", "help")),
        (255, "Package manager (package) commands:\nError: permission denied", ("pm", "help")),
        (255, "Package manager (package) commands:\n  help", ("pm", "list", "packages")),
    ],
)
def test_help_exception_does_not_hide_command_failures(code, output, command):
    runner = Mock(return_value=subprocess.CompletedProcess([], code, output, ""))
    with pytest.raises(AdbError):
        Adb("/usr/bin/true", runner=runner).shell("device", *command)


@pytest.mark.parametrize("package", ["com.example.module", "oplus"])
def test_module_listing_android_success_exit_one(package):
    runner = Mock(
        return_value=subprocess.CompletedProcess(
            [], 1, f"ModuleInfo{{abc Module}} packageName: {package}\n", ""
        )
    )
    adb = Adb("/usr/bin/true", runner=runner)
    assert package in adb.shell("device", "pm", "get-moduleinfo", "--all")


@pytest.mark.parametrize(
    "output", ["Error: permission denied", "ModuleInfo{abc Module} packageName: bad;command"]
)
def test_module_exit_one_still_rejects_invalid_output(output):
    runner = Mock(return_value=subprocess.CompletedProcess([], 1, output, ""))
    with pytest.raises(AdbError):
        Adb("/usr/bin/true", runner=runner).shell("device", "pm", "get-moduleinfo", "--all")


def test_empty_module_list_is_a_successful_android_result():
    runner = Mock(return_value=subprocess.CompletedProcess([], 1, "", ""))
    assert Adb("/usr/bin/true", runner=runner).shell("device", "pm", "get-moduleinfo", "--all") == ""


def test_adb_rejects_shell_injection_and_read_only_mutations():
    runner = Mock()
    adb = Adb("/usr/bin/true", read_only=True, runner=runner)
    for command in [("pm", "uninstall", "com.example.app"), ("dumpsys", "package", "com.foo;reboot")]:
        with pytest.raises(AdbError):
            adb.shell("device", *command)
    with pytest.raises(AdbError, match="read-only"):
        adb.mutate("device", "uninstall", 10, "com.example.app")
    assert not runner.called
