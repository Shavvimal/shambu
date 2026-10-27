"""vault.py: the shape checks, FileVault (the 0600 credentials file in the config dir), MemoryVault (the test fake and
second Vault implementation) and the runtime secret set in conf.py that every vault read feeds."""

from __future__ import annotations

import json
import os
import stat
import threading

import pytest

from shambu import conf, vault
from shambu.vault import BAD_FILE, FileVault, MemoryVault, VaultError, safe_code, safe_serial

SERIAL = "00X0TEST0000999"
SERIAL2 = "00X0TEST0000888"
CODE = "SENTINEL-CODE-8812"
REAL_DEFAULT_VAULT = vault.default_vault  # read at import, before the offline fixture patches it


@pytest.mark.parametrize("code", [CODE, "12345678", "abcd", "a1._:@+=/-z", "a" * 128])
def test_safe_code_accepts(code):
    assert safe_code(code)


@pytest.mark.parametrize("code", ["", "abc", "-abc1234", "ab cd1234", 'ab"cd1234', "ab\\cd1234", "ab'cd1234", "a" * 129, None, 12345678])
def test_safe_code_refuses(code):
    assert not safe_code(code)


@pytest.mark.parametrize(
    ("serial", "ok"),
    [(SERIAL, True), ("A" * 32, True), ("A" * 7, False), ("A" * 33, False), ("00X0-TEST999", False), ("", False), (None, False)],
)
def test_safe_serial(serial, ok):
    assert safe_serial(serial) is ok


def test_memory_vault_get_put_delete():
    v = MemoryVault()
    assert v.get(SERIAL) is None
    v.put(SERIAL, CODE)
    assert v.get(SERIAL) == CODE
    v.put(SERIAL, "12345678")  # put replaces
    assert v.get(SERIAL) == "12345678"
    assert v.delete(SERIAL) is True
    assert v.delete(SERIAL) is False
    assert v.get(SERIAL) is None
    assert v.calls == [
        ("get", SERIAL),
        ("put", SERIAL),
        ("get", SERIAL),
        ("put", SERIAL),
        ("get", SERIAL),
        ("delete", SERIAL),
        ("delete", SERIAL),
        ("get", SERIAL),
    ]
    assert CODE not in repr(v) and CODE not in json.dumps(v.calls)


def test_memory_vault_refuses_unsafe_values_before_recording():
    v = MemoryVault()
    for call in (lambda: v.put(SERIAL, "ab cd1234"), lambda: v.put("bad serial", CODE), lambda: v.get("short"), lambda: v.delete("")):
        with pytest.raises(VaultError) as e:
            call()
        assert "ab cd1234" not in str(e.value) and CODE not in str(e.value)
    assert v.calls == [] and v.items == {}


def test_memory_vault_error_propagates():
    v = MemoryVault(items={SERIAL: CODE}, error=VaultError("file locked"))
    with pytest.raises(VaultError, match="file locked"):
        v.get(SERIAL)
    assert CODE not in conf.secret_values()  # a failed get registers nothing
    for call in (lambda: v.put(SERIAL, CODE), lambda: v.delete(SERIAL)):
        with pytest.raises(VaultError, match="file locked"):
            call()
    assert v.calls == [("get", SERIAL), ("put", SERIAL), ("delete", SERIAL)]
    assert v.items == {SERIAL: CODE}
    assert CODE in conf.secret_values()  # a put registers its code before anything else, even when it then fails


def test_memory_vault_get_registers_the_code():
    assert CODE not in conf.sensitive_values() and CODE not in conf.secret_values()
    MemoryVault(items={SERIAL: CODE}).get(SERIAL)
    assert CODE in conf.sensitive_values() and CODE in conf.secret_values()
    assert conf.scrub_text(f"code {CODE} here") == "code <redacted> here"
    conf.forget_secrets()
    assert CODE not in conf.sensitive_values() and CODE not in conf.secret_values()


def test_default_vault_in_tests_is_the_shared_fake(offline):
    assert vault.default_vault() is offline.vault is vault.default_vault()
    offline.vault.put(SERIAL, CODE)
    assert vault.default_vault().get(SERIAL) == CODE


