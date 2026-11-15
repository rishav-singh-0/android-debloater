"""Non-editable core rules plus fresh, profile-scoped device role checks."""

import re

from .adb import PACKAGE_RE, AdbError, package_list

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


def core_reason(package):
    base = package.split(".auto_generated", 1)[0]
    if base in CORE or package.startswith(CORE_PREFIXES):
        return "Protected core platform component or shared system dependency."
    return ""


def live_protection(adb, transport, user):
    protected = {name: core_reason(name) for name in CORE}
    for role in ["HOME", "DIALER", "SMS", "BROWSER", "ASSISTANT", "EMERGENCY"]:
        output = adb.shell(
            transport, "cmd", "role", "get-role-holders", "--user", str(user), "android.app.role." + role
        )
        for name in output.splitlines():
            name = name.strip()
            if not PACKAGE_RE.fullmatch(name):
                raise AdbError(f"Cannot verify the {role.lower()} role. Phone changes are blocked.")
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
    for name in re.findall(r"([A-Za-z][A-Za-z0-9_]*(?:\.[A-Za-z0-9_]+)+)/[.A-Za-z0-9_$]+", policy):
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
    dump = adb.shell(transport, "dumpsys", "package")
    known = re.search(r"^Known Packages:\n(.*?)(?=^\S|\Z)", dump, re.MULTILINE | re.DOTALL)
    if not known:
        raise AdbError("Cannot verify package-manager core dependencies.")
    for line in known[1].splitlines():
        name = line.strip()
        if PACKAGE_RE.fullmatch(name):
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
