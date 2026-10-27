"""vault.py: the shape checks, SecurityVault (the Keychain through `security -i`, with a fake runner and one opt-in
real round trip), MemoryVault (the test fake and second Vault implementation) and the runtime secret set in conf.py
that every vault read feeds."""

from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path

import pytest

from shambu import conf, vault
from shambu.vault import MemoryVault, SecurityVault, UnavailableVault, VaultError, safe_code, safe_serial

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


def test_default_vault_is_the_keychain():
    v = REAL_DEFAULT_VAULT()
    assert isinstance(v, SecurityVault)
    assert (v.binary, v.service, v.timeout) == (Path("/usr/bin/security"), "shambu", 5.0)


# ---------------------------------------------------------------------------- SecurityVault over a fake runner
class FakeRunner:
    """Records `(argv, kwargs)` per call and returns the configured CompletedProcess, or raises `error`."""

    def __init__(self, returncode: int = 0, stdout: str = "", stderr: str = "", error: BaseException | None = None):
        self.returncode, self.stdout, self.stderr, self.error = returncode, stdout, stderr, error
        self.calls: list[tuple[list[str], dict]] = []

    def __call__(self, argv, **kwargs):
        self.calls.append((argv, kwargs))
        if self.error is not None:
            raise self.error
        return subprocess.CompletedProcess(argv, self.returncode, self.stdout, self.stderr)


def _vault(**kw) -> tuple[SecurityVault, FakeRunner]:
    runner = FakeRunner(**kw)
    return SecurityVault(runner=runner), runner


def _assert_framing(runner: FakeRunner) -> None:
    assert runner.calls
    for argv, kw in runner.calls:
        assert argv == ["/usr/bin/security", "-i"]
        assert "-v" not in argv and "-q" not in argv
        assert kw["input"].endswith("\n") and kw["input"].count("\n") == 1
        assert kw["timeout"] == 5.0 and kw["capture_output"] is True and kw["text"] is True


def test_put_sends_the_code_on_stdin_only():
    v, runner = _vault()
    v.put(SERIAL, CODE)
    _assert_framing(runner)
    argv, kw = runner.calls[0]
    assert CODE not in " ".join(argv)
    assert kw["input"] == f"add-generic-password -U -a {SERIAL} -s shambu -T /usr/bin/security -w {CODE}\n"
    assert CODE in conf.secret_values()
    assert CODE not in repr(v)


def test_get_missing_item_is_none():
    v, runner = _vault(returncode=44, stderr="security: SecKeychainSearchCopyNext: The specified item could not be found.")
    assert v.get(SERIAL) is None
    _assert_framing(runner)
    assert runner.calls[0][1]["input"] == f"find-generic-password -a {SERIAL} -s shambu -w\n"


def test_get_returns_the_code_and_registers_it():
    v, runner = _vault(stdout=CODE + "\n")
    assert CODE not in conf.secret_values()
    assert v.get(SERIAL) == CODE
    assert CODE in conf.secret_values()
    _assert_framing(runner)


def test_get_strips_only_one_newline():
    v, _ = _vault(stdout="abcd \n\n")
    assert v.get(SERIAL) == "abcd \n"


def test_get_empty_item_is_an_error():
    v, _ = _vault(stdout="\n")
    with pytest.raises(VaultError, match="is empty") as e:
        v.get(SERIAL)
    assert SERIAL not in str(e.value) and conf.mask(SERIAL) in str(e.value)


def test_get_failure_redacts_a_known_code_in_stderr():
    conf.remember_secret(CODE)  # a code this process already read
    v, _ = _vault(returncode=1, stderr="security: weird " + CODE + " " + "x" * 300)
    with pytest.raises(VaultError, match=r"could not read the Keychain item \(exit 1: security: weird <redacted> x") as e:
        v.get(SERIAL)
    assert CODE not in str(e.value) and len(str(e.value)) < 300


