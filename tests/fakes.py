"""Stateful phone simulator. It never starts a host process or talks to hardware."""

import copy

from android_debloater.adb import ENABLED_COMMANDS, Adb, AdbError, Result


class FakeAdb(Adb):
    def __init__(self, *, read_only=False):
        super().__init__(read_only=read_only)
        self.calls = []
        self.mutations = []
        self.devices_map = {"usb-one": "one", "10.0.0.1:5555": "one"}
        self.phone = {
            key: {
                "serial": key,
                "model": "Test phone",
                "maker": "Test maker",
                "fingerprint": "test/firmware/1",
                "user": 10,
                "user_serial": 11,
                "android_id": "0123456789abcdef",
                "role": "com.example.home",
                "state": "device",
                "packages": {
                    "com.example.system": {"system": True, "installed": True, "enabled": 3, "version": 42},
                    "com.example.extra": {"system": True, "installed": True, "enabled": 0, "version": 3},
                    "com.example.third": {"system": False, "installed": True, "enabled": 1, "version": 4},
                    "com.example.home": {"system": False, "installed": True, "enabled": 0, "version": 1},
                    "com.android.systemui": {"system": True, "installed": True, "enabled": 0, "version": 36},
                },
            }
            for key in ["one", "two"]
        }
        self.failure = None
        self.before_mutation = None
        self.bad_policy = False
        self.bad_modules = False
        self.timeout_on = None

    def dump(self, phone, name=None):
        text = "Known Packages:\n  System:\n    android\n\nPackages:\n"
        for package, p in phone["packages"].items():
            if name and name != package:
                continue
            if not p.get("available", True):
                continue
            text += (
                f"  Package [{package}] (123):\n"
                f"    codePath=/data/app/{package}\n"
                f"    versionCode={p['version']}\n"
                f"    pkgFlags=[ {'SYSTEM UPDATED_SYSTEM_APP' if p['system'] else 'HAS_CODE'} ]\n"
                f"    User {phone['user']}: installed={str(p['installed']).lower()} hidden=false suspended=false enabled={p['enabled']}\n"
            )
        return text

    def _run(self, args, timeout=30):
        self.calls.append(args)
        if args == ["devices", "-l"]:
            output = "List of devices attached\n"
            for transport, key in self.devices_map.items():
                output += (
                    f"{transport} {self.phone[key]['state']} model:Test_phone"
                    + (" usb:1" if ":" not in transport else "")
                    + "\n"
                )
            return Result(args, 0, output)
        assert args[0] == "-s" and args[2] == "shell", args
        if args[1] not in self.devices_map:
            raise AdbError("Device disconnected")
        phone = self.phone[self.devices_map[args[1]]]
        if phone["state"] != "device":
            raise AdbError("Device disconnected")
        cmd = args[3:]
        if cmd[0] == "getprop":
            output = {
                "ro.serialno": phone["serial"],
                "ro.boot.serialno": phone["serial"],
                "ro.product.manufacturer": phone["maker"],
                "ro.product.model": phone["model"],
                "ro.build.version.release": "16",
                "ro.build.version.sdk": "36",
                "ro.build.fingerprint": phone["fingerprint"],
            }[cmd[1]]
        elif cmd == ["am", "get-current-user"] or cmd == ["cmd", "user", "get-main-user"]:
            output = str(phone["user"])
        elif cmd == ["dumpsys", "user"]:
            output = f"Users:\n  UserInfo{{{phone['user']}:Personal:4403}} serialNo={phone['user_serial']}\n    Type: android.os.usertype.full.SECONDARY\n"
        elif cmd[:2] == ["dumpsys", "package"]:
            output = self.dump(phone, cmd[2] if len(cmd) > 2 else None)
        elif cmd == ["dumpsys", "device_policy"]:
            output = (
                "Permission denied"
                if self.bad_policy
                else f"Current Device Policy Manager state:\n  Enabled Device Admins (User {phone['user']}, provisioningState: 0):\n"
            )
        elif cmd[:3] == ["cmd", "role", "get-role-holders"]:
            output = phone["role"] if cmd[-1] == "android.app.role.HOME" else ""
        elif cmd[0] == "settings":
            assert cmd[1:5] == ["--user", str(phone["user"]), "get", "secure"]
            output = phone["android_id"] if cmd[-1] == "android_id" else "null"
        elif cmd == ["pm", "help"]:
            output = (
                "uninstall [-k] [--user USER_ID] [--versionCode VERSION_CODE]\n\n"
                "install-existing [--user USER_ID|all|current]\n"
                "default-state enable disable disable-user disable-until-used"
            )
        elif cmd == ["pm", "get-moduleinfo", "--all"]:
            output = (
                "unknown output"
                if self.bad_modules
                else "ModuleInfo{123 Module} packageName: com.example.module"
            )
        elif cmd[:3] == ["pm", "list", "packages"]:
            names = phone["packages"]
            if "--apex-only" in cmd:
                output = "package:com.example.apex"
            else:
                assert "--user" in cmd and cmd[cmd.index("--user") + 1] == str(phone["user"])
                output = "\n".join(
                    "package:" + name
                    for name, p in names.items()
                    if p.get("available", True)
                    and (p["installed"] or "-u" in cmd)
                    and ("-s" not in cmd or p["system"])
                    and ("-3" not in cmd or not p["system"])
                    and ("-d" not in cmd or p["enabled"] in {2, 3, 4})
                )
        elif (
            cmd[:2] == ["pm", "uninstall"]
            or cmd[:3] == ["cmd", "package", "install-existing"]
            or cmd[0] == "pm"
            and cmd[1] in ENABLED_COMMANDS.values()
        ):
            self.mutations.append(args)
            if self.before_mutation:
                self.before_mutation(cmd[-1])
            if self.timeout_on == cmd[-1]:
                raise AdbError("ADB timed out after 60s")
            if self.failure:
                return Result(args, 0, self.failure)
            p = phone["packages"][cmd[-1]]
            assert cmd[cmd.index("--user") + 1] == str(phone["user"])
            if cmd[1] == "uninstall":
                assert "-k" in cmd
                if "--versionCode" in cmd:
                    assert int(cmd[cmd.index("--versionCode") + 1]) == p["version"]
                p["installed"] = False
                output = "Success"
            elif cmd[1] == "package":
                p["installed"] = True
                p["enabled"] = 0
                output = f"Package {cmd[-1]} installed for user: {phone['user']}"
            else:
                p["enabled"] = next(k for k, v in ENABLED_COMMANDS.items() if v == cmd[1])
                output = f"Package {cmd[-1]} new state: {cmd[1]}"
        else:
            raise AssertionError(f"Unexpected ADB command: {cmd}")
        return Result(args, 0, output)

    def clone_phone(self, key):
        self.phone[key] = copy.deepcopy(self.phone["one"])
        self.phone[key]["serial"] = key
