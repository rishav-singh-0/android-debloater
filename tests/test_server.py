from fastapi.testclient import TestClient

from android_debloater.server import create_app


def test_shared_edits_replace_only_the_edited_local_fields(tmp_path):
    from fakes import FakeAdb

    adb = FakeAdb()
    adb.devices_map["usb-two"] = "two"
    app = create_app(tmp_path, adb=adb)
    base = "http://127.0.0.1:8765"
    package = "com.example.extra"
    with TestClient(app, base_url=base) as client:
        headers = {"Origin": base, "X-Session-Token": app.state.token}
        devices = client.post("/api/refresh", json={}, headers=headers).json()["devices"]
        device_id = devices[0]["id"]
        url = f"/api/devices/{device_id}/overrides/{package}"

        def edit(scope, **fields):
            response = client.post(url, json={"scope": scope, **fields}, headers=headers)
            assert response.status_code == 200

        edit("device", name="Old local name", notes="Keep on this phone")
        edit("global", name="New shared name")
        for device in devices:
            detail = client.get(f"/api/devices/{device['id']}").json()
            row = next(p for p in detail["packages"] if p["id"] == package)
            assert row["name"] == "New shared name"
            assert row["notes"] == ("Keep on this phone" if device["id"] == device_id else "")
        local = app.state.store.get(device_id).overrides[package]
        assert local.name is None and local.notes == "Keep on this phone"
        edit("global", notes="Shared note")
        assert package not in app.state.store.get(device_id).overrides
        edit("device", name="Keep this local name")
        edit("global")
        assert app.state.store.get(device_id).overrides[package].name == "Keep this local name"
        assert package not in app.state.store.overrides.packages


def test_failed_shared_save_keeps_local_edits(tmp_path, monkeypatch):
    from fakes import FakeAdb

    from android_debloater.storage import StorageError

    app = create_app(tmp_path, adb=FakeAdb())
    base = "http://127.0.0.1:8765"
    package = "com.example.extra"
    with TestClient(app, base_url=base) as client:
        headers = {"Origin": base, "X-Session-Token": app.state.token}
        device_id = client.post("/api/refresh", json={}, headers=headers).json()["devices"][0]["id"]
        url = f"/api/devices/{device_id}/overrides/{package}"
        assert client.post(url, json={"name": "Local name"}, headers=headers).status_code == 200

        def fail_save(_):
            raise StorageError("Disk full")

        monkeypatch.setattr(app.state.store, "save_overrides", fail_save)
        response = client.post(url, json={"scope": "global", "name": "Shared name"}, headers=headers)
        assert response.status_code == 409
        assert app.state.store.get(device_id).overrides[package].name == "Local name"
        assert package not in app.state.store.overrides.packages


def test_loopback_session_boundary(tmp_path):
    app = create_app(tmp_path)
    with TestClient(app, base_url="http://127.0.0.1:8765") as client:
        assert client.get("/api/health").json()["status"] == "ready"
        assert client.get("/api/health", headers={"host": "evil.example:8765"}).status_code == 403
        assert client.get("/api/health", headers={"origin": "https://evil.example"}).status_code == 403
        assert client.get("/api/health", headers={"sec-fetch-site": "cross-site"}).status_code == 403
        assert client.post("/api/settings").status_code == 403
        assert client.post("/api/settings", headers={"origin": "http://127.0.0.1:8765"}).status_code == 403
        assert "frame-ancestors 'none'" in client.get("/api/health").headers["content-security-policy"]


def test_api_workflow_overrides_protection_and_restart(tmp_path):
    from fakes import FakeAdb

    adb = FakeAdb()
    app = create_app(tmp_path, adb=adb)
    base = "http://127.0.0.1:8765"
    with TestClient(app, base_url=base) as client:
        headers = {"Origin": base, "X-Session-Token": app.state.token}
        assert client.get("/").status_code == 200
        assert client.get("/static/fonts/IBMPlexSans.ttf").status_code == 200
        response = client.post("/api/refresh?force=true", json={}, headers=headers)
        device_id = response.json()["devices"][0]["id"]
        detail = client.get("/api/devices/" + device_id).json()
        assert len(detail["packages"]) == 5
        url = f"/api/devices/{device_id}/overrides/com.example.system"
        assert (
            client.post(
                url,
                json={"scope": "global", "name": "Global name", "usefulness": "optional"},
                headers=headers,
            ).status_code
            == 200
        )
        assert client.post(url, json={"name": "Device name"}, headers=headers).status_code == 200
        row = next(
            p
            for p in client.get("/api/devices/" + device_id).json()["packages"]
            if p["id"] == "com.example.system"
        )
        assert row["name"] == "Device name" and row["usefulness"] == "optional"
        assert client.post(url, json={"installation": "third-party"}, headers=headers).status_code == 422
        assert not adb.mutations
    restarted = create_app(tmp_path, adb=adb)
    with TestClient(restarted, base_url=base) as client:
        detail = client.get("/api/devices/" + device_id).json()
        assert not detail["connected"]
        row = next(p for p in detail["packages"] if p["id"] == "com.example.system")
        assert row["name"] == "Device name"
        assert next(p for p in detail["packages"] if p["id"] == "com.android.systemui")["protection"]
        assert not adb.mutations


def test_missing_saved_adb_keeps_settings_accessible(tmp_path):
    from android_debloater.models import Settings
    from android_debloater.storage import Store

    store = Store(tmp_path)
    store.open()
    store.save_settings(Settings(adb_path="/does-not-exist/adb"))
    store.close()
    app = create_app(tmp_path)
    with TestClient(app, base_url="http://127.0.0.1:8765") as client:
        assert client.get("/").status_code == 200
        headers = {"Origin": "http://127.0.0.1:8765", "X-Session-Token": app.state.token}
        data = client.post("/api/refresh", json={}, headers=headers).json()
        assert data["error"]
        assert client.get("/api/settings").status_code == 200


def test_simulated_session_cannot_switch_to_real_adb(monkeypatch, tmp_path):
    from fakes import FakeAdb

    from android_debloater.adb import Adb

    called = []
    monkeypatch.setattr(Adb, "devices", lambda self: called.append(True) or [])
    adb = FakeAdb()
    app = create_app(tmp_path, adb=adb, read_only=True)
    base = "http://127.0.0.1:8765"
    with TestClient(app, base_url=base) as client:
        headers = {"Origin": base, "X-Session-Token": app.state.token}
        response = client.post("/api/settings", json={"adb_path": None}, headers=headers)
        assert response.status_code == 409
        assert not called
        assert app.state.devices.adb is adb
        assert adb.read_only
