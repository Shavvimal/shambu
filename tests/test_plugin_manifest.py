"""The plugin manifest and the package must agree on the version; the marketplace entry carries none."""

import json
from pathlib import Path

import shambu

ROOT = Path(__file__).resolve().parents[1]


def test_plugin_version_matches_package():
    manifest = json.loads((ROOT / ".claude-plugin" / "plugin.json").read_text())
    assert manifest["name"] == "shambu"
    assert manifest["version"] == shambu.__version__


def test_marketplace_entries_have_no_version():
    market = json.loads((ROOT / ".claude-plugin" / "marketplace.json").read_text())
    assert market["name"] == "shambu"
    for entry in market["plugins"]:
        assert "version" not in entry
        assert entry["source"] == "./"


def test_skills_exist():
    for name in ("shambu", "setup"):
        assert (ROOT / "skills" / name / "SKILL.md").is_file()
