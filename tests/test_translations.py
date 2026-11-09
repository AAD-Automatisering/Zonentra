"""Every UI identifier used in code has a non-empty label in every language.

Pure file checks (no Home Assistant instance): a missing translation renders as
an empty menu entry, field or state in the frontend without any error.
"""

import ast
import json
import re
from pathlib import Path

import pytest

from custom_components.zonentra.thermostat import PRESETS

ROOT = Path(__file__).parents[1] / "custom_components" / "zonentra"
FILES = {
    "strings": ROOT / "strings.json",
    "en": ROOT / "translations" / "en.json",
    "nl": ROOT / "translations" / "nl.json",
}
TRANSLATIONS = {name: json.loads(path.read_text()) for name, path in FILES.items()}
FLOW = ast.parse((ROOT / "config_flow.py").read_text())


def leaves(tree, prefix=()):
    if isinstance(tree, dict):
        for key, value in tree.items():
            yield from leaves(value, (*prefix, key))
    else:
        yield prefix, tree


def label(language, *path):
    node = TRANSLATIONS[language]
    for key in path:
        node = node.get(key, {}) if isinstance(node, dict) else {}
    return node if isinstance(node, str) else ""


def keyword(call, name):
    return next((k.value for k in call.keywords if k.arg == name), None)


def flow_classes():
    """Map each flow class in config_flow.py to its translation section(s)."""
    classes = {n.name: n for n in FLOW.body if isinstance(n, ast.ClassDef)}
    roots = {"ConfigFlow": "config", "OptionsFlow": "options"}

    def sections(name, seen=()):
        found = set()
        for base in classes[name].bases:
            base_name = getattr(base, "id", "")
            if base_name in roots or base_name == "OptionsFlowWithReload":
                found.add(roots.get(base_name, "options"))
            elif base_name in classes and base_name not in seen:
                found |= sections(base_name, (*seen, name))
        return found

    result = {name: sections(name) for name in classes}
    for name in classes:  # Mixins take the sections of the flows using them.
        if not result[name]:
            result[name] = set().union(
                *(
                    result[other]
                    for other, node in classes.items()
                    if name in {getattr(base, "id", "") for base in node.bases}
                )
            )
    return classes, result


def menus():
    """(section, step_id, option) for every async_show_menu call."""
    classes, sections = flow_classes()
    for name, cls in classes.items():
        for node in ast.walk(cls):
            if isinstance(node, ast.Call) and getattr(node.func, "attr", "") == (
                "async_show_menu"
            ):
                step = keyword(node, "step_id").value
                for option in keyword(node, "menu_options").elts:
                    for section in sections[name]:
                        yield section, step, option.value


def flow_steps():
    """(section, step) for every async_step_* method that can show a step."""
    classes, sections = flow_classes()
    steps = set()
    for name, cls in classes.items():
        for node in cls.body:
            if isinstance(node, ast.AsyncFunctionDef) and node.name.startswith(
                "async_step_"
            ):
                step = node.name.removeprefix("async_step_")
                if step != "finish":  # Saves only; never shows a form.
                    steps |= {(section, step) for section in sections[name]}
    return sorted(steps)


def schema_fields():
    """Every vol.Required/vol.Optional field name used in a form."""
    return sorted(
        {
            node.args[0].value
            for node in ast.walk(FLOW)
            if isinstance(node, ast.Call)
            and getattr(node.func, "attr", "") in ("Required", "Optional")
            and node.args
            and isinstance(node.args[0], ast.Constant)
        }
    )


def sensor_translations():
    tree = ast.parse((ROOT / "sensor.py").read_text())
    for cls in (n for n in ast.walk(tree) if isinstance(n, ast.ClassDef)):
        values = {
            target.id: statement.value
            for statement in cls.body
            if isinstance(statement, ast.Assign)
            for target in statement.targets
            if isinstance(target, ast.Name)
        }
        if "_attr_translation_key" in values:
            options = values.get("_attr_options")
            yield (
                values["_attr_translation_key"].value,
                [item.value for item in options.elts] if options else [],
            )


def error_codes():
    source = (ROOT / "models.py").read_text() + (ROOT / "config_flow.py").read_text()
    codes = set(re.findall(r'ValueError\("(\w+)"\)', source))
    codes |= set(re.findall(r'errors=\{"base": "(\w+)"\}', source))
    return sorted(codes)


