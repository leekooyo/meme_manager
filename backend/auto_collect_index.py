"""SQLite-backed fingerprint index for the auto-collect pipeline.

[patcher:sqlite-index]

为什么改：`lookup()` 原本每次都重走整棵 pack 目录树，对每个文件做
`Path.resolve()` + `stat()`（几千文件十几秒），而它面对的只是"一张新图"。

现在把「路径 -> (大小, mtime_ns, sha256, dHash)」落在 pack 同级的 SQLite 文件里，
枚举改用 `os.scandir()`——Windows 上目录项自带 stat，不再需要逐文件
`resolve()`/`stat()`——并且**只对"大小/mtime 变了或新出现"的文件重算 sha256**。
原地改写（目录 mtime 不变）也能被逐文件比对捕捉到，语义与原实现一致。

空间代价约 0.7 KB/张（几千张约 2~3 MB）；内存是"不驻留"的：
没有常驻的目录/指纹字典，只有一个 2 MB 的 SQLite 页缓存，逐目录流式比对，
单次 lookup 的临时峰值约几百 KB，函数返回即释放。
"""

from __future__ import annotations

import hashlib
import os
import sqlite3
from collections import OrderedDict
from dataclasses import dataclass
from pathlib import Path
from threading import RLock

from PIL import Image

IMAGE_EXTENSIONS = {".jpg", ".jpeg", ".png", ".gif", ".webp"}

_SCHEMA = "sqlite-index/3"
_DHASH_UNSET = -1
_DDL = (
    "CREATE TABLE IF NOT EXISTS files("
    "  p TEXT PRIMARY KEY, d TEXT, s INTEGER, m INTEGER, h TEXT, v INTEGER);"
    "CREATE INDEX IF NOT EXISTS idx_files_h ON files(h);"
    "CREATE INDEX IF NOT EXISTS idx_files_d ON files(d);"
)


@dataclass(frozen=True, slots=True)
class ImageMatch:
    """Describe a byte-identical image and an optional visual review hint.

    Args:
        exact_path: Existing image with identical SHA-256, if found.
        similar_path: Static image with a nearby dHash, for human review only.
        distance: Number of differing dHash bits for the visual hint.
        root: Resolved image root that owned the matched row when the hit was
            produced. Callers must use it (instead of their own assumption)
            to explain the hit's location.
        relative_path: POSIX-style path of the hit relative to ``root``.
    """

    exact_path: Path | None = None
    similar_path: Path | None = None
    distance: int | None = None
    root: Path | None = None
    relative_path: str = ""

    def source_label(self) -> str:
        """Return a stable ``<pack_id>/<relative path>`` label for the hit.

        The label describes where the matched image actually lives, which is
        not necessarily the pack currently being collected into. Returns an
        empty string when no hit exists.
        """
        if not self.relative_path:
            return ""
        pack_id = self.root.parent.name if self.root is not None else ""
        return f"{pack_id}/{self.relative_path}" if pack_id else self.relative_path


def _relative_label(path: Path, root: Path) -> str:
    """Return ``path`` relative to its owning root as a POSIX string.

    The computation happens where the authoritative root is known, so callers
    never have to guess which pack a hit belongs to. An empty string means the
    row could not be explained by this root and must not be reported.
    """
    try:
        return path.relative_to(root).as_posix()
    except ValueError:
        return ""


def _index_dir(pack_root: Path) -> Path:
    """Return the directory that holds index databases for one pack tree."""
    packs_dir = pack_root.parent.parent
    if packs_dir.name == "packs":
        # <plugin_data>/packs/<pack_id>/memes -> <plugin_data>/auto_collect_index
        return packs_dir.parent / "auto_collect_index"
    return pack_root / ".auto_collect_index"


def _db_file(pack_root: Path) -> Path:
    """Return the SQLite path for one pack tree."""
    name = pack_root.parent.name or "pack"
    safe = "".join(ch if (ch.isalnum() or ch in "._-") else "_" for ch in name)
    return _index_dir(pack_root) / ((safe or "pack") + ".sqlite3")


def _to_signed(value: int | None) -> int | None:
    """Store a 64-bit dHash in a signed column without losing bits."""
    if value is None or value == _DHASH_UNSET:
        return _DHASH_UNSET
    return value - (1 << 64) if value >= (1 << 63) else value


def _to_unsigned(value: int | None) -> int | None:
    """Restore a signed column value to a 64-bit unsigned dHash."""
    if value is None or value == _DHASH_UNSET:
        return None
    return value & ((1 << 64) - 1)