def test_default_vault_is_the_credentials_file():
    v = REAL_DEFAULT_VAULT()
    assert isinstance(v, FileVault) and v.path is None and v._path() == conf.CREDENTIALS


# ---------------------------------------------------------------------------- FileVault
def _mode(p) -> int:
    return stat.S_IMODE(p.stat().st_mode)


def test_file_vault_round_trip_creates_a_private_file_in_a_private_dir(tmp_path):
    p = tmp_path / "config" / "shambu" / "credentials.json"
    v = FileVault(p)
    assert v.get(SERIAL) is None and not p.exists()
    v.put(SERIAL, CODE)
    assert v.get(SERIAL) == CODE
    assert json.loads(p.read_text()) == {"version": 1, "codes": {SERIAL: CODE}}
    assert _mode(p) == 0o600 and _mode(p.parent) == 0o700
    assert sorted(x.name for x in p.parent.iterdir()) == ["credentials.json", "credentials.json.lock"]  # no temp file
    v.put(SERIAL, "12345678")  # put replaces
    assert v.get(SERIAL) == "12345678"
    assert CODE not in repr(v)


def test_file_vault_holds_two_serials_and_delete_says_whether_there_was_one(tmp_path):
    v = FileVault(tmp_path / "credentials.json")
    assert v.delete(SERIAL) is False
    v.put(SERIAL, CODE)
    v.put(SERIAL2, "87654321")
    assert (v.get(SERIAL), v.get(SERIAL2)) == (CODE, "87654321")
    assert v.delete(SERIAL) is True
    assert v.delete(SERIAL) is False
    assert (v.get(SERIAL), v.get(SERIAL2)) == (None, "87654321")


def test_file_vault_refuses_a_file_others_can_read(tmp_path):
    p = tmp_path / "credentials.json"
    p.write_text(json.dumps({"version": 1, "codes": {SERIAL: CODE}}))
    p.chmod(0o644)
    with pytest.raises(VaultError) as e:
        FileVault(p).get(SERIAL)
    assert str(e.value) == f"{p} must be private: chmod 600 {p}"
    assert CODE not in str(e.value) and CODE not in conf.secret_values()


@pytest.mark.parametrize(
    "text",
    [
        "{not json " + CODE,
        json.dumps([CODE]),
        json.dumps({"version": 2, "codes": {SERIAL: CODE}}),
        json.dumps({"version": True, "codes": {SERIAL: CODE}}),
        json.dumps({"version": 1, "codes": [CODE]}),
        json.dumps({"version": 1, "codes": {SERIAL: CODE}, "extra": 1}),
        json.dumps({"version": 1, "codes": {SERIAL: 12345678}}),
        json.dumps({"version": 1, "codes": {"bad serial": CODE}}),
    ],
)
def test_file_vault_refuses_a_damaged_file_with_a_fixed_text(tmp_path, text):
    p = tmp_path / "credentials.json"
    p.write_text(text)
    p.chmod(0o600)
    with pytest.raises(VaultError) as e:
        FileVault(p).get(SERIAL)
    assert str(e.value) == BAD_FILE and CODE not in str(e.value) and e.value.__cause__ is None


@pytest.mark.parametrize(
    ("serial", "code"), [(SERIAL, "ab cd1234"), (SERIAL, "-abc1234"), (SERIAL, "abc"), ("bad serial", CODE), ("short", CODE)]
)
def test_file_vault_put_refuses_unsafe_values_before_any_write(tmp_path, serial, code):
    p = tmp_path / "config" / "credentials.json"
    with pytest.raises(VaultError) as e:
        FileVault(p).put(serial, code)
    assert str(e.value) == vault.UNSAFE and code not in str(e.value) and serial not in str(e.value)
    assert not p.parent.exists()


@pytest.mark.parametrize("op", ["get", "delete"])
def test_file_vault_get_and_delete_refuse_an_unsafe_serial(tmp_path, op):
    with pytest.raises(VaultError, match="serial"):
        getattr(FileVault(tmp_path / "credentials.json"), op)("bad serial; rm")
    assert list(tmp_path.iterdir()) == []


