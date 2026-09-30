"""Catalog contracts complement real HA entity and service integration checks."""

import json
from pathlib import Path

import pytest

COMPONENT = Path(__file__).parents[1] / "custom_components/sems_wallbox"


def catalog(name):
    return json.loads((COMPONENT / name).read_text(encoding="utf-8"))


@pytest.mark.parametrize("language", ["strings.json", "translations/en.json", "translations/cs.json"])
def test_icon_entities_and_states_have_matching_translation_contracts(language):
    entities = catalog(language)["entity"]
    for domain, definitions in catalog("icons.json")["entity"].items():
        for key, definition in definitions.items():
            assert key in entities[domain], (domain, key)
            assert definition["default"].startswith("mdi:")
            for state, icon in definition.get("state", {}).items():
                assert icon.startswith("mdi:")
                if domain not in ("switch", "binary_sensor"):
                    assert state in entities[domain][key]["state"], (domain, key, state)


def test_measurements_keep_standard_device_class_icons():
    sensors = catalog("icons.json")["entity"]["sensor"]
    assert "power" not in sensors
    assert not any(key.startswith(("current_", "voltage_")) for key in sensors)


def test_english_source_and_catalog_remain_identical():
    assert catalog("strings.json") == catalog("translations/en.json")


@pytest.mark.parametrize("language", ["en", "cs", "de", "es"])
def test_all_service_errors_have_translation_and_matching_placeholders(language):
    import re

    source = catalog("strings.json")["exceptions"]
    translated = catalog(f"translations/{language}.json")["exceptions"]
    assert source.keys() == translated.keys()
    for key, value in source.items():
        english = value["message"]
        czech = translated[key]["message"]
        assert czech.strip()
        assert set(re.findall(r"\{([^{}]+)\}", english)) == set(re.findall(r"\{([^{}]+)\}", czech))


@pytest.mark.parametrize("language", ["en", "cs", "de", "es"])
def test_catalog_covers_the_complete_source_structure(language):
    def paths(value, prefix=()):
        if isinstance(value, dict):
            return {path for key, child in value.items()
                    for path in paths(child, (*prefix, key))}
        return {prefix}

    assert paths(catalog("strings.json")) == paths(catalog(f"translations/{language}.json"))
    for definitions in catalog(f"translations/{language}.json")["entity"].values():
        for definition in definitions.values():
            assert definition["name"].strip()
            assert all(value.strip() for value in definition.get("state", {}).values())


def test_entity_code_uses_translated_names_and_known_keys():
    """Check all platform declarations, including native shared constructors."""
    import ast
    import re

    entities = catalog("strings.json")["entity"]
    for domain in ("sensor", "binary_sensor", "switch", "number", "select", "button"):
        source = (COMPONENT / (domain + ".py")).read_text(encoding="utf-8")
        tree = ast.parse(source)
        for node in ast.walk(tree):
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                assert node.name != "name", (domain, node.lineno)
            if isinstance(node, ast.Assign):
                for target in node.targets:
                    attribute = getattr(target, "id", getattr(target, "attr", ""))
                    if attribute == "_attr_name":
                        assert isinstance(node.value, ast.Constant) and node.value.value is None
                    if attribute == "_attr_translation_key" and isinstance(node.value, ast.Constant):
                        assert node.value.value in entities[domain], (domain, node.value.value)
            if (isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
                    and node.func.id.endswith("EntityDescription")):
                for keyword in node.keywords:
                    if keyword.arg == "translation_key":
                        assert ast.literal_eval(keyword.value) in entities[domain]
        # Dynamic phase keys are the only formatted platform translation keys.
        for phase in "abc":
            assert "current_" + phase in entities["sensor"]
            assert "voltage_" + phase in entities["sensor"]

    for path in COMPONENT.glob("*.py"):
        assert not re.search("[\u011b\u0161\u010d\u0159\u017e\u00fd\u00e1\u00ed\u00e9\u00fa\u016f\u0165\u010f\u0148]",
                             path.read_text(encoding="utf-8")), path.name

    tree = ast.parse((COMPONENT / "native_cloud_settings.py").read_text(encoding="utf-8"))
    for node in ast.walk(tree):
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id == "Setting":
            domain, key = ast.literal_eval(node.args[0]), ast.literal_eval(node.args[2])
            assert key in entities[domain], (domain, key)

    constructors = {
        "ChargingSwitch": "switch", "ConnectionSwitch": "switch",
        "ChargeModeSelect": "select", "ChargePowerNumber": "number",
        "ValueSensor": "sensor", "SessionEnergySensor": "sensor",
        "TransportStatusSensor": "sensor", "TotalEnergySensor": "sensor",
    }
    tree = ast.parse((COMPONENT / "native_entities.py").read_text(encoding="utf-8"))
    for node in ast.walk(tree):
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id in constructors:
            key = node.args[2]
            if isinstance(key, ast.Constant):
                assert key.value in entities[constructors[node.func.id]], key.value


