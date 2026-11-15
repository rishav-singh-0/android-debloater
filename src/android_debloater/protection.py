"""Non-editable core rules plus fresh, profile-scoped device role checks."""

import re

from .adb import PACKAGE_RE, AdbError, package_list
from .models import PACKAGE_PATTERN

CORE = {
    "android",
    "com.android.systemui",
    "com.android.settings",
    "com.android.phone",
    "com.android.shell",
    "com.android.bluetooth",
    "com.android.nfc",
    "com.android.se",
    "com.android.keychain",
    "com.android.permissioncontroller",
    "com.google.android.permissioncontroller",
    "com.android.packageinstaller",
    "com.google.android.packageinstaller",
    "com.android.server.telecom",
    "com.android.networkstack",
    "com.google.android.networkstack",
    "com.android.networkstack.tethering",
    "com.google.android.networkstack.tethering",
    "com.android.webview",
    "com.google.android.webview",
    "com.android.externalstorage",
    "com.android.documentsui",
    "com.google.android.documentsui",
    "com.android.certinstaller",
    "com.android.credentialmanager",
    "com.android.intentresolver",
    "com.android.inputdevices",
    "com.android.location.fused",
    "com.android.proxyhandler",
    "com.android.dynsystem",
    "com.android.managedprovisioning",
    "com.android.settings.intelligence",
    "com.android.carrierconfig",
    "com.android.carrierdefaultapp",
    "com.android.stk",
    "com.google.android.gms",
    "com.google.android.gsf",
    "com.android.vending",
    "com.google.android.setupwizard",
    "com.sec.android.app.SecSetupWizard",
    "com.sec.android.app.launcher",
    "com.sec.imsservice",
    "com.sec.ims",
    "com.samsung.ims",
    "com.samsung.android.incallui",
    "com.samsung.android.dialer",
    "com.samsung.android.providers.contacts",
    "com.samsung.android.honeyboard",
    "com.samsung.android.kgclient",
    "com.osp.app.signin",
    "com.samsung.android.authfw",
    "com.samsung.android.samsungpass",
    "com.samsung.android.samsungpassautofill",
}
CORE_PREFIXES = (
    "com.android.providers.",
    "com.google.android.providers.",
    "com.android.internal.",
    "com.android.overlay.",
    "com.google.android.overlay.",
    "com.samsung.android.knox.",
    "com.samsung.android.providers.",
    "com.samsung.android.biometrics.",
)
ROLES = ("HOME", "DIALER", "SMS", "BROWSER", "ASSISTANT", "EMERGENCY")


def core_reason(package):
    base = package.split(".auto_generated", 1)[0]
    if base in CORE or package.startswith(CORE_PREFIXES):
        return "Protected core platform component or shared system dependency."
    return ""


def parse_role_dump(text, user):
    # Android 10/11 dumps use a scalar/object for one entry, an array for several.
    holder = rf"(?:{PACKAGE_PATTERN}|\[\n(?:{PACKAGE_PATTERN}\n)+\])"
    role = r"\{\nname=([A-Za-z0-9_.]+)\n(?:holders=(" + holder + r")\n)?\}\n"
    roles = "(?:" + role + r"|\[\n(?:" + role + r")+\]\n)"
    state = (
        r"\{\nuser_id=(\d+)\nversion=-?\d+\npackages_hash=[^\n{}\[\]]*\n"
        r"(?:roles=(" + roles + r"))?\}\n"
    )
    dump = (
        r"ROLE MANAGER STATE \(dumpsys role\):\n\{\nuser_states=(?:"
        + state
        + r"|\[\n(?:"
        + state
        + r")+\]\n)\}\n"
    )
    normalized = "\n".join(line.strip() for line in text.strip().splitlines()) + "\n"
    error = (
        "Cannot verify default apps for the personal profile from dumpsys role. Phone changes are blocked."
    )
    if not re.fullmatch(dump, normalized):
        raise AdbError(error)
    states = list(re.finditer(state, normalized))
    selected = [match for match in states if int(match[1]) == user]
    if len({int(match[1]) for match in states}) != len(states) or len(selected) != 1:
        raise AdbError(error)
    holders = {}
    for match in re.finditer(role, selected[0][2] or ""):
        if match[1] in holders:
            raise AdbError(error)
        holders[match[1]] = (match[2] or "").strip("[]\n").splitlines()
    if any("android.app.role." + name not in holders for name in ROLES):
        raise AdbError(error)
    return {name: holders["android.app.role." + name] for name in ROLES}


