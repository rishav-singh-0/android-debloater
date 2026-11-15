"""Validated file records and API input. Live facts never come from category edits."""

from datetime import UTC, datetime
from typing import Annotated, Literal
from uuid import uuid4

from pydantic import BaseModel, BeforeValidator, ConfigDict, Field

PackageId = Annotated[
    str, Field(pattern=r"^(?:android|[A-Za-z][A-Za-z0-9_]*(?:\.[A-Za-z0-9_]+)+)$", max_length=255)
]
Purpose = Literal["communication", "money", "entertainment", "tools", "system", "other"]
LEGACY_PURPOSES = {
    "social": "communication",
    "messaging": "communication",
    "banking": "money",
    "payments": "money",
    "shopping": "money",
    "games": "entertainment",
    "media": "entertainment",
    "productivity": "tools",
    "health": "tools",
    "navigation": "tools",
    "security": "tools",
    "backup": "tools",
    "utilities": "tools",
    "system services": "system",
    "unknown": "other",
}


def combine_purposes(value):
    # Accept old saved categories without rewriting a user's records on startup.
    if isinstance(value, list) and all(isinstance(item, str) for item in value):
        return list(dict.fromkeys(LEGACY_PURPOSES.get(item, item) for item in value))
    return value


Purposes = Annotated[list[Purpose], Field(max_length=6), BeforeValidator(combine_purposes)]
Usefulness = Literal["essential", "optional", "suspected bloatware", "unknown"]


def now() -> str:
    return datetime.now(UTC).isoformat()


def new_id() -> str:
    return str(uuid4())


class Record(BaseModel):
    model_config = ConfigDict(extra="forbid", validate_assignment=True)


class Override(Record):
    name: str | None = Field(default=None, max_length=120)
    purposes: Purposes | None = None
    usefulness: Usefulness | None = None
    notes: str | None = Field(default=None, max_length=4000)


class Profile(Record):
    id: int = Field(ge=0)
    name: str
    flags: int
    serial: int | None = None
    created: str | None = None
    kind: str = ""


class Package(Record):
    id: PackageId
    version: int | None = None
    installation: Literal["system", "third-party", "unknown"] = "unknown"
    installed: bool = True
    enabled: int | None = Field(default=None, ge=0, le=4)
    available: bool = True
    apex: bool = False
    critical: bool = False
    unusual: bool = False
    installer: str | None = None
    state: Literal["installed/enabled", "installed/disabled", "removed", "unavailable", "unverified"] = (
        "unverified"
    )


class Operation(Record):
    id: str = Field(default_factory=new_id)
    batch_id: str
    action: Literal["uninstall", "restore"]
    package: PackageId
    user_id: int
    user_key: str
    timestamp: str = Field(default_factory=now)
    before: Package
    recovery: str
    previous_enabled: int | None = None
    status: Literal[
        "pending", "attempting", "succeeded", "failed", "unverified", "partial", "cancelled", "unattempted"
    ] = "pending"
    command: list[str] = Field(default_factory=list)
    outcome: str = ""
    verification: str = ""
    observed_at: str | None = None
    observed_state: str | None = None


class Device(Record):
    schema_version: Literal[1] = 1
    id: str = Field(default_factory=new_id, pattern=r"^[a-f0-9-]{36}$")
    serial: str = ""
    manufacturer: str = ""
    model: str = "Android device"
    nickname: str = ""
    aliases: list[str] = Field(default_factory=list)
    first_seen: str = Field(default_factory=now)
    last_seen: str = Field(default_factory=now)
    android: str = ""
    sdk: int = 0
    fingerprint: str = ""
    firmware_history: list[str] = Field(default_factory=list)
    profiles: list[Profile] = Field(default_factory=list)
    managed_user: int | None = None
    user_key: str = ""
    reset_key: str = ""
    inventory: dict[PackageId, Package] = Field(default_factory=dict)
    scanned_at: str | None = None
    overrides: dict[PackageId, Override] = Field(default_factory=dict)
    history: list[Operation] = Field(default_factory=list)
    reconciliation_required: bool = False
    blockers: list[str] = Field(default_factory=list)
    capabilities: dict[str, bool] = Field(default_factory=dict)
    protection: dict[str, str] = Field(default_factory=dict)


class Settings(Record):
    schema_version: Literal[1] = 1
    adb_path: str | None = Field(default=None, max_length=4096)


class GlobalOverrides(Record):
    schema_version: Literal[1] = 1
    packages: dict[PackageId, Override] = Field(default_factory=dict)


class AppInfo(Record):
    name: str | None = Field(default=None, max_length=120)
    description: str = Field(min_length=1, max_length=12000)
    purposes: Purposes | None = None
    category: str = Field(default="", max_length=80)
    source: Literal["Google Play", "UAD-ng"]
    fetched_at: str = Field(default_factory=now, max_length=64)


class OnlineCatalog(Record):
    schema_version: Literal[1] = 1
    packages: dict[PackageId, AppInfo] = Field(default_factory=dict)


class ReviewRequest(Record):
    action: Literal["uninstall", "restore"]
    packages: list[PackageId] = Field(min_length=1, max_length=1000)


class EditOverride(Override):
    scope: Literal["device", "global"] = "device"


class NicknameRequest(Record):
    nickname: str = Field(max_length=120)


class MatchRequest(Record):
    transport: str = Field(min_length=1, max_length=255)


class Plan(Record):
    id: str = Field(default_factory=new_id)
    device_id: str
    device_name: str
    user_id: int
    user_key: str
    fingerprint: str
    reset_key: str
    action: Literal["uninstall", "restore"]
    created_at: str = Field(default_factory=now)
    expires_at: str
    packages: list[Package]
    recovery: dict[str, str]
    previous_enabled: dict[str, int | None]
    excluded: dict[str, str]
    batch_id: str | None = None
