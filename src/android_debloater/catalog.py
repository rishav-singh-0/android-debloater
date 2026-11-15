import json
from importlib.resources import files

from .models import Override


class Catalog:
    def __init__(self):
        data = json.loads(files("android_debloater").joinpath("resources/catalog.json").read_text())
        self.version = data["version"]
        self.entries = data["packages"]

    def describe(self, package, local: dict, global_overrides: dict):
        description = self.entries.get(package, {})
        result = {
            "name": package,
            "purposes": ["other"],
            "usefulness": "unknown",
            "notes": "",
            "purpose": "No description available for this package.",
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
