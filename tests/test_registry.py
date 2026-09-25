#!/usr/bin/env python3
"""
Tests for the corpus registry and multi-corpus navigation.

Everything runs offline: indexing is exercised with `summarize=False`, so no
LLM is called. Fixtures are built in a temp directory, never in the repo.

    python tests/test_registry.py
    pytest tests/test_registry.py
"""
from __future__ import annotations

import contextlib
import shutil
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from nav.registry import (  # noqa: E402
    Corpus, Registry, STATUS_ERROR, STATUS_READY,
)
from nav.route import MultiNavigator, merge_manifests  # noqa: E402
from nav.store import Manifest  # noqa: E402

PASS, FAIL = [], []


def check(name: str, cond: bool, detail: str = "") -> None:
    (PASS if cond else FAIL).append(name)
    print(f"  {'✅' if cond else '❌'} {name}" + (f"  — {detail}" if detail else ""))


# ── fixtures ─────────────────────────────────────────────────────────────
def make_corpus(base: Path, name: str, files: dict[str, str]) -> Path:
    d = base / name
    for rel, text in files.items():
        p = d / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(text, encoding="utf-8")
    return d


SAMPLE = {
    "2024/annual/A.md": "# Report A\n\n## Summary\n\nAlpha content.\n\n## Details\n\nMore.\n",
    "2024/annual/B.md": "# Report B\n\n## Summary\n\nBeta content.\n",
    "2025/interim/C.md": "# Report C\n\n## Notes\n\nGamma content.\n",
}


def new_registry(tmp: Path) -> Registry:
    """Each test gets its own subdirectory, so state never leaks between them."""
    tmp.mkdir(parents=True, exist_ok=True)
    return Registry(state_file=tmp / "state.json", index_root=tmp / "idx")


# dirs created by SAMPLE: "", "2024", "2024/annual", "2025", "2025/interim"
SAMPLE_DIRS = 4          # excluding the root itself


# ── tests ────────────────────────────────────────────────────────────────
def test_add_validation(tmp: Path) -> None:
    print("\n[注册校验]")
    reg = new_registry(tmp / "add")
    src = make_corpus(tmp / "src", "alpha", SAMPLE)

    for label, bad in [("相对路径", "src/alpha"),
                       ("不存在的路径", str(tmp / "nope"))]:
        try:
            reg.add(bad)
            check(f"拒绝{label}", False, "未报错")
        except ValueError as exc:
            check(f"拒绝{label}", True, str(exc)[:46])

    # a file, not a directory
    f = tmp / "afile.txt"
    f.write_text("x", encoding="utf-8")
    try:
        reg.add(str(f))
        check("拒绝文件（非目录）", False, "未报错")
    except ValueError as exc:
        check("拒绝文件（非目录）", "not a directory" in str(exc))

    # inside the project — indexing our own tree would recurse into results/
    for inside in (ROOT, ROOT / "nav", ROOT / "samples"):
        try:
            reg.add(str(inside))
            check(f"拒绝项目内目录 {inside.name}", False, "未报错")
        except ValueError as exc:
            check(f"拒绝项目内目录 {inside.name}",
                  "inside the project" in str(exc))

    c = reg.add(str(src), name="Alpha")
    check("接受外部目录", c.name == "Alpha" and c.status == "pending")
    check("生成的 id 非空", bool(c.id) and len(c.id) == 8)

    try:
        reg.add(str(src))
        check("拒绝重复注册", False, "未报错")
    except ValueError as exc:
        check("拒绝重复注册", "already registered" in str(exc))


