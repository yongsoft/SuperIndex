# index/ — 所有索引集中于此

这个目录是 SuperIndex 的**唯一索引存储位置**。所有树、manifest、注册表都在这里，
按来源分组；**源文件本身不动**，索引里只记录它们的绝对路径。

```
index/
├── registry.json            语料注册表：每个语料的源路径、状态、统计
├── corpora/                 注册进来的系统目录，一个语料一个子目录
│   └── <corpus-id>/
│       ├── manifest.json      目录树 + 每个文件的描述
│       └── trees/<key>.json   每个文件的章节树
├── pageindex/               PageIndex 文档库（PDF 走 doc store 那条路时用）
└── trees/                   PageIndex 离线树（scripts/01_build_trees.py 产出）
```

## 投放区：`data/`

**往 `data/` 下丢目录就会被自动索引**，不用注册：

```
data/                          ← 默认文档根目录（只读）
├── 中国太保/                    → 自动成为语料
├── 友邦保险/                    → 自动成为语料
└── contracts/                  → 后加的，watcher 一个轮询周期内发现
```

规则：
- `data/` 的**每个直接子目录**是一个语料
- 里面没有任何可索引文件（只有 `.sh`/空目录）→ 跳过，不产生空语料
- 服务启动时同步一次；之后 watcher 每轮也同步一次，所以新目录会自动进来
- `data/` 本身**只读** —— 索引一律写到 `index/corpora/`
- 换位置：`export SUPERINDEX_DATA_DIR=/path/to/docs`

要索引 `data/` 之外的目录，用 UI 的「添加目录」。

## 为什么集中在这里

**索引是构建产物，源文件不是。** 把两者放在一起会导致两个问题：一是注册一个
系统目录就会往那个目录里写东西，二是删除语料时不知道该删什么。所以：

- 源文件留在原处（`/data/reports`、`~/Documents/合同` 等），只读
- 索引一律写到 `index/corpora/<corpus-id>/`，与源文件完全分离
- 注册表记的是**绝对路径**，所以索引和源文件可以各自移动
- 删掉整个 `index/` 不会损坏任何源文件，重建即可

## 重建

`index/` 下的内容都是可重建的：

```bash
# 单个语料：在 Web UI 里点「重建索引」，或
python -c "from nav.registry import Registry; Registry().index('<corpus-id>')"

# 全部语料
python -c "from nav.registry import Registry; [Registry().index(c.id) for c in Registry().list()]"
```

## 版本控制

`index/` 里的内容**不入库**（`.gitignore` 里 `index/*` 已排除，只保留本文件和
`.gitkeep`）。原因：索引包含 LLM 生成的摘要，体积随语料增长，且随时可重建。

## 换个位置

默认写在本项目下。要放到别处（比如大盘或共享存储），设一个环境变量即可：

```bash
export SUPERINDEX_INDEX_DIR=/Volumes/big/superindex
```

代码里所有路径都从 `nav.registry.INDEX_ROOT` 取，不会散落在各处。
