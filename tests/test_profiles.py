"""profiles.py: the printer-profile schema, the validators and the Profile/ProfileSet types; conf.py: printers.json I/O."""

from __future__ import annotations

import json
import threading

import pytest

from shambu import conf
from shambu.errors import ConfError
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
FULLWIDTH_YEAR = "".join(chr(0xFF10 + int(c)) for c in "2026")  # \d matches these; [0-9] does not


def entry(**over) -> dict:
    d = {
        "name": "desk",
        "serial": SERIAL,
        "ip": "10.0.0.9",
        "mode": "lan",
        "insecure": False,
        "added": "2026-10-27T09:00:00Z",
        "verified": None,
    }
    return d | over


def doc(*printers: dict, default: str | None = "desk", version=PROFILES_VERSION) -> dict:
    return {"version": version, "default": default, "printers": list(printers)}


def test_profile_round_trip():
    d = entry(verified="2026-10-27T09:05:00Z", ip="fe80::1", mode="unknown")
    p = Profile.from_dict(d)
    assert p.to_dict() == d
    assert p.public()["serial"] == conf.mask(SERIAL) and p.public()["ip"] == "fe80::1"


def test_profile_set_round_trip():
    d = doc(entry(), entry(name="garage", serial=SERIAL2, ip=None, mode="cloud", insecure=True))
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
        ({"ip": "fe80::1%SENTINEL-CODE-8812"}, SENTINEL),  # no IPv6 scope id
        ({"ip": "fe80::1%en0"}, "en0"),
        ({"name": SERIAL}, SERIAL),
        ({"name": f"x{SERIAL.lower()}"}, SERIAL.lower()),
        ({"name": "desk-" + conf.mask(SERIAL)}, "desk-"),
        ({"added": FULLWIDTH_YEAR + "-10-27T09:00:00Z"}, FULLWIDTH_YEAR),  # full-width digits
        ({"added": "2026-13-27T09:00:00Z"}, "2026-13"),  # month 13
        ({"verified": "2026-02-30T09:00:00Z"}, "2026-02-30"),
        ({"mode": "wifi"}, "wifi"),
        ({"mode": None}, "never-in-text"),
        ({"insecure": 1}, "never-in-text"),
        ({"insecure": "false"}, "never-in-text"),
        ({"added": "2026-10-27 09:00:00"}, "2026-10-27"),
        ({"added": None}, "never-in-text"),
        ({"verified": "yesterday"}, "yesterday"),
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


def test_a_known_secret_in_a_name_or_ip_is_refused():
    d = entry(name="desk-abcd12")
    validate_profile(d)  # no secret given: only the shape is checked
    for secrets in (("abcd12",), ("ABCD12",)):  # any case
        with pytest.raises(ProfileError) as e:
            validate_profile(d, secrets=secrets)
        assert str(e.value) == REFUSAL and "abcd12" not in str(e.value)
    with pytest.raises(ProfileError):
        validate_profile(entry(ip="10.0.0.99"), secrets=("0.99",))
    validate_profile(entry(), secrets=("", SENTINEL))  # an empty secret matches nothing


def test_validate_document_and_upsert_pass_the_secrets_through():
    ok = doc(entry(name="desk-abcd12"), default=None)
    validate_document(ok)
    with pytest.raises(ProfileError):
        validate_document(ok, secrets=("abcd12",))
    with pytest.raises(ProfileError):
        ProfileSet.from_dict(ok, secrets=("abcd12",))
    with pytest.raises(ProfileError, match=f"^{REFUSAL}$"):
        ProfileSet().upsert(Profile.from_dict(entry(name="desk-abcd12")), secrets=("abcd12",))


def test_repr_holds_neither_the_serial_nor_the_ip():
    p = Profile.from_dict(entry())
    ps = ProfileSet().upsert(p)
    for text in (repr(p), repr(ps)):
        assert SERIAL not in text and "10.0.0.9" not in text and "desk" in text


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


