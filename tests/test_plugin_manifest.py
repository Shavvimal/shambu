"""The plugin manifest and the package must agree on the version; the marketplace entry carries none."""

import json
import re
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


VERSION_LITERALS = (
    re.compile(r"--expect-version (\d+\.\d+\.\d+)"),  # the shambu skill's doctor line, the setup skill's checks
    re.compile(r"shambu@v(\d+\.\d+\.\d+)"),  # the setup skill's install tag
    re.compile(r"must print: shambu (\d+\.\d+\.\d+)"),
)


def test_skill_version_literals_match_package():
    """Each skill text names the CLI version it was written for; the release bump must move every literal with
    `__version__` (a 0.2.0 skill once drove a 0.2.1 CLI and a 0.3.0 checkout in one session)."""
    skill = (ROOT / "skills" / "shambu" / "SKILL.md").read_text()
    setup = (ROOT / "skills" / "setup" / "SKILL.md").read_text()
    assert VERSION_LITERALS[0].findall(skill), "the shambu skill must run doctor with --expect-version"
    assert VERSION_LITERALS[1].findall(setup), "the setup skill must install a tagged version"
    found = {v for text in (skill, setup) for rx in VERSION_LITERALS for v in rx.findall(text)}
    assert found == {shambu.__version__}