def role_holders(adb, transport, user):
    holders = {}
    for role in ROLES:
        try:
            output = adb.shell(
                transport, "cmd", "role", "get-role-holders", "--user", str(user), "android.app.role." + role
            )
        except AdbError as exc:
            if str(exc) != "Unknown command: get-role-holders":
                raise
            return parse_role_dump(adb.shell(transport, "dumpsys", "role"), user)
        holders[role] = [name.strip() for name in output.splitlines()]
        if any(not PACKAGE_RE.fullmatch(name) for name in holders[role]):
            raise AdbError(f"Cannot verify the {role.lower()} role. Phone changes are blocked.")
    return holders


def package_dependencies(adb, transport):
    dump = adb.shell(transport, "dumpsys", "package")
    known = re.search(r"^Known Packages:\n(.*?)(?=^\S|\Z)", dump, re.MULTILINE | re.DOTALL)
    if known:
        return {line.strip() for line in known[1].splitlines() if PACKAGE_RE.fullmatch(line.strip())}
    error = "Cannot verify package-manager core dependencies."
    if adb.shell(transport, "getprop", "ro.build.version.sdk") not in {"29", "30"}:
        raise AdbError(error)
    # Android 10/11 expose verifiers and shared libraries without a Known Packages section.
    dependencies = set()
    for title, label in [("Verifiers", "Required"), ("Intent Filter Verifier", "Using")]:
        if title == "Intent Filter Verifier" and "\nNo Intent Filter Verifier available!\n" in dump:
            continue
        section = re.search(r"^" + title + r":\n(.*?)(?=^\S|\Z)", dump, re.MULTILINE | re.DOTALL)
        match = re.fullmatch(label + r": (\S+) \(uid=-?\d+\)", section[1].strip()) if section else None
        if not match or not PACKAGE_RE.fullmatch(match[1]):
            raise AdbError(error)
        if match[1] != "null":
            dependencies.add(match[1])
    libraries = re.search(r"^Libraries:\n(.*?)(?=^\S|\Z)", dump, re.MULTILINE | re.DOTALL)
    if not libraries or not libraries[1].strip():
        raise AdbError(error)
    for line in libraries[1].strip().splitlines():
        match = re.fullmatch(r"\S+(?: version=\d+)?\s+->\s+\((jar|apk)\) (\S+)", line.strip())
        if not match or match[1] == "apk" and not PACKAGE_RE.fullmatch(match[2]):
            raise AdbError(error)
        if match[1] == "apk":
            dependencies.add(match[2])
    return dependencies


def live_protection(adb, transport, user):
    protected = {name: core_reason(name) for name in CORE}
    for role, names in role_holders(adb, transport, user).items():
        for name in names:
            protected[name] = "Current default " + role.lower() + " app."
    for setting in [
        "default_input_method",
        "enabled_input_methods",
        "enabled_accessibility_services",
        "autofill_service",
        "tts_default_synth",
    ]:
        output = adb.shell(transport, "settings", "--user", str(user), "get", "secure", setting)
        if output in {"", "null"}:
            continue
        for entry in output.split(":"):
            component = entry.split(";")[0]
            name = component.split("/")[0]
            if (
                not PACKAGE_RE.fullmatch(name)
                or "/" in component
                and not re.fullmatch(r"[A-Za-z0-9_.$]+", component.split("/", 1)[1])
            ):
                raise AdbError(f"Cannot verify {setting}. Phone changes are blocked.")
            protected[name] = "Active service: " + setting.replace("_", " ") + "."
    policy = adb.shell(transport, "dumpsys", "device_policy")
    if not re.search(r"Enabled Device Admins \(User " + str(user) + r"\b", policy):
        raise AdbError("Cannot verify device administrators and owners for the personal profile.")
    for name in re.findall("(" + PACKAGE_PATTERN + r")/[.A-Za-z0-9_$]+", policy):
        protected[name] = "Active administrator, device owner, or profile owner."
    # Module enumeration must succeed even when the device reports an empty list.
    for name in package_list(adb.shell(transport, "pm", "list", "packages", "--apex-only")):
        protected[name] = "Protected APEX system module."
    modules = adb.shell(transport, "pm", "get-moduleinfo", "--all")
    for line in modules.splitlines():
        match = re.fullmatch(r"ModuleInfo\{[^}]+\}\s+packageName:\s+(\S+)", line.strip())
        if not match or not PACKAGE_RE.fullmatch(match[1]):
            raise AdbError("Cannot verify system modules. Phone changes are blocked.")
        protected[match[1]] = "Protected system module."
    for name in package_dependencies(adb, transport):
        protected[name] = "System dependency reported by Android package manager."
    return protected


def reason(package, protected):
    return (
        core_reason(package.id)
        or protected.get(package.id, "")
        or (
            "Protected APEX system module."
            if package.apex
            else "Protected persistent system service or shared platform identity."
            if package.critical
            else "Hidden, suspended, quarantined, or archived package. Manage it on the phone."
            if package.unusual
            else "Package state is unverified. Scan again."
            if package.state == "unverified"
            else ""
        )
    )
