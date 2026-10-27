"""profiles.py: the printer-profile schema, the validators and the Profile/ProfileSet types. Pure: no file, no vault."""

from __future__ import annotations

import pytest

from shambu import conf
from shambu.profiles import (
    CLASH,
    PROFILES_VERSION,
    REFUSAL,
    UNKNOWN,
    Profile,
    ProfileError,
    ProfileSet,
    validate_document,
    validate_profile,
)

SERIAL = "00X0TEST0000999"
SERIAL2 = "00X0TEST0000888"
SENTINEL = "SENTINEL-CODE-8812"


def entry(**over) -> dict:
    d = {
        "name": "desk",
        "serial": SERIAL,
        "ip": "10.0.0.9",
        "model": "Bambu Lab X2D",
        "mode": "lan",
        "code_source": "keychain",
        "insecure": False,
        "added": "2026-10-27T09:00:00Z",
        "verified": None,
        "revision": 0,
    }
    return d | over


def doc(*printers: dict, default: str | None = "desk", version=PROFILES_VERSION) -> dict:
    return {"version": version, "default": default, "printers": list(printers)}


def test_profile_round_trip():
    d = entry(verified="2026-10-27T09:05:00Z", revision=3, ip="fe80::1", code_source=None, mode="unknown")
    p = Profile.from_dict(d)
    assert p.to_dict() == d
    assert p.public()["serial"] == conf.mask(SERIAL) and p.public()["ip"] == "fe80::1"


def test_profile_set_round_trip():
    d = doc(entry(), entry(name="garage", serial=SERIAL2, ip=None, model=None, mode="cloud", insecure=True))
    ps = ProfileSet.from_dict(d)
    assert ps.to_dict() == d
    assert ps.get("garage").serial == SERIAL2 and ps.by_serial(SERIAL).name == "desk"
    assert ps.get("nope") is None and ps.by_serial(SERIAL2[:-1] + "7") is None


def test_an_empty_document_is_valid():
    assert ProfileSet.from_dict(doc(default=None)) == ProfileSet()
    assert ProfileSet().to_dict() == doc(default=None)


@pytest.mark.parametrize(
    ("change", "refused"),
    [
        ({"extra": 1}, "extra"),
        ({"name": "10.0.0.40"}, "10.0.0.40"),
        ({"name": "192.0.2.40"}, "192.0.2.40"),
        ({"name": "x2d.local"}, "x2d.local"),
        ({"name": "00X…999"}, "00X"),
        ({"name": "00X...999"}, "00X"),
        ({"name": "-desk"}, "-desk"),
        ({"name": "a" * 33}, "a" * 33),
        ({"name": "desk room"}, "desk room"),
        ({"name": 7}, "7"),
        ({"serial": "00X0"}, "00X0"),
        ({"serial": "00X0TEST-000999"}, "00X0TEST-000999"),
        ({"ip": "10.0.0.9:8883"}, "10.0.0.9"),
        ({"ip": "printer.local"}, "printer.local"),
        ({"ip": 167772169}, "167772169"),
        ({"model": "X2D at 10.0.0.9"}, "10.0.0.9"),
        ({"model": "x2d.local"}, "x2d.local"),
        ({"model": "m" * 41}, "m" * 41),
        ({"model": "X2D\n"}, "X2D\n"),
        ({"model": ""}, "never-in-text"),
        ({"mode": "wifi"}, "wifi"),
        ({"mode": None}, "never-in-text"),
        ({"code_source": "vault"}, "vault"),
        ({"insecure": 1}, "never-in-text"),
        ({"insecure": "false"}, "never-in-text"),
        ({"added": "2026-10-27 09:00:00"}, "2026-10-27"),
        ({"added": None}, "never-in-text"),
        ({"verified": "yesterday"}, "yesterday"),
        ({"revision": True}, "never-in-text"),
        ({"revision": -1}, "never-in-text"),
        ({"revision": 1.0}, "never-in-text"),
    ],
)
def test_validate_profile_refuses(change, refused):
    with pytest.raises(ProfileError) as e:
        validate_profile(entry(**change))
    assert str(e.value) == REFUSAL and refused not in str(e.value)