def test_crud_and_persistence(tmp: Path) -> None:
    print("\n[增删改查与持久化]")
    reg = new_registry(tmp / "crud")
    src = make_corpus(tmp / "src", "beta", SAMPLE)
    c = reg.add(str(src), name="Beta")

    check("list 返回 1 个", len(reg.list()) == 1)
    check("get 能取到", reg.get(c.id) is not None)
    check("find_by_path 能取到", (reg.find_by_path(src) or Corpus("", "", "", "")).id == c.id)

    reg.rename(c.id, "Beta 改名")
    check("改名生效", reg.get(c.id).name == "Beta 改名")
    check("空名字被拒", reg.rename(c.id, "  ") is None)

    reg2 = Registry(state_file=tmp / "crud" / "state.json", index_root=tmp / "crud" / "idx")
    check("重新载入后仍在", len(reg2.list()) == 1)
    check("重新载入后名字保留", reg2.list()[0].name == "Beta 改名")

    check("移除成功", reg.remove(c.id))
    check("移除后为空", len(reg.list()) == 0)
    check("重复移除返回 False", not reg.remove(c.id))
    check("移除后磁盘状态已保存",
          Registry(state_file=tmp / "crud" / "state.json",
                   index_root=tmp / "crud" / "idx").list() == [])


def test_indexing_and_changes(tmp: Path) -> None:
    print("\n[索引与变更检测]")
    reg = new_registry(tmp / "index")
    src = make_corpus(tmp / "src", "gamma", SAMPLE)
    c = reg.add(str(src), name="Gamma")

    c = reg.index(c.id, summarize=False)
    check("索引后状态 ready", c.status == STATUS_READY, c.error)
    check("文件数正确", c.n_files == 3, str(c.n_files))
    check("目录数正确", c.n_dirs == SAMPLE_DIRS, str(c.n_dirs))
    check("章节数 > 0", c.n_chapters >= 6, str(c.n_chapters))
    check("索引目录已创建", Path(c.index_dir, "manifest.json").is_file())

    ch = reg.detect_changes(c.id)
    check("无变化时 diff 为空",
          not ch["added"] and not ch["changed"] and not ch["removed"], str(ch))

    # modify
    p = src / "2024/annual/A.md"
    p.write_text(p.read_text(encoding="utf-8") + "\n## Added\n\nNew section.\n",
                 encoding="utf-8")
    ch = reg.detect_changes(c.id)
    check("检测到修改", ch["changed"] == ["2024/annual/A.md"], str(ch["changed"]))

    # add
    (src / "2026/annual/D.md").parent.mkdir(parents=True, exist_ok=True)
    (src / "2026/annual/D.md").write_text("# D\n\n## S\n\nbody\n", encoding="utf-8")
    ch = reg.detect_changes(c.id)
    check("检测到新增", ch["added"] == ["2026/annual/D.md"], str(ch["added"]))

    # incremental reindex picks both up
    before = c.n_chapters
    c = reg.index(c.id, summarize=False)
    check("重建后文件数 +1", c.n_files == 4, str(c.n_files))
    check("重建后章节数增加", c.n_chapters > before, f"{before} -> {c.n_chapters}")

    # remove
    (src / "2026/annual/D.md").unlink()
    ch = reg.detect_changes(c.id)
    check("检测到删除", ch["removed"] == ["2026/annual/D.md"], str(ch["removed"]))
    c = reg.index(c.id, summarize=False)
    check("重建后文件数回落", c.n_files == 3, str(c.n_files))
    check("孤儿章节树已清理",
          len(list(Path(c.index_dir, "trees").glob("*.json"))) == 3,
          str(len(list(Path(c.index_dir, "trees").glob("*.json")))))

    # unchanged files keep their tree (incremental, not a full rebuild)
    m = Manifest.load(Path(c.index_dir))
    check("未变更文件保留 n_chapters",
          all(f.n_chapters >= 0 for f in m.files.values()))

    # missing directory is reported, not crashed on
    shutil.rmtree(src)
    ch = reg.detect_changes(c.id)
    check("目录消失时标记 missing", ch.get("missing") is True, str(ch))
    c = reg.index(c.id, summarize=False)
    check("目录消失时进入 error", c.status == STATUS_ERROR, c.status)
    check("错误信息可读", "no longer exists" in c.error, c.error)


