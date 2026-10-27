"""The LAN access code at rest: one 0600 JSON file, `credentials.json` in shambu's config dir (`conf.CREDENTIALS`).

What it handles: the `Vault` Protocol (get, put, delete a code by serial), the shape checks a serial and a code must
pass before they are stored (`safe_serial`, `safe_code`), `FileVault` (the file), `MemoryVault` (the in-memory
implementation the tests use) and `default_vault()`. The file is `{"version": 1, "codes": {"<serial>": "<code>"}}`,
written atomically with mode 0600 under a lock on `credentials.json.lock`. A file with any group or other permission
bit is refused, and so is a symlink, anything that is not a regular file and a file another user owns (`NOT_PRIVATE`).
Every code a vault reads or is given is registered with `conf.remember_secret`, so every outward text can scrub it: a
read registers every code in the file, and a put registers its code before it touches any file.

What it does not do: printer profiles (profiles.py, conf.py), IP addresses, Bambu Studio's config (conf.py) or any
network access. A code never appears in a repr, an error text or a log line.

Threat model: the file gets the same protection as Bambu Studio's own plain-text config against another process that
runs as the same user, with a tighter file mode, and it lives outside any repository. It does not protect the code
from that user's own processes or from anyone who can read that user's files.
"""

from __future__ import annotations

import errno
import json
import os
import re
import stat
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Protocol, TypeVar

from . import conf
from .errors import BambuError

T = TypeVar("T")

CREDENTIALS_VERSION = 1
UNSAFE = "a printer serial must be 8 to 32 letters or digits, and a code 4 to 128 characters with no space, quote or backslash"
BAD_FILE = (
    'the credentials file is not a shambu credentials document (want {"version": 1, "codes": {...}}): '
    "move it away and run shambu printer login again"
)
NOT_PRIVATE = "{path}: not a regular file owned by this user; remove it and run shambu printer login again"

# At least four characters, no whitespace, quote or backslash, no leading '-'.
_SAFE_CODE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:@+=/-]{3,127}")
_SAFE_SERIAL = re.compile(r"[A-Za-z0-9]{8,32}")


class VaultError(BambuError):
    """The credentials store refused or failed a read, a write or a delete, or a serial or code has an unsafe shape."""


def safe_code(code) -> bool:
    """True when `code` is 4 to 128 safe characters (a sanity rule for user input)."""
    return isinstance(code, str) and _SAFE_CODE.fullmatch(code) is not None


def safe_serial(serial) -> bool:
    """True when `serial` is 8 to 32 letters or digits."""
    return isinstance(serial, str) and _SAFE_SERIAL.fullmatch(serial) is not None


class Vault(Protocol):
    """Where a printer's code is kept at rest, by serial."""

    def get(self, serial: str) -> str | None:
        """The code for `serial`, or None when there is none."""
        ...

    def put(self, serial: str, code: str) -> None:
        """Store `code` for `serial`, replacing any code there."""
        ...

    def delete(self, serial: str) -> bool:
        """Remove the code for `serial`; False when there was none."""
        ...


@dataclass
class MemoryVault:
    """A `Vault` in a dict: the test fake and the second implementation of the Protocol. `calls` records
    `(op, serial)` and never the code; when `error` is set, every call raises it after recording the call. Unsafe
    serials and codes are refused before anything is recorded, as `FileVault` refuses them."""

    items: dict[str, str] = field(default_factory=dict, repr=False)
    calls: list[tuple[str, str]] = field(default_factory=list)
    error: Exception | None = None

    def _call(self, op: str, serial: str) -> None:
        if not safe_serial(serial):
            raise VaultError(UNSAFE)
        self.calls.append((op, serial))
        if self.error is not None:
            raise self.error

    def get(self, serial: str) -> str | None:
        """The stored code; every stored code is registered with `conf.remember_secret`, as `FileVault` registers every
        code in its file. None when there is none."""
        self._call("get", serial)
        for c in self.items.values():
            conf.remember_secret(c)
        return self.items.get(serial)

    def put(self, serial: str, code: str) -> None:
        """Store `code` for `serial` (registered with `conf.remember_secret` first); VaultError for an unsafe code."""
        conf.remember_secret(code)
        if not safe_code(code):
            raise VaultError(UNSAFE)
        self._call("put", serial)
        self.items[serial] = code

    def delete(self, serial: str) -> bool:
        """Remove the code for `serial`; False when there was none."""
        self._call("delete", serial)
        return self.items.pop(serial, None) is not None


