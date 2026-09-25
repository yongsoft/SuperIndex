"""Corpus registry — registered directories, their indexes, and a watcher.

Turns `nav` from a one-shot CLI into something a UI can drive:

* **register** a directory (a real path on the machine, read-only)
* **index** it in the background, reporting status as it goes
* **watch** it for changes and re-index only what changed
* **query** any subset of the registered corpora in one call

Each corpus gets its **own index directory**, so indexing one cannot corrupt
another and a failure is contained. Querying several at once merges their
manifests into one routing view — see `nav.route.MultiNavigator`.

State lives in `index/registry.json` and each corpus's index in
`index/corpora/<id>/` — one central store, separate from the source files it
describes. Override the location with `SUPERINDEX_INDEX_DIR`.
"""
from __future__ import annotations

import json
import os
import shutil
import sys
import threading
import time
import uuid
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Callable, Optional

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from extractors.backend import Extractor  # noqa: E402
from nav.build import (corpus_fingerprint, corpus_summary,  # noqa: E402
                       scan, summarize_chapters, summarize_files)
from nav.store import Manifest  # noqa: E402

DEFAULT_INCLUDES = {".md", ".markdown", ".txt", ".pdf"}
DEFAULT_EXCLUDES = {".git", "node_modules", "__pycache__", ".venv", "venv",
                    ".idea", ".vscode", "dist", "build"}

# Every index this project writes lives under one root, so registering a corpus
# never writes into the directory being indexed. Point SUPERINDEX_INDEX_DIR at
# a bigger or shared volume to move the whole store elsewhere.
INDEX_ROOT = Path(os.getenv("SUPERINDEX_INDEX_DIR") or (ROOT / "index"))
REGISTRY_FILE = INDEX_ROOT / "registry.json"
CORPORA_ROOT = INDEX_ROOT / "corpora"
PAGEINDEX_STORE = INDEX_ROOT / "pageindex"
TREES_ROOT = INDEX_ROOT / "trees"

# The default place to put documents. Each immediate subdirectory becomes a
# corpus automatically, so dropping a folder in here is the whole workflow.
DATA_ROOT = Path(os.getenv("SUPERINDEX_DATA_DIR") or (ROOT / "data"))

STATUS_PENDING = "pending"
STATUS_INDEXING = "indexing"
STATUS_READY = "ready"
STATUS_ERROR = "error"

# Indexing is LLM-bound; running two corpora at once just trips rate limits.
_index_lock = threading.Lock()


@dataclass
class Corpus:
    """One registered directory and the state of its index."""

    id: str
    path: str
    name: str
    index_dir: str
    status: str = STATUS_PENDING
    stage: str = ""                    # what the indexer is doing right now
    error: str = ""
    added_at: float = 0.0
    indexed_at: float = 0.0
    checked_at: float = 0.0            # last watcher poll
    n_files: int = 0
    n_dirs: int = 0
    n_chapters: int = 0
    n_summarized: int = 0              # files carrying a description
    deep_index: bool = False           # chapter summaries enabled
    summarize_files: bool = True       # file descriptions enabled
    summary: str = ""                  # content-derived, used for corpus routing
    summary_fingerprint: str = ""      # the inputs `summary` was built from
    changes: dict = field(default_factory=dict)   # last detected change set

    @property
    def ready(self) -> bool:
        return self.status == STATUS_READY

    @property
    def index_path(self) -> Path:
        return Path(self.index_dir)

    @property
    def missing(self) -> bool:
        """The registered directory has gone away since it was added."""
        return not Path(self.path).is_dir()

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict) -> "Corpus":
        known = {f for f in cls.__dataclass_fields__}
        return cls(**{k: v for k, v in d.items() if k in known})


