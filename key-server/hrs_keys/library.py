"""The methodology draft on the server: the source of truth the admin page edits.

Every save is a revision: a full file tree {path: sha256} in the revisions table; file contents are
blobs in <data>/library/blobs, named by their hash, so revisions share unchanged files. The newest
revision is the draft. History, comparison and rollback need nothing more: a rollback is a new
revision with an old tree. Publishing builds the bundle from one revision (service.KeyServer).
"""
import hashlib
import io
import json
import posixpath
import zipfile
from pathlib import Path

from hrshturman_core import methodology

MAX_FILE = 2 * 1024 * 1024
MAX_FILES = 2000
# The main prompt is the methodology author's text and lives only in the private methodology: a library
# without one takes it from the published bundle, or starts from this placeholder, which the check refuses
# to publish until the author writes the rules in the admin.
GENESIS_PLACEHOLDER = """# Главные правила

Здесь будут главные правила работы агента. Агент читает этот файл первым, и он главнее текста навыка.
Напишите правила или загрузите библиотеку с файлом agent/genesis.md: пока здесь эта заглушка,
черновик не публикуется.
"""


class LibraryError(Exception):
    def __init__(self, code: str, message: str, status: int = 400):
        super().__init__(message)
        self.code, self.status = code, status


def sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def clean_path(path: str) -> str:
    """A relative path inside the library; refuses anything that climbs out or looks hidden."""
    path = (path or "").replace("\\", "/").strip().strip("/")
    norm = posixpath.normpath(path) if path else ""
    parts = norm.split("/")
    if not norm or norm.startswith("..") or any(p in ("", ".", "..") or p.startswith(".") for p in parts) \
            or len(norm) > 300:
        raise LibraryError("bad_path", f"Недопустимый путь: {path!r}")
    return norm


class Library:
    def __init__(self, db, root: Path):
        self.db = db
        self.blobs = Path(root) / "blobs"
        self.blobs.mkdir(parents=True, exist_ok=True)

    # --- blobs ---

    def _put(self, data: bytes) -> str:
        if len(data) > MAX_FILE:
            raise LibraryError("too_large", f"Файл больше {MAX_FILE // 1024 // 1024} МБ")
        digest = sha(data)
        target = self.blobs / digest
        if not target.is_file():
            tmp = target.with_suffix(".tmp")
            tmp.write_bytes(data)
            tmp.replace(target)
        return digest

    def blob(self, digest: str) -> bytes:
        return (self.blobs / digest).read_bytes()

    # --- revisions ---

    def _row(self, c, rev: int | None = None):
        if rev is None:
            return c.execute("SELECT * FROM revisions ORDER BY id DESC LIMIT 1").fetchone()
        row = c.execute("SELECT * FROM revisions WHERE id = ?", (rev,)).fetchone()
        if row is None:
            raise LibraryError("not_found", f"Нет версии черновика {rev}", 404)
        return row

    def tree(self, c, rev: int | None = None) -> dict[str, str]:
        row = self._row(c, rev)
        return json.loads(row["tree"]) if row else {}

    def head(self, c) -> dict | None:
        row = self._row(c)
        return self._public(row) if row else None

    @staticmethod
    def _public(row) -> dict:
        return {"id": row["id"], "at": row["at"], "by": row["by_role"], "message": row["message"],
                "files": len(json.loads(row["tree"]))}

    def files(self, c, rev: int | None = None) -> dict[str, bytes]:
        return {path: self.blob(digest) for path, digest in self.tree(c, rev).items()}

    def commit(self, c, tree: dict[str, str], by: str, message: str, at: str) -> dict:
        if len(tree) > MAX_FILES:
            raise LibraryError("too_many_files", f"В методике больше {MAX_FILES} файлов")
        if tree == self.tree(c):
            return self.head(c) | {"unchanged": True}
        c.execute("INSERT INTO revisions (at, by_role, message, tree) VALUES (?, ?, ?, ?)",
                  (at, by, message[:200], json.dumps(dict(sorted(tree.items())), ensure_ascii=False)))
        return self.head(c)

    def change(self, c, changes: dict[str, bytes | None], by: str, message: str, at: str,
               expected: dict[str, str | None] | None = None, replace: bool = False) -> dict:
        """changes: {path: new bytes or None to delete}. expected: {path: sha the editor opened, None for
        a new file}; a file changed by someone else since then is a conflict, nothing is written."""
        tree = {} if replace else self.tree(c)
        current = self.tree(c)
        for path, want in (expected or {}).items():
            if current.get(clean_path(path)) != want:
                raise LibraryError("conflict", f"{path} изменили, пока он был открыт: обновите страницу", 409)
        for path, data in changes.items():
            path = clean_path(path)
            if data is None:
                if path not in tree:
                    raise LibraryError("not_found", f"Нет файла {path}", 404)
                del tree[path]
            else:
                tree[path] = self._put(data)
        return self.commit(c, tree, by, message, at)

    def revisions(self, c, limit: int = 100) -> list[dict]:
        rows = c.execute("SELECT * FROM revisions ORDER BY id DESC LIMIT ?", (limit,)).fetchall()
        return [self._public(r) for r in rows]

    def diff(self, c, rev: int, against: int | None = None) -> list[dict]:
        """What revision `rev` changed against `against` (by default the one before it)."""
        new = self.tree(c, rev)
        if against is None:
            prev = c.execute("SELECT id FROM revisions WHERE id < ? ORDER BY id DESC LIMIT 1", (rev,)).fetchone()
            old = self.tree(c, prev["id"]) if prev else {}
        else:
            old = self.tree(c, against)
        out = []
        for path in sorted(set(old) | set(new)):
            if old.get(path) == new.get(path):
                continue
            kind = "added" if path not in old else "removed" if path not in new else "changed"
            item = {"path": path, "change": kind}
            if path.endswith((".md", ".json", ".csv", ".txt")):
                item["before"] = self.blob(old[path]).decode("utf-8", "replace") if path in old else None
                item["after"] = self.blob(new[path]).decode("utf-8", "replace") if path in new else None
            out.append(item)
        return out


