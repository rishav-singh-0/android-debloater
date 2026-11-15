"""Reviewed, journaled operations. Only this module dispatches phone mutations."""

from datetime import UTC, datetime, timedelta
from threading import Event, RLock, Thread

from .adb import AdbError, package_list, read_package_states
from .inventory import removed_entries
from .models import Operation, Package, Plan, new_id, now
from .protection import live_protection, reason
from .storage import StorageError


def read_package(adb, address, user, name, previous=None):
    installed = package_list(adb.shell(address, "pm", "list", "packages", "--user", str(user)))
    states = read_package_states(adb, address, user, installed, name)
    package = states.get(name)
    if package is None:
        if name in installed:
            raise AdbError("Cannot read the installed package state for " + name)
        package = (previous or Package(id=name)).model_copy(
            update={"installed": False, "available": False, "state": "unavailable"}
        )
    if package.installed != (name in installed):
        raise AdbError("Package state changed while reading it. Scan and review again.")
    return package


def same_package(a, b):
    return all(
        getattr(a, key) == getattr(b, key)
        for key in ["id", "version", "installation", "installed", "enabled", "available", "apex", "unusual"]
    )


class Operations:
    def __init__(self, devices):
        self.devices, self.store = devices, devices.store
        self.plans: dict[str, Plan] = {}
        self.lock = RLock()
        self.stop = Event()
        self.worker = None
        self.batch = None
        for device in self.store.all():
            interrupted = False
            for entry in device.history:
                if entry.status in {"pending", "attempting"}:
                    entry.status = "unverified" if entry.status == "attempting" else "unattempted"
                    entry.verification = "Interrupted operation. Connect and scan to observe current state; the cause of any change is unknown."
                    interrupted = True
            if interrupted:
                try:
                    self.store.save(device)
                except StorageError:
                    pass  # The storage error stays visible and blocks writes to this device.

    def review(self, device_id, request):
        with self.lock:
            if self.devices.active:
                raise AdbError("A batch is already running. Wait for it to finish.")
            device = self.store.get(device_id)
            # Preview the displayed snapshot; confirmation and dispatch both validate live state.
            if device.reconciliation_required:
                raise AdbError("This phone was reset or its profile changed. Refresh and confirm it first.")
            if device.blockers or device.managed_user is None or not device.scanned_at:
                raise AdbError("; ".join(device.blockers) or "Refresh your apps before reviewing changes.")
            action = request.action
            if not device.capabilities.get(action):
                raise AdbError(f"This device does not report support for profile-scoped {action}.")
            recoveries = {row["package"]: row for row in removed_entries(device, True)}
            selected, excluded, recovery, previous_enabled = [], {}, {}, {}
            for name in dict.fromkeys(request.packages):
                p = device.inventory.get(name)
                issue = reason(p, device.protection) if p else "Package is not in the scanned inventory."
                if p and action == "uninstall":
                    issue = issue or (
                        "" if p.installed else "Package is not installed for the personal profile."
                    )
                    issue = issue or (
                        ""
                        if p.version is not None and p.enabled is not None and p.installation != "unknown"
                        else "Package version or state is unreadable."
                    )
                    recovery[name] = (
                        "System-package restore while available on the device"
                        if p.installation == "system"
                        else "Manual reinstall required"
                    )
                    previous_enabled[name] = p.enabled
                elif p:
                    entry = recoveries.get(name)
                    issue = issue or (
                        ""
                        if entry and entry["eligible"]
                        else entry["recovery"]
                        if entry
                        else "No verified removal by this application for this profile."
                    )
                    previous_enabled[name] = entry["previous_enabled"] if entry else None
                    if previous_enabled[name] is None or not device.capabilities.get("enabled_state"):
                        issue = issue or "Cannot restore the recorded package enabled setting on this device."
                    recovery[name] = (
                        "Install the available system package and restore its recorded enabled setting"
                    )
                if issue:
                    excluded[name] = issue
                else:
                    selected.append(p)
            plan = Plan(
                device_id=device.id,
                device_name=device.nickname or device.model,
                user_id=device.managed_user,
                user_key=device.user_key,
                fingerprint=device.fingerprint,
                reset_key=device.reset_key,
                action=action,
                expires_at=(datetime.now(UTC) + timedelta(minutes=5)).isoformat(),
                packages=selected,
                excluded=excluded,
                recovery=recovery,
                previous_enabled=previous_enabled,
            )
            self.plans = {
                key: value
                for key, value in self.plans.items()
                if value.batch_id or datetime.fromisoformat(value.expires_at) > datetime.now(UTC)
            }
            self.plans[plan.id] = plan
            return plan.model_copy(deep=True)

    def _validate_plan(self, plan):
        device = self.store.get(plan.device_id)
        if any(
            getattr(device, key) != getattr(plan, key) for key in ["user_key", "fingerprint", "reset_key"]
        ):
            raise AdbError("This review is stale. Scan and review again.")
        address = self.devices.validate(device)
        return device, address

    def confirm(self, plan_id):
        with self.lock:
            plan = self.plans.get(plan_id)
            if not plan:
                raise AdbError("Review not found. Prepare a new review.")
            if plan.batch_id:
                return self.get_batch(plan.batch_id)
            if self.devices.active:
                raise AdbError("Another mutation batch is already running.")
            if self.devices.adb.read_only:
                raise AdbError("This server is in read-only mode. Confirmation is blocked.")
            if not plan.packages:
                raise AdbError("No eligible apps in this review.")
            with self.devices.lock:
                if datetime.fromisoformat(plan.expires_at) <= datetime.now(UTC):
                    raise AdbError("This review expired. Prepare a new review.")
                device, address = self._validate_plan(plan)
                protected = live_protection(self.devices.adb, address, plan.user_id)
                # Validate the whole review before any write or dispatch, and each item again in the worker.
                for snapshot in plan.packages:
                    current = read_package(self.devices.adb, address, plan.user_id, snapshot.id, snapshot)
                    if not same_package(current, snapshot):
                        raise AdbError(
                            "Package state or version changed. Prepare a new review: " + snapshot.id
                        )
                    if problem := reason(current, protected):
                        raise AdbError(problem + " " + snapshot.id)
                batch_id = new_id()
                device.history.extend(
                    Operation(
                        batch_id=batch_id,
                        action=plan.action,
                        package=p.id,
                        user_id=plan.user_id,
                        user_key=plan.user_key,
                        before=p,
                        recovery=plan.recovery[p.id],
                        previous_enabled=plan.previous_enabled[p.id],
                    )
                    for p in plan.packages
                )
                self.store.save(device)
                plan.batch_id = batch_id
                self.batch = {
                    "id": batch_id,
                    "device_id": device.id,
                    "device_name": plan.device_name,
                    "user_id": plan.user_id,
                    "action": plan.action,
                    "running": True,
                    "stop_requested": False,
                    "error": "",
                }
                self.stop.clear()
                self.devices.active = True
                self.worker = Thread(
                    target=self._run, args=(plan.model_copy(deep=True),), name="package-operation"
                )
                self.worker.start()
                return self.get_batch(batch_id)

    def get_batch(self, batch_id=None):
        with self.lock:
            if self.batch and (batch_id is None or batch_id == self.batch["id"]):
                result = dict(self.batch)
            elif batch_id:
                for device in self.store.all():
                    entries = [e for e in device.history if e.batch_id == batch_id]
                    if entries:
                        return {
                            "id": batch_id,
                            "device_id": device.id,
                            "device_name": device.nickname or device.model,
                            "user_id": entries[0].user_id,
                            "action": entries[0].action,
                            "running": False,
                            "stop_requested": False,
                            "error": "",
                            "entries": entries,
                        }
                raise AdbError("Batch not found.")
            else:
                return None
        # Read outside the operations lock; the worker never holds it while running ADB.
        device = self.store.get(result["device_id"])
        result["entries"] = [e for e in device.history if e.batch_id == result["id"]]
        return result

    def cancel(self, batch_id):
        with self.lock:
            if not self.batch or self.batch["id"] != batch_id:
                raise AdbError("Active batch not found.")
            self.stop.set()
            self.batch["stop_requested"] = True
        return self.get_batch(batch_id)

    def close(self):
        self.stop.set()
        if self.worker:
            self.worker.join()

    def _run(self, plan):
        error = ""
        try:
            for snapshot in plan.packages:
                if self.stop.is_set():
                    break
                with self.devices.lock:
                    device = self.store.get(plan.device_id)
                    entry = next(
                        e for e in device.history if e.batch_id == plan.batch_id and e.package == snapshot.id
                    )
                    installed_during_restore = False
                    try:
                        _, address = self._validate_plan(plan)
                        protected = live_protection(self.devices.adb, address, plan.user_id)
                        current = read_package(self.devices.adb, address, plan.user_id, snapshot.id, snapshot)
                        if not same_package(current, snapshot):
                            raise AdbError("State changed after review. Scan and review again.")
                        if problem := reason(current, protected):
                            raise AdbError(problem)
                        if self.stop.is_set():
                            break
                        entry.status = "attempting"
                        entry.command = self.devices.adb.mutation_args(
                            address,
                            plan.action,
                            plan.user_id,
                            snapshot.id,
                            version=snapshot.version if device.capabilities.get("version_check") else None,
                        )
                        self.store.save(device)
                        result = self.devices.adb.mutate(
                            address,
                            plan.action,
                            plan.user_id,
                            snapshot.id,
                            version=snapshot.version if device.capabilities.get("version_check") else None,
                        )
                        entry.outcome = f"Exit {result.code}: {result.output}"
                        after = read_package(self.devices.adb, address, plan.user_id, snapshot.id, snapshot)
                        device.inventory[snapshot.id] = after
                        entry.observed_state, entry.observed_at = after.state, now()
                        success_output = (
                            result.output.strip() == "Success"
                            if plan.action == "uninstall"
                            else (
                                result.output.strip()
                                == f"Package {snapshot.id} installed for user: {plan.user_id}"
                            )
                        )
                        if result.code or not success_output:
                            entry.status = "failed" if same_package(after, snapshot) else "unverified"
                            entry.verification = (
                                "Command did not report success. Observed state: " + after.state
                            )
                        elif plan.action == "uninstall":
                            entry.status = "succeeded" if not after.installed else "unverified"
                            entry.verification = (
                                "Verified absent from the personal profile."
                                if not after.installed
                                else "Still installed after command; result is uncertain."
                            )
                        elif not after.installed:
                            entry.status = "unverified"
                            entry.verification = "Package is still absent after install-existing."
                        else:
                            installed_during_restore = True
                            entry.verification = "Installation verified; enabled-state recovery pending."
                            self.store.save(device)
                            if after.enabled != entry.previous_enabled:
                                # The second recovery command has its own durable pre-dispatch record.
                                address = self.devices.validate(device)
                                protected = live_protection(self.devices.adb, address, plan.user_id)
                                current = read_package(
                                    self.devices.adb, address, plan.user_id, snapshot.id, after
                                )
                                if not same_package(current, after):
                                    raise AdbError(
                                        "Package state changed during restore. Scan and review again."
                                    )
                                if problem := reason(current, protected):
                                    raise AdbError(problem)
                                entry.command += [
                                    "then",
                                    *self.devices.adb.mutation_args(
                                        address,
                                        "enabled",
                                        plan.user_id,
                                        snapshot.id,
                                        enabled=entry.previous_enabled,
                                    ),
                                ]
                                self.store.save(device)
                                enabled_result = self.devices.adb.mutate(
                                    address,
                                    "enabled",
                                    plan.user_id,
                                    snapshot.id,
                                    enabled=entry.previous_enabled,
                                )
                                entry.outcome += (
                                    f"\nEnabled setting exit {enabled_result.code}: {enabled_result.output}"
                                )
                                after = read_package(
                                    self.devices.adb, address, plan.user_id, snapshot.id, after
                                )
                                device.inventory[snapshot.id] = after
                                entry.observed_state, entry.observed_at = after.state, now()
                                if enabled_result.code or "new state:" not in enabled_result.output:
                                    raise AdbError(
                                        "Installation succeeded, but the enabled-state command did not report success."
                                    )
                            entry.status = (
                                "succeeded"
                                if after.installed and after.enabled == entry.previous_enabled
                                else "partial"
                            )
                            entry.verification = (
                                "Verified installed with the recorded package enabled setting."
                                if entry.status == "succeeded"
                                else "Installation succeeded; the recorded enabled setting was not recovered."
                            )
                        device.scanned_at = now()
                        self.store.save(device)
                        if entry.status != "succeeded":
                            error = entry.verification
                            break
                    except (AdbError, StorageError) as exc:
                        error = str(exc)
                        # A command may have reached Android even when its response or final write was lost.
                        entry.status = (
                            "partial"
                            if installed_during_restore
                            else "unverified"
                            if entry.status == "attempting"
                            else "failed"
                        )
                        entry.verification = (
                            "Installation succeeded; enabled-state recovery is incomplete. "
                            if installed_during_restore
                            else ""
                        ) + error
                        try:
                            self.store.save(device)
                        except StorageError:
                            pass  # Keep the durable attempting record for reconciliation on the next launch.
                        break
        except Exception as exc:  # noqa: BLE001 - Stop the worker and retain its journal on any unexpected failure.
            error = f"Batch interrupted: {exc}. Scan to reconcile before any retry."
        finally:
            try:
                with self.devices.lock:
                    device = self.store.get(plan.device_id)
                    for entry in device.history:
                        if entry.batch_id == plan.batch_id and entry.status == "pending":
                            entry.status = "cancelled" if self.stop.is_set() else "unattempted"
                            entry.verification = "Not dispatched."
                    self.store.save(device)
            except StorageError as exc:
                error = error or str(exc)
            with self.lock:
                self.devices.active = False
                self.batch.update(running=False, error=error)
