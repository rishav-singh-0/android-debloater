"""Single-process ownership and durable JSON replacement on macOS/Linux."""

import fcntl
import os
from pathlib import Path
from threading import RLock
from uuid import UUID

from pydantic import ValidationError

from .models import Device, GlobalOverrides, Record, Settings


class StorageError(RuntimeError):
    pass


class Store:
    def __init__(self, directory: Path):
        self.directory = directory.resolve()
        self.mutex = RLock()
        self.lock_file = None
        self.errors: dict[str, str] = {}
        self.records: dict[str, Device] = {}
        self.settings = Settings()
        self.overrides = GlobalOverrides()

    def open(self):
        self.directory.mkdir(parents=True, exist_ok=True, mode=0o700)
        (self.directory / "devices").mkdir(exist_ok=True, mode=0o700)
        self.lock_file = (self.directory / ".lock").open("a+")
        try:
            fcntl.flock(self.lock_file, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as exc:
            self.lock_file.close()
            self.lock_file = None
            raise StorageError("Another server is using this data directory.") from exc
        for name, model, attr in [
            ("settings.json", Settings, "settings"),
            ("category-overrides.json", GlobalOverrides, "overrides"),
        ]:
            path = self.directory / name
            if path.exists():
                try:
                    setattr(self, attr, model.model_validate_json(path.read_bytes()))
                except (OSError, ValidationError) as exc:
                    self.errors[name] = f"Cannot read {name}: {exc}. Original file retained."
        for path in sorted((self.directory / "devices").glob("*.json")):
            try:
                device = Device.model_validate_json(path.read_bytes())
                if str(UUID(device.id)) != path.stem:
                    raise ValueError("Device ID does not match filename")
                self.records[device.id] = device
            except (OSError, ValueError) as exc:
                self.errors[path.stem] = f"Cannot read {path.name}: {exc}. Original and backup retained."

    def close(self):
        if self.lock_file:
            self.lock_file.close()
            self.lock_file = None

    def _replace(self, path: Path, content: bytes):
        temp = path.with_suffix(path.suffix + ".tmp")
        try:
            fd = os.open(temp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
            with os.fdopen(fd, "wb") as handle:
                handle.write(content)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temp, path)
            directory_fd = os.open(path.parent, os.O_RDONLY)
            try:
                os.fsync(directory_fd)
            finally:
                os.close(directory_fd)
        finally:
            temp.unlink(missing_ok=True)

    def _write(self, path: Path, record: Record, key: str):
        if key in self.errors:
            raise StorageError(self.errors[key])
        try:
            # Validate the old bytes before making them the last-known-good backup.
            if path.exists():
                old = path.read_bytes()
                type(record).model_validate_json(old)
                self._replace(path.with_suffix(".json.bak"), old)
            self._replace(path, (record.model_dump_json(indent=2) + "\n").encode())
        except (OSError, ValueError) as exc:
            message = f"Cannot save {path.name}. Mutations blocked; original and backup retained: {exc}"
            self.errors[key] = message
            raise StorageError(message) from exc

    def save(self, device: Device):
        with self.mutex:
            if str(UUID(device.id)) != device.id:
                raise StorageError("Invalid device ID")
            self._write(self.directory / "devices" / f"{device.id}.json", device, device.id)
            self.records[device.id] = device.model_copy(deep=True)

    def get(self, device_id: str) -> Device:
        with self.mutex:
            if device_id in self.errors:
                raise StorageError(self.errors[device_id])
            if device_id not in self.records:
                raise StorageError("Device record not found.")
            return self.records[device_id].model_copy(deep=True)

    def all(self) -> list[Device]:
        with self.mutex:
            return [r.model_copy(deep=True) for r in self.records.values()]

    def save_settings(self, settings: Settings):
        with self.mutex:
            self._write(self.directory / "settings.json", settings, "settings.json")
            self.settings = settings

    def save_overrides(self, overrides: GlobalOverrides):
        with self.mutex:
            self._write(self.directory / "category-overrides.json", overrides, "category-overrides.json")
            self.overrides = overrides
