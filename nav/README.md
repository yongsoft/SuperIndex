# nav — 两级导航检索

面向「多级目录 + 上千文件」语料的检索层。参考 PageIndex 的思路，但把树的粒度
从「文档内部章节」扩展到「语料目录 → 文档 → 章节」三层。

**核心流程**

```
问题
 ├─ 第 1 级：在目录树里定位文件（一个或多个）
 └─ 第 2 级：在每个候选文件的章节树里定位章节
        ↓
      返回章节原文 + 来源路径
```

## 快速开始

```bash
PY=.venv/bin/python          # after: python3 -m venv .venv && source .venv/bin/activate

# 1. 建结构索引 —— 免费、无 LLM，上千文件也是秒级
$PY -m nav.build /path/to/reports --out index/demo

# 2. 生成路由用摘要（文件 + 目录级）      [LLM]
$PY -m nav.build /path/to/reports --out index/demo --summarize-files

# 3. 生成章节级摘要                        [LLM]
$PY -m nav.build /path/to/reports --out index/demo --summarize-chapters

# 查询
$PY -m nav.route index/demo "友邦保险 2024 年全年的每股股息是多少？"
$PY -m nav.route index/demo "..." --show-content      # 附章节原文
$PY -m nav.route index/demo "..." --json              # 机器可读
```

> `--out` 建议放在项目的 `index/` 下（CLI 单语料用 `index/demo`，UI 注册的语料用
> `index/corpora/<id>`）。这样**所有索引集中在一处**，源目录保持只读 —— 见
> `index/README.md`。

步骤 2/3 是**增量**的：文件大小与 mtime 未变且已有对应摘要时直接跳过。
内容变了才会重建该文件的章节树（其旧章节摘要随之失效）。
新增文件自动挂进索引，删除的文件自动移除。

## 索引结构

单个语料的索引目录（`index/demo` 或 `index/corpora/<id>`）内部：

```
<index-dir>/
├── manifest.json         目录树 + 文件元数据 + 摘要   ← 小，常驻内存
└── trees/<key>.json      每份文档的章节树             ← 大，按需加载
```

刻意拆成两份：**路由索引要小到能整棵读进 prompt，章节树只在少数文件上按需取**。

以 144 文件 / 229 目录 / 2,304 章节节点的语料为例：

| | 大小 |
|---|---|
| 建结构索引耗时 | **约 0.09 秒** |
| `manifest.json` | **125 KB** |
| 完整目录树 listing | **8,904 字符**（约 2K tokens） |

目录树远小于文件数，所以完整目录树一直能塞进一次 prompt。

## 数据模型

```json
// 目录节点
{ "rel_path": "友邦保险/2024/annual", "name": "annual", "parent": "友邦保险/2024",
  "child_dirs": [], "files": ["友邦保险/2024/annual/AIA_AR2024.md"],
  "summary": "友邦保险 2024 年年报，含新业务价值、税后营运溢利、股息...",
  "n_files": 1, "n_dirs": 0 }

// 文件节点
{ "rel_path": "友邦保险/2024/annual/AIA_AR2024.md", "name": "AIA_AR2024.md",
  "parent": "友邦保险/2024/annual", "ext": ".md", "size": 4096, "mtime": 1.7e9,
  "summary": "2024 年友邦保险全年股息为每股 150.72 港仙...",
  "n_chapters": 15, "max_depth": 3, "tree_key": "a1b2c3d4e5f6a7b8" }

// 章节节点
{ "title": "股息", "level": 3, "start": 11, "end": 21,
  "summary": "2024 年全年股息每股 150.72 港仙（中期 39.19 + 末期 111.53）",
  "children": [] }
```

`start` / `end` 对 markdown 是**行号**，对 PDF 是**页码**。取正文时按这个区间切片。

## 四个关键设计

### 1. 第 1 级读完整目录树，而不是逐层下钻

目录数远小于文件数（144 文件只有 229 目录），所以完整目录树一直塞得下。
一次调用让模型选目录，再一次选文件 —— **两次调用，与目录深度无关**。

只有在目录数超过 `DIR_TREE_BUDGET`（默认 240）时才退回逐层下钻。

### 2. 提示词强制「必须选择」

早期版本的提示词写了「都不相关时两个列表都留空」。结果模型**不可预测地**返回空
列表，在明明可回答的问题上直接中止导航。实测同一个问题连跑两次，
一次选中一次不选。

改成「**必须至少选 1 个**」之后稳定了。模型的默认倾向是保守，不要给它
「留空」这个出口 —— 真要表达不相关，交给下游的阈值判断。

### 3. 每一级都有确定性回退

模型仍可能返回空（尤其在没有摘要的索引上）。所以每一级都有回退：

| 位置 | 回退策略 |
|---|---|
| 目录选择 | 按问题里的**期间**（权重 3）+ 中英文词元匹配目录路径 + 策略权重 |
| 文件选择 | 同上，匹配文件路径 |
| 逐层下钻中的目录 | 优先回退到**本层文件**，其次才是子目录 |
| 章节选择 | 词元匹配标题（权重 1）+ 摘要（权重 0.5） |

