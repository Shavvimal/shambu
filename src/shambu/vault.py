"""The LAN access code at rest: one macOS Keychain item per printer serial (service `shambu`), reached through
`/usr/bin/security`.

What it handles: the `Vault` Protocol (get, put, delete a code by serial), the shape checks a serial and a code must
pass before they reach the secure store (`safe_serial`, `safe_code`), `SecurityVault` (the Keychain), `MemoryVault`
(the in-memory implementation the tests use) and `default_vault()`. Every code read from a vault is registered with
`conf.remember_secret`, so every outward text can scrub it.

The stdin protocol: `SecurityVault` runs `security -i` (argv is only the binary and `-i`, never `-v` or `-q`) and
writes one command to its stdin, so the code never sits on a command line another process can read. The framing
rule: exactly one command and one trailing newline, nothing else. `security -i` returns the status of the last
command it ran, so a missing newline runs nothing (exit 0) and a second blank line masks a failed lookup (exit 0).
On `get` the code comes back on the child's captured stdout: that is IPC with a trusted binary, not output.

The ACL: the application that creates an item is trusted to read it without a prompt. That application is
`security` itself, and `-T /usr/bin/security` makes it explicit. A locked keychain or a changed ACL can still show a
Keychain dialog, and `timeout` bounds the wait for it.

What it does not do: printer profiles (profiles.py, conf.py), IP addresses, Bambu Studio's config (conf.py) or any
network access. A code never appears in a repr, an error text, a log line or a command line.
"""

from __future__ import annotations

import re
import subprocess
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import NoReturn, Protocol

from . import conf
from .errors import BambuError

SERVICE = "shambu"
SECURITY = Path("/usr/bin/security")
UNAVAILABLE = "no secure store on this platform (no /usr/bin/security); pass --access-code-file or --access-code-stdin per command"
UNSAFE = "a printer serial must be 8 to 32 letters or digits, and a code 4 to 128 characters with no space, quote or backslash"
UNSAFE_CODE = "the access code holds characters the Keychain write cannot carry (4 to 128 of letters, digits and ._:@+=/-, no leading -)"

Runner = Callable[..., "subprocess.CompletedProcess[str]"]

# Exit statuses of `security`: errSecItemNotFound (44) and errSecDuplicateItem (45).
_NOT_FOUND = 44
_DUPLICATE = 45

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


@dataclass
class SecurityVault:
    """A `Vault` in the macOS Keychain, through `security -i` with the command (and so the code) on stdin. Serials
    and codes are checked before any subprocess runs; every failure is a VaultError (UnavailableVault when there is
    no `security` binary) whose text never holds the code."""

    binary: Path = SECURITY
    service: str = SERVICE
    timeout: float = 5.0
    runner: Runner = field(default=subprocess.run, repr=False)  # injectable: tests pass a fake

    def _run(self, words: list[str], secret: str = "") -> subprocess.CompletedProcess[str]:
        """Run one `security -i` command: argv is the binary and `-i` only, stdin is the command and one newline."""
        try:
            return self.runner([str(self.binary), "-i"], input=" ".join(words) + "\n", capture_output=True, text=True, timeout=self.timeout)
        except FileNotFoundError:
            raise UnavailableVault(UNAVAILABLE) from None
        except subprocess.TimeoutExpired:
            raise VaultError(f"security gave no answer within {self.timeout:g} s (a Keychain dialog may be waiting)") from None
        except OSError as e:
            reason = str(e.strerror or e.errno)
            raise VaultError(f"cannot run security ({reason.replace(secret, '<redacted>') if secret else reason})") from None

    def _raise(self, r: subprocess.CompletedProcess[str], what: str, secret: str = "") -> NoReturn:
        """Raise VaultError for a failed command: stderr with `secret` and every code this process knows redacted
        first, then cut to 200 characters (a cut first could leave part of a code that no longer matches)."""
        text = conf.scrub_text((r.stderr or "").strip(), ([secret] if secret else []) + conf.secret_values())[:200]
        raise VaultError(f"security could not {what} the Keychain item (exit {r.returncode}: {text})")

    def get(self, serial: str) -> str | None:
        """The code for `serial`, registered with `conf.remember_secret`; None when there is no item."""
        if not safe_serial(serial):
            raise VaultError(UNSAFE)
        r = self._run(["find-generic-password", "-a", serial, "-s", self.service, "-w"])
        if r.returncode == _NOT_FOUND:
            return None
        if r.returncode != 0:
            self._raise(r, "read")
        out = r.stdout or ""
        code = out[:-1] if out.endswith("\n") else out
        if not code:
            raise VaultError(f"the Keychain item for {conf.mask(serial)} is empty")
        conf.remember_secret(code)
        return code

    def put(self, serial: str, code: str) -> None:
        """Store `code` for `serial`, updating any item there in place (`-U`)."""
        if not safe_serial(serial):
            raise VaultError(UNSAFE)
        if not safe_code(code):
            raise VaultError(UNSAFE_CODE)
        r = self._run(["add-generic-password", "-U", "-a", serial, "-s", self.service, "-T", str(self.binary), "-w", code], secret=code)
        if r.returncode == _DUPLICATE:
            raise VaultError(f"a Keychain item for {conf.mask(serial)} already exists and security could not update it")
        if r.returncode != 0:
            self._raise(r, "write", secret=code)
        conf.remember_secret(code)

    def delete(self, serial: str) -> bool:
        """Remove the item for `serial`; False when there was none."""
        if not safe_serial(serial):
            raise VaultError(UNSAFE)
        r = self._run(["delete-generic-password", "-a", serial, "-s", self.service])
        if r.returncode == _NOT_FOUND:
            return False
        if r.returncode != 0:
            self._raise(r, "delete")
        return True


def default_vault() -> Vault:
    """The vault the commands use: the Keychain. Callers call it lazily, at use; the tests' `offline` fixture patches
    it. With no `/usr/bin/security`, every call raises UnavailableVault."""
    return SecurityVault()