def test_put_failure_redacts_the_code_before_truncation():
    v, _ = _vault(returncode=1, stderr="x" * 190 + CODE + " more")
    with pytest.raises(VaultError) as e:
        v.put(SERIAL, CODE)
    text = str(e.value)
    assert "<redacted>" in text and CODE not in text and CODE[:6] not in text
    assert "could not write the Keychain item (exit 1:" in text


def test_put_duplicate_is_an_error():
    v, _ = _vault(returncode=45, stderr="security: The specified item already exists in the keychain.")
    with pytest.raises(VaultError, match="already exists") as e:
        v.put(SERIAL, CODE)
    assert CODE not in str(e.value) and SERIAL not in str(e.value)
    assert CODE not in conf.secret_values()  # a failed write registers nothing


@pytest.mark.parametrize(
    ("serial", "code"), [(SERIAL, "ab cd1234"), (SERIAL, "-abc1234"), (SERIAL, 'ab"cd1234'), ("bad serial", CODE), ("short", CODE)]
)
def test_put_refuses_unsafe_values_before_any_subprocess(serial, code):
    v, runner = _vault()
    with pytest.raises(VaultError) as e:
        v.put(serial, code)
    assert runner.calls == []
    assert code not in str(e.value) and serial not in str(e.value)


@pytest.mark.parametrize("op", ["get", "delete"])
def test_get_and_delete_refuse_an_unsafe_serial(op):
    v, runner = _vault()
    with pytest.raises(VaultError, match="serial"):
        getattr(v, op)("bad serial; rm")
    assert runner.calls == []


def test_unsafe_code_text_is_fixed():
    v, _ = _vault()
    with pytest.raises(VaultError) as e:
        v.put(SERIAL, "ab cd1234")
    assert str(e.value) == vault.UNSAFE_CODE


@pytest.mark.parametrize(("returncode", "result"), [(44, False), (0, True)])
def test_delete(returncode, result):
    v, runner = _vault(returncode=returncode)
    assert v.delete(SERIAL) is result
    _assert_framing(runner)
    assert runner.calls[0][1]["input"] == f"delete-generic-password -a {SERIAL} -s shambu\n"


def test_delete_failure_is_an_error():
    v, _ = _vault(returncode=51, stderr="security: denied")
    with pytest.raises(VaultError, match=r"could not delete the Keychain item \(exit 51: security: denied\)"):
        v.delete(SERIAL)


def test_no_security_binary_is_unavailable():
    v, _ = _vault(error=FileNotFoundError(2, "No such file or directory"))
    for call in (lambda: v.get(SERIAL), lambda: v.put(SERIAL, CODE), lambda: v.delete(SERIAL)):
        with pytest.raises(UnavailableVault) as e:
            call()
        assert str(e.value) == vault.UNAVAILABLE


def test_timeout_names_a_keychain_dialog():
    v, _ = _vault(error=subprocess.TimeoutExpired(["/usr/bin/security", "-i"], 5.0))
    with pytest.raises(VaultError, match=r"no answer within 5 s \(a Keychain dialog may be waiting\)") as e:
        v.put(SERIAL, CODE)
    assert CODE not in str(e.value)


def test_other_os_error_is_a_vault_error():
    v, _ = _vault(error=PermissionError(13, "Permission denied"))
    with pytest.raises(VaultError, match=r"cannot run security \(Permission denied\)"):
        v.get(SERIAL)


@pytest.mark.integration
@pytest.mark.skipif(
    os.environ.get("SHAMBU_INTEGRATION") != "1" or not Path("/usr/bin/security").exists(), reason="needs SHAMBU_INTEGRATION=1 and macOS"
)
def test_keychain_round_trip():
    v = SecurityVault(service="shambu-test")
    first, second = "TEST-code-0001", "a1._:@+=/-Z9"  # the update uses every allowed character class
    try:
        v.put(SERIAL, first)
        assert v.get(SERIAL) == first
        v.put(SERIAL, second)  # -U updates in place
        assert v.get(SERIAL) == second
        assert v.delete(SERIAL) is True
        assert v.get(SERIAL) is None
        assert v.delete(SERIAL) is False
    finally:
        v.delete(SERIAL)  # nothing left in the Keychain, whatever failed above


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