def test_strings_and_english_are_identical():
    assert TRANSLATIONS["strings"] == TRANSLATIONS["en"]


@pytest.mark.parametrize("language", ["en", "nl"])
def test_languages_have_identical_keys_and_no_empty_labels(language):
    reference = {path for path, _ in leaves(TRANSLATIONS["strings"])}
    assert {path for path, _ in leaves(TRANSLATIONS[language])} == reference
    empty = [path for path, value in leaves(TRANSLATIONS[language]) if not value]
    assert not empty


@pytest.mark.parametrize("language", list(FILES))
@pytest.mark.parametrize(("section", "step", "option"), list(menus()))
def test_every_menu_option_has_a_label(language, section, step, option):
    assert label(language, section, "step", step, "menu_options", option)


def test_menu_contains_central_configuration():
    assert ("options", "init", "general") in list(menus())
    assert ("config", "user", "central") in list(menus())
    assert label("nl", "config", "step", "user", "menu_options", "zone") == "Ruimte"


@pytest.mark.parametrize("language", list(FILES))
@pytest.mark.parametrize(("section", "step"), flow_steps())
def test_every_flow_step_has_a_title(language, section, step):
    assert label(language, section, "step", step, "title")


@pytest.mark.parametrize("language", list(FILES))
@pytest.mark.parametrize("field", schema_fields())
def test_every_form_field_has_a_label(language, field):
    sections = TRANSLATIONS[language]["options"]["step"].values()
    config = TRANSLATIONS[language]["config"]["step"].values()
    assert any(field in step.get("data", {}) for step in (*sections, *config))


@pytest.mark.parametrize("language", list(FILES))
@pytest.mark.parametrize("section", ["config", "options"])
@pytest.mark.parametrize("code", error_codes())
def test_every_error_has_a_message(language, section, code):
    assert label(language, section, "error", code)


@pytest.mark.parametrize("language", list(FILES))
def test_legacy_repair_issue_is_translated(language):
    assert label(language, "issues", "legacy_configuration", "title")
    assert "{name}" in label(language, "issues", "legacy_configuration", "description")


@pytest.mark.parametrize("language", list(FILES))
@pytest.mark.parametrize(("key", "states"), list(sensor_translations()))
def test_every_sensor_state_has_a_label(language, key, states):
    assert label(language, "entity", "sensor", key, "name")
    for state in states:
        assert label(language, "entity", "sensor", key, "state", state)


# 0.9: control entities on the zone and central devices, and service errors.
@pytest.mark.parametrize("language", list(FILES))
@pytest.mark.parametrize(
    ("platform", "key"),
    [
        ("switch", "active_mode_control"),
        ("switch", "room_regulation"),
        ("switch", "window_bypass"),
        ("switch", "fan_control"),
        ("switch", "screen_regulation"),
        ("number", "comfort_heating_limit"),
        ("number", "comfort_cooling_limit"),
        ("number", "central_heating_limit"),
        ("number", "central_cooling_limit"),
        ("number", "central_eco_heating_limit"),
        ("number", "central_eco_cooling_limit"),
        ("number", "central_away_heating_limit"),
        ("number", "central_away_cooling_limit"),
        ("number", "eco_heating_limit"),
        ("number", "eco_cooling_limit"),
        ("number", "away_heating_limit"),
        ("number", "away_cooling_limit"),
    ],
)
def test_control_entities_have_a_name(language, platform, key):
    assert label(language, "entity", platform, key, "name")


@pytest.mark.parametrize("language", list(FILES))
@pytest.mark.parametrize("preset", PRESETS)
def test_thermostat_presets_are_translated(language, preset):
    path = ("entity", "climate", "thermostat", "state_attributes", "preset_mode")
    assert label(language, *path, "state", preset)


def service_error_codes():
    source = "".join(
        (ROOT / name).read_text()
        for name in ("thermostat.py", "runtime.py", "entity.py")
    )
    codes = set(re.findall(r'ValueError\("(\w+)"\)', source))
    return sorted(codes | set(re.findall(r'translation_key="(\w+)"', source)))


@pytest.mark.parametrize("language", list(FILES))
@pytest.mark.parametrize("code", service_error_codes())
def test_every_service_error_has_a_message(language, code):
    assert label(language, "exceptions", code, "message")