回退优先在本层解决 —— 回退到全语料是很粗的手段，语料越大越糟。

**期间是最强的路由信号。** 财务问答的跨期混淆是最大的错误来源，
而目录路径里通常带年份，所以抽出的期间同时用于提示词和回退打分。
`year_hints()` 是内置的 4 位年份提取器；`RoutingPolicy.periods_in()` 在它
之上补业务写法（`FY24`、`2024H1`、`Q3`），空策略时两者完全等价。

回退是**唯一**能让策略真正改变结果的地方（模型在场时策略只是「更倾向」），
所以别名扩展和目录权重都接在这里 —— 这里的排序错了就没有第二道防线。

### 4. 业务知识外置到 `policy.py`

上面三条是「面对未知语料」的合理默认，但在企业内部，语料并不是未知的 ——
有人知道法定年报在 `annual/` 下、`_drafts/` 永远不该被搜、"友邦" 和 "AIA"
是同一家公司。这部分知识属于业务方，所以放在
[`config/routing_policy.yaml`](../config/routing_policy.yaml)，改它不用碰
Python，每次提问重新读，不用重启。

`RoutingPolicy` 在四个位置注入，每个位置对应 route.py 里的一个假设：

| 注入点 | 原本的假设 | 策略提供 |
|---|---|---|
| `periods_in()` | 报告期就是 4 位年份 | `periods.patterns` —— `FY24` 归一成 `2024`，好和目录对上 |
| `weight_for()` | 每个目录同等值得看 | `directories.weights` —— 加分，并在提示词里标注 |
| `prompt_block()` | 只有问题本身的词 | `directories.scopes` / `instructions` —— 纯提示 |
| `is_excluded()` / `alias_terms()` | 问题里有什么词就用什么 | `directories.exclude` 硬过滤、`aliases` 同义词扩展 |

**两条设计红线**

1. **空策略 = 内置行为。** `RoutingPolicy()` 对所有方法都返回和改造前一样的
   结果。删掉配置文件，路由行为和这个功能不存在时完全一致 ——
   `tests/test_policy.py` 逐条断言了这一点，因为这是这个功能敢上线的唯一理由。
2. **配置排序，不设闸门。** 权重和业务域只是让模型更倾向于某些目录；
   只有 `exclude` 真的把目录从候选里拿掉，因为 `_drafts/` 被搜到永远不是好事。
   而且 `exclude` 按**路径整段**匹配（不是子串），所以 `exclude: [draft]`
   不会误伤 `drafting-guidelines/`。

**单语料覆盖**：`corpora:` 段按语料**显示名**写（配置文件是给人看的），
`MultiNavigator` 构造时一次性绑定到内部 id。覆盖项与全局**合并**而不是替换 ——
否则每个语料都要把整份策略抄一遍，正是全局层要避免的漂移。

**坏配置不致命**：YAML 写错会抛 `PolicyError`，由 `nav/route._bind_policy`
接住、退回默认行为、打一行 stderr，并记进 `results/logs/errors.jsonl`
（`where: policy.load`）。服务器不会因此起不来。

```bash
$PY -m nav.route index/demo "..." --show-policy        # 看当前生效的策略
$PY -m nav.route index/demo "..." --policy /path/x.yaml --corpus 友邦保险
SUPERINDEX_ROUTING_POLICY=/path/x.yaml $PY -m nav.route ...   # 换文件位置
```

### 5. 示例问题生成 `suggest.py`

输入框上方那排 chips 不是手写的 —— 手写示例只对写它的那份语料成立，换一份语料
就是在教用户问一个索引答不上来的问题，看起来像**检索失败**，其实是**示例失败**。
所以示例问题从语料自身推导，用的是路由已经在看的同一批证据：语料描述、目录主题、
文件摘要、章节标题。**章节标题是最强的信号** —— 「股息」「新业务价值」「分市场表现」
字面上就是可以被提问的东西。

- 每个语料一次调用，结果缓存在注册表里，和语料摘要共用同一个内容指纹做失效判断。
- **任何失败都返回空列表**，UI 直接不显示 chips。宁可没有示例，也不要一个错的示例：
  错的会浪费用户的第一次点击，让索引显得比实际更差。
- 生成逻辑只写临时目录的测试见 `tests/test_suggest.py`（75 断言）。

## 实测结果

**功能验证**（16 文件测试语料，带摘要）

| 问题 | 命中目录 | 命中章节 | 结果 |
|---|---|---|---|
| 友邦保险 2024 年全年每股股息？ | 友邦保险/2024/annual/ | 股息 | ✅ 150.72 港仙 |
| 中国平安 2023 上半年中期股息？ | 中国平安/2023/interim/ | 股息 | ✅ 36.95 港仙 |
| 三家上市险企 2024 VONB 对比？ | 行业汇总/ | 概览 | ✅ 找到对比表 |
| 中国太保 2025 全年股息？ | 中国太保/2025/annual/ | 股息 | ✅ 142.98 港仙 |
| 友邦 2023 泰国市场 VONB？ | 友邦保险/2023/annual/ | 泰国 | ✅ |

