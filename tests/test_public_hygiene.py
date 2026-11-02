"""The repository is public: no tracked file may carry a home path, a LAN address, a Bambu account folder id, a
printer serial, a session id or a path into the owner's private notes. These shapes are generic, so they live here and
CI checks them. Employer, client and private project names cannot be listed in a public file: they live in the
untracked file `$XDG_CONFIG_HOME/shambu/private-terms` (one per line), which only a local run reads. CI has no such
file, and `test_tracked_files_carry_no_private_term` says so in its skip reason instead of passing silently."""

from __future__ import annotations

import os
import re
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent

# Session and thread ids. Agent sessions and review threads are UUIDs, and a post-mortem often cites only the first
# eight hex digits. A bare hex rule cannot work here: commit hashes (7 to 40 hex digits after "commit", "HEAD", "at"
# or "tag"), uv.lock digests and colour codes are legitimate in tracked files. So the rules are:
# - a full UUID anywhere, except as an XML `uuid="..."` attribute, where 3MF object, component and build ids live
#   (Bambu Studio writes fixed ones, and the fixtures and the format reference copy them);
# - a short id only when the word session, thread, conversation or transcript comes just before it, and only when
#   it holds 7 or more hex digits with at least one letter, so "session 2" and a date such as "thread 20261029" pass.
UUID = r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}"
SESSION_UUID = re.compile(rf'(?<![0-9a-z-])(?<!uuid=")(?<!UUID="){UUID}(?![0-9a-z-])', re.I)
SESSION_PREFIX = re.compile(r"\b(?:session|thread|conversation|transcript)\W{1,3}(?=[0-9]*[a-f])[0-9a-f]{7,}(?![0-9a-z])", re.I)

# Bambu printer serials: 15 upper-case characters that start with a digit, mostly digits, with at least one letter
# (an X2D serial starts `20P`). shambu masks a serial as its first and last three characters around an ellipsis.
# The fixtures use invented serials that hold a run of four or more letters (`00X0TEST0000999`), and every invented
# masked serial starts with the invented model code `00X`; both forms pass, a real serial or mask fails.
SERIAL = re.compile(r"(?<![0-9A-Za-z])(?![0-9A-Z]*[A-Z]{4})(?=(?:[A-Z]*[0-9]){8})(?=[0-9]*[A-Z])[0-9][0-9A-Z]{14}(?![0-9A-Za-z])")
MASKED_SERIAL = re.compile(r"(?<![0-9A-Za-z])(?!00X)[0-9][0-9A-Z]{2}(?:…|\.\.\.)[0-9A-Z]{3}(?![0-9A-Za-z])")

# A path into a personal site or notes repo: `content/drafts/...` or a `blog` path segment. A URL such as
# `https://example.com/blog/` passes: the segment must not follow a host name or another URL character.
PRIVATE_PATH = re.compile(r"\bcontent/drafts/|(?<![\w.:/~-])(?:[\w.~-]*/)+blogs?(?![\w.-])|(?<![\w.:/~-])blogs?/")

# Generic words that only appear when a private context leaks. Safe to publish: none names a person or a company.
GENERIC_TERMS = ["blog repo", "owner's blog", "my employer", "our client"]

# Each shape: (name, pattern, path prefix it applies to, or "" for every tracked file).
SHAPES = [
    ("home path", re.compile(r"/(?:Users|home)/(?!<)[A-Za-z0-9_.-]+"), ""),
    ("192.168 address", re.compile(r"\b192\.168\.\d{1,3}\.\d{1,3}\b"), ""),
    ("Bambu preset folder id", re.compile(r"\buser/\d{7,}/|preset_folder\W{0,4}\d{7,}"), ""),
    ("session id", SESSION_UUID, ""),
    ("session id prefix", SESSION_PREFIX, ""),
    ("printer serial", SERIAL, ""),
    ("masked printer serial", MASKED_SERIAL, ""),
    ("private notes path", PRIVATE_PATH, ""),
]

SELF = "tests/test_public_hygiene.py"

# Files that name the author on purpose (licence holder, package and plugin author). The shapes still apply to them;
# the private terms do not, because the owner's own name is one of those terms.
AUTHORSHIP = {"LICENSE", "pyproject.toml", ".claude-plugin/plugin.json", ".claude-plugin/marketplace.json"}


def tracked_files() -> list[str]:
    try:
        out = subprocess.run(["git", "ls-files", "-z"], cwd=ROOT, capture_output=True, check=True).stdout
    except (OSError, subprocess.CalledProcessError):
        pytest.skip("not a git checkout")
    return [p for p in out.decode().split("\0") if p and p != SELF]


def private_terms_path() -> Path:
    base = os.environ.get("XDG_CONFIG_HOME") or str(Path.home() / ".config")
    return Path(base) / "shambu" / "private-terms"


def private_terms() -> list[str] | None:
    """The local terms, or None when the file is absent (always so in CI)."""
    path = private_terms_path()
    if not path.is_file():
        return None
    lines = (line.strip() for line in path.read_text(encoding="utf-8").splitlines())
    return [line for line in lines if line and not line.startswith("#")]