@pytest.mark.parametrize("filename", ["strings.json", "translations/en.json", "translations/cs.json"])
def test_equivalent_transport_controls_have_identical_labels(filename):
    entities = catalog(filename)["entity"]
    for domain, first, second in [
        ("switch", "ensure_minimum_charging_power", "modbus_maintain_min_power"),
        ("switch", "start_charging", "modbus_start_charging"),
        ("switch", "dynamic_load_control", "modbus_dynamic_load"),
        ("switch", "phase_switch", "modbus_phase_switch"),
        ("switch", "plug_and_charge", "modbus_plug_charge"),
        ("select", "charge_mode", "modbus_charge_mode"),
        ("select", "charge_duration", "modbus_charge_duration"),
        ("number", "charge_power", "modbus_charge_power"),
        ("number", "current_limit", "modbus_current_limit"),
        ("number", "max_session_energy", "modbus_max_capacity"),
        ("number", "min_session_energy", "modbus_min_capacity"),
        ("number", "charge_target_soc", "modbus_bat_soc_limit"),
    ]:
        assert entities[domain][first] == entities[domain][second], (domain, first, second)


def test_declared_enum_options_have_translations():
    """Resolve static option declarations without importing platform dependencies."""
    import ast

    entities = catalog("strings.json")["entity"]

    def resolve(node, names):
        if isinstance(node, ast.Name):
            return names[node.id]
        if isinstance(node, ast.BinOp) and isinstance(node.op, ast.Add):
            return resolve(node.left, names) + resolve(node.right, names)
        if isinstance(node, ast.Call):
            if isinstance(node.func, ast.Name) and node.func.id == "list":
                return list(resolve(node.args[0], names))
            if isinstance(node.func, ast.Attribute) and node.func.attr == "values":
                return resolve(node.func.value, names).values()
        return ast.literal_eval(node)

    for filename, domain in [("sensor.py", "sensor"), ("select.py", "select"),
                             ("native_entities.py", None)]:
        tree = ast.parse((COMPONENT / filename).read_text(encoding="utf-8"))
        names = {}
        for node in tree.body:
            if isinstance(node, (ast.Assign, ast.AnnAssign)):
                target = node.targets[0] if isinstance(node, ast.Assign) else node.target
                try:
                    names[target.id] = resolve(node.value, names)
                except (ValueError, KeyError, TypeError, AttributeError):
                    continue  # Imports and runtime expressions are not option declarations.
        for cls in (node for node in tree.body if isinstance(node, ast.ClassDef)):
            attrs = {}
            for node in cls.body:
                if isinstance(node, (ast.Assign, ast.AnnAssign)):
                    target = node.targets[0] if isinstance(node, ast.Assign) else node.target
                    if isinstance(target, ast.Name) and target.id in ("_attr_translation_key", "_attr_options"):
                        attrs[target.id] = resolve(node.value, names)
            if "_attr_options" not in attrs:
                continue
            key = attrs.get("_attr_translation_key")
            if domain is None:
                native_keys = {"TransportStatusSensor": ("sensor", "active_transport"),
                               "ChargeModeSelect": ("select", "charge_mode")}
                entity_domain, key = native_keys[cls.name]
            else:
                entity_domain = domain
            assert set(attrs["_attr_options"]) <= entities[entity_domain][key]["state"].keys(), cls.name