def test_validate_profile_refuses_a_missing_key_and_a_non_dict():
    d = entry()
    del d["verified"]
    for bad in (d, None, [entry()], "desk"):
        with pytest.raises(ProfileError, match=f"^{REFUSAL}$"):
            validate_profile(bad)


@pytest.mark.parametrize("field", ["name", "model"])
def test_a_known_secret_in_a_name_is_refused(field):
    d = entry(**{field: f"desk-{SENTINEL}"})
    validate_profile(d)  # no secret given: only the shape is checked
    with pytest.raises(ProfileError) as e:
        validate_profile(d, secrets=(SENTINEL,))
    assert str(e.value) == REFUSAL and SENTINEL not in str(e.value)
    validate_profile(entry(), secrets=("", SENTINEL))  # an empty secret matches nothing


@pytest.mark.parametrize(
    "bad",
    [
        doc(entry(), version=2),
        doc(entry(), version=True),
        doc(entry(), entry(serial=SERIAL2)),  # duplicate names
        doc(entry(), entry(name="garage")),  # duplicate serials
        doc(entry(), default="garage"),  # the default names no entry
        doc(entry(), default=7),
        doc(entry(name="x2d.local"), default=None),
        {"version": 1, "default": None},
        doc(default=None) | {"extra": []},
        {"version": 1, "default": None, "printers": {}},
        [entry()],
    ],
)
def test_validate_document_refuses(bad):
    with pytest.raises(ProfileError) as e:
        validate_document(bad)
    assert str(e.value) == REFUSAL and SERIAL not in str(e.value) and "x2d.local" not in str(e.value)
    with pytest.raises(ProfileError):
        ProfileSet.from_dict(bad)


def test_the_first_profile_becomes_the_default_and_selected_none_returns_it():
    ps = ProfileSet().upsert(Profile.from_dict(entry()))
    assert ps.default == "desk" and ps.selected(None).name == "desk"
    ps = ps.upsert(Profile.from_dict(entry(name="garage", serial=SERIAL2)))
    assert ps.default == "desk" and ps.selected(None).name == "desk" and ps.selected("garage").serial == SERIAL2
    assert ps.selected("nope") is None and ProfileSet().selected(None) is None
    assert ps.upsert(Profile.from_dict(entry(name="garage", serial=SERIAL2)), default=True).default == "garage"


def test_upsert_by_serial_keeps_added_and_increments_revision():
    ps = ProfileSet().upsert(Profile.from_dict(entry()))
    again = Profile.from_dict(entry(name="office", ip="10.0.0.10", added="2026-10-28T10:00:00Z", revision=0))
    ps2 = ps.upsert(again)
    (p,) = ps2.printers
    assert (p.name, p.ip, p.added, p.revision) == ("office", "10.0.0.10", "2026-10-27T09:00:00Z", 1)
    assert ps2.default == "office"  # the renamed printer stays the default
    assert ps2.upsert(again).printers[0].revision == 2
    assert ps.printers[0].revision == 0  # the old set is unchanged


def test_upsert_refuses_a_name_another_serial_has():
    ps = ProfileSet().upsert(Profile.from_dict(entry()))
    with pytest.raises(ProfileError) as e:
        ps.upsert(Profile.from_dict(entry(serial=SERIAL2)))
    assert str(e.value) == CLASH and SERIAL2 not in str(e.value)


def test_upsert_refuses_a_profile_that_does_not_validate():
    with pytest.raises(ProfileError, match=f"^{REFUSAL}$"):
        ProfileSet().upsert(Profile(**entry(name="x2d.local")))


def test_remove_moves_the_default():
    ps = ProfileSet().upsert(Profile.from_dict(entry())).upsert(Profile.from_dict(entry(name="garage", serial=SERIAL2)))
    after = ps.remove("desk")
    assert after.default == "garage" and [p.name for p in after.printers] == ["garage"]
    assert ps.remove("garage").default == "desk"  # removing another profile keeps the default
    assert after.remove("garage") == ProfileSet()
    with pytest.raises(ProfileError) as e:
        ps.remove("nope")
    assert str(e.value) == UNKNOWN and "nope" not in str(e.value)
