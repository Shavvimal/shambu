"""Printer profiles: the schema, the validator and the pure Profile/ProfileSet types for printers.json. No I/O (conf.py
reads and writes the file), no access code (vault.py), no network.

A profile names one printer: its serial, its last known IP, its model, the connection mode, where its code is kept
(`keychain`, `studio` or unknown), whether the TLS check is skipped, when it was added and last verified, and a
revision that every change increments (so a stale relocation proposal is detected even when the IP is the same).
`validate_profile` and `validate_document` refuse anything else with the fixed `REFUSAL` text: no extra key, no
identifier (IP, host name, masked serial) or known secret in a name, typed fields only. A refusal never echoes the
refused content.
"""

from __future__ import annotations

import ipaddress
import re
from collections.abc import Iterable
from dataclasses import dataclass, replace

from . import conf
from .errors import BambuError

PROFILES_VERSION = 1
MODES = ("lan", "cloud", "unknown")
CODE_SOURCES = ("keychain", "studio")
REFUSAL = "a printer profile must match the schema: fixed fields, typed values, no identifier or secret in a name"
CLASH = "another printer profile already has that name"
UNKNOWN = "no printer profile has that name"

_NAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,31}")
_SERIAL = re.compile(r"[A-Za-z0-9]{8,32}")
_STAMP = re.compile(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z")
_KEYS = frozenset({"name", "serial", "ip", "model", "mode", "code_source", "insecure", "added", "verified", "revision"})
_DOC_KEYS = frozenset({"version", "default", "printers"})
_MODEL_MAX = 40

identifying = conf.identifying  # one detector (conf.py) for the profile names and every outward text


class ProfileError(BambuError):
    """A printer profile or the printers document does not match the schema, or a change to the set is refused."""


def _holds_secret(v: str, secrets: Iterable[str]) -> bool:
    return any(s and s in v for s in secrets)


def _ip(v) -> bool:
    if not isinstance(v, str):
        return False
    try:
        ipaddress.ip_address(v)
    except ValueError:
        return False
    return True


def _stamp(v) -> bool:
    return isinstance(v, str) and _STAMP.fullmatch(v) is not None


def validate_profile(d, secrets: Iterable[str] = ()) -> None:
    """Raises ProfileError(REFUSAL) unless `d` is one printer profile with exactly the schema's keys and valid values."""
    secrets = tuple(secrets)
    if not isinstance(d, dict) or set(d) != _KEYS:
        raise ProfileError(REFUSAL)
    name, serial, ip, model = d["name"], d["serial"], d["ip"], d["model"]
    ok = (
        isinstance(name, str)
        and _NAME.fullmatch(name) is not None
        and not identifying(name)
        and not _holds_secret(name, secrets)
        and isinstance(serial, str)
        and _SERIAL.fullmatch(serial) is not None
        and (ip is None or _ip(ip))
        and (
            model is None
            or (
                isinstance(model, str)
                and 0 < len(model) <= _MODEL_MAX
                and model.isprintable()
                and not identifying(model)
                and not _holds_secret(model, secrets)
            )
        )
        and d["mode"] in MODES
        and (d["code_source"] is None or d["code_source"] in CODE_SOURCES)
        and type(d["insecure"]) is bool
        and _stamp(d["added"])
        and (d["verified"] is None or _stamp(d["verified"]))
        and type(d["revision"]) is int
        and d["revision"] >= 0
    )
    if not ok:
        raise ProfileError(REFUSAL)


def validate_document(doc) -> None:
    """Raises ProfileError(REFUSAL) unless `doc` is a version-1 printers document: valid profiles, unique names and
    serials, and a default that is None or the name of an entry."""
    if not isinstance(doc, dict) or set(doc) != _DOC_KEYS:
        raise ProfileError(REFUSAL)
    if type(doc["version"]) is not int or doc["version"] != PROFILES_VERSION or not isinstance(doc["printers"], list):
        raise ProfileError(REFUSAL)
    for d in doc["printers"]:
        validate_profile(d)
    names = [d["name"] for d in doc["printers"]]
    serials = [d["serial"] for d in doc["printers"]]
    if len(set(names)) != len(names) or len(set(serials)) != len(serials):
        raise ProfileError(REFUSAL)
    if doc["default"] is not None and (not isinstance(doc["default"], str) or doc["default"] not in names):
        raise ProfileError(REFUSAL)


@dataclass(frozen=True)
class Profile:
    """One printer: its serial, last known IP, model, mode, where its code is kept, and the bookkeeping fields."""

    name: str
    serial: str
    ip: str | None
    model: str | None
    mode: str
    code_source: str | None
    insecure: bool
    added: str
    verified: str | None
    revision: int = 0

    @classmethod
    def from_dict(cls, d) -> Profile:
        """The profile in `d`, validated first (ProfileError on any refusal)."""
        validate_profile(d)
        return cls(**d)

    def to_dict(self) -> dict:
        """The stored form: every schema key."""
        return {
            "name": self.name,
            "serial": self.serial,
            "ip": self.ip,
            "model": self.model,
            "mode": self.mode,
            "code_source": self.code_source,
            "insecure": self.insecure,
            "added": self.added,
            "verified": self.verified,
            "revision": self.revision,
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

    def upsert(self, p: Profile, default: bool = False) -> ProfileSet:
        """The set with `p` added, or replacing the profile with the same serial whatever its name (its `added` kept,
        its `revision` incremented). ProfileError when another serial already has the name. The first profile
        becomes the default; `default=True` makes `p` the default."""
        validate_profile(p.to_dict())
        old = self.by_serial(p.serial)
        clash = self.get(p.name)
        if clash is not None and clash.serial != p.serial:
            raise ProfileError(CLASH)
        if old is None:
            printers = (*self.printers, p)
        else:
            p = replace(p, added=old.added, revision=old.revision + 1)
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
    def from_dict(cls, doc) -> ProfileSet:
        """The set in a printers document, validated first (ProfileError on any refusal)."""
        validate_document(doc)
        return cls(default=doc["default"], printers=tuple(Profile(**d) for d in doc["printers"]))

    def to_dict(self) -> dict:
        """The printers document: version, default and every profile in order."""
        return {"version": PROFILES_VERSION, "default": self.default, "printers": [p.to_dict() for p in self.printers]}
