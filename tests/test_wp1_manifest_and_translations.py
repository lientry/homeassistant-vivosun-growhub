"""WP-1 manifest and translation tests."""

from __future__ import annotations

import json
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parents[1]
HACS_PATH = BASE_DIR / "hacs.json"
MANIFEST_PATH = BASE_DIR / "custom_components" / "vivosun_growhub" / "manifest.json"
STRINGS_PATH = BASE_DIR / "custom_components" / "vivosun_growhub" / "strings.json"
PYPROJECT_PATH = BASE_DIR / "pyproject.toml"
EN_TRANSLATION_PATH = BASE_DIR / "custom_components" / "vivosun_growhub" / "translations" / "en.json"


def _flatten_leaf_paths(node: object, prefix: str = "") -> set[str]:
    """Return the set of leaf-key paths for a dictionary-like JSON object."""
    if isinstance(node, dict):
        leaf_paths: set[str] = set()
        for key, value in node.items():
            path = f"{prefix}.{key}" if prefix else key
            leaf_paths.update(_flatten_leaf_paths(value, path))
        return leaf_paths

    return {prefix}


def test_manifest_matches_wp1_requirements() -> None:
    """manifest.json must contain required WP-1 fields/values."""
    manifest = json.loads(MANIFEST_PATH.read_text(encoding="utf-8"))

    assert manifest["domain"] == "vivosun_growhub"
    assert manifest["name"] == "Vivosun GrowHub"
    pyproject = PYPROJECT_PATH.read_text(encoding="utf-8")
    assert f'version = "{manifest["version"]}"' in pyproject
    assert manifest["documentation"] == "https://github.com/lientry/homeassistant-vivosun-growhub"
    assert manifest["issue_tracker"] == "https://github.com/lientry/homeassistant-vivosun-growhub/issues"
    assert manifest["config_flow"] is True
    assert manifest["requirements"] == ["websockets>=13.1"]
    assert manifest["dependencies"] == []
    codeowners = manifest["codeowners"]
    assert isinstance(codeowners, list)
    assert codeowners
    assert all(isinstance(owner, str) and owner.startswith("@") for owner in codeowners)
    assert "@lientry" in codeowners
    assert manifest["iot_class"] == "cloud_polling"
    assert manifest["loggers"] == ["custom_components.vivosun_growhub"]


def test_strings_and_en_translation_key_parity() -> None:
    """strings.json and en.json should expose the same required key structure."""
    strings = json.loads(STRINGS_PATH.read_text(encoding="utf-8"))
    translation = json.loads(EN_TRANSLATION_PATH.read_text(encoding="utf-8"))

    required_top_level = ("config", "options")
    for key in required_top_level:
        assert key in strings
        assert key in translation

    assert _flatten_leaf_paths(strings["config"]) == _flatten_leaf_paths(translation["config"])
    assert _flatten_leaf_paths(strings["options"]) == _flatten_leaf_paths(translation["options"])
    assert "selector" not in strings and "selector" not in translation

    assert "error.invalid_auth" in _flatten_leaf_paths(strings["config"])
    assert "error.cannot_connect" in _flatten_leaf_paths(strings["config"])
    assert "error.unknown" in _flatten_leaf_paths(strings["config"])
    assert "step.user.data.email" in _flatten_leaf_paths(strings["config"])
    assert "step.user.data.password" in _flatten_leaf_paths(strings["config"])
    assert "step.init.data.temp_unit" not in _flatten_leaf_paths(strings["options"])


def test_marketplace_declares_real_november_minimum() -> None:
    """Installation metadata and CI target the supported native Home Assistant API."""
    import tomllib

    hacs = json.loads(HACS_PATH.read_text(encoding="utf-8"))
    pyproject = tomllib.loads(PYPROJECT_PATH.read_text(encoding="utf-8"))
    assert hacs["homeassistant"] == "2026.11.0"
    assert pyproject["project"]["requires-python"] == ">=3.14.2"
    workflow = (BASE_DIR / ".github" / "workflows" / "ci.yml").read_text(encoding="utf-8")
    assert '"homeassistant==2026.11.0"' in workflow
    assert '"pytest-homeassistant==2026.11.0"' in workflow
