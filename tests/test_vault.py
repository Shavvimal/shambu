"""vault.py: the shape checks, MemoryVault (the test fake and second Vault implementation), the unavailable vault,
and the runtime secret set in conf.py that every vault read feeds. The Keychain vault's tests come with slice B."""

from __future__ import annotations

import json

import pytest

from shambu import conf, vault
from shambu.vault import MemoryVault, UnavailableVault, VaultError, safe_code, safe_serial

SERIAL = "00X0TEST0000999"
SERIAL2 = "00X0TEST0000888"
CODE = "SENTINEL-CODE-8812"


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
    v = MemoryVault(items={SERIAL: CODE}, error=VaultError("keychain locked"))
    for call in (lambda: v.get(SERIAL), lambda: v.put(SERIAL, CODE), lambda: v.delete(SERIAL)):
        with pytest.raises(VaultError, match="keychain locked"):
            call()
    assert v.calls == [("get", SERIAL), ("put", SERIAL), ("delete", SERIAL)]
    assert v.items == {SERIAL: CODE}
    assert CODE not in conf.secret_values()  # a failed get registers nothing


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


def test_the_unavailable_vault_raises_on_every_method():
    v = vault._Unavailable()
    for call in (lambda: v.get(SERIAL), lambda: v.put(SERIAL, CODE), lambda: v.delete(SERIAL)):
        with pytest.raises(UnavailableVault) as e:
            call()
        assert str(e.value) == vault.UNAVAILABLE and CODE not in str(e.value) and SERIAL not in str(e.value)


# ---------------------------------------------------------------------------- the runtime secret set in conf.py
def test_remember_secret_ignores_empty_and_keeps_the_length_rule():
    conf.remember_secret("")
    conf.remember_secret("abc")  # held, but shorter than 4: not a scrub value (it would scrub ordinary words)
    conf.remember_secret("abcd")
    conf.remember_secret(CODE)
    assert conf.sensitive_values() == [CODE, "abcd"] == conf.secret_values()


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
