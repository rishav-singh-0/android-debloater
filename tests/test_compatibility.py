"""Android protocol variants, not claims of certification for a manufacturer's firmware."""

import pytest
from fakes import FakeAdb
from fastapi.testclient import TestClient
from test_protection import LEGACY_DEPENDENCIES, ROLE_DUMP

from android_debloater.adb import Result
from android_debloater.server import create_app


class AndroidAdb(FakeAdb):
    def __init__(self, sdk, user, enabled):
        super().__init__()
        self.sdk = sdk
        self.devices_map = {"usb-one": "one"}
        self.phone["one"]["user"] = user
        self.phone["one"]["packages"]["com.example.system"]["enabled"] = enabled
        self.phone["one"]["packages"]["com.example.third"]["dump_installed"] = True

    def _run(self, args, timeout=30):
        command = args[3:]
        user = self.phone["one"]["user"]
        if self.sdk < 31 and command[:3] == ["cmd", "role", "get-role-holders"]:
            self.calls.append(args)
            return Result(args, 255, "Unknown command: get-role-holders")
        if command == ["dumpsys", "role"]:
            self.calls.append(args)
            return Result(args, 0, ROLE_DUMP.replace("user_id=10", f"user_id={user}"))
        if self.sdk < 34 and command == ["cmd", "user", "get-main-user"]:
            self.calls.append(args)
            return Result(args, 255, "Unknown command: get-main-user")
        result = super()._run(args, timeout)
        if command == ["getprop", "ro.build.version.sdk"]:
            result.output = str(self.sdk)
        elif command == ["dumpsys", "user"]:
            result.output = (
                f"Users:\n  UserInfo{{{user}:Owner:{'4403' if self.sdk >= 34 else '13'}}} serialNo=11\n"
            )
        elif command == ["pm", "help"] and self.sdk < 31:
            result.code = 255
            result.output = "Package manager (package) commands:\n" + result.output
        elif command == ["pm", "get-moduleinfo", "--all"]:
            result.code = 1
            result.output = ""
        elif command[:2] == ["dumpsys", "package"] and self.sdk < 31:
            result.output = result.output.replace(
                "Known Packages:\n  System:\n    android\n\n", LEGACY_DEPENDENCIES
            ).replace("pkgFlags=", "flags=")
        return result


@pytest.mark.parametrize("sdk,user", [(29, 0), (30, 0), (31, 0), (33, 0), (34, 0), (35, 0), (36, 10)])
@pytest.mark.parametrize("enabled", [0, 1, 2, 3, 4])
def test_scan_review_uninstall_restore_and_restart_across_android_formats(tmp_path, sdk, user, enabled):
    adb = AndroidAdb(sdk, user, enabled)
    base = "http://127.0.0.1:8765"
    app = create_app(tmp_path, adb=adb)
    with TestClient(app, base_url=base) as client:
        headers = {"Origin": base, "X-Session-Token": app.state.token}
        response = client.post("/api/refresh?force=true", json={}, headers=headers)
        assert response.status_code == 200
        device = response.json()["devices"][0]
        assert not device["blockers"] and device["managed_user"] == user
        device_id = device["id"]

        def run(action, packages):
            response = client.post(
                f"/api/devices/{device_id}/review",
                json={"action": action, "packages": packages},
                headers=headers,
            )
            assert response.status_code == 200
            plan = response.json()
            response = client.post(f"/api/plans/{plan['id']}/confirm", json={}, headers=headers)
            assert response.status_code == 200
            app.state.operations.worker.join(5)
            assert not app.state.operations.worker.is_alive()
            batch = client.get(f"/api/batches/{response.json()['id']}").json()
            assert not batch["error"]
            assert all(entry["status"] == "succeeded" for entry in batch["entries"])
            return plan

        plan = run("uninstall", ["com.example.system", "com.example.third", "com.example.home"])
        assert "com.example.home" in plan["excluded"]
        assert client.post("/api/refresh?force=true", json={}, headers=headers).status_code == 200
        plan = run("restore", ["com.example.system", "com.example.third"])
        assert "com.example.third" in plan["excluded"]
        assert adb.phone["one"]["packages"]["com.example.system"]["enabled"] == enabled
        assert all(call[call.index("--user") + 1] == str(user) for call in adb.mutations)
    with TestClient(create_app(tmp_path, adb=adb), base_url=base) as client:
        device = client.get(f"/api/devices/{device_id}").json()["device"]
        assert len(device["history"]) == 3
        assert device["inventory"]["com.example.system"]["enabled"] == enabled
        assert not device["inventory"]["com.example.third"]["installed"]
