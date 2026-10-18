"""Where the access key lives: the macOS Keychain, not license.json.

macOS: a generic password (service "hrshturman", account = installation id) through the `security` tool.
The key is passed on stdin (`security -i`), never in the command line other processes can see.
Windows: DPAPI is not wired yet (S6); until then, and on Linux, the key is kept in a file next to
license.json readable only by its owner, and hrs warns about it. HRS_KEYSTORE=file forces the file;
HRS_KEYCHAIN points at another keychain file (tests).
"""
import os
import subprocess
import sys

from hrshturman_core import DomainError

from . import paths

SERVICE = "hrshturman"
FILE_WARNING = "Ключ доступа хранится в файле с доступом только для владельца: системного хранилища ключей нет."


def _key_file():
    return paths.data_dir() / "access_key"


class FileStore:
    name = "file"
    warning = FILE_WARNING

    def save(self, account: str, secret: str) -> None:
        path = _key_file()
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(".tmp")
        tmp.write_text(f"{account}\n{secret}\n", encoding="utf-8")
        if os.name != "nt":
            tmp.chmod(0o600)
        tmp.replace(path)

    def load(self, account: str) -> str | None:
        try:
            saved, secret = _key_file().read_text(encoding="utf-8").splitlines()[:2]
        except (FileNotFoundError, ValueError):
            return None
        return secret if saved == account else None

    def delete(self, account: str) -> None:
        if self.load(account) is not None:
            _key_file().unlink()


class KeychainStore:
    name = "keychain"
    warning = None

    def _keychain(self) -> list[str]:
        return [os.environ["HRS_KEYCHAIN"]] if os.environ.get("HRS_KEYCHAIN") else []

    def save(self, account: str, secret: str) -> None:
        if any(ch.isspace() or ch in "\"'\\" for ch in secret + account):
            raise DomainError("invalid_key", "В ключе есть недопустимые символы")
        command = " ".join(["add-generic-password", "-U", "-s", SERVICE, "-a", account, "-w", secret,
                            *self._keychain()])
        done = subprocess.run(["security", "-i"], input=command + "\n", capture_output=True, text=True, check=False)
        if done.returncode != 0 or "error" in (done.stdout + done.stderr).lower():
            raise DomainError("keychain_error", "Не удалось сохранить ключ в связку ключей macOS")

    def load(self, account: str) -> str | None:
        done = subprocess.run(["security", "find-generic-password", "-s", SERVICE, "-a", account, "-w",
                               *self._keychain()], capture_output=True, text=True, check=False)
        return (done.stdout.strip() or None) if done.returncode == 0 else None

    def delete(self, account: str) -> None:
        subprocess.run(["security", "delete-generic-password", "-s", SERVICE, "-a", account, *self._keychain()],
                       capture_output=True, text=True, check=False)


class DpapiStore(FileStore):
    """Windows DPAPI comes in S6. Same interface; for now the owner-only file, with a warning."""
    name = "dpapi-fallback-file"


def store():
    forced = os.environ.get("HRS_KEYSTORE")
    if forced == "file":
        return FileStore()
    if sys.platform == "darwin":
        return KeychainStore()
    if sys.platform == "win32":
        return DpapiStore()
    return FileStore()