def test_file_vault_get_registers_the_code(tmp_path):
    p = tmp_path / "credentials.json"
    FileVault(p).put(SERIAL, CODE)
    conf.forget_secrets()
    assert CODE not in conf.secret_values()
    assert FileVault(p).get(SERIAL) == CODE
    assert CODE in conf.secret_values() and conf.scrub_text(f"code {CODE}") == "code <redacted>"


def test_file_vault_default_path_follows_the_patched_constant():
    v = FileVault()
    v.put(SERIAL, CODE)
    assert conf.CREDENTIALS.is_file() and _mode(conf.CREDENTIALS) == 0o600 and _mode(conf.CREDENTIALS.parent) == 0o700
    assert v.get(SERIAL) == CODE


def test_file_vault_write_failure_is_a_vault_error_without_the_code(tmp_path, monkeypatch):
    def boom(*a):
        raise OSError(28, "No space left on device")

    monkeypatch.setattr(conf.os, "replace", boom)
    with pytest.raises(VaultError, match="No space left") as e:
        FileVault(tmp_path / "credentials.json").put(SERIAL, CODE)
    assert CODE not in str(e.value)
    assert [x.name for x in tmp_path.iterdir()] == ["credentials.json.lock"]


def test_two_concurrent_puts_both_land(tmp_path, monkeypatch):
    """Thread A holds the lock inside its load; thread B's put waits for it and then sees A's code."""
    p = tmp_path / "credentials.json"
    inside, release = threading.Event(), threading.Event()
    real = FileVault._load

    def slow(self, path):
        if threading.current_thread().name == "a":
            inside.set()
            release.wait(5)
        return real(self, path)

    monkeypatch.setattr(FileVault, "_load", slow)
    a = threading.Thread(target=FileVault(p).put, args=(SERIAL, CODE), name="a")
    a.start()
    assert inside.wait(5)
    b = threading.Thread(target=FileVault(p).put, args=(SERIAL2, "87654321"), name="b")
    b.start()
    b.join(0.3)
    assert b.is_alive()  # B waits on the lock
    release.set()
    a.join(5)
    b.join(5)
    assert json.loads(p.read_text())["codes"] == {SERIAL: CODE, SERIAL2: "87654321"}


# ---------------------------------------------------------------------------- the runtime secret set in conf.py
def test_remember_secret_ignores_empty_and_keeps_every_length():
    conf.remember_secret("")
    conf.remember_secret("abc")  # a runtime secret of any length is scrubbed
    conf.remember_secret("abcd")
    conf.remember_secret(CODE)
    assert conf.sensitive_values() == [CODE, "abcd", "abc"] == conf.secret_values()
    assert conf.scrub_text("x abc y") == "x <redacted> y"


def test_sensitive_values_hold_runtime_secrets_with_no_studio_config():
    assert not conf.CONF.exists()  # the offline fixture
    assert conf.sensitive_values() == [] == conf.secret_values()
    conf.remember_secret(CODE)
    assert conf.sensitive_values() == [CODE] == conf.secret_values()


def test_secret_values_hold_codes_only(tmp_path, monkeypatch):
    p = tmp_path / "BambuStudio.conf"
    p.write_text(json.dumps({"app": {"user_last_selected_machine": SERIAL}, "access_code": {SERIAL: "12345678", SERIAL2: "87654321"}}))
    monkeypatch.setattr(conf, "CONF", p)
    conf.remember_secret(CODE)
    secrets = conf.secret_values()
    assert secrets[0] == CODE and sorted(secrets[1:]) == ["12345678", "87654321"]
    assert SERIAL not in secrets and conf.mask(SERIAL) not in secrets
    sensitive = conf.sensitive_values()
    assert {CODE, "12345678", SERIAL, SERIAL2, conf.mask(SERIAL)} <= set(sensitive)
    assert [len(v) for v in sensitive] == sorted((len(v) for v in sensitive), reverse=True)


# ---------------------------------------------------------------------------- the shared write helper
def test_atomic_write_writes_the_document(tmp_path):
    p = tmp_path / "shambu" / "printers.json"
    conf._atomic_write(p, {"version": 1, "default": None, "printers": []}, ".printers-")
    assert json.loads(p.read_text()) == {"version": 1, "default": None, "printers": []}
    assert [x.name for x in p.parent.iterdir()] == ["printers.json"]