class Registry:
    """Manage registered corpora, their indexes, and the change watcher."""

    def __init__(self, state_file: "str | Path | None" = None,
                 index_root: "str | Path | None" = None,
                 *, model: str = "", workers: int = 6,
                 extractor: str = "auto"):
        # Defaults come from INDEX_ROOT, so the whole store moves as one unit.
        self.state_file = Path(state_file or REGISTRY_FILE)
        self.index_root = Path(index_root or CORPORA_ROOT)
        self.model = model
        self.workers = workers
        self.extractor = extractor
        self._corpora: dict[str, Corpus] = {}
        self._lock = threading.RLock()
        self._stop = threading.Event()
        self._watcher: Optional[threading.Thread] = None
        self._workers_busy: set[str] = set()
        self.load()

    # ── persistence ──────────────────────────────────────────────────────
    def load(self) -> None:
        if not self.state_file.is_file():
            return
        try:
            raw = json.loads(self.state_file.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return
        with self._lock:
            self._corpora = {}
            for item in raw.get("corpora", []):
                c = Corpus.from_dict(item)
                if c.status == STATUS_INDEXING:
                    # A restart mid-index leaves a stale status behind.
                    c.status = STATUS_PENDING
                    c.stage = "interrupted — reindex to resume"
                self._corpora[c.id] = c

    def save(self) -> None:
        with self._lock:
            payload = {
                "version": 1,
                "corpora": [c.to_dict() for c in self._corpora.values()],
            }
        self.state_file.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.state_file.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2),
                       encoding="utf-8")
        os.replace(tmp, self.state_file)

    # ── CRUD ─────────────────────────────────────────────────────────────
    def list(self) -> list[Corpus]:
        with self._lock:
            return sorted(self._corpora.values(), key=lambda c: c.added_at)

    def get(self, cid: str) -> Optional[Corpus]:
        with self._lock:
            return self._corpora.get(cid)

    def find_by_path(self, path: "str | Path") -> Optional[Corpus]:
        target = str(Path(path).expanduser().resolve())
        for c in self.list():
            if c.path == target:
                return c
        return None

    def add(self, path: "str | Path", name: str = "",
            *, deep_index: bool = False,
            summarize_files_enabled: bool = True) -> Corpus:
        """Register a directory. Raises ValueError if it is not usable."""
        p = Path(path).expanduser()
        if not p.is_absolute():
            raise ValueError("path must be absolute")
        if not p.exists():
            raise ValueError(f"path does not exist: {p}")
        if not p.is_dir():
            raise ValueError(f"not a directory: {p}")
        p = p.resolve()
        if _inside(p, ROOT) and not _inside(p, DATA_ROOT):
            # Indexing our own tree would pull in PageIndex/, .venv/ and index/
            # and recurse into whatever index it is building. The data/ drop
            # zone is the one exception: indexes go to index/, not into data/.
            raise ValueError(
                f"cannot register a directory inside the project "
                f"(put it under {DATA_ROOT.name}/ instead)")
        existing = self.find_by_path(p)
        if existing is not None:
            raise ValueError(f"already registered as {existing.name!r}")

        cid = uuid.uuid4().hex[:8]
        c = Corpus(
            id=cid,
            path=str(p),
            name=name.strip() or p.name or str(p),
            index_dir=str(self.index_root / cid),
            added_at=time.time(),
            deep_index=deep_index,
            summarize_files=summarize_files_enabled,
        )
        with self._lock:
            self._corpora[cid] = c
        self.save()
        return c

    def remove(self, cid: str, *, delete_index: bool = True) -> bool:
        with self._lock:
            c = self._corpora.pop(cid, None)
        if c is None:
            return False
        if delete_index:
            shutil.rmtree(c.index_dir, ignore_errors=True)
        self.save()
        return True

    def rename(self, cid: str, name: str) -> Optional[Corpus]:
        c = self.get(cid)
        if c is None or not name.strip():
            return None
        c.name = name.strip()
        self.save()
        return c

    # ── change detection ─────────────────────────────────────────────────
    # ── the data/ drop zone ──────────────────────────────────────────────
    def discover_data_root(self) -> list[Path]:
        """Immediate subdirectories of DATA_ROOT that hold indexable files.

        A folder with nothing indexable is skipped, so an empty placeholder or
        a stray directory does not create an empty corpus.
        """
        root = DATA_ROOT
        if not root.is_dir():
            return []
        out: list[Path] = []
        for child in sorted(root.iterdir(), key=lambda x: x.name.lower()):
            if not child.is_dir() or child.name.startswith("."):
                continue
            if child.name in DEFAULT_EXCLUDES:
                continue
            try:
                if _has_indexable(child):
                    out.append(child.resolve())
            except OSError:
                continue
        return out

    def sync_data_root(self, *, index: bool = True) -> list[Corpus]:
        """Register every new subdirectory of DATA_ROOT and start indexing it.

        This is what makes "drop a folder into data/ and it gets indexed" true.
        Already-registered directories are left alone — their own watcher deals
        with content changes.
        """
        fresh: list[Corpus] = []
        for path in self.discover_data_root():
            if self.find_by_path(path) is not None:
                continue
            try:
                fresh.append(self.add(str(path)))
            except ValueError:
                continue
        if index:
            for c in fresh:
                self.index_async(c.id)
        return fresh

    def detect_changes(self, cid: str) -> dict:
        """Stat-only diff against the stored index. Cheap enough to poll.

        Extraction is skipped entirely (`stats_only=True`), so this costs one
        directory walk and nothing else.
        """
        c = self.get(cid)
        if c is None:
            return {}
        root = Path(c.path)
        if not root.is_dir():
            return {"added": [], "changed": [], "removed": [],
                    "total": 0, "missing": True}

        index_dir = Path(c.index_dir)
        prev = (Manifest.load(index_dir)
                if (index_dir / "manifest.json").is_file() else None)
        if prev is None:
            return {"added": [], "changed": [], "removed": [],
                    "total": 0, "unindexed": True}

        fresh, _ = scan(root, DEFAULT_INCLUDES, DEFAULT_EXCLUDES,
                        previous=None, stats_only=True)
        added = [rp for rp in fresh.files if rp not in prev.files]
        changed = [
            rp for rp, fe in fresh.files.items()
            if rp in prev.files
            and (prev.files[rp].size != fe.size
                 or prev.files[rp].mtime != fe.mtime)
        ]
        removed = [rp for rp in prev.files if rp not in fresh.files]
        return {
            "added": sorted(added),
            "changed": sorted(changed),
            "removed": sorted(removed),
            "total": len(fresh.files),
            "checked_at": time.time(),
        }

    # ── indexing ─────────────────────────────────────────────────────────
    def index(self, cid: str, *, summarize: Optional[bool] = None,
              deep_index: Optional[bool] = None,
              force: bool = False,
              progress: Optional[Callable[[str], None]] = None) -> Corpus:
        """(Re)build one corpus index. Incremental: only changed files are
        re-extracted, and only files without a description get summarised.

        `summarize=None` (the default) honours the corpus's own setting, so a
        corpus registered without descriptions never starts calling an LLM
        just because the watcher noticed a change.
        """
        c = self.get(cid)
        if c is None:
            raise KeyError(cid)
        if summarize is None:
            summarize = c.summarize_files
        root = Path(c.path)
        if not root.is_dir():
            c.status = STATUS_ERROR
            c.error = f"directory no longer exists: {c.path}"
            self.save()
            return c

        if deep_index is not None:
            c.deep_index = deep_index
        index_dir = Path(c.index_dir)
        index_dir.mkdir(parents=True, exist_ok=True)

        def note(msg: str) -> None:
            c.stage = msg
            if progress:
                progress(msg)

        with _index_lock:
            c.status = STATUS_INDEXING
            c.error = ""
            self.save()
            try:
                prev = (Manifest.load(index_dir)
                        if (index_dir / "manifest.json").is_file() else None)

                note("scanning")
                extractor = Extractor() if _has_pdf(root) else None
                m, trees = scan(root, DEFAULT_INCLUDES, DEFAULT_EXCLUDES,
                                None, extractor, previous=prev)

                note(f"extracting {len(trees)} changed file(s)")
                for rp, (chapters, lines) in trees.items():
                    fe = m.files.get(rp)
                    if fe is not None:
                        m.save_tree(index_dir, fe.tree_key, chapters,
                                    lines or None)
                _prune_orphan_trees(index_dir, m)
                m.save(index_dir)

                if summarize:
                    note("writing file descriptions")
                    summarize_files(m, index_dir, self.model or _default_model(),
                                    self.workers, force)
                    m.save(index_dir)
                    if c.deep_index:
                        note("writing chapter summaries")
                        summarize_chapters(m, index_dir,
                                           self.model or _default_model(),
                                           self.workers, force)
                        m.save(index_dir)

                    # Corpus-level description. When several corpora are in
                    # scope this is the only content signal the router gets,
                    # so it must not be a file count. Refreshed only when its
                    # inputs moved — same rule as directory summaries.
                    fp = corpus_fingerprint(m)
                    if fp and (fp != c.summary_fingerprint or not c.summary):
                        note("describing corpus")
                        text = corpus_summary(m, self.model or _default_model())
                        if text:
                            c.summary = text
                            c.summary_fingerprint = fp

                c.n_files = len(m.files)
                c.n_dirs = max(0, len(m.dirs) - 1)
                c.n_chapters = sum(f.n_chapters for f in m.files.values())
                c.n_summarized = sum(1 for f in m.files.values() if f.summary)
                c.indexed_at = time.time()
                c.status = STATUS_READY
                c.stage = ""
                c.changes = {}
            except Exception as exc:  # noqa: BLE001 - surfaced in the UI
                c.status = STATUS_ERROR
                c.error = f"{type(exc).__name__}: {exc}"
                c.stage = ""
            self.save()
        return c

    def index_async(self, cid: str, **kwargs) -> threading.Thread:
        """Kick off indexing without blocking the HTTP request."""
        with self._lock:
            self._workers_busy.add(cid)

        def run() -> None:
            try:
                self.index(cid, **kwargs)
            finally:
                with self._lock:
                    self._workers_busy.discard(cid)

        t = threading.Thread(target=run, name=f"index-{cid}", daemon=True)
        t.start()
        return t

    @property
    def busy(self) -> set[str]:
        with self._lock:
            return set(self._workers_busy)

    # ── watcher ──────────────────────────────────────────────────────────
    def start_watcher(self, interval: float = 30.0,
                      *, auto_index: bool = True) -> None:
        """Poll every registered corpus and re-index the ones that changed.

        `auto_index=False` makes this a dry run: changes are detected and
        recorded on the corpus, but nothing is rebuilt. Useful for inspecting
        what a corpus would do without spending anything.
        """
        if self._watcher is not None and self._watcher.is_alive():
            return
        self._stop.clear()

        def loop() -> None:
            while not self._stop.is_set():
                try:
                    # New folder dropped into data/? Register and index it.
                    if auto_index:
                        try:
                            self.sync_data_root()
                        except Exception:  # noqa: BLE001
                            pass
                    for c in self.list():
                        if self._stop.is_set():
                            break
                        if c.status == STATUS_INDEXING or c.id in self.busy:
                            continue
                        if not Path(c.path).is_dir():
                            if c.status != STATUS_ERROR:
                                c.status = STATUS_ERROR
                                c.error = f"directory no longer exists: {c.path}"
                                self.save()
                            continue
                        try:
                            ch = self.detect_changes(c.id)
                        except Exception as exc:  # noqa: BLE001
                            c.error = f"watch failed: {exc}"
                            self.save()
                            continue
                        c.checked_at = time.time()
                        c.changes = {k: ch.get(k, []) for k in
                                     ("added", "changed", "removed")}
                        dirty = any(c.changes.values())
                        self.save()
                        if not auto_index:
                            continue
                        if dirty or c.status == STATUS_PENDING:
                            self.index(c.id)
                except Exception:  # noqa: BLE001 - the loop must survive
                    pass
                self._stop.wait(interval)

        self._watcher = threading.Thread(target=loop, name="corpus-watcher",
                                         daemon=True)
        self._watcher.start()

    def stop_watcher(self) -> None:
        self._stop.set()
        if self._watcher is not None:
            self._watcher.join(timeout=5)
            self._watcher = None

    @property
    def watching(self) -> bool:
        return self._watcher is not None and self._watcher.is_alive()

    # ── querying ─────────────────────────────────────────────────────────
    def corpus_tree(self, cid: str, *, max_depth: int = 12,
                    max_files: int = 400) -> dict:
        """The document tree of one corpus, for the UI's expandable listing.

        Reads the index, not the filesystem: the point of the tree view is to
        show what has been indexed, which is not always what is on disk (a
        dropped folder is indexed within one watcher tick).
        """
        c = self.get(cid)
        if c is None:
            return {"error": "unknown corpus"}
        idir = Path(c.index_dir)
        if not (idir / "manifest.json").is_file():
            return {"error": "not indexed yet"}
        m = Manifest.load(idir)

        dirs_by_parent: dict[str, list] = {}
        for d in m.dirs.values():
            if d.rel_path == "" or d.parent is None:
                continue
            dirs_by_parent.setdefault(d.parent, []).append(d)

        files_by_parent: dict[str, list] = {}
        for f in m.files.values():
            files_by_parent.setdefault(f.parent or "", []).append(f)

        shown = [0]

        def build(parent: str, depth: int) -> list[dict]:
            out: list[dict] = []
            if depth > max_depth:
                return out
            for d in sorted(dirs_by_parent.get(parent, []),
                            key=lambda x: x.name.lower()):
                out.append({
                    "type": "dir", "name": d.name, "path": d.rel_path,
                    "n_files": d.n_files, "summary": d.summary or "",
                    "children": build(d.rel_path, depth + 1),
                })
            for f in sorted(files_by_parent.get(parent, []),
                            key=lambda x: x.name.lower()):
                if shown[0] >= max_files:
                    break
                shown[0] += 1
                out.append({
                    "type": "file", "name": f.name, "path": f.rel_path,
                    "n_chapters": f.n_chapters,
                    "has_summary": bool(f.summary),
                })
            return out

        return {
            "id": c.id, "name": c.name,
            "n_files": len(m.files), "n_dirs": max(0, len(m.dirs) - 1),
            "n_chapters": sum(f.n_chapters for f in m.files.values()),
            "truncated": shown[0] >= max_files,
            "nodes": build("", 0),
        }

    def navigator(self, corpus_ids: Optional[list[str]] = None,
                  *, verbose: bool = False):
        """A MultiNavigator over the selected corpora.

        Only `ready` corpora are eligible; if nothing is selected, every ready
        corpus is used, which is what makes "indexed corpora are automatically
        in scope" true without the UI having to say so.
        """
        from nav.route import MultiNavigator

        ready = [c for c in self.list() if c.ready]
        if corpus_ids:
            wanted = set(corpus_ids)
            ready = [c for c in ready if c.id in wanted]
        if not ready:
            raise ValueError("no indexed corpora in scope")
        return MultiNavigator(
            [(c.id, c.index_dir, c.name,
              c.summary or _corpus_summary(c)) for c in ready],
            verbose=verbose)