@dataclass
class FileVault:
    """A `Vault` in `credentials.json`. `path` None means `conf.CREDENTIALS`, read at each call. Serials and codes are
    checked before any file is touched; every failure is a VaultError whose text never holds a code."""

    path: Path | None = None

    def _path(self) -> Path:
        return self.path if self.path is not None else conf.CREDENTIALS

    def _load(self, p: Path) -> dict[str, str]:
        """The codes in the file; {} when it does not exist. VaultError for a symlink, anything that is not a regular
        file owned by this user (`NOT_PRIVATE`), a file that is not private or not a version-1 credentials document.
        The open never follows a link and never blocks (a FIFO at the path is refused, not waited on)."""
        try:
            fd = os.open(p, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
        except FileNotFoundError:
            return {}
        except OSError as e:
            if e.errno == errno.ELOOP:  # O_NOFOLLOW met a symlink
                raise VaultError(NOT_PRIVATE.format(path=p)) from None
            raise VaultError(f"{p}: cannot read it ({e.strerror})") from None
        try:
            st = os.fstat(fd)
        except OSError as e:
            os.close(fd)
            raise VaultError(f"{p}: cannot read it ({e.strerror})") from None
        if not stat.S_ISREG(st.st_mode) or st.st_uid != os.getuid():  # checked before fdopen: a directory never reaches it
            os.close(fd)
            raise VaultError(NOT_PRIVATE.format(path=p))
        with os.fdopen(fd, encoding="utf-8") as f:
            if st.st_mode & 0o077:
                raise VaultError(f"{p} must be private: chmod 600 {p}")
            try:
                d = json.loads(f.read())
            except OSError as e:
                raise VaultError(f"{p}: cannot read it ({e.strerror})") from None
            except ValueError:
                raise VaultError(BAD_FILE) from None
        if (
            not isinstance(d, dict)
            or set(d) != {"version", "codes"}
            or type(d["version"]) is not int
            or d["version"] != CREDENTIALS_VERSION
            or not isinstance(d["codes"], dict)
            or not all(safe_serial(k) and safe_code(v) for k, v in d["codes"].items())
        ):
            raise VaultError(BAD_FILE)
        for c in d["codes"].values():  # every code in the file, not only the one asked for
            conf.remember_secret(c)
        return dict(d["codes"])

    def _save(self, p: Path, codes: dict[str, str]) -> None:
        conf._atomic_write(p, {"version": CREDENTIALS_VERSION, "codes": codes}, ".credentials-", mode=0o600)

    def _change(self, p: Path, fn: Callable[[dict[str, str]], T]) -> T:
        """`fn(codes)` under the lock on `credentials.json.lock`; a lock or write failure becomes a VaultError."""
        try:
            return conf._locked(p, lambda: fn(self._load(p)))
        except conf.DataFileError as e:
            raise VaultError(str(e)) from None

    def get(self, serial: str) -> str | None:
        """The code for `serial`, registered with `conf.remember_secret`; None when there is none."""
        if not safe_serial(serial):
            raise VaultError(UNSAFE)
        return self._load(self._path()).get(serial) or None  # `_load` registered every code

    def put(self, serial: str, code: str) -> None:
        """Store `code` for `serial`, replacing any code there. The code is registered with `conf.remember_secret` before
        any file is touched."""
        conf.remember_secret(code)
        if not safe_serial(serial) or not safe_code(code):
            raise VaultError(UNSAFE)
        p = self._path()

        def run(codes: dict[str, str]) -> None:
            codes[serial] = code
            self._save(p, codes)

        self._change(p, run)

    def delete(self, serial: str) -> bool:
        """Remove the code for `serial`; False when there was none."""
        if not safe_serial(serial):
            raise VaultError(UNSAFE)
        p = self._path()

        def run(codes: dict[str, str]) -> bool:
            if serial not in codes:
                return False
            del codes[serial]
            self._save(p, codes)
            return True

        return self._change(p, run)


def default_vault() -> Vault:
    """The vault the commands use: the credentials file in the config dir. Callers call it lazily, at use; the tests'
    `offline` fixture patches it."""
    return FileVault()
