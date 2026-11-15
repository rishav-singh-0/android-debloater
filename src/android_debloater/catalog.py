import json
from importlib.resources import files

from .models import Override


class Catalog:
    def __init__(self):
        data = json.loads(files("android_debloater").joinpath("resources/catalog.json").read_text())
        self.version = data["version"]
        self.entries = data["packages"]

    def describe(self, package, local: dict, global_overrides: dict, online: dict | None = None):
        description = self.entries.get(package, {})
        result = {
            "name": package,
            "purposes": ["other"],
            "usefulness": "unknown",
            "notes": "",
            "purpose": "No description yet. Use Update app info to look it up.",
            "impact": "Removal impact is unknown. Check its purpose and dependencies before deciding.",
            "advice": "unknown",
            "confidence": "unknown",
            "source": "Unknown package",
            "name_source": "Package ID",
        }
        result.update(description)
        if description:
            result["source"] = f"Bundled catalog {self.version}"
            result["name_source"] = result["source"]
        info = (online or {}).get(package)
        if info:
            result.update(
                purpose=info.description,
                info_source=info.source,
                info_category=info.category,
                info_fetched_at=info.fetched_at,
                info_url=(
                    f"https://play.google.com/store/apps/details?id={package}"
                    if info.source == "Google Play"
                    else "https://github.com/Universal-Debloater-Alliance/"
                    "universal-android-debloater-next-generation/blob/main/resources/assets/uad_lists.json"
                ),
            )
            result["source"] = f"{result['source']}; {info.source}" if description else info.source
            if info.name:
                result.update(name=info.name, name_source=info.source)
            if info.purposes:
                result["purposes"] = info.purposes
        for source, values in [
            ("Personal override for all devices", global_overrides),
            ("Personal override for this device", local),
        ]:
            override = values.get(package)
            if override:
                fields = Override.model_validate(override).model_dump(exclude_none=True)
                if not fields.get("name"):
                    fields.pop("name", None)
                result.update(fields)
                if fields:
                    result["source"] += "; " + source.lower()
                if "name" in fields:
                    result["name_source"] = source
        return result
