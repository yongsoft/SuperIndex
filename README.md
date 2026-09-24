# SuperIndex

**Structure-navigation retrieval for large document corpora — built on [PageIndex](https://github.com/VectifyAI/PageIndex).**

PageIndex answers *"where in this report is the figure?"*. SuperIndex answers the
question that comes first: **"which report?"** — across thousands of files spread
over a deep directory tree, with no vector store and no embedding model anywhere
in the stack.

---

## The problem

Vector RAG retrieves passages that are *similar* to the question. On financial
and regulatory documents that is frequently the wrong passage. Two years'
dividend notes differ by a single digit, so their embeddings are nearly
identical — and when we measured it on a real corpus, **57% of chunks had a
near-duplicate in another document**, some of them character-for-character
identical. No embedding model can separate those, because the input is the same.

PageIndex takes a different route: it builds an explicit tree over a document and
has the model *reason* about which node to open. Answers trace back to a page
number instead of a cosine score.

That solves it for **one** document. But a real corpus is not one document. It is
thousands of files, organised in directories several levels deep, and the first
question a user asks is which file to look in at all. Hand a model a flat list of
1,000 filenames and it is guessing.

**SuperIndex adds the missing level.**

---

## What you get

### Navigate a corpus, not a document

A three-level addressing model — **corpus → document → section** — where each
level is a separate index sized so the cheap one stays resident and the expensive
one is fetched on demand.

```
question
 ├─ L0  which file?      read the whole directory tree, model picks 1–4 directories
 ├─ L1  which file?      list files under those, model picks 1–5
 └─ L2  which section?   read each candidate's chapter tree, model picks 1–6
        ↓
      section text + provenance
```

Three model decisions, each with a deterministic fallback. **Cost does not grow
with directory depth**, because the directory tree is far smaller than the file
count — 144 files lived in 229 directories in our test corpus, and the whole
directory listing fits in one prompt at ~2K tokens.

### Scales cheaply, and you can measure it before spending

Structural indexing needs **no LLM and no API key**.

| | |
|---|---|
| Structural index build | **0.09 s** for 144 files / 2,304 chapter nodes |
| Routing index size | **125 KB** — small enough to hold in memory and re-read per query |
| Full directory-tree listing | **8,904 characters** (~2K tokens) |

You can index an entire corpus, inspect the structure, and decide whether it is
worth summarising — before paying for anything.

### Better text in, better answers out

Upstream reads the PDF text layer with PyPDF2. On table-heavy documents that
falls apart: a bar-chart page flattens to `175230`, two numbers fused, with the
label-to-value association gone. Scanned PDFs are refused outright.

SuperIndex makes extraction **pluggable**, with **Azure Document Intelligence as
the default whenever it is configured**:

| | text layer | Azure Document Intelligence |
|---|---|---|
| Tables | mangled | **real Markdown tables** |
| Scanned / image-only | refused | **OCR'd** |
| Page anchors | synthesised | injected at real page boundaries |

Set two environment variables and every entry point takes it up automatically —
no flag to remember. With nothing configured you get the text layer, exactly as
before. **The backend never switches silently**, and a configured-but-failing
backend aborts rather than quietly degrading index quality.

### Upstream stays pristine

SuperIndex is a **layer, not a fork**. Not one file inside `PageIndex/` is
modified — the engine is vendored at a pinned commit, and every customisation is
applied from outside at runtime. `git pull` inside `PageIndex/` stays clean.

### No vectors, anywhere

No vector store, no embedding model, not even `numpy` in the dependency list.
Every answer is traceable to a file and a page.

### Diagnostics included

Retrieval quality problems are usually measurable before they are fixable:

| Tool | Answers |
|---|---|
| `scripts/04_md_audit.py` | Will this Markdown index well? Predicts the tree **without running an LLM** |
| `scripts/05_similarity_probe.py` | How much cross-document duplication is polluting retrieval? |
| `scripts/profile_query.py` | Where is the latency actually going? |
| `scripts/03_md_tree_probe.py` | What tree will a given Markdown file produce? |

### Tested offline

**55 assertions** across the extraction and navigation layers, with no network
and no credentials required:

```bash
python tests/test_azure_di.py    # 28 assertions — config, page markers, error mapping
python tests/test_backend.py     # 27 assertions — backend resolution, page splitting
```

---

## How it fits together

```
        ┌──────────────────────────────────────────────┐
        │  Application    webapp/ chat UI · scripts/   │
        ├──────────────────────────────────────────────┤
        │  ADDED BY       extractors/   nav/           │
        │  SUPERINDEX     where text    corpus→file    │
        │                 comes from    →section       │
        ├ ─ ─ ─ ─ ─ ─ ─ ─ ─ ─ ─ ─ ─ ─ ─ ─ ─ ─ ─ ─ ─ ─ ┤
        │  UPSTREAM       flash/classic  agent loop    │
        │  (unmodified)   tree building  4 tools       │
        ├──────────────────────────────────────────────┤
        │  Storage        PageIndex store · nav index  │
        └──────────────────────────────────────────────┘
```

**[`docs/ArchitectureIntro.html`](docs/ArchitectureIntro.html) has the full
picture** — 8 diagrams covering both the upstream engine and SuperIndex, the
storage model, and the design decisions that turned out to be load-bearing.
It is self-contained: open it in a browser, no server needed.

---

## Quick start

```bash
git clone https://github.com/yongsoft/SuperIndex.git
cd SuperIndex

# 1. PageIndex is vendored, so one clone is enough. Only the sample PDFs
#    are fetched separately (~27 MB, publicly downloadable).
bash data/aia_reports/download.sh

# 2. Environment — Python 3.10 or newer
python3 -m venv .venv
source .venv/bin/activate          # Windows: .venv\Scripts\activate
pip install -r requirements.txt    # includes PageIndex's pinned set
pip install -e PageIndex --no-deps

# 3. Build a navigation index over a directory of documents.
#    This step needs no LLM and no API key.
python -m nav.build /path/to/your/corpus --out corpus_index

# 4. Generate summaries so routing has something to reason over  [needs an LLM]
python -m nav.build /path/to/your/corpus --out corpus_index --summarize-files

# 5. Ask a question
python -m nav.route corpus_index "What was the dividend per share in 2024?"
```

**Two install steps, on purpose.** `requirements.txt` pulls in the pinned set
from `PageIndex/requirements.txt` (installing straight from upstream's
`pyproject.toml` makes pip backtrack badly on its unpinned `litellm` /
`openai-agents` ranges — the pinned file resolves in seconds). The package
itself is then added with `--no-deps` because its dependencies are already
satisfied.

`nav.build` accepts **`.md`, `.markdown`, `.txt` and `.pdf`** and walks nested
directories. It is incremental — unchanged files are skipped, so re-running after
adding a few documents is cheap.

### Configuration

Copy `.env.example` to `.env`. It documents every knob, including the optional
Azure Document Intelligence section. The only required value for the offline
path is nothing at all; for summarisation and QA you need one LLM provider key.

### Options worth knowing

| Flag | Effect |
|---|---|
| `--extractor {auto,azure-di,text-layer}` | Force a backend, to measure what Azure DI actually buys on your corpus |
| `--summarize-files` / `--summarize-chapters` | Generate routing summaries (incremental, LLM-backed) |
| `--max-files N` | Limit scope while evaluating |
| `--json` (on `nav.route`) | Machine-readable output: selected files, sections, line ranges |

---

## Try it on the sample corpus

The repository ships a **16-file synthetic corpus** with a realistic three-level
directory structure, plus a pre-built index, so you can see the navigator work
before touching your own data:

```bash
$PY -m nav.route samples/test_index "友邦保险 2024 年全年的每股股息是多少？"
```

```
  [目录] 全树 28 个 → 选 1 个
      友邦保险/2024/annual/
  [文件] 1 个候选 → 选 1 个
      友邦保险/2024/annual/AIA_AR2024.md
  [章节] AIA_AR2024.md (15 节点) → 股息, 主席报告
```

Separately, the project uses **AIA Group's published annual and interim reports**
as a real-world PDF sample — long, table-heavy, and full of the cross-year
duplication that breaks similarity search. Ten reports, 2,640 pages, downloaded
by the script above. They are there to exercise the PDF path and to give the
diagnostics something real to measure; nothing in the code is specific to AIA.

---

## When to use this

**Good fit**

- Thousands of documents in a nested directory structure
- Answers must be traceable to a source and a page
- Documents where near-duplicate text across versions is common (reports,
  filings, contracts, standards)
- Table-heavy or scanned PDFs, where you can enable Azure DI
- You want to index a corpus before committing to a per-page extraction bill

**Poor fit**

- A handful of short documents — plain PageIndex or a simple prompt will do
- Questions that need arithmetic across documents (see limitations)
- A corpus with no meaningful directory structure and no useful filenames
- Latency budgets under a second — navigation costs several LLM round trips

---

## Limitations

Stated plainly, because they are the questions a new user hits first.

| Limit | Detail |
|---|---|
| **Locating ≠ computing** | The navigator finds the right section. If the answer needs arithmetic across sections, the model still does it by reading text. Numbers need a structured path — see [`docs/dify-improvement-plan.md`](docs/dify-improvement-plan.md). |
| **Chapter summaries need an LLM** | A one-time per-corpus cost. Without them, section selection degrades to token matching. |
| **PDFs need bookmarks or Azure DI** | With neither, a PDF falls back to one node per page — coarse, but never silently dropped. |
| **Directory quality sets the ceiling** | Thousands of files flattened into one directory degrade level 0 to listing thousands of names. That is a corpus-organisation problem. |
| **Fallbacks match tokens, not synonyms** | "Life insurance" will not match a directory named `人身险`. An embedding pre-filter would fix it — at the cost of reintroducing the similarity problem the design avoids. |
| **Validated on synthetic corpora** | 16-file and 144-file corpora with known structure. The scale numbers are real; routing accuracy on a genuinely messy production corpus is not yet measured. |

---

## Documentation

| | |
|---|---|
| **Architecture** | [`docs/ArchitectureIntro.html`](docs/ArchitectureIntro.html) — 8 diagrams, upstream + SuperIndex |
| Navigator API and limits | [`nav/README.md`](nav/README.md) |
| Project state, decisions, gotchas | [`docs/HANDOVER.md`](docs/HANDOVER.md) |
| Structured-table proposal | [`docs/dify-improvement-plan.md`](docs/dify-improvement-plan.md) |
| What was vendored, how to update | [`PageIndex/UPSTREAM.md`](PageIndex/UPSTREAM.md) |

---

## Credits and licence

Built on **[PageIndex](https://github.com/VectifyAI/PageIndex)** by
[Vectify AI](https://vectify.ai), MIT licensed and vendored here at commit
`71714e8`. Upstream provides the tree-building engines and the agentic retrieval
loop; SuperIndex adds corpus-level navigation and pluggable extraction on top.

This project's own code is MIT licensed. See `PageIndex/LICENSE` for the upstream
licence, which is retained as required.
