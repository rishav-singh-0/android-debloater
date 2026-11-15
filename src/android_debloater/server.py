"""Local HTTP boundary. Mutations require an exact origin and a session token."""

import secrets
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from .adb import Adb, AdbError
from .app_info import AppInfoUpdates
from .catalog import Catalog
from .devices import Devices
from .inventory import removed_entries
from .models import EditOverride, MatchRequest, NicknameRequest, Override, ReviewRequest, Settings
from .operations import Operations
from .protection import reason
from .storage import StorageError, Store


def create_app(data_dir: Path, *, port=8765, adb_path=None, read_only=False, adb=None, info_transport=None):
    store = Store(data_dir)
    catalog = Catalog()
    devices = None
    operations = None
    app_info = None
    token = secrets.token_urlsafe(32)
    allowed_hosts = {f"127.0.0.1:{port}", f"localhost:{port}"}
    origins = {f"http://{host}" for host in allowed_hosts}

    @asynccontextmanager
    async def lifespan(app):
        nonlocal devices, operations, app_info
        store.open()
        try:
            devices = Devices(store, adb or Adb(adb_path or store.settings.adb_path, read_only=read_only))
            if read_only:
                devices.adb.read_only = True
            app.state.devices = devices
            operations = Operations(devices)
            app.state.operations = operations
            app_info = AppInfoUpdates(store, transport=info_transport)
            app.state.app_info = app_info
            yield
        finally:
            if app_info:
                await app_info.close()
            if operations:
                operations.close()
            store.close()

    app = FastAPI(lifespan=lifespan, docs_url=None, redoc_url=None, openapi_url=None)
    app.state.store = store
    app.state.token = token
    app.state.url = f"http://127.0.0.1:{port}"
    app.state.open_browser = False
    resources = Path(__file__).parent
    templates = Jinja2Templates(directory=resources / "templates")
    app.mount("/static", StaticFiles(directory=resources / "static"), name="static")

    @app.middleware("http")
    async def local_session(request: Request, call_next):
        host = request.headers.get("host", "")
        origin = request.headers.get("origin")
        if host not in allowed_hosts or (origin is not None and origin not in origins):
            return JSONResponse({"detail": "Only this local application origin is allowed."}, status_code=403)
        if request.headers.get("sec-fetch-site") == "cross-site":
            return JSONResponse({"detail": "Cross-site requests are blocked."}, status_code=403)
        if request.method not in {"GET", "HEAD"} and (
            request.method != "POST"
            or origin != f"http://{host}"
            or not secrets.compare_digest(request.headers.get("x-session-token", ""), token)
        ):
            return JSONResponse({"detail": "Reload this page before making changes."}, status_code=403)
        response = await call_next(request)
        response.headers.update(
            {
                "Cache-Control": "no-store",
                "X-Content-Type-Options": "nosniff",
                "Referrer-Policy": "no-referrer",
                "Content-Security-Policy": "default-src 'self'; script-src 'self'; style-src 'self'; "
                "font-src 'self'; img-src 'self' data:; connect-src 'self'; "
                "frame-ancestors 'none'; base-uri 'none'; form-action 'self'",
            }
        )
        return response

    @app.exception_handler(StorageError)
    @app.exception_handler(AdbError)
    async def storage_error(request, exc):
        return JSONResponse({"detail": str(exc)}, status_code=409)

    @app.get("/api/health")
    def health():
        return {"status": "ready", "read_only": read_only}

    @app.get("/api/settings")
    def settings():
        return {
            "data_dir": str(store.directory),
            "read_only": read_only,
            "errors": store.errors,
            "adb_path": devices.adb.path,
            "catalog_version": catalog.version,
            "global_overrides": store.overrides.packages,
        }

    @app.get("/")
    def index(request: Request):
        return templates.TemplateResponse(request=request, name="index.html", context={"token": token})

    @app.get("/api/devices")
    def list_devices():
        return {
            "devices": devices.summaries(),
            "pending": devices.pending,
            "error": devices.error,
            "storage_errors": store.errors,
            "active_batch": devices.active,
        }

    @app.post("/api/refresh")
    def refresh(force: bool = False):
        devices.refresh(force=force)
        return list_devices()

    @app.get("/api/devices/{device_id}")
    def detail(device_id: str):
        device = store.get(device_id)
        online = store.app_info.packages
        packages = []
        for package in device.inventory.values():
            packages.append(
                {
                    **package.model_dump(),
                    **catalog.describe(package.id, device.overrides, store.overrides.packages, online),
                    "protection": reason(package, device.protection),
                }
            )
        return {
            "device": device,
            "packages": packages,
            "removed": removed_entries(device, devices.connected(device_id)),
            "connected": devices.connected(device_id),
        }

    @app.post("/api/devices/{device_id}/app-info", status_code=202)
    async def update_app_info(device_id: str):
        return await app_info.start(device_id)

    @app.get("/api/app-info")
    async def app_info_status():
        return app_info.status()

    @app.post("/api/app-info/stop")
    async def stop_app_info():
        return app_info.stop()

    @app.post("/api/devices/{device_id}/nickname")
    def nickname(device_id: str, body: NicknameRequest):
        with devices.lock:
            device = store.get(device_id)
            device.nickname = body.nickname.strip()
            store.save(device)
        return {"ok": True}

    @app.post("/api/devices/{device_id}/overrides/{package}")
    def override(device_id: str, package: str, body: EditOverride):
        with devices.lock:
            device = store.get(device_id)
            if package not in device.inventory:
                raise AdbError("Package is not in this device inventory.")
            saved = store.overrides.packages if body.scope == "global" else device.overrides
            changes = body.model_dump(exclude={"scope"}, exclude_unset=True)
            # A scope-only request is the existing Reset action; null clears one field.
            value = (
                Override(**(saved.get(package, Override()).model_dump() | changes)) if changes else Override()
            )
            if body.scope == "global":
                overrides = store.overrides.model_copy(deep=True)
                if value.model_dump(exclude_none=True):
                    overrides.packages[package] = value
                else:
                    overrides.packages.pop(package, None)
                store.save_overrides(overrides)
                if not changes or package not in device.overrides:
                    return {"ok": True}
                # Clear local fields only after their shared replacements are safely saved.
                value = Override(**device.overrides[package].model_dump(exclude=set(changes)))
            if value.model_dump(exclude_none=True):
                device.overrides[package] = value
            else:
                device.overrides.pop(package, None)
            store.save(device)
        return {"ok": True}

    @app.post("/api/devices/{device_id}/reconcile")
    def acknowledge(device_id: str):
        devices.acknowledge(device_id)
        return {"ok": True}

    @app.post("/api/match/{device_id}")
    def match(device_id: str, body: MatchRequest):
        return {"device_id": devices.match(body.transport, None if device_id == "new" else device_id)}

    @app.post("/api/settings")
    def save_settings(body: Settings):
        with devices.lock:
            if adb is not None:
                raise AdbError("The ADB connection cannot be changed in a simulated session.")
            if devices.active:
                raise AdbError("Wait for the current batch to finish.")
            replacement = Adb(body.adb_path, read_only=read_only)
            if body.adb_path and not Path(body.adb_path).is_absolute():
                raise AdbError("Choose an absolute path to an existing ADB executable.")
            replacement.devices()
            store.save_settings(body)
            devices.adb = replacement
            devices.transports.clear()
        return {"ok": True}

    @app.post("/api/global-overrides/{package}/reset")
    def reset_global(package: str):
        with devices.lock:
            overrides = store.overrides.model_copy(deep=True)
            overrides.packages.pop(package, None)
            store.save_overrides(overrides)
        return {"ok": True}

    @app.post("/api/devices/{device_id}/review")
    def review(device_id: str, body: ReviewRequest):
        return operations.review(device_id, body)

    @app.post("/api/plans/{plan_id}/confirm")
    def confirm(plan_id: str):
        return operations.confirm(plan_id)

    @app.get("/api/batches/current")
    def current_batch():
        return operations.get_batch()

    @app.get("/api/batches/{batch_id}")
    def batch(batch_id: str):
        return operations.get_batch(batch_id)

    @app.post("/api/batches/{batch_id}/stop")
    def stop(batch_id: str):
        return operations.cancel(batch_id)

    return app
