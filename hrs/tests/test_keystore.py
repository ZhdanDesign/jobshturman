"""The access key is kept in the macOS Keychain, not in license.json; old files are migrated."""
import json
import subprocess
import sys

import pytest
from hrs_cli import keystore, license
from hrshturman_core import DomainError
from test_license import issue, keys  # noqa: F401 (fixture)


def files_with(root, secret):
    return [p for p in root.rglob("*") if p.is_file() and secret.encode() in p.read_bytes()]


def test_license_json_holds_no_key(keys, tmp_path):  # noqa: F811
    key = issue(keys)["key"]
    license.login(key, http=keys)
    assert "key" not in json.loads(license.state_file().read_text(encoding="utf-8"))
    assert files_with(tmp_path / "home", key) == [tmp_path / "home" / "access_key"]
    assert (keystore._key_file().stat().st_mode & 0o777) == 0o600
    view = license.view()
    assert view["key_storage"] == "file" and "storage_warning" in view and view["methodology"] is True


def test_an_old_license_json_with_the_key_is_migrated(keys):  # noqa: F811
    key = issue(keys)["key"]
    license.login(key, http=keys)
    raw = json.loads(license.state_file().read_text(encoding="utf-8"))
    keystore._key_file().unlink()
    license.state_file().write_text(json.dumps(raw | {"key": key}), encoding="utf-8")  # as hrs S4 wrote it
    assert license.load()["key"] == key
    assert "key" not in json.loads(license.state_file().read_text(encoding="utf-8"))
    assert keystore.store().load(raw["install_id"]) == key
    assert license.BundleLibrary().read("skills/stage-07-master-cv/references/procedure.md")


def test_a_lost_key_asks_for_login_again(keys):  # noqa: F811
    license.login(issue(keys)["key"], http=keys)
    keystore._key_file().unlink()
    assert (license.view()["state"], license.view()["methodology"]) == ("none", False)


def test_windows_uses_the_dpapi_stub_with_a_file_and_a_warning(monkeypatch):
    monkeypatch.delenv("HRS_KEYSTORE")
    monkeypatch.setattr(sys, "platform", "win32")
    store = keystore.store()
    assert store.name == "dpapi-fallback-file" and store.warning
    store.save("install-1", "HRS-AAAA")
    assert store.load("install-1") == "HRS-AAAA" and store.load("other") is None


@pytest.mark.skipif(sys.platform != "darwin", reason="macOS Keychain")
def test_macos_keychain(keys, tmp_path, monkeypatch):  # noqa: F811
    chain = tmp_path / "test.keychain-db"
    subprocess.run(["security", "create-keychain", "-p", "test", str(chain)], check=True)
    try:
        monkeypatch.delenv("HRS_KEYSTORE")
        monkeypatch.setenv("HRS_KEYCHAIN", str(chain))
        key = issue(keys)["key"]
        out = license.login(key, http=keys)
        assert out["key_storage"] == "keychain" and "storage_warning" not in out and out["methodology"]
        assert files_with(tmp_path / "home", key) == []
        install = json.loads(license.state_file().read_text(encoding="utf-8"))["install_id"]
        found = subprocess.run(["security", "find-generic-password", "-s", "hrshturman", "-a", install, "-w",
                                str(chain)], capture_output=True, text=True, check=False)
        assert found.stdout.strip() == key
        assert license.BundleLibrary().read("skills/stage-07-master-cv/references/procedure.md")
    finally:
        subprocess.run(["security", "delete-keychain", str(chain)], check=False)


def fake_security(tmp_path, monkeypatch, log):
    """A `security` that keeps passwords in a file and writes every call to the log; any platform."""
    exe = tmp_path / "bin" / "security"
    exe.parent.mkdir(exist_ok=True)
    exe.write_text(f'''#!/bin/sh
PATH=/usr/bin:/bin:$PATH  # the test leaves only this folder on PATH
echo "$@" >> "{log}"
db="{tmp_path / 'chain'}"
case "$1" in
  -i) read line; echo "$line" >> "{log}"; set -- $line
      case "$1" in add-generic-password) echo "$6 $8" >> "$db"; exit 0;; esac; exit 1;;
  find-generic-password) grep "^$5 " "$db" 2>/dev/null | tail -1 | cut -d" " -f2 | grep . && exit 0 || exit 44;;
  delete-generic-password) grep -v "^$5 " "$db" > "$db.new" 2>/dev/null; mv "$db.new" "$db"; exit 0;;
esac
exit 1
''', encoding="utf-8")
    exe.chmod(0o755)
    monkeypatch.setenv("PATH", str(exe.parent))
    monkeypatch.delenv("HRS_KEYSTORE", raising=False)
    monkeypatch.setattr(sys, "platform", "darwin")
    return keystore.store()


@pytest.mark.skipif(sys.platform == "win32", reason="a shell script stands in for `security`")
def test_the_keychain_store_talks_to_security_and_never_puts_the_key_on_the_command_line(tmp_path, monkeypatch):
    log = tmp_path / "security.log"
    store = fake_security(tmp_path, monkeypatch, log)
    assert store.name == "keychain" and store.warning is None
    store.save("install-1", "HRS-AAAA-BBBB")
    assert store.load("install-1") == "HRS-AAAA-BBBB" and store.load("install-2") is None
    calls = log.read_text(encoding="utf-8").splitlines()
    assert calls[0] == "-i" and "HRS-AAAA-BBBB" not in calls[0]  # the key went through stdin
    assert any("find-generic-password -s hrshturman -a install-1 -w" in c for c in calls)
    store.delete("install-1")
    assert store.load("install-1") is None
    with pytest.raises(DomainError) as exc:  # a key with quotes or spaces would break the security command line
        store.save("install-1", 'HRS-AA"AA')
    assert exc.value.code == "invalid_key"


@pytest.mark.skipif(sys.platform == "win32", reason="a shell script stands in for `security`")
def test_a_failing_security_is_a_keychain_error_not_a_crash(tmp_path, monkeypatch):
    log = tmp_path / "security.log"
    store = fake_security(tmp_path, monkeypatch, log)
    (tmp_path / "bin" / "security").write_text("#!/bin/sh\necho 'security: SecKeychainItemCreateFromContent error' >&2\nexit 1\n")
    with pytest.raises(DomainError) as exc:
        store.save("install-1", "HRS-AAAA")
    assert exc.value.code == "keychain_error"
    assert store.load("install-1") is None