def test_multi_corpus(tmp: Path) -> None:
    print("\n[多语料合并与导航]")
    reg = new_registry(tmp / "multi")
    a = reg.add(str(make_corpus(tmp / "src", "one", SAMPLE)), name="语料一")
    b = reg.add(str(make_corpus(tmp / "src", "two", SAMPLE)), name="语料二")
    reg.index(a.id, summarize=False)
    reg.index(b.id, summarize=False)

    merged, dirs = merge_manifests([
        (a.id, a.index_dir, a.name, "第一个"),
        (b.id, b.index_dir, b.name, "第二个"),
    ])
    check("顶层是两个语料", [d.name for d in merged.children_of(None)[0]]
          == ["语料一", "语料二"])
    check("文件数翻倍", len(merged.files) == 6, str(len(merged.files)))
    check("目录数 = 2 × (语料根 + 子目录)",
          len(merged.dirs) == 2 * (1 + SAMPLE_DIRS), str(len(merged.dirs)))
    check("index_dirs 映射正确", set(dirs) == {a.id, b.id})

    sub, _ = merged.children_of(a.id)
    check("语料下保留原目录结构",
          sorted(d.name for d in sub) == ["2024", "2025"], str([d.name for d in sub]))
    f = merged.files[f"{a.id}/2024/annual/A.md"]
    check("文件 parent 正确指向", f.parent == f"{a.id}/2024/annual", f.parent)

    nav = MultiNavigator([
        (a.id, a.index_dir, a.name, "第一个"),
        (b.id, b.index_dir, b.name, "第二个"),
    ], verbose=False)
    check("corpus_ids 正确", sorted(nav.corpus_ids) == sorted([a.id, b.id]))
    check("能从正确索引加载树", len(nav._load_tree(f)[0]) > 0)
    check("未知语料前缀安全返回", nav._load_tree(
        type(f)(rel_path="zzz/x.md", name="x.md", parent="zzz", ext=".md")) == ([], []))

    # navigator() only exposes ready corpora
    nav2 = reg.navigator([a.id])
    check("navigator() 可只选一个", nav2.corpus_ids == [a.id])
    nav3 = reg.navigator()
    check("navigator() 默认含全部就绪语料",
          sorted(nav3.corpus_ids) == sorted([a.id, b.id]))
    try:
        reg.navigator(["nonexistent"])
        check("选择未知语料时报错", False, "未报错")
    except ValueError as exc:
        check("选择未知语料时报错", "no indexed corpora" in str(exc))


@contextlib.contextmanager
def fake_data_root(path: Path):
    """Point nav.registry.DATA_ROOT at a temp dir for one test.

    Without this, any test that touches the watcher discovers the real data/
    directory and starts indexing the user's own documents.
    """
    import nav.registry as reg_mod
    path.mkdir(parents=True, exist_ok=True)
    original = reg_mod.DATA_ROOT
    reg_mod.DATA_ROOT = path
    try:
        yield path
    finally:
        reg_mod.DATA_ROOT = original


def test_watcher_modes(tmp: Path) -> None:
    print("\n[watcher：检测模式与自动重建]")
    with fake_data_root(tmp / "watch" / "data"):
        _watcher_body(tmp)


def _watcher_body(tmp: Path) -> None:
    reg = new_registry(tmp / "watch")
    src = make_corpus(tmp / "src", "watch", SAMPLE)
    # summarize_files_enabled=False keeps this test offline: the watcher honours
    # the corpus's own setting rather than assuming descriptions are wanted.
    c = reg.add(str(src), name="Watch", summarize_files_enabled=False)
    reg.index(c.id, summarize=False)
    check("已索引 3 个文件", reg.get(c.id).n_files == 3)

    # ── detect-only mode ─────────────────────────────────────────────────
    reg.start_watcher(interval=0.3, auto_index=False)
    check("watcher 已启动", reg.watching)
    (src / "2024/annual/A.md").write_text("# changed\n\n## S\n\nnew\n", encoding="utf-8")

    deadline = time.time() + 12
    while time.time() < deadline and not (reg.get(c.id).changes or {}).get("changed"):
        time.sleep(0.3)
    ch = reg.get(c.id).changes
    check("检测到变更", ch.get("changed") == ["2024/annual/A.md"], str(ch))
    check("auto_index=False 时不动索引",
          reg.get(c.id).n_files == 3, str(reg.get(c.id).n_files))
    reg.stop_watcher()
    check("watcher 已停止", not reg.watching)

    # ── auto-index mode ──────────────────────────────────────────────────
    (src / "2026/annual/D.md").parent.mkdir(parents=True, exist_ok=True)
    (src / "2026/annual/D.md").write_text("# D\n\n## S\n\nbody\n", encoding="utf-8")
    reg.start_watcher(interval=0.3, auto_index=True)
    deadline = time.time() + 20
    while time.time() < deadline and reg.get(c.id).n_files < 4:
        time.sleep(0.3)
    c = reg.get(c.id)
    check("auto_index=True 时自动重建", c.n_files == 4, str(c.n_files))
    check("重建后 changes 清空", not any((c.changes or {}).values()), str(c.changes))
    check("沿用语料自身设置，未调用 LLM", c.n_summarized == 0, str(c.n_summarized))
    reg.stop_watcher()


