"""ADB subprocess boundary. Every remote argument is constrained before dispatch."""

import os
import re
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path

from .models import PACKAGE_PATTERN, Package, Profile

PACKAGE_RE = re.compile(PACKAGE_PATTERN + r"\Z")
SAFE_ARG = re.compile(r"[A-Za-z0-9_./:@=+,-]+\Z")
ENABLED_COMMANDS = {0: "default-state", 1: "enable", 2: "disable", 3: "disable-user", 4: "disable-until-used"}


class AdbError(RuntimeError):
    pass


@dataclass
class Result:
    args: list[str]
    code: int
    output: str


class Adb:
    def __init__(self, path=None, *, read_only=False, runner=subprocess.run):
        self.path = str(path) if path else shutil.which("adb")
        self.read_only = read_only
        self.runner = runner

    def _run(self, args, timeout=30) -> Result:
        if self.path and (not Path(self.path).is_file() or not os.access(self.path, os.X_OK)):
            raise AdbError("ADB path must be an existing executable file. Choose a new path in Settings.")
        if not self.path:
            raise AdbError(
                "ADB is missing. Install Android SDK Platform Tools or select an existing adb in Settings."
            )
        try:
            completed = self.runner(
                [self.path, *args],
                capture_output=True,
                text=True,
                timeout=timeout,
                check=False,
                errors="replace",
            )
        except subprocess.TimeoutExpired as exc:
            raise AdbError(f"ADB timed out after {timeout}s. Scan before any deliberate retry.") from exc
        except OSError as exc:
            raise AdbError(f"Cannot run ADB: {exc}") from exc
        return Result(args, completed.returncode, (completed.stdout + "\n" + completed.stderr).strip())

    def devices(self):
        result = self._run(["devices", "-l"])
        if result.code:
            raise AdbError(result.output)
        return parse_devices(result.output)

    def shell(self, transport: str, *args: str) -> str:
        # No free-form shell text, pipes, quotes, substitutions, or caller-provided command names.
        if not args or any(not SAFE_ARG.fullmatch(str(arg)) for arg in args):
            raise AdbError("Invalid remote argument.")
        if not transport or transport.startswith("-") or any(c.isspace() for c in transport):
            raise AdbError("Invalid ADB transport.")
        read_command = (
            args[0] == "getprop"
            and len(args) == 2
            and args[1].startswith("ro.")
            or args[:2] == ("am", "get-current-user")
            or args[:2] == ("dumpsys", "user")
            or args[:2] == ("dumpsys", "package")
            or args[:2] == ("dumpsys", "device_policy")
            or args == ("dumpsys", "role")
            or args[:3] == ("pm", "list", "packages")
            or args[:3] == ("pm", "list", "users")
            or args == ("pm", "help")
            or args == ("pm", "get-moduleinfo", "--all")
            or args[:3] == ("cmd", "user", "get-main-user")
            or args[:3] == ("cmd", "role", "get-role-holders")
            or len(args) == 6
            and args[0] == "settings"
            and args[1] == "--user"
            and args[3:5] == ("get", "secure")
        )
        if not read_command:
            raise AdbError("This command is not an allowed read-only query.")
        result = self._run(["-s", transport, "shell", *args])
        # AOSP runGetModuleInfo returns 1 on success. Accept only a complete, validated module list.
        module_success = (
            args == ("pm", "get-moduleinfo", "--all")
            and result.code == 1
            and bool(result.output)
            and all(
                re.fullmatch(r"ModuleInfo\{[^}]+\}\s+packageName:\s+" + PACKAGE_PATTERN, line)
                for line in result.output.splitlines()
            )
        )
        # Android 11's help handler returns -1, which ADB reports as 255.
        help_success = (
            args == ("pm", "help")
            and result.code == 255
            and result.output.startswith("Package manager (package) commands:\n")
        )
        if (result.code and not module_success and not help_success) or re.search(
            r"(?im)^(?:error|failure|exception|unknown command|unknown option|java\.)", result.output
        ):
            raise AdbError(result.output[:1500] or "ADB query failed.")
        return result.output

    def mutation_args(
        self, transport: str, action: str, user: int, package: str, *, version=None, enabled=None
    ):
        if not PACKAGE_RE.fullmatch(package) or type(user) is not int or user < 0:
            raise AdbError("Invalid package or target user.")
        if not transport or transport.startswith("-") or any(c.isspace() for c in transport):
            raise AdbError("Invalid transport.")
        if action == "uninstall":
            args = ["pm", "uninstall", "-k", "--user", str(user)]
            if version is not None:
                if type(version) is not int or version < 0:
                    raise AdbError("Invalid version code.")
                args += ["--versionCode", str(version)]
        elif action == "restore":
            args = ["cmd", "package", "install-existing", "--user", str(user)]
        elif action == "enabled" and enabled in ENABLED_COMMANDS:
            args = ["pm", ENABLED_COMMANDS[enabled], "--user", str(user)]
        else:
            raise AdbError("Unsupported mutation.")
        return ["-s", transport, "shell", *args, package]

    def mutate(self, transport: str, action: str, user: int, package: str, *, version=None, enabled=None):
        if self.read_only:
            raise AdbError("This server is in read-only mode.")
        return self._run(
            self.mutation_args(transport, action, user, package, version=version, enabled=enabled), timeout=60
        )


