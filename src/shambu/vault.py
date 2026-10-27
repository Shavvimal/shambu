"""The LAN access code at rest: one macOS Keychain item per printer serial (service `shambu`), reached through
`/usr/bin/security`.

What it handles: the `Vault` Protocol (get, put, delete a code by serial), the shape checks a serial and a code must
pass before they reach the secure store (`safe_serial`, `safe_code`), `MemoryVault` (the in-memory implementation the
tests use) and `default_vault()`. Every code read from a vault is registered with `conf.remember_secret`, so every
outward text can scrub it.

What it does not do: printer profiles (profiles.py, conf.py), IP addresses, Bambu Studio's config (conf.py) or any
network access. A code never appears in a repr, an error text, a log line or a command line.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Protocol

from . import conf
from .errors import BambuError

SERVICE = "shambu"
SECURITY = Path("/usr/bin/security")
UNAVAILABLE = "no secure store on this platform (no /usr/bin/security); pass --access-code-file or --access-code-stdin per command"
UNSAFE = "a printer serial must be 8 to 32 letters or digits, and a code 4 to 128 characters with no space, quote or backslash"

# At least four characters (so `sensitive_values` keeps it), no whitespace, quote or backslash, no leading '-'.
_SAFE_CODE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:@+=/-]{3,127}")
_SAFE_SERIAL = re.compile(r"[A-Za-z0-9]{8,32}")


class VaultError(BambuError):
    """The secure store refused or failed a read, a write or a delete, or a serial or code has an unsafe shape."""


class UnavailableVault(VaultError):
    """There is no secure store on this platform: the code must come from a one-shot flag."""


def safe_code(code) -> bool:
    """True when `code` is a string the secure store can take as one argument: 4 to 128 safe characters."""
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
    serials and codes are refused before anything is recorded, as the Keychain vault refuses them."""

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
        """The stored code, registered with `conf.remember_secret`; None when there is none."""
        self._call("get", serial)
        code = self.items.get(serial)
        if code is not None:
            conf.remember_secret(code)
        return code

    def put(self, serial: str, code: str) -> None:
        """Store `code` for `serial`; VaultError for an unsafe code."""
        if not safe_code(code):
            raise VaultError(UNSAFE)
        self._call("put", serial)
        self.items[serial] = code

    def delete(self, serial: str) -> bool:
        """Remove the code for `serial`; False when there was none."""
        self._call("delete", serial)
        return self.items.pop(serial, None) is not None


class _Unavailable:
    """The vault of a platform with no secure store: every call raises UnavailableVault."""

    def get(self, serial: str) -> str | None:
        """Raises UnavailableVault."""
        raise UnavailableVault(UNAVAILABLE)

    def put(self, serial: str, code: str) -> None:
        """Raises UnavailableVault."""
        raise UnavailableVault(UNAVAILABLE)

    def delete(self, serial: str) -> bool:
        """Raises UnavailableVault."""
        raise UnavailableVault(UNAVAILABLE)


def default_vault() -> Vault:
    """The vault the commands use. Callers call it lazily, at use; the tests' `offline` fixture patches it."""
    # Slice B adds SecurityVault (the Keychain through /usr/bin/security) and returns it here.
    return _Unavailable()