def test_upsert_by_serial_keeps_added():
    ps = ProfileSet().upsert(Profile.from_dict(entry()))
    again = Profile.from_dict(entry(name="office", ip="10.0.0.10", added="2026-10-28T10:00:00Z", verified="2026-10-28T10:00:00Z"))
    ps2 = ps.upsert(again)
    (p,) = ps2.printers
    assert (p.name, p.ip, p.added, p.verified) == ("office", "10.0.0.10", "2026-10-27T09:00:00Z", "2026-10-28T10:00:00Z")
    assert ps2.default == "office"  # the renamed printer stays the default
    assert ps.printers[0].name == "desk"  # the old set is unchanged


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


# ---------------------------------------------------------------------------- printers.json (conf.py)
def two() -> ProfileSet:
    return ProfileSet.from_dict(doc(entry(), entry(name="garage", serial=SERIAL2, ip=None)))


def test_load_of_a_missing_file_is_an_empty_set(tmp_path):
    assert conf.load_profiles() == ProfileSet(None, ())
    assert conf.load_profiles(tmp_path / "printers.json") == ProfileSet()


def test_save_then_load_round_trips_and_leaves_only_the_file(tmp_path):
    p = tmp_path / "shambu" / "printers.json"
    assert conf.save_profiles(two(), p) == p
    assert conf.load_profiles(p) == two()
    assert json.loads(p.read_text())["version"] == 1
    assert [x.name for x in p.parent.iterdir()] == ["printers.json"]  # no temp file left
    assert p.stat().st_mode & 0o777 == 0o600


def test_save_uses_the_default_path():
    assert conf.save_profiles(two()) == conf.PRINTERS
    assert conf.load_profiles() == two()


@pytest.mark.parametrize(
    "text",
    [
        "{not json " + SERIAL,
        json.dumps(doc(entry(), version=2)),
        json.dumps([entry()]),
        json.dumps(doc({k: v for k, v in entry().items() if k != "verified"})),
        json.dumps(doc(entry(name="10.0.0.40"), default=None)),
    ],
)
def test_a_damaged_file_is_refused_without_its_content(tmp_path, text):
    p = tmp_path / "printers.json"
    p.write_text(text)
    with pytest.raises(ConfError) as e:
        conf.load_profiles(p)
    assert str(e.value) == REFUSAL
    assert SERIAL not in str(e.value) and "10.0.0" not in str(e.value) and e.value.__cause__ is None


def test_save_refuses_an_invalid_set_and_writes_nothing(tmp_path):
    p = tmp_path / "printers.json"
    bad = ProfileSet(default="desk", printers=(Profile(**entry(name="x2d.local")),))
    with pytest.raises(ConfError) as e:
        conf.save_profiles(bad, p)
    assert str(e.value) == REFUSAL and "x2d.local" not in str(e.value)
    assert list(tmp_path.iterdir()) == []


def test_a_write_failure_is_a_data_file_error(tmp_path, monkeypatch):
    def boom(*a):
        raise OSError(28, "No space left on device")

    monkeypatch.setattr(conf.os, "replace", boom)
    with pytest.raises(conf.DataFileError, match="No space left"):
        conf.save_profiles(two(), tmp_path / "printers.json")
    assert list(tmp_path.iterdir()) == []


def test_concurrent_updates_both_land(tmp_path):
    """Thread A holds the lock inside its change; thread B's update waits for it and then sees A's profile."""
    p = tmp_path / "printers.json"
    inside, release = threading.Event(), threading.Event()

    def slow(ps):
        inside.set()
        release.wait(5)
        return ps.upsert(Profile.from_dict(entry()))

    a = threading.Thread(target=conf.update_profiles, args=(slow, p))
    a.start()
    assert inside.wait(5)
    b = threading.Thread(
        target=conf.update_profiles, args=(lambda ps: ps.upsert(Profile.from_dict(entry(name="garage", serial=SERIAL2))), p)
    )
    b.start()
    b.join(0.3)
    assert b.is_alive()  # B waits on the lock
    release.set()
    a.join(5)
    b.join(5)
    assert sorted(x.name for x in conf.load_profiles(p).printers) == ["desk", "garage"]