def parse_devices(text):
    if "List of devices attached" not in text:
        raise AdbError("Unrecognized ADB device list.")
    result = []
    for line in text.splitlines():
        if not line.strip() or line.startswith(("List of devices", "*")):
            continue
        parts = line.split()
        if len(parts) < 2 or parts[1] not in {"device", "offline", "unauthorized", "no"}:
            raise AdbError("Unrecognized device state: " + line[:200])
        info = dict(item.split(":", 1) for item in parts[2:] if ":" in item)
        result.append(
            {
                "transport": parts[0],
                "state": {
                    "device": "Connected",
                    "offline": "Offline",
                    "unauthorized": "Needs authorization",
                    "no": "Needs authorization",
                }[parts[1]],
                "connection": "USB"
                if "usb" in info or ":" not in parts[0] and not parts[0].startswith("adb-")
                else "Wi-Fi",
                "model": info.get("model", "Android device").replace("_", " "),
            }
        )
    return result


def package_list(text) -> dict[str, dict]:
    packages = {}
    for line in text.splitlines():
        if not line.strip():
            continue
        match = re.fullmatch(r"package:(\S+)(?:\s+(.*))?", line.strip())
        if not match:
            raise AdbError("Unrecognized package list: " + line[:200])
        name = match[1].rsplit("=", 1)[-1] if match[1].startswith("/") else match[1]
        if not PACKAGE_RE.fullmatch(name):
            raise AdbError("Invalid package ID in device output.")
        extra = match[2] or ""
        version = re.search(r"\bversionCode:(\d+)", extra)
        installer = re.search(r"\binstaller=(\S+)", extra)
        packages[name] = {
            "version": int(version[1]) if version else None,
            "installer": installer[1] if installer and PACKAGE_RE.fullmatch(installer[1]) else None,
        }
    return packages


def parse_profiles(text: str) -> list[Profile]:
    matches = list(re.finditer(r"UserInfo\{(\d+):([^\n{}]*):([0-9a-fA-F]+)\}", text))
    if not matches:
        raise AdbError("Cannot read user metadata. Inventory will remain read-only.")
    profiles = []
    for i, match in enumerate(matches):
        block = text[match.start() : matches[i + 1].start() if i + 1 < len(matches) else len(text)]
        serial = re.search(r"\bserialNo=(\d+)", block)
        created = re.search(r"^\s+Created: (.+)$", block, re.MULTILINE)
        kind = re.search(r"^\s+Type: (\S+)", block, re.MULTILINE)
        profiles.append(
            Profile(
                id=int(match[1]),
                name=match[2],
                flags=int(match[3], 16),
                serial=int(serial[1]) if serial else None,
                created=created[1] if created else None,
                kind=kind[1] if kind else "",
            )
        )
    return profiles