def term_pattern(terms: list[str]) -> re.Pattern[str] | None:
    """One case-insensitive pattern; a term matches as a whole word, so a short name does not match inside a handle."""
    if not terms:
        return None
    alternatives = "|".join(re.escape(t) for t in sorted(terms, key=len, reverse=True))
    return re.compile(rf"(?<!\w)(?:{alternatives})(?!\w)", re.I)


GENERIC = term_pattern(GENERIC_TERMS)


def scan(rel: str, text: str, terms: list[str], shapes: bool = True) -> list[str]:
    """Hits as `path:line: kind`. A private term is never printed: the hit says only "private term"."""
    private = None if rel in AUTHORSHIP else term_pattern(terms)
    hits = []
    for n, line in enumerate(text.splitlines(), 1):
        if shapes:
            hits += [f"{rel}:{n}: {name}" for name, rx, prefix in SHAPES if rel.startswith(prefix) and rx.search(line)]
            if GENERIC is not None and GENERIC.search(line):
                hits.append(f"{rel}:{n}: private context word")
        if private is not None and private.search(line):
            hits.append(f"{rel}:{n}: private term")
    return hits


def scan_tree(files: list[str], terms: list[str], shapes: bool = True) -> list[str]:
    hits = []
    for rel in files:
        try:
            text = (ROOT / rel).read_text(encoding="utf-8")
        except (UnicodeDecodeError, FileNotFoundError, IsADirectoryError):
            continue  # binary fixture or a deleted, not yet staged file
        hits += scan(rel, text, terms, shapes)
    return hits


def test_tracked_files_carry_no_private_shape():
    hits = scan_tree(tracked_files(), [])
    assert hits == [], "private identifiers in tracked files:\n" + "\n".join(hits)


def test_tracked_files_carry_no_private_term():
    terms = private_terms()
    if terms is None:
        pytest.skip(
            f"no {private_terms_path()}: employer, client and private project names were NOT checked "
            "(CI never has this file; see CONTRIBUTING.md)"
        )
    if not terms:
        pytest.skip(f"{private_terms_path()} lists no terms: private names were NOT checked")
    hits = scan_tree(tracked_files(), terms, shapes=False)
    assert hits == [], f"private terms from {private_terms_path()} in tracked files:\n" + "\n".join(hits)


@pytest.mark.parametrize(
    ("rel", "bad"),
    [
        ("README.md", "/Users/someone/Code"),
        ("README.md", "host 192.168.1.20"),
        ("docs/x.md", "BambuStudio/user/1234567/process/a.json"),
        ("docs/x.md", "`preset_folder` 1234567)"),
        ("docs/errors/x.md", "session 0f0e0d0c-1111-4222-8333-444455556666"),
        ("README.md", "resume 0f0e0d0c-1111-4222-8333-444455556666"),
        ("docs/errors/x.md", "Claude Code session `0f0e0d0c` on 29 October"),
        ("docs/specs/x.md", "Design review (Codex, thread 0a1b2c3d, 2026-10-29)"),
        ("docs/x.md", "the X2D printer `01P09A123456789`"),
        ("tests/x.py", 'SERIAL = "0309CA470300123"'),
        ("docs/x.md", "the X2D printer `01P…789`"),
        ("docs/x.md", "the X2D printer 01P...789"),
        ("docs/x.md", "Evidence folder: `content/drafts/project/plates/`"),
        ("docs/x.md", "logs in `~/Code/blog/plates/`"),
        ("docs/x.md", "see notes/blog for the plates"),
        ("docs/x.md", "the plate files in the Blog Repo"),
        ("docs/x.md", "the Secret-Project plate"),
        ("docs/x.md", "Secret-Project's plate"),
    ],
)
def test_scan_flags_each_shape(rel, bad):
    assert scan(rel, f"ok line\n{bad}\n", ["secret-project"]) != []


def test_scan_passes_placeholders():
    text = (
        "~/Library/Application Support/BambuStudio/user/<preset_folder>/\nsession `<session-id>`\n/Users/<name>/x\n"
        'SERIAL = "00X0TEST0000999"\nOTHER = "00X0OTHER000111"\nmasked 00X…999 and 00X…123\n'
        "the X2D printer `<printer-serial>`, serial prefix `20P`, `hms_en_20P.json`\n"
        "`<repo>/plates/` in the owner's private notes\n"
    )
    assert scan("docs/errors/x.md", text, []) == []


def test_scan_passes_commit_hashes_3mf_uuids_and_urls():
    text = (
        "shambu 0.5.1 (git tag v0.5.1, commit 5efeabd), Studio master `79df7ae`, HEAD `926a7192`\n"
        '<build p:UUID="2c7c17d8-22b5-4d84-8835-1976022ea369">\n'
        '<part id="5" uuid="81d97e24-a4e0-4acd-a9aa-45f427c676e6">\n'
        "session 2 of 3, thread 20261029\n"
        "https://printago.io/blog/bambu-studio-cli-reference and https://blog.bambulab.com/firmware-update\n"
        "a private term matches whole words only: secretary, topsecret\n"
    )
    assert scan("docs/errors/x.md", text, ["secret"]) == []


def test_authorship_files_skip_private_terms_only():
    assert scan("LICENSE", "Copyright (c) 2026 Secret Person\n", ["secret"]) == []
    assert scan("LICENSE", "/Users/someone/x\n", ["secret"]) != []
