#!/usr/bin/env python3
"""
SuperIndex web UI — directory-scoped document search.

A dependency-free HTTP server (Python stdlib only) that exposes the two-level
navigator over a set of registered directories.

    GET    /                          the UI
    GET    /api/state                 models, watcher status, corpora
    GET    /api/browse?path=...       list sub-directories (read-only)
    POST   /api/corpora               {"path": ..., "name": ..., "deep_index": bool}
    PATCH  /api/corpora/<id>          {"name": ...}
    DELETE /api/corpora/<id>          unregister (and delete its index)
    POST   /api/corpora/<id>/reindex  {"deep_index": bool, "force": bool}
    POST   /api/ask                   {"question": ..., "corpus_ids": [...]} -> SSE

Registered directories are indexed in the background; once a corpus is `ready`
it is automatically in scope for questions, unless the caller names a subset.

The ask stream carries the navigation trace — which directories, files and
sections the model chose — so an answer can be audited rather than trusted.

Usage:
    python webapp/server.py                      # http://127.0.0.1:8787
    python webapp/server.py --port 9000 --no-watch
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import threading
import time
import traceback
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from dotenv import load_dotenv  # noqa: E402

load_dotenv(ROOT / ".env")

from nav import llm  # noqa: E402
from nav.debuglog import QueryTrace, read as read_log, stats as log_stats  # noqa: E402
from nav.registry import DATA_ROOT, Registry  # noqa: E402
from nav.route import Result, build_context, answer_prompt  # noqa: E402

STATIC = Path(__file__).resolve().parent / "static"

# "low" cut wall clock 10.3s -> 5.8s on a deep question with the answer
# unchanged; it is the single biggest latency lever we measured.
REASONING_EFFORT = os.getenv("PAGEINDEX_REASONING_EFFORT", "low").strip() or None

# Reasoning tokens count against max_tokens, so a tight budget with reasoning on
# returns an empty answer with finish_reason=length. 1500 was too small; the
# retry logic in nav/llm.py now escalates, but start with enough room.
ANSWER_MAX_TOKENS = int(os.getenv("SUPERINDEX_ANSWER_MAX_TOKENS", "4096"))

_registry: Registry | None = None
_registry_lock = threading.Lock()
_ask_lock = threading.Lock()          # one answer at a time, for readable traces


def registry() -> Registry:
    global _registry
    with _registry_lock:
        if _registry is None:
            _registry = Registry(
                model=llm.DEFAULT_MODEL,
                workers=int(os.getenv("SUPERINDEX_INDEX_WORKERS", "6")),
            )
        return _registry


def corpus_json(c) -> dict:
    return {
        "id": c.id,
        "name": c.name,
        "path": c.path,
        "status": c.status,
        "stage": c.stage,
        "error": c.error,
        "ready": c.ready,
        "missing": c.missing,
        "n_files": c.n_files,
        "n_dirs": c.n_dirs,
        "n_chapters": c.n_chapters,
        "n_summarized": c.n_summarized,
        "deep_index": c.deep_index,
        "added_at": c.added_at,
        "indexed_at": c.indexed_at,
        "checked_at": c.checked_at,
        "changes": c.changes,
    }


class Handler(BaseHTTPRequestHandler):
    server_version = "SuperIndex/1.0"
    protocol_version = "HTTP/1.1"

    def log_message(self, fmt, *args):        # keep the console readable
        if "/api/ask" not in (self.path or ""):
            sys.stderr.write("%s - %s\n" % (self.address_string(), fmt % args))

    # ── helpers ──────────────────────────────────────────────────────────
    def _json(self, obj, code=200):
        body = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _file(self, path: Path, ctype: str):
        if not path.is_file():
            self.send_error(404, "Not found")
            return
        body = path.read_bytes()
        self.send_response(200)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _body(self) -> dict:
        try:
            n = int(self.headers.get("Content-Length") or 0)
            return json.loads(self.rfile.read(n) or b"{}")
        except (ValueError, TypeError):
            return {}

    # ── GET ──────────────────────────────────────────────────────────────
    def do_GET(self):
        url = urlparse(self.path)
        path = url.path
        reg = registry()

        if path in ("/", "/index.html"):
            self._file(STATIC / "index.html", "text/html; charset=utf-8")
        elif path == "/api/state":
            self._json({
                "corpora": [corpus_json(c) for c in reg.list()],
                "watching": reg.watching,
                "busy": sorted(reg.busy),
                "index_model": llm.DEFAULT_MODEL,
                "chat_model": llm.DEFAULT_MODEL,
                "reasoning_effort": REASONING_EFFORT,
                "home": str(Path.home()),
                "data_root": str(DATA_ROOT),
                "logs": log_stats(),
            })
        elif path == "/api/logs":
            q = parse_qs(url.query)
            kind = q.get("kind", ["queries"])[0]
            if kind not in ("queries", "errors"):
                return self._json({"error": "kind must be queries or errors"}, 400)
            try:
                limit = max(1, min(int(q.get("limit", ["50"])[0]), 500))
            except ValueError:
                limit = 50
            self._json({
                "kind": kind,
                "stats": log_stats(),
                "records": read_log(
                    kind, limit=limit,
                    only_failed=q.get("failed", ["0"])[0] in ("1", "true"),
                    query_id=q.get("id", [""])[0]),
            })
        elif path == "/api/browse":
            q = parse_qs(url.query)
            self._json(browse(q.get("path", [str(DATA_ROOT)])[0]))
        elif path == "/api/health":
            self._json({"ok": True})
        elif path.startswith("/static/"):
            target = (STATIC / path[len("/static/"):]).resolve()
            if STATIC.resolve() not in target.parents:
                self.send_error(403, "Forbidden")
                return
            ctype = {".html": "text/html; charset=utf-8",
                     ".css": "text/css; charset=utf-8",
                     ".js": "application/javascript; charset=utf-8",
                     }.get(target.suffix, "application/octet-stream")
            self._file(target, ctype)
        else:
            self.send_error(404, "Not found")

    # ── POST / PATCH / DELETE ────────────────────────────────────────────
    def do_POST(self):
        url = urlparse(self.path)
        parts = [p for p in url.path.split("/") if p]
        reg = registry()

        if url.path == "/api/ask":
            return self._ask()
        if url.path == "/api/corpora":
            payload = self._body()
            try:
                c = reg.add(payload.get("path") or "",
                            payload.get("name") or "",
                            deep_index=bool(payload.get("deep_index")))
            except ValueError as exc:
                return self._json({"error": str(exc)}, 400)
            reg.index_async(c.id)
            return self._json({"corpus": corpus_json(c)}, 201)
        if len(parts) == 4 and parts[1] == "corpora" and parts[3] == "reindex":
            c = reg.get(parts[2])
            if c is None:
                return self._json({"error": "unknown corpus"}, 404)
            payload = self._body()
            if reg.busy:
                return self._json(
                    {"error": "another index is already running"}, 409)
            reg.index_async(c.id,
                            deep_index=bool(payload.get("deep_index",
                                                       c.deep_index)),
                            force=bool(payload.get("force")))
            return self._json({"corpus": corpus_json(c)})
        self.send_error(404, "Not found")

    def do_PATCH(self):
        parts = [p for p in urlparse(self.path).path.split("/") if p]
        if len(parts) == 3 and parts[1] == "corpora":
            c = registry().rename(parts[2], (self._body().get("name") or ""))
            if c is None:
                return self._json({"error": "unknown corpus or empty name"}, 400)
            return self._json({"corpus": corpus_json(c)})
        self.send_error(404, "Not found")

    def do_DELETE(self):
        parts = [p for p in urlparse(self.path).path.split("/") if p]
        if len(parts) == 3 and parts[1] == "corpora":
            if registry().remove(parts[2]):
                return self._json({"ok": True})
            return self._json({"error": "unknown corpus"}, 404)
        self.send_error(404, "Not found")

    # ── the streaming answer ─────────────────────────────────────────────
    def _ask(self):
        payload = self._body()
        question = (payload.get("question") or "").strip()
        corpus_ids = payload.get("corpus_ids") or []
        if isinstance(corpus_ids, str):
            corpus_ids = [corpus_ids]
        if not question:
            return self._json({"error": "question is required"}, 400)

        reg = registry()
        try:
            nav = reg.navigator(corpus_ids or None)
        except ValueError as exc:
            return self._json({"error": str(exc)}, 400)

        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream; charset=utf-8")
        self.send_header("Cache-Control", "no-cache")
        self.send_header("X-Accel-Buffering", "no")
        self.send_header("Connection", "close")
        self.end_headers()

        def emit(event: str, data):
            self.wfile.write(
                f"event: {event}\ndata: {json.dumps(data, ensure_ascii=False)}\n\n"
                .encode("utf-8"))
            self.wfile.flush()

        started = time.time()
        trace = QueryTrace(question,
                           [corpus_name(reg, c) for c in nav.corpus_ids],
                           model=llm.DEFAULT_MODEL)
        try:
            with _ask_lock:
                emit("stage", {"text": f"路由：{len(nav.corpus_ids)} 个语料"})
                res = Result(question=question)

                # Level 0/1: which files. Emitted per step so the UI can show
                # the model narrowing down while it happens.
                trace.mark("route")
                res.files, t1 = nav.find_files(question, top_n=5)
                for st in t1:
                    emit("nav", {"level": "dir", "where": st.where,
                                 "detail": st.detail, "picked": st.picked,
                                 "note": st.note})
                    trace.route_step(level="dir", where=st.where, detail=st.detail,
                                     picked=st.picked, note=st.note)
                trace.stage_ms("route", trace.elapsed("route"))
                trace.files([f.rel_path for f in res.files])
                if not res.files:
                    trace.abort("no files located")
                    emit("error", {"message": "没有定位到相关文件"})
                    emit("done", {"ok": False, "ms": int((time.time()-started)*1000)})
                    return

                # Level 2: which sections.
                trace.mark("sections")
                for fe in res.files:
                    secs, t2 = nav.find_sections(question, fe, top_n=6)
                    for st in t2:
                        emit("nav", {"level": "chapter", "where": st.where,
                                     "detail": st.detail, "picked": st.picked,
                                     "note": st.note})
                        trace.route_step(level="chapter", where=st.where,
                                         detail=st.detail, picked=st.picked,
                                         note=st.note)
                    res.sections += [(fe, s) for s in secs]
                trace.stage_ms("sections", trace.elapsed("sections"))

                context, sources = build_context(res, nav)
                trace.sources(sources)
                emit("sources", sources)

                if not sources:
                    trace.abort("no sections located")
                    emit("error", {"message": "定位到文件但没找到具体章节"})
                    emit("done", {"ok": False, "ms": int((time.time()-started)*1000)})
                    return

                emit("stage", {"text": f"生成回答（{len(sources)} 个来源）"})
                trace.mark("answer")
                answer = ""
                for delta in llm.chat_stream_text(
                        answer_prompt(question, context),
                        effort=REASONING_EFFORT,
                        max_tokens=ANSWER_MAX_TOKENS):
                    answer += delta
                    emit("answer", {"delta": delta})
                trace.stage_ms("answer", trace.elapsed("answer"))
                trace.finish(answer, context_chars=len(context))

            emit("done", {"ok": True, "ms": int((time.time() - started) * 1000)})
        except Exception as exc:  # noqa: BLE001
            traceback.print_exc()
            # One record in queries.jsonl (ok=false) plus one in errors.jsonl,
            # sharing the same id so they can be joined.
            trace.fail(exc, stage="ask")
            try:
                emit("error", {"message": f"{type(exc).__name__}: {exc}"})
                emit("done", {"ok": False, "ms": int((time.time()-started)*1000)})
            except Exception:  # noqa: BLE001 - client already gone
                pass


def corpus_name(reg: Registry, cid: str) -> str:
    c = reg.get(cid)
    return c.name if c else cid


def browse(raw: str) -> dict:
    """List sub-directories of `raw`. Read-only: names only, never contents.

    This is a localhost tool for picking a directory to index, so it needs to
    see the filesystem. It deliberately does not read file contents, and the
    server binds to 127.0.0.1 by default.
    """
    if not raw:
        raw = str(DATA_ROOT)
    p = Path(raw).expanduser()
    try:
        p = p.resolve()
    except OSError as exc:
        return {"error": str(exc), "path": raw, "dirs": [], "parent": None}
    if not p.is_dir():
        return {"error": f"not a directory: {p}", "path": str(p),
                "dirs": [], "parent": None}
    dirs = []
    try:
        for child in sorted(p.iterdir(), key=lambda x: x.name.lower()):
            if child.name.startswith("."):
                continue
            try:
                if child.is_dir():
                    dirs.append({"name": child.name, "path": str(child)})
            except OSError:
                continue
    except PermissionError:
        return {"error": f"permission denied: {p}", "path": str(p),
                "dirs": [], "parent": None}
    return {
        "path": str(p),
        "parent": str(p.parent) if p.parent != p else None,
        "dirs": dirs[:500],
        "truncated": len(dirs) > 500,
    }


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, default=8787)
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--no-watch", action="store_true",
                    help="do not poll registered directories for changes")
    ap.add_argument("--watch-interval", type=float, default=30.0)
    args = ap.parse_args()

    reg = registry()
    print("SuperIndex web UI")
    print(f"  model        : {llm.DEFAULT_MODEL}")
    print(f"  data root    : {DATA_ROOT}")

    # Anything already sitting in data/ becomes a corpus on startup.
    fresh = reg.sync_data_root()
    if fresh:
        print(f"  discovered   : {', '.join(c.name for c in fresh)}")

    print(f"  corpora      : {len(reg.list())} registered, "
          f"{sum(1 for c in reg.list() if c.ready)} ready")

    if args.no_watch:
        print("  watcher      : disabled")
    else:
        reg.start_watcher(interval=args.watch_interval)
        print(f"  watcher      : every {args.watch_interval:.0f}s")
    print(f"  -> http://{args.host}:{args.port}\n")

    httpd = ThreadingHTTPServer((args.host, args.port), Handler)
    httpd.daemon_threads = True
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\nshutting down")
    finally:
        reg.stop_watcher()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