def main_profile(profiles: list[Profile], current: int, reported: int | None):
    personal = [
        p for p in profiles if not p.flags & (0x4 | 0x8 | 0x20 | 0x1000) and ".profile." not in p.kind
    ]
    if reported is not None:
        candidates = [p for p in personal if p.id == reported]
    else:
        candidates = [p for p in personal if p.flags & 0x4000] or [p for p in personal if p.flags & 1]
    if len(candidates) != 1 or candidates[0].serial is None:
        return (
            None,
            "Cannot reliably identify the main personal user and its serial. Phone changes are blocked.",
        )
    profile = candidates[0]
    if profile.id != current:
        return profile, f"Switch the phone to the main personal user {profile.id}, then scan again."
    return profile, ""


def package_states(text: str, user: int) -> dict[str, Package]:
    # First current Package block wins; hidden factory copies may follow updated system apps.
    starts = list(re.finditer(r"^\s{2}Package \[([^\]]+)\].*$", text, re.MULTILINE))
    result = {}
    for index, start in enumerate(starts):
        name = start[1]
        if name in result or not PACKAGE_RE.fullmatch(name):
            continue
        block = text[start.end() : starts[index + 1].start() if index + 1 < len(starts) else len(text)]
        state = re.search(r"^\s+User " + str(user) + r": ([^\n]+)", block, re.MULTILINE)
        flags = re.search(r"^\s+(?:pkgFlags|flags)=\[([^]]*)\]", block, re.MULTILINE)
        version = re.search(r"\bversionCode=(\d+)", block)
        installer = re.search(r"\binstallerPackageName=(\S+)", block)
        if not state or not flags:
            continue
        enabled = re.search(r"\benabled=(\d+)\b", state[1])
        installed = re.search(r"\binstalled=(true|false)\b", state[1])
        if not enabled or int(enabled[1]) not in ENABLED_COMMANDS or not installed:
            continue
        present = installed[1] == "true"
        result[name] = Package(
            id=name,
            version=int(version[1]) if version else None,
            installation="system" if "SYSTEM" in flags[1].split() else "third-party",
            installed=present,
            enabled=int(enabled[1]),
            installer=installer[1] if installer and PACKAGE_RE.fullmatch(installer[1]) else None,
            available=bool(re.search(r"^\s+codePath=\S+", block, re.MULTILINE)),
            apex=bool(
                re.search(
                    r"^\s+(?:isApex=true|codePath=/apex/|apexModuleName=(?!null\b)\S+)", block, re.MULTILINE
                )
            ),
            critical=("SYSTEM" in flags[1].split() and "PERSISTENT" in flags[1].split())
            or bool(
                re.search(
                    r"\bsharedUser=\S+ android\.uid\.(?:system|phone|radio|networkstack|bluetooth|nfc|systemui)/",
                    block,
                )
            ),
            unusual=bool(re.search(r"\b(?:hidden|suspended|quarantined|archived)=true", state[1])),
            state=("installed/disabled" if int(enabled[1]) in {2, 3, 4} else "installed/enabled")
            if present
            else "removed",
        )
    return result


def read_package_states(adb, transport, user, installed, name=None):
    states = package_states(adb.shell(transport, "dumpsys", "package", *((name,) if name else ())), user)
    if installed != package_list(adb.shell(transport, "pm", "list", "packages", "--user", str(user))):
        raise AdbError("Package inventory changed during the scan. Scan again.")
    for package in states.values():
        # Android can retain installed=true and a stale codePath after a third-party uninstall -k.
        if (
            package.installed
            and package.id not in installed
            and package.installation == "third-party"
            and not package.unusual
        ):
            package.installed = False
            package.available = False
            package.state = "unavailable"
    return states
