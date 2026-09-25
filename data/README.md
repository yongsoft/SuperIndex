# data/ — 默认文档根目录（投放区）

**把目录丢进这里就会被自动索引**，不需要注册、不需要改配置。

```bash
cp -r ~/Documents/2024-filings   data/          # → 自动成为语料
mkdir data/contracts && cp *.pdf data/contracts/
```

服务启动时同步一次，之后 watcher 每 30 秒同步一次，所以运行中新增的目录
也会在一个轮询周期内自动进来。

## 规则

| | |
|---|---|
| **每个直接子目录 = 一个语料** | `data/中国太保/` → 语料「中国太保」 |
| **没有可索引文件的目录跳过** | 空目录、只有 `.sh` 的目录不会产生空语料 |
| **隐藏目录跳过** | `.git`、`.DS_Store` 之类 |
| **只读** | 索引写到 `index/corpora/<id>/`，**不会写回 data/** |
| **不入库** | `data/` 的内容是你的文档，不是仓库内容（见 `.gitignore`） |

可索引的扩展名：`.md` `.markdown` `.txt` `.pdf`

## 换位置

```bash
export SUPERINDEX_DATA_DIR=/Volumes/docs
```

## 索引 `data/` 之外的目录

用 Web UI 的「添加目录」—— 一个只读的目录浏览器。
注册表记的是**绝对路径**，所以外部目录和 `data/` 下的目录等价，
只是不会自动发现。

## 相关内容

- [`index/README.md`](../index/README.md) —— 索引存哪、为什么与源文件分离
- [`README.md`](../README.md) —— Web UI 用法