# ── helpers ──────────────────────────────────────────────────────────────
def _inside(path: Path, base: Path) -> bool:
    """True if `path` is `base` or sits underneath it."""
    return path == base or base in path.parents


def _has_indexable(root: Path) -> bool:
    """True if the tree contains at least one file we would actually index."""
    for p in root.rglob("*"):
        if not p.is_file() or p.suffix.lower() not in DEFAULT_INCLUDES:
            continue
        if set(p.relative_to(root).parts) & DEFAULT_EXCLUDES:
            continue
        return True
    return False


def _has_pdf(root: Path) -> bool:
    for p in root.rglob("*.pdf"):
        if not (set(p.relative_to(root).parts) & DEFAULT_EXCLUDES):
            return True
    return False


def _prune_orphan_trees(index_dir: Path, m: Manifest) -> None:
    """Delete tree files whose document is gone, so removals actually reclaim
    disk instead of leaving dead chapter trees behind."""
    keep = {f.tree_key for f in m.files.values()}
    trees_dir = index_dir / "trees"
    if not trees_dir.is_dir():
        return
    for p in trees_dir.glob("*.json"):
        if p.stem not in keep:
            p.unlink(missing_ok=True)


def _corpus_summary(c: Corpus) -> str:
    """Counts only — the fallback when no content-derived summary exists yet.

    `Corpus.summary` is what routing normally sees; this covers a corpus that
    was indexed with descriptions off, where there is nothing to derive from.
    """
    bits = [f"{c.n_files} files"]
    if c.n_dirs:
        bits.append(f"{c.n_dirs} subdirectories")
    if c.n_summarized:
        bits.append(f"{c.n_summarized} described")
    if c.deep_index:
        bits.append("chapter summaries")
    return f"Registered corpus {c.name} — " + ", ".join(bits) + "."


def _default_model() -> str:
    from nav import llm
    return llm.DEFAULT_MODEL


__all__ = ["Corpus", "Registry", "STATUS_PENDING", "STATUS_INDEXING",
           "STATUS_READY", "STATUS_ERROR", "DEFAULT_INCLUDES",
           "DEFAULT_EXCLUDES"]
