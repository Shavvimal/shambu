"""Printer profiles: the schema, the validator and the pure Profile/ProfileSet types for printers.json. No I/O (conf.py
reads and writes the file), no access code (vault.py), no network.

A profile names one printer: its serial, its last known IP, the connection mode, whether the TLS check is skipped, when
it was added and when its last successful `login` was (`verified`). The model, the firmware and where the code is kept
are not stored: shambu derives them when it needs them. `validate_profile` and `validate_document` refuse anything else
with the fixed `REFUSAL` text: no extra key, no identifier (IP, host name, masked serial), serial or known secret in a
name, no IPv6 scope id, typed fields only. A refusal never echoes the refused content.
"""

from __future__ import annotations

import ipaddress
import re
from collections.abc import Iterable
from dataclasses import dataclass, field, replace
from datetime import datetime

from . import conf
from .errors import BambuError

PROFILES_VERSION = 1
MODES = ("lan", "cloud", "unknown")
REFUSAL = "a printer profile must match the schema: fixed fields, typed values, no identifier or secret in a name"
CLASH = "another printer profile already has that name"
UNKNOWN = "no printer profile has that name"

_NAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,31}")
_SERIAL = re.compile(r"[A-Za-z0-9]{8,32}")
_STAMP = re.compile(r"[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}Z")
_STAMP_FORMAT = "%Y-%m-%dT%H:%M:%SZ"
_KEYS = frozenset({"name", "serial", "ip", "mode", "insecure", "added", "verified"})
_DOC_KEYS = frozenset({"version", "default", "printers"})

identifying = conf.identifying  # one detector (conf.py) for the profile names and every outward text


class ProfileError(BambuError):
    """A printer profile or the printers document does not match the schema, or a change to the set is refused."""


def _holds_secret(v: str, secrets: Iterable[str]) -> bool:
    """True when any non-empty secret is in `v`, case-insensitively."""
    low = v.casefold()
    return any(s and s.casefold() in low for s in secrets)


def _ip(v) -> bool:
    """An IPv4 or IPv6 address with no scope id (`%`)."""
    if not isinstance(v, str) or "%" in v:
        return False
    try:
        ipaddress.ip_address(v)
    except ValueError:
        return False
    return True


def _stamp(v) -> bool:
    """A UTC time stamp `YYYY-MM-DDTHH:MM:SSZ` in ASCII digits that is a real date and time."""
    if not isinstance(v, str) or _STAMP.fullmatch(v) is None:
        return False
    try:
        datetime.strptime(v, _STAMP_FORMAT)
    except ValueError:
        return False
    return True


def valid_name(name: str) -> bool:
    """True when `name` passes the profile name rule of `validate_profile`: 1 to 32 letters, digits, '.', '_' or '-',
    starting with a letter or a digit, and no IP, host name or masked serial (`identifying`)."""
    return isinstance(name, str) and _NAME.fullmatch(name) is not None and not identifying(name)


def validate_profile(d, secrets: Iterable[str] = ()) -> None:
    """Raises ProfileError(REFUSAL) unless `d` is one printer profile with exactly the schema's keys and valid values.
    `name` and `ip` must hold none of `secrets`; `name` must also hold neither the serial nor its mask (any case)."""
    secrets = tuple(secrets)
    if not isinstance(d, dict) or set(d) != _KEYS:
        raise ProfileError(REFUSAL)
    name, serial, ip = d["name"], d["serial"], d["ip"]
    if not isinstance(serial, str) or _SERIAL.fullmatch(serial) is None:
        raise ProfileError(REFUSAL)
    ok = (
        isinstance(name, str)
        and _NAME.fullmatch(name) is not None
        and not identifying(name)
        and not _holds_secret(name, (*secrets, serial, conf.mask(serial)))
        and (ip is None or (_ip(ip) and not _holds_secret(ip, secrets)))
        and d["mode"] in MODES
        and type(d["insecure"]) is bool
        and _stamp(d["added"])
        and (d["verified"] is None or _stamp(d["verified"]))
    )
    if not ok:
        raise ProfileError(REFUSAL)