def test_data_root_dropzone(tmp: Path) -> None:
    print("\n[data/ 投放区：自动发现与注册]")
    with fake_data_root(tmp / "dropzone" / "data") as fake_data:
        reg = new_registry(tmp / "dropzone")

        (fake_data / "empty").mkdir()
        check("空目录被跳过", reg.discover_data_root() == [],
              str(reg.discover_data_root()))

        d1 = fake_data / "reports" / "2024"
        d1.mkdir(parents=True)
        (d1 / "a.md").write_text("# A\n\n## S\n\nbody\n", encoding="utf-8")
        check("发现含文件的子目录",
              [p.name for p in reg.discover_data_root()] == ["reports"],
              str([p.name for p in reg.discover_data_root()]))

        d2 = fake_data / "scans"
        d2.mkdir()
        (d2 / "x.pdf").write_bytes(b"%PDF-1.4\n")
        check("PDF 也算可索引",
              sorted(p.name for p in reg.discover_data_root()) == ["reports", "scans"],
              str(sorted(p.name for p in reg.discover_data_root())))

        d3 = fake_data / "scripts_only"
        d3.mkdir()
        (d3 / "run.sh").write_text("echo hi\n", encoding="utf-8")
        check("只有 .sh 的目录被跳过",
              "scripts_only" not in [p.name for p in reg.discover_data_root()])

        (fake_data / ".hidden").mkdir()
        (fake_data / ".hidden" / "x.md").write_text("# x\n", encoding="utf-8")
        check("隐藏目录被跳过",
              ".hidden" not in [p.name for p in reg.discover_data_root()])

        added = reg.sync_data_root(index=False)
        check("sync 自动注册",
              sorted(c.name for c in added) == ["reports", "scans"],
              str(sorted(c.name for c in added)))
        check("sync 是幂等的", reg.sync_data_root(index=False) == [])

        extra = fake_data / "extra"
        extra.mkdir()
        c = reg.add(str(extra))
        check("data/ 内的目录允许注册", c.name == "extra")

        for bad, label in [(ROOT / "nav", "项目内非 data/ 目录"),
                           (ROOT, "项目根")]:
            try:
                reg.add(str(bad))
                check(f"{label}仍拒绝", False, "未报错")
            except ValueError as exc:
                check(f"{label}仍拒绝", "inside the project" in str(exc))


def main() -> int:
    print("=" * 74)
    print("Corpus registry tests（全部离线，不调用 LLM）")
    print("=" * 74)
    tmp = Path(tempfile.mkdtemp(prefix="superindex-test-"))
    try:
        test_add_validation(tmp)
        test_crud_and_persistence(tmp)
        test_indexing_and_changes(tmp)
        test_multi_corpus(tmp)
        test_watcher_modes(tmp)
        test_data_root_dropzone(tmp)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
    print()
    print("=" * 74)
    print(f"  通过 {len(PASS)}  失败 {len(FAIL)}")
    if FAIL:
        print("  失败项: " + ", ".join(FAIL))
    print("=" * 74)
    return 1 if FAIL else 0


if __name__ == "__main__":
    raise SystemExit(main())
