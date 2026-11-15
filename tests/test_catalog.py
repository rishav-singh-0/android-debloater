from android_debloater.catalog import Catalog
from android_debloater.models import Override


def test_unknown_packages_stay_unknown_and_overrides_are_field_specific():
    catalog = Catalog()
    package = "com.unknown.oem"
    assert catalog.describe(package, {}, {})["confidence"] == "unknown"
    local = {package: Override(name="Local")}
    global_values = {package: Override(name="Global", purposes=["utilities"], notes="My notes")}
    row = catalog.describe(package, local, global_values)
    assert row["name"] == "Local"
    assert row["purposes"] == ["tools"]
    assert row["notes"] == "My notes"
    assert row["advice"] == "unknown"


def test_all_bundled_categories_fit_the_six_groups():
    from typing import get_args

    from android_debloater.models import Purpose

    allowed = set(get_args(Purpose))
    assert len(allowed) == 6
    catalog = Catalog()
    for package in [*catalog.entries, "com.unknown.app"]:
        assert set(catalog.describe(package, {}, {})["purposes"]) <= allowed