def validate_document(doc, secrets: Iterable[str] = ()) -> None:
    """Raises ProfileError(REFUSAL) unless `doc` is a version-1 printers document: valid profiles, unique names and
    serials, and a default that is None or the name of an entry. Each name and IP is checked against every serial and
    mask in the document, not only its own, so a document the writer accepts is accepted on every later load."""
    secrets = tuple(secrets)
    if not isinstance(doc, dict) or set(doc) != _DOC_KEYS:
        raise ProfileError(REFUSAL)
    if type(doc["version"]) is not int or doc["version"] != PROFILES_VERSION or not isinstance(doc["printers"], list):
        raise ProfileError(REFUSAL)
    serials_in_doc = [d["serial"] for d in doc["printers"] if isinstance(d, dict) and isinstance(d.get("serial"), str)]
    ids = tuple(v for s in serials_in_doc if s for v in (s, conf.mask(s)) if v != "…")
    for d in doc["printers"]:
        validate_profile(d, (*secrets, *ids))
    names = [d["name"] for d in doc["printers"]]
    serials = [d["serial"] for d in doc["printers"]]
    if len(set(names)) != len(names) or len(set(serials)) != len(serials):
        raise ProfileError(REFUSAL)
    if doc["default"] is not None and (not isinstance(doc["default"], str) or doc["default"] not in names):
        raise ProfileError(REFUSAL)


@dataclass(frozen=True)
class Profile:
    """One printer: its serial, last known IP, mode, TLS flag, when it was added and when its last successful `login`
    was (`verified`). The serial and the IP stay out of the repr."""

    name: str
    serial: str = field(repr=False)
    ip: str | None = field(repr=False)
    mode: str
    insecure: bool
    added: str
    verified: str | None

    @classmethod
    def from_dict(cls, d, secrets: Iterable[str] = ()) -> Profile:
        """The profile in `d`, validated first (ProfileError on any refusal)."""
        validate_profile(d, secrets)
        return cls(**d)

    def to_dict(self) -> dict:
        """The stored form: every schema key."""
        return {
            "name": self.name,
            "serial": self.serial,
            "ip": self.ip,
            "mode": self.mode,
            "insecure": self.insecure,
            "added": self.added,
            "verified": self.verified,
        }

    def public(self) -> dict:
        """The stored form with the serial masked (the IP stays: `printer list` is the one place it shows)."""
        return self.to_dict() | {"serial": conf.mask(self.serial)}


@dataclass(frozen=True)
class ProfileSet:
    """Every printer profile and the name of the default one. Changes return a new set."""

    default: str | None = None
    printers: tuple[Profile, ...] = ()

    def get(self, name: str) -> Profile | None:
        """The profile named `name`, or None."""
        return next((p for p in self.printers if p.name == name), None)

    def by_serial(self, serial: str) -> Profile | None:
        """The profile for `serial`, or None."""
        return next((p for p in self.printers if p.serial == serial), None)

    def selected(self, name: str | None = None) -> Profile | None:
        """The profile named `name`, or the default profile when `name` is None; None when there is none."""
        if name is None:
            return self.get(self.default) if self.default is not None else None
        return self.get(name)

    def upsert(self, p: Profile, default: bool = False, secrets: Iterable[str] = ()) -> ProfileSet:
        """The set with `p` added, or replacing the profile with the same serial whatever its name (its `added` kept).
        ProfileError(CLASH) when another serial already has the name, ProfileError(REFUSAL) when `p` does not validate
        against `secrets`. The first profile becomes the default; `default=True` makes `p` the default."""
        validate_profile(p.to_dict(), secrets)
        old = self.by_serial(p.serial)
        clash = self.get(p.name)
        if clash is not None and clash.serial != p.serial:
            raise ProfileError(CLASH)
        if old is None:
            printers = (*self.printers, p)
        else:
            p = replace(p, added=old.added)
            printers = tuple(p if x.serial == p.serial else x for x in self.printers)
        new_default = self.default
        if old is not None and self.default == old.name:
            new_default = p.name  # a rename keeps the default on the same printer
        if new_default is None or default:
            new_default = p.name
        return ProfileSet(default=new_default, printers=printers)

    def remove(self, name: str) -> ProfileSet:
        """The set without the profile named `name`; when it was the default, the first remaining profile (or None)
        becomes the default. ProfileError when no profile has that name."""
        if self.get(name) is None:
            raise ProfileError(UNKNOWN)
        printers = tuple(p for p in self.printers if p.name != name)
        new_default = self.default
        if self.default == name:
            new_default = printers[0].name if printers else None
        return ProfileSet(default=new_default, printers=printers)

    @classmethod
    def from_dict(cls, doc, secrets: Iterable[str] = ()) -> ProfileSet:
        """The set in a printers document, validated first against `secrets` (ProfileError on any refusal)."""
        validate_document(doc, secrets)
        return cls(default=doc["default"], printers=tuple(Profile(**d) for d in doc["printers"]))

    def to_dict(self) -> dict:
        """The printers document: version, default and every profile in order."""
        return {"version": PROFILES_VERSION, "default": self.default, "printers": [p.to_dict() for p in self.printers]}