def test_update_returns_the_saved_set():
    ps = conf.update_profiles(lambda ps: ps.upsert(Profile.from_dict(entry())))
    assert ps == conf.load_profiles() and ps.default == "desk"


def test_locked_profiles_serialises_two_commits_and_lets_fn_save(tmp_path):
    p = tmp_path / "printers.json"
    inside, release = threading.Event(), threading.Event()
    seen: list[tuple[str, ...]] = []

    def commit(new: dict, wait: bool):
        def fn(ps):
            seen.append(tuple(x.name for x in ps.printers))
            if wait:
                inside.set()
                release.wait(5)
            conf.save_profiles(ps.upsert(Profile.from_dict(new)), p)  # the save inside the lock
            return new["name"]

        return fn

    out: list[str] = []
    a = threading.Thread(target=lambda: out.append(conf.locked_profiles(commit(entry(), True), p)))
    a.start()
    assert inside.wait(5)
    b = threading.Thread(target=lambda: out.append(conf.locked_profiles(commit(entry(name="garage", serial=SERIAL2), False), p)))
    b.start()
    b.join(0.3)
    assert b.is_alive() and seen == [()]  # B has not loaded yet
    release.set()
    a.join(5)
    b.join(5)
    assert out == ["desk", "garage"] and seen == [(), ("desk",)]  # B saw A's commit
    assert [x.name for x in conf.load_profiles(p).printers] == ["desk", "garage"]


def test_select_profile_default_by_name_and_unknown(tmp_path):
    ps = two()
    assert conf.select_profile(None, ps).name == "desk"
    assert conf.select_profile("garage", ps).serial == SERIAL2
    assert conf.select_profile(None, ProfileSet()) is None
    with pytest.raises(ConfError) as e:
        conf.select_profile("nope", ps)
    assert str(e.value) == "no printer profile named 'nope': shambu printer list"
    p = tmp_path / "printers.json"
    conf.save_profiles(ps, p)
    assert conf.select_profile("garage", path=p).serial == SERIAL2
    assert conf.select_profile(None) is None  # no file at conf.PRINTERS


def test_load_registers_every_profile_serial_and_its_mask(tmp_path, monkeypatch):
    conf.save_profiles(two())
    conf.forget_secrets()
    conf.remember_secret(SENTINEL)
    assert SERIAL not in conf.sensitive_values()
    conf.load_profiles()
    vals = conf.sensitive_values()  # no Studio config
    for s in (SERIAL, SERIAL2, conf.mask(SERIAL), conf.mask(SERIAL2), SENTINEL):
        assert s in vals
    studio = tmp_path / "BambuStudio.conf"
    studio.write_text(
        json.dumps({"app": {"user_last_selected_machine": "00X0STUDIO00777"}, "access_code": {"00X0STUDIO00777": "abcd1234"}})
    )
    monkeypatch.setattr(conf, "CONF", studio)
    vals = conf.sensitive_values()
    for s in (SERIAL, SERIAL2, conf.mask(SERIAL), "00X0STUDIO00777", conf.mask("00X0STUDIO00777"), "abcd1234", SENTINEL):
        assert s in vals
    assert SERIAL not in conf.secret_values() and conf.mask(SERIAL) not in conf.secret_values()  # codes only
    conf.forget_secrets()
    assert SERIAL not in conf.sensitive_values()


def test_sensitive_values_do_not_read_the_profile_file(monkeypatch):
    def boom(p):
        raise AssertionError("sensitive_values must not read printers.json")

    monkeypatch.setattr(conf, "_read_profiles", boom)
    conf.remember_secret(SENTINEL)
    assert conf.sensitive_values() == [SENTINEL]


def test_a_hand_edited_name_holding_a_runtime_secret_refuses_to_load(tmp_path):
    p = tmp_path / "printers.json"
    p.write_text(json.dumps(doc(entry(name="desk-xy12"), default=None)))
    assert conf.load_profiles(p).printers[0].name == "desk-xy12"
    conf.remember_secret("xy12")
    with pytest.raises(ConfError) as e:
        conf.load_profiles(p)
    assert str(e.value) == REFUSAL and "xy12" not in str(e.value)
    with pytest.raises(ConfError, match=f"^{REFUSAL}$"):
        conf.save_profiles(ProfileSet.from_dict(doc(entry(name="desk-xy12"), default=None)), tmp_path / "other.json")


