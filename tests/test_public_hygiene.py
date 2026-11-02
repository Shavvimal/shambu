"""The repository is public: no tracked file may carry a home path, a LAN address, a Bambu account folder id or a
session id. Extra private names live in the untracked file `$XDG_CONFIG_HOME/shambu/private-terms` (one per line)."""

from __future__ import annotations

import os
import re
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent

# Each shape: (name, pattern, path prefix it applies to, or "" for every tracked file).
SHAPES = [
    ("home path", re.compile(r"/(?:Users|home)/(?!<)[A-Za-z0-9_.-]+"), ""),
    ("192.168 address", re.compile(r"\b192\.168\.\d{1,3}\.\d{1,3}\b"), ""),
    ("Bambu preset folder id", re.compile(r"\buser/\d{7,}/|preset_folder\W{0,4}\d{7,}"), ""),
    ("session id", re.compile(r"\b[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}\b", re.I), "docs/errors/"),
]

SELF = "tests/test_public_hygiene.py"


def tracked_files() -> list[str]:
    try:
        out = subprocess.run(["git", "ls-files", "-z"], cwd=ROOT, capture_output=True, check=True).stdout
    except (OSError, subprocess.CalledProcessError):
        pytest.skip("not a git checkout")
    return [p for p in out.decode().split("\0") if p and p != SELF]


def private_terms() -> list[str]:
    base = os.environ.get("XDG_CONFIG_HOME") or str(Path.home() / ".config")
    path = Path(base) / "shambu" / "private-terms"
    if not path.is_file():
        return []
    lines = (line.strip() for line in path.read_text(encoding="utf-8").splitlines())
    return [line for line in lines if line and not line.startswith("#")]


def scan(rel: str, text: str, terms: list[str]) -> list[str]:
    hits = []
    for n, line in enumerate(text.splitlines(), 1):
        for name, rx, prefix in SHAPES:
            if rel.startswith(prefix) and rx.search(line):
                hits.append(f"{rel}:{n}: {name}")
        lowered = line.lower()
        hits += [f"{rel}:{n}: private term" for t in terms if t.lower() in lowered]
    return hits


def scan_tree(files: list[str], terms: list[str]) -> list[str]:
    hits = []
    for rel in files:
        try:
            text = (ROOT / rel).read_text(encoding="utf-8")
        except (UnicodeDecodeError, FileNotFoundError, IsADirectoryError):
            continue  # binary fixture or a deleted, not yet staged file
        hits += scan(rel, text, terms)
    return hits


def test_tracked_files_carry_no_private_identifier():
    hits = scan_tree(tracked_files(), private_terms())
    assert hits == [], "private identifiers in tracked files:\n" + "\n".join(hits)


@pytest.mark.parametrize(
    ("rel", "bad"),
    [
        ("README.md", "/Users/someone/Code"),
        ("README.md", "host 192.168.1.20"),
        ("docs/x.md", "BambuStudio/user/1234567/process/a.json"),
        ("docs/x.md", "`preset_folder` 1234567)"),
        ("docs/errors/x.md", "session 0f0e0d0c-1111-4222-8333-444455556666"),
        ("docs/x.md", "the Secret-Project plate"),
    ],
)
def test_scan_flags_each_shape(rel, bad):
    assert scan(rel, f"ok line\n{bad}\n", ["secret-project"]) != []


def test_scan_passes_placeholders():
    text = "~/Library/Application Support/BambuStudio/user/<preset_folder>/\nsession `<session-id>`\n/Users/<name>/x\n"
    assert scan("docs/errors/x.md", text, []) == []