def test_atomic_write_leaves_no_temp_file_when_replace_fails(tmp_path, monkeypatch):
    def boom(*a):
        raise OSError(28, "No space left on device")

    monkeypatch.setattr(conf.os, "replace", boom)
    with pytest.raises(conf.DataFileError, match="No space left"):
        conf._atomic_write(tmp_path / "printers.json", {"version": 1}, ".printers-")
    assert [x.name for x in tmp_path.iterdir()] == []


def test_locked_returns_the_result_and_leaves_the_lock_file(tmp_path):
    p = tmp_path / "printers.json"
    assert conf._locked(p, lambda: 42) == 42
    assert [x.name for x in tmp_path.iterdir()] == ["printers.json.lock"]


# ---------------------------------------------------------------------------- symlinks, FIFOs and directories
def _seeded(path):
    path.write_text(json.dumps({"version": 1, "codes": {SERIAL: CODE}}))
    path.chmod(0o600)
    return path


def test_a_symlinked_credentials_file_is_refused_on_get_and_put(tmp_path):
    target = _seeded(tmp_path / "elsewhere.json")
    before = target.read_text()
    link = tmp_path / "config" / "credentials.json"
    link.parent.mkdir()
    link.symlink_to(target)
    v = FileVault(link)
    want = vault.NOT_PRIVATE.format(path=link)
    with pytest.raises(VaultError) as e:
        v.get(SERIAL)
    assert str(e.value) == want and CODE not in str(e.value)
    with pytest.raises(VaultError) as e:
        v.put(SERIAL2, "OTHER-CODE-1234")
    assert str(e.value) == want
    assert link.is_symlink() and link.readlink() == target and target.read_text() == before  # both untouched
    assert sorted(x.name for x in link.parent.iterdir()) == ["credentials.json", "credentials.json.lock"]


def test_a_fifo_at_the_credentials_path_is_refused_without_blocking(tmp_path):
    fifo = tmp_path / "credentials.json"
    os.mkfifo(fifo, 0o600)
    v = FileVault(fifo)
    done = threading.Event()
    box: dict = {}

    def run():
        try:
            v.get(SERIAL)
        except VaultError as e:
            box["error"] = str(e)
        finally:
            done.set()

    threading.Thread(target=run, daemon=True).start()
    assert done.wait(2.0), "a FIFO at the path blocked the read"
    assert box["error"] == vault.NOT_PRIVATE.format(path=fifo)
    with pytest.raises(VaultError, match="not a regular file"):
        v.put(SERIAL, CODE)
    assert stat.S_ISFIFO(os.lstat(fifo).st_mode)


def test_a_directory_at_the_credentials_path_is_refused(tmp_path):
    d = tmp_path / "credentials.json"
    d.mkdir(mode=0o700)
    with pytest.raises(VaultError) as e:
        FileVault(d).get(SERIAL)
    assert str(e.value) == vault.NOT_PRIVATE.format(path=d)
    with pytest.raises(VaultError, match="not a regular file"):
        FileVault(d).put(SERIAL, CODE)
    assert d.is_dir()


def test_a_file_another_user_owns_is_refused(tmp_path, monkeypatch):
    p = _seeded(tmp_path / "credentials.json")
    monkeypatch.setattr(vault.os, "getuid", lambda: os.stat(p).st_uid + 1)
    with pytest.raises(VaultError) as e:
        FileVault(p).get(SERIAL)
    assert str(e.value) == vault.NOT_PRIVATE.format(path=p)


def test_atomic_write_never_replaces_a_symlink(tmp_path):
    target = tmp_path / "target.json"
    target.write_text("{}")
    link = tmp_path / "printers.json"
    link.symlink_to(target)
    with pytest.raises(conf.DataFileError) as e:
        conf._atomic_write(link, {"version": 1}, ".printers-")
    assert str(e.value) == f"{link}: not a regular file; remove it and retry"
    assert link.is_symlink() and target.read_text() == "{}"
    assert sorted(x.name for x in tmp_path.iterdir()) == ["printers.json", "target.json"]