class AutoCollectImageIndex:
    """Persist pack fingerprints on disk and rehash only new or changed files."""

    def __init__(self, max_packs: int = 8):
        """Initialize an index safe for concurrent background lookups.

        Args:
            max_packs: Maximum number of open pack databases retained.
        """
        self.max_packs = max(1, max_packs)
        self._packs: OrderedDict[Path, sqlite3.Connection] = OrderedDict()
        self._lock = RLock()

    @staticmethod
    def _difference_hash(path: Path) -> int | None:
        """Decode a bounded static image for an advisory visual fingerprint.

        Args:
            path: Image to inspect.

        Returns:
            A 64-bit dHash, or None for animated, oversized, or invalid images.
        """
        try:
            with Image.open(path) as image:
                if (
                    getattr(image, "is_animated", False)
                    or image.width * image.height > 40_000_000
                ):
                    return None
                with image.convert("L") as grayscale:
                    with grayscale.resize((9, 8), Image.Resampling.LANCZOS) as small:
                        pixels = small.tobytes()
                fingerprint = 0
                for y in range(8):
                    for x in range(8):
                        fingerprint = (fingerprint << 1) | (
                            pixels[y * 9 + x] > pixels[y * 9 + x + 1]
                        )
                return fingerprint
        except (OSError, ValueError, Image.DecompressionBombError):
            return None

    # ------------------------------------------------------------------ 存储
    def _connect(self, pack_root: Path) -> sqlite3.Connection:
        """Open (or reuse) the SQLite database for one pack tree."""
        db_file = _db_file(pack_root)
        cached = self._packs.pop(db_file, None)
        if cached is not None:
            self._packs[db_file] = cached
            return cached

        db_file.parent.mkdir(parents=True, exist_ok=True)
        connection = sqlite3.connect(db_file, timeout=30, check_same_thread=False)
        connection.execute("PRAGMA journal_mode=WAL")
        connection.execute("PRAGMA synchronous=NORMAL")
        connection.execute("PRAGMA cache_size=-2048")  # 页缓存上限约 2 MB
        connection.execute("CREATE TABLE IF NOT EXISTS meta(k TEXT PRIMARY KEY, v TEXT)")
        row = connection.execute("SELECT v FROM meta WHERE k='schema'").fetchone()
        if row is None or row[0] != _SCHEMA:
            # 结构变更时整体重建，避免残留旧列。
            connection.executescript("DROP TABLE IF EXISTS dirs; DROP TABLE IF EXISTS files;")
            connection.execute(
                "INSERT OR REPLACE INTO meta(k, v) VALUES('schema', ?)", (_SCHEMA,)
            )
        connection.executescript(_DDL)
        connection.commit()

        self._packs[db_file] = connection
        while len(self._packs) > self.max_packs:
            _, stale = self._packs.popitem(last=False)
            try:
                stale.close()
            except sqlite3.Error:
                pass
        return connection

    # ------------------------------------------------------------------ 扫描
    @staticmethod
    def _digest_of(path: Path) -> str | None:
        """Hash one file's bytes, returning None when it cannot be read."""
        try:
            hasher = hashlib.sha256()
            with path.open("rb") as source:
                for chunk in iter(lambda: source.read(1024 * 1024), b""):
                    hasher.update(chunk)
            return hasher.hexdigest()
        except OSError:
            return None

    def _sync_directory(
        self, connection: sqlite3.Connection, root: Path, resolved: Path
    ) -> list[Path]:
        """Reconcile one directory and return its in-root sub-directories.

        Only files whose size or mtime moved are hashed again; rows for files
        that vanished are deleted. Nothing is retained after the call.
        """
        key = str(resolved)
        try:
            scan = os.scandir(resolved)
        except OSError:
            return []

        known = {
            row[0]: (row[1], row[2])
            for row in connection.execute("SELECT p, s, m FROM files WHERE d=?", (key,))
        }
        subdirs: list[Path] = []
        seen = set()
        dirty = False

        with scan as scanner:
            for entry in scanner:
                try:
                    # 排除符号链接，包括 Windows 目录联接。
                    if entry.is_symlink():
                        continue
                    if entry.is_dir(follow_symlinks=False):
                        child = Path(entry.path)
                        try:
                            if child.resolve().is_relative_to(root):
                                subdirs.append(child)
                        except (OSError, RuntimeError):
                            pass
                        continue
                    if not entry.is_file(follow_symlinks=False):
                        continue
                    if Path(entry.name).suffix.lower() not in IMAGE_EXTENSIONS:
                        continue
                    # Windows 上 DirEntry.stat() 直接来自目录枚举，不额外发系统调用。
                    stat = entry.stat()
                    signature = (stat.st_size, stat.st_mtime_ns)
                    path_text = entry.path
                    seen.add(path_text)
                    if known.get(path_text) == signature:
                        continue
                    digest = self._digest_of(resolved / entry.name)
                    if digest is None:
                        continue
                    connection.execute(
                        "INSERT OR REPLACE INTO files(p, d, s, m, h, v)"
                        " VALUES(?,?,?,?,?,NULL)",
                        (path_text, key, signature[0], signature[1], digest),
                    )
                    dirty = True
                except OSError:
                    continue

        stale = [(path_text,) for path_text in known.keys() - seen]
        if stale:
            connection.executemany("DELETE FROM files WHERE p=?", stale)
            dirty = True
        if dirty:
            connection.commit()
        return subdirs

    def _refresh(self, connection: sqlite3.Connection, root: Path) -> None:
        """Walk the tree with scandir and rehash only new or changed files."""
        pending = [root]
        visited = set()
        while pending:
            directory = pending.pop()
            try:
                resolved = directory.resolve()
                if not resolved.is_relative_to(root):
                    continue
                key = str(resolved)
                if key in visited:
                    continue
                visited.add(key)
            except (OSError, RuntimeError):
                continue
            pending.extend(self._sync_directory(connection, root, resolved))
        self._prune_outside_dirs(connection, visited)

    @staticmethod
    def _prune_outside_dirs(
        connection: sqlite3.Connection, visited: set[str]
    ) -> None:
        """删除不属于本次遍历范围的目录行。

        `_sync_directory` 只能清理「本次访问到的目录里消失的文件」，因此
        **包目录被改名**（旧绝对路径整体消失）后，旧行永远不会进入 ``known``，
        也就永远不会被删掉，会永久残留在库里污染精确/近似查询。
        这里以本次遍历到的目录集为白名单整体裁剪一次。

        Args:
            connection: 该包的索引连接。
            visited: 本次遍历成功访问到的目录绝对路径集合。
        """
        if not visited:
            return
        try:
            present = connection.execute("SELECT DISTINCT d FROM files").fetchall()
        except sqlite3.Error:
            return
        orphan_dirs: list[tuple[str]] = []
        has_null = False
        for (dir_text,) in present:
            if dir_text is None:
                has_null = True
            elif dir_text not in visited:
                orphan_dirs.append((dir_text,))
        if not (has_null or orphan_dirs):
            return
        try:
            if has_null:
                connection.execute("DELETE FROM files WHERE d IS NULL")
            if orphan_dirs:
                connection.executemany("DELETE FROM files WHERE d=?", orphan_dirs)
            connection.commit()
        except sqlite3.Error:
            return

    def _fill_visual_hashes(self, connection: sqlite3.Connection) -> None:
        """Compute dHash for rows that still carry no visual fingerprint."""
        rows = connection.execute("SELECT p FROM files WHERE v IS NULL").fetchall()
        if not rows:
            return
        for (path_text,) in rows:
            visual = self._difference_hash(Path(path_text))
            connection.execute(
                "UPDATE files SET v=? WHERE p=?", (_to_signed(visual), path_text)
            )
        connection.commit()

    # ------------------------------------------------------------------ 查询
    def lookup(
        self, pack_root: Path, digest: str, candidate_path: Path | None = None
    ) -> ImageMatch:
        """Refresh cheap file stats and find an exact match or a review hint.

        This method performs disk reads and image decoding. Async callers must
        use a worker thread. Only changed files are hashed again. Visual hashes
        are computed lazily when a candidate image is supplied. Approximate
        matches must never be treated as duplicates: text variants may matter.

        Args:
            pack_root: Directory containing the target pack's images.
            digest: SHA-256 of the candidate's original bytes.
            candidate_path: Optional candidate file for visual similarity hints.

        Returns:
            Exact match or closest static-image hint within five differing bits.
        """
        try:
            root = Path(pack_root).resolve()
        except OSError:
            return ImageMatch()
        if not root.is_dir():
            return ImageMatch()

        with self._lock:
            connection = self._connect(root)
            self._refresh(connection, root)
            # 只认当前 root 下的行：包目录改名后旧绝对路径可能残留，
            # 一旦被选中，调用方拿到的路径并不存在（甚至 relative_to 直接抛错）。
            prefix = str(root) + os.sep
            row = connection.execute(
                "SELECT p FROM files WHERE h=? AND substr(p,1,?)=? LIMIT 1",
                (digest, len(prefix), prefix),
            ).fetchone()
            if row is not None:
                exact_path = Path(row[0])
                return ImageMatch(
                    exact_path=exact_path,
                    root=root,
                    relative_path=_relative_label(exact_path, root),
                )
            if candidate_path is None:
                return ImageMatch()

            candidate_hash = self._difference_hash(Path(candidate_path))
            if candidate_hash is None:
                return ImageMatch()
            self._fill_visual_hashes(connection)

            closest = ImageMatch()
            for path_text, stored in connection.execute(
                "SELECT p, v FROM files WHERE v IS NOT NULL AND v <> ?",
                (_DHASH_UNSET,),
            ):
                if not path_text.startswith(prefix):
                    # 越界行只可能来自包目录改名，忽略即可（无需在此处删除）。
                    continue
                value = _to_unsigned(stored)
                if value is None:
                    continue
                distance = (candidate_hash ^ value).bit_count()
                if distance <= 5 and (
                    closest.distance is None or distance < closest.distance
                ):
                    similar_path = Path(path_text)
                    closest = ImageMatch(
                        similar_path=similar_path,
                        distance=distance,
                        root=root,
                        relative_path=_relative_label(similar_path, root),
                    )
            return closest
