import json
from unittest.mock import patch

import pytest

from android_debloater.models import Device
from android_debloater.storage import StorageError, Store


def test_restart_backup_and_exclusive_ownership(tmp_path):
    store = Store(tmp_path)
    store.open()
    device = Device(serial="one")
    store.save(device)
    device.nickname = "My phone"
    store.save(device)
    assert json.loads((tmp_path / "devices" / f"{device.id}.json.bak").read_text())["nickname"] == ""
    other = Store(tmp_path)
    with pytest.raises(StorageError, match="Another server"):
        other.open()
    store.close()
    other.open()
    assert other.get(device.id).nickname == "My phone"
    other.close()


def test_corruption_is_retained_and_blocks_writes(tmp_path):
    store = Store(tmp_path)
    store.open()
    device = Device()
    store.save(device)
    store.close()
    path = tmp_path / "devices" / f"{device.id}.json"
    path.write_text("{broken")
    store.open()
    with pytest.raises(StorageError, match="Original and backup retained"):
        store.save(device)
    assert path.read_text() == "{broken"
    store.close()


def test_failed_atomic_replace_keeps_authoritative_record(tmp_path):
    store = Store(tmp_path)
    store.open()
    device = Device(nickname="before")
    store.save(device)
    device.nickname = "after"
    with (
        patch("android_debloater.storage.os.replace", side_effect=OSError("disk full")),
        pytest.raises(StorageError, match="disk full"),
    ):
        store.save(device)
    assert json.loads((tmp_path / "devices" / f"{device.id}.json").read_text())["nickname"] == "before"
    with pytest.raises(StorageError):
        store.save(device)
    store.close()


def test_old_categories_load_without_rewriting_files_or_losing_history(tmp_path):
    from android_debloater.models import Operation, Package

    package = "com.example.app"
    device = Device(
        nickname="My phone",
        history=[
            Operation(
                batch_id="saved",
                action="uninstall",
                package=package,
                user_id=0,
                user_key="personal",
                before=Package(id=package),
                recovery="system",
                status="succeeded",
            )
        ],
    )
    record = device.model_dump()
    record["overrides"] = {
        package: {
            "name": "My bank",
            "purposes": ["banking", "payments", "shopping"],
            "notes": "Keep this app",
        }
    }
    paths = {
        tmp_path / "devices" / f"{device.id}.json": record,
        tmp_path / "category-overrides.json": {
            "schema_version": 1,
            "packages": {
                package: {"purposes": ["utilities", "backup", "security", "health", "navigation"]},
            },
        },
        tmp_path / "app-info.json": {
            "schema_version": 1,
            "packages": {
                package: {
                    "description": "Saved description",
                    "source": "Google Play",
                    "purposes": ["games", "media"],
                }
            },
        },
    }
    for path, value in paths.items():
        path.parent.mkdir(exist_ok=True)
        path.write_text(json.dumps(value))
    originals = {path: path.read_bytes() for path in paths}
    store = Store(tmp_path)
    store.open()
    try:
        assert not store.errors
        saved = store.get(device.id)
        assert saved.overrides[package].purposes == ["money"]
        assert saved.overrides[package].notes == "Keep this app"
        assert store.overrides.packages[package].purposes == ["tools"]
        assert store.app_info.packages[package].purposes == ["entertainment"]
        assert saved.history == device.history
        assert all(path.read_bytes() == originals[path] for path in paths)
        store.save(saved)
        assert json.loads(next(iter(paths)).read_text())["history"] == record["history"]
    finally:
        store.close()


def test_combined_categories_still_validate_untrusted_input():
    from pydantic import ValidationError

    from android_debloater.models import Override

    assert Override(purposes=["unknown", "other"]).purposes == ["other"]
    assert Override(purposes=[]).purposes == []
    assert Override().purposes is None
    for value in [["not-a-category"], [{"tools": True}], "tools"]:
        with pytest.raises(ValidationError):
            Override(purposes=value)