4/4 正确，年份全部匹配正确。

**规模验证**（144 文件 / 229 目录 / 2,304 节点，无摘要）

- 结构索引 0.09 秒建成
- 完整目录树 listing 8.9K 字符
- 全树路径与逐层下钻路径都能正确定位到 `公司07/2023/interim/公司07_2023_interim.md`

## 多语料：`registry.py`

上面的 CLI 是一次性建索引 + 查询。要让 UI 驱动（注册目录、看状态、自动跟进
文件变化），用 `nav/registry.py`。

```python
from nav.registry import Registry

reg = Registry()                     # 索引集中存 index/（见 index/README.md）
c = reg.add("/data/reports", name="年报库")        # 每个语料一个独立索引目录
reg.index_async(c.id)                             # 后台建索引，状态可轮询
reg.start_watcher(interval=30)                    # 轮询文件变化并自动增量重建

nav = reg.navigator([c.id])                       # 或多语料：reg.navigator()
res = nav.run("2024 年全年股息是多少？")
```

**投放区**：`sync_data_root()` 会把 `data/` 下每个直接子目录自动注册并索引，
所以「往 `data/` 丢一个目录」就是完整的工作流。watcher 每轮也会调用它，
新目录自动进来。

**多语料查询**走 `MultiNavigator`：把 N 个语料的 manifest 合并成一棵路由树，
每个语料成为顶层的一个伪目录。这样第 1 级仍然是**一次调用**，而且模型能跨语料
比较分支，而不是分别路由再猜哪个结果更好。

```python
from nav.route import MultiNavigator, build_context, answer_prompt
nav = MultiNavigator([("id1", "/path/idx1", "语料一", "摘要")])
res = nav.run(question)
context, sources = build_context(res, nav)        # 按相关性截断，预算内取满
```

### 设计要点

| | |
|---|---|
| **一个语料一个索引** | 互不污染；某个语料失败不影响其他；删除就是删目录 |
| **增量重建** | `scan(previous=...)` 跳过 size+mtime 未变的文件，**不重新抽取**。否则 watcher 每次轮询都会把每个 PDF 重发给 Azure DI |
| **只补缺失的摘要** | 有描述的跳过，所以改一个文件只花一个文件的摘要钱 |
| **沿用语料自身设置** | 注册时关掉文件描述，watcher 就不会偷偷开始调 LLM |
| **拒绝项目内目录** | 否则会递归进 `index/`，边写索引边读自己 |
| **索引与源文件分离** | 注册 `/data/reports` 只往 `index/corpora/<id>/` 写，源目录只读 |
| **目录消失 → error** | 带可读错误信息，而不是静默返回空结果 |

## 调试日志：`debuglog.py`

每次提问都会记一条结构化记录，回答错了能事后查：

```python
from nav.debuglog import QueryTrace
t = QueryTrace(question, scope_names, model=...)
t.route_step(level="dir", where=..., picked=[...])
t.sources([...])
t.finish(answer)          # 正常
t.abort("no files located")   # 跑完但没找到（只写 queries）
t.fail(exc, stage="ask")      # 抛异常（queries + errors，id 关联）
```

写到 `results/logs/queries.jsonl` 与 `errors.jsonl`，用
`python scripts/07_logs.py --id <id>` 还原单条全过程。
日志写入失败只打一行 stderr，**绝不影响主流程**。

## 已知限制

- **章节级摘要需要 LLM**，上千文件的语料是一次性成本。没有摘要时章节定位
  明显变弱（回退到词元匹配）。
- **PDF 走 flash**。没有 Azure DI 时用 PageIndex 的离线 `flash` 引擎，
  从字号/位置/排版统计推导标题 —— 不用 LLM、不用 Azure。
  顺序：Azure DI → flash → 每页一节点 → 书签。
  需要的话接 `pageindex` 的 flash 模式补上。
- **目录结构本身的质量决定上限**。如果语料是一坨平铺的几千个文件（没有子目录），
  第 1 级的目录树退化成一次列几千个文件名 —— 这时应先做一层目录治理，
  或改用 `_descend_dirs` 的批处理策略。
- **回退用词元匹配**，对同义词本身无能为力（问「寿险」不会命中「人身险」）。
  能枚举出来的同义词用 `aliases:` 解决；要泛化就得把回退换成 embedding 预筛，
  但那就把相似度问题引进来了 —— 权衡后当前选择保持确定性。
- **策略只是「更倾向」，不是「会推理」**。权重和业务域是在模型已经看到候选之后
  施加的偏向，救不回答案本身就不存在的问题；而写错的 `exclude` 会真的藏掉目录 ——
  所以排除按路径整段匹配（不是子串），`exclude: [draft]` 不会误伤
  `drafting-guidelines/`。