def unzip(data: bytes) -> dict[str, bytes]:
    """A library or a published bundle as {path: bytes}: one top folder is stripped, bundle.json and
    system junk are dropped."""
    try:
        z = zipfile.ZipFile(io.BytesIO(data))
    except zipfile.BadZipFile as exc:
        raise LibraryError("bad_zip", "Это не zip-архив") from exc
    names = [i.filename for i in z.infolist() if not i.is_dir()]
    names = [n for n in names if not any(p.startswith(("._", ".")) or p == "__MACOSX" for p in n.split("/"))]
    tops = {n.split("/")[0] for n in names}
    strip = len(tops) == 1 and all("/" in n for n in names) and methodology.MANIFEST not in names
    out = {}
    for n in names:
        rel = n.split("/", 1)[1] if strip else n
        if rel == "bundle.json":
            continue
        out[clean_path(rel)] = z.read(n)
    if len(out) > MAX_FILES:
        raise LibraryError("too_many_files", f"В архиве больше {MAX_FILES} файлов")
    return out


def is_placeholder(text: str | bytes | None) -> bool:
    if isinstance(text, bytes):
        text = text.decode("utf-8", "replace")
    return (text or "").strip() == GENESIS_PLACEHOLDER.strip()


def prepare(files: dict[str, bytes], genesis: bytes | None = None) -> dict[str, bytes]:
    """A library as it enters the draft: the manifest in format 2 (the route written out, so it can be
    edited) and a main prompt: its own, else `genesis` (the published one), else the placeholder."""
    files = dict(files)
    names = list(files)
    raw = json.loads(files[methodology.MANIFEST]) if methodology.MANIFEST in files else {}
    if raw.get("format") != methodology.FORMAT:
        files[methodology.MANIFEST] = dump(methodology.upgrade(raw, names))
    if methodology.GENESIS not in files:
        files[methodology.GENESIS] = genesis or GENESIS_PLACEHOLDER.encode("utf-8")
    return files


def dump(manifest: dict) -> bytes:
    return (json.dumps(manifest, ensure_ascii=False, indent=2) + "\n").encode("utf-8")


STAGE_SKILL = """---
name: {id}
description: Этап методики поиска работы: {title}. Используй, когда кандидат на этом этапе.
---

# {title}

Цель этапа: …

Что делает агент: …

Результат этапа: …
"""
STAGE_PROCEDURE = """# {title}: процедура

1. …
2. …
3. …
"""