# ---------------------------------------------------------------------------- descriptor, fsync and lock order
def _open_fds() -> int:
    return len(os.listdir("/dev/fd"))


def test_atomic_write_closes_its_descriptor_when_fchmod_fails(tmp_path, monkeypatch):
    def boom(*a):
        raise OSError(1, "Operation not permitted")

    conf._atomic_write(tmp_path / "warm.json", {"version": 1}, ".warm-")  # first-use imports and caches out of the count
    before = _open_fds()
    monkeypatch.setattr(conf.os, "fchmod", boom)
    with pytest.raises(conf.DataFileError, match="Operation not permitted"):
        conf._atomic_write(tmp_path / "printers.json", {"version": 1}, ".printers-")
    assert _open_fds() == before
    assert sorted(x.name for x in tmp_path.iterdir()) == ["warm.json"]


def test_atomic_write_flushes_and_fsyncs_before_the_replace(tmp_path, monkeypatch):
    order: list[str] = []
    real_fsync, real_replace = os.fsync, os.replace
    monkeypatch.setattr(conf.os, "fsync", lambda fd: (order.append("fsync"), real_fsync(fd))[1])
    monkeypatch.setattr(conf.os, "replace", lambda a, b: (order.append("replace"), real_replace(a, b))[1])
    conf._atomic_write(tmp_path / "printers.json", {"version": 1}, ".printers-")
    assert order == ["fsync", "replace"]
    assert json.loads((tmp_path / "printers.json").read_text()) == {"version": 1}


def test_a_put_inside_locked_profiles_and_a_lone_put_both_land(tmp_path):
    """The lock order a login commit uses: the profiles lock, then the credentials lock inside it. A second thread's
    lone put (the credentials lock only) waits its turn and lands too; nothing deadlocks."""
    creds = tmp_path / "credentials.json"
    printers = tmp_path / "printers.json"
    v = FileVault(creds)
    errors: list[VaultError] = []

    def lone():
        try:
            FileVault(creds).put(SERIAL2, "OTHER-CODE-1234")
        except VaultError as e:
            errors.append(e)

    other = threading.Thread(target=lone)

    def commit(ps):
        other.start()
        v.put(SERIAL, CODE)
        return "done"

    assert conf.locked_profiles(commit, printers) == "done"
    other.join(5.0)
    assert not other.is_alive() and errors == []
    assert FileVault(creds).get(SERIAL) == CODE and FileVault(creds).get(SERIAL2) == "OTHER-CODE-1234"


# ---------------------------------------------------------------------------- every code is registered
def test_a_file_vault_read_registers_every_code_in_the_file(tmp_path):
    p = tmp_path / "credentials.json"
    p.write_text(json.dumps({"version": 1, "codes": {SERIAL: CODE, SERIAL2: "OTHER-CODE-1234"}}))
    p.chmod(0o600)
    assert FileVault(p).get(SERIAL) == CODE
    assert {CODE, "OTHER-CODE-1234"} <= set(conf.secret_values())


def test_a_put_registers_its_code_before_any_file_operation(tmp_path, monkeypatch):
    def no_lock(p, fn):
        assert CODE in conf.secret_values(), "the code is registered before the lock file is touched"
        raise conf.DataFileError("stop here")

    monkeypatch.setattr(conf, "_locked", no_lock)
    with pytest.raises(VaultError, match="stop here"):
        FileVault(tmp_path / "credentials.json").put(SERIAL, CODE)
    assert list(tmp_path.iterdir()) == []


def test_a_memory_vault_get_registers_every_stored_code():
    MemoryVault(items={SERIAL: CODE, SERIAL2: "OTHER-CODE-1234"}).get(SERIAL)
    assert {CODE, "OTHER-CODE-1234"} <= set(conf.secret_values())


def test_remember_secret_ignores_a_value_that_is_not_text():
    conf.remember_secret(12345678)  # type: ignore[arg-type]  # a numeric Studio value
    conf.remember_secret(None)  # type: ignore[arg-type]
    assert conf.secret_values() == []
