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

可索引的扩展名：`.md` `.markdown` `.txt` `.pdf`

## 入库规则（**注意**）

仓库里自带一份小的合成语料，所以**新克隆的人开箱即可演示**：

```
data/中国太保/   data/中国平安/   data/友邦保险/   data/行业汇总/
```

**只有 PDF 被 `.gitignore` 排除**（AIA 真实报告 27 MB，用
`data/aia_reports/download.sh` 拉取）。其余内容会**正常入库**。

> ⚠️ **不要把私密文档放进 `data/`** —— 它会被提交。
> 私密或体积大的语料请放在项目外，用 Web UI 的「添加目录」注册
> （注册表记绝对路径，索引照样写到 `index/`）。

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