@pytest.mark.parametrize("studio", [{"access_code": ["broken"]}, ["a", "list", "root"], {"access_code": {"S": 5}}, {"app": "x"}])
def test_a_malformed_studio_config_never_breaks_scrubbing(tmp_path, monkeypatch, studio):
    p = tmp_path / "BambuStudio.conf"
    p.write_text(json.dumps(studio))
    monkeypatch.setattr(conf, "CONF", p)
    conf.remember_secret(SENTINEL)
    assert conf.sensitive_values() == [SENTINEL] == conf.secret_values()
    assert conf.scrub_text(f"x {SENTINEL}") == "x <redacted>"


def test_access_code_registers_the_code():
    assert not conf.CONF.exists()  # the offline fixture
    assert conf.access_code(SERIAL, {"access_code": {SERIAL: "abcd1234"}}) == "abcd1234"
    assert conf.scrub_text("code abcd1234 here") == "code <redacted> here"


# ---------------------------------------------------------------------------- the config dir and the helpers
def test_config_dir_rule(monkeypatch, tmp_path):
    monkeypatch.setenv("SHAMBU_CONFIG_DIR", str(tmp_path / "own"))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "xdg"))
    assert conf.config_dir() == tmp_path / "own"
    monkeypatch.setenv("SHAMBU_CONFIG_DIR", "")
    assert conf.config_dir() == tmp_path / "xdg" / "shambu"
    monkeypatch.delenv("SHAMBU_CONFIG_DIR")
    monkeypatch.setenv("XDG_CONFIG_HOME", "")
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    assert conf.config_dir() == tmp_path / "home" / ".config" / "shambu"


def test_atomic_write_sets_the_mode_and_creates_a_private_dir(tmp_path):
    p = tmp_path / "new" / "printers.json"
    conf._atomic_write(p, {"version": 1}, ".printers-")
    assert p.stat().st_mode & 0o777 == 0o600 and p.parent.stat().st_mode & 0o777 == 0o700
    q = tmp_path / "calibration.json"
    conf._atomic_write(q, {"version": 1}, ".calibration-", mode=0o640)
    assert q.stat().st_mode & 0o777 == 0o640
    tmp_path.chmod(0o755)
    conf._atomic_write(tmp_path / "x.json", {}, ".x-")
    assert tmp_path.stat().st_mode & 0o777 == 0o755  # a dir that exists keeps its mode


def test_atomic_write_mkstemp_failure_is_a_data_file_error(tmp_path, monkeypatch):
    def boom(**kw):
        raise OSError(13, "Permission denied")

    monkeypatch.setattr(conf.tempfile, "mkstemp", boom)
    with pytest.raises(conf.DataFileError, match="cannot write there"):
        conf._atomic_write(tmp_path / "printers.json", {"version": 1}, ".printers-")
    assert list(tmp_path.iterdir()) == []


def test_locked_cannot_create_the_lock_file(tmp_path):
    parent = tmp_path / "a-file"
    parent.write_text("")
    with pytest.raises(conf.DataFileError, match="cannot create its lock file"):
        conf._locked(parent / "printers.json", lambda: 1)


def test_locked_flock_failure_is_a_data_file_error(tmp_path, monkeypatch):
    def boom(fd, op):
        raise OSError(35, "Resource temporarily unavailable")

    monkeypatch.setattr(conf.fcntl, "flock", boom)
    ran = []
    with pytest.raises(conf.DataFileError, match="cannot lock it"):
        conf._locked(tmp_path / "printers.json", lambda: ran.append(1))
    assert ran == []


def test_the_missing_code_text_names_both_ways_to_fix_it():
    with pytest.raises(ConfError) as e:
        conf.access_code(SERIAL, {"access_code": {}})
    assert str(e.value) == (
        f"no access code for serial {conf.mask(SERIAL)}: run shambu printer login, or open the printer once in Bambu Studio's Device tab"
    )
