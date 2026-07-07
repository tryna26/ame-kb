# ame-kb

最小可用的知识图谱构建器（V1）：扫描本地文档 → LLM 抽取实体与关系 → 存入 MySQL（三张表）→ 按名查询与一跳关系。

三表设计借鉴自 oceanai_site：
- `kg_domain_entity`：类型定义层（schema），V1 由固定 schema 写入种子
- `kg_graph_node`：实例节点
- `kg_graph_edge`：实例边

## 环境要求

- Python 3.9+
- 一个 MySQL 8.0 实例（本地或云端，如阿里云 RDS）
- 一个 OpenAI 兼容的 LLM endpoint（key + base_url + model）

## 安装

```bash
cd ame-kb
python3 -m pip install -e .
```

## 配置

复制 `.env.example` 为 `.env` 并填写：

```
LLM_API_KEY=...                 # LLM key
LLM_BASE_URL=https://.../v1     # OpenAI 兼容 endpoint（SDK 会在其后拼 /chat/completions）
LLM_MODEL=...                   # 模型名

MYSQL_DSN=mysql+pymysql://user:pass@host:3306/kb?charset=utf8mb4
SOURCE_DIR=./data               # 待扫描的文档目录

GRAPH_NO=default                # 逻辑图谱标识（V1 固定）
GRAPH_VERSION=1
```

`.env` 已在 `.gitignore` 中，不会被提交。

### 数据库权限提示

首次 `init-db` 需要账号对目标库有 `CREATE / INSERT / UPDATE / DELETE` 权限。
阿里云 RDS 的默认账号常被裁剪权限，需在控制台「账号管理」给目标库授「读写」。

## 使用

```bash
# 1. 探测连通性 + 建三张表 + 写入固定 schema 种子（幂等，可重复跑）
python3 -m ame_kb.cli init-db

# 2. 干跑：抽取并打印结果，不写库（用于检查抽取质量）
python3 -m ame_kb.cli ingest --dry-run

# 3. 真正入库
python3 -m ame_kb.cli ingest

# 4. 按名查实体 + 一跳直接关系
python3 -m ame_kb.cli query "Ada"

# 辅助：查看某 type/name 的业务键（graph_node_no）
python3 -m ame_kb.cli node-no Person "Ada Lovelace"
```

把你的 `.md` / `.txt` 文件放进 `SOURCE_DIR`（默认 `./data`）即可被扫描抽取。

## 抽取机制

- **固定 schema（V1）**：节点类型与边类型在 `src/ame_kb/schema.py` 中手写：
  - 节点：`Person / Organization / Project / Document / Concept`
  - 边：`works_for / authored / part_of / mentions / related_to`
- **填空式抽取**：prompt（`src/ame_kb/prompts/extract_v1.txt`）把固定 schema 注入，
  让 LLM 只做「填空」——只能输出上述类型，不能发明新类型。
- **行号来源**：文档每行加 `[N] ` 前缀（借鉴 oceanai `addLineNumbers`），
  LLM 在 `source` 里回填行范围，便于溯源。
- **入库校验**：越界的 `type` / `label` / 不合法端点会被丢弃（`extract.py: validate`）。
- **去重**：`graph_node_no = type:slug(name)`，精确同名在写入时天然合并（upsert）。

## 数据库结构

见 `sql/001_init.sql`。三张表均带 `graph_no / graph_version`，为后续多版本演进预留。

## 测试

```bash
python3 -m pytest tests/ -q
```

离线测试（不连 DB/LLM）覆盖：schema 校验、node_no 去重、JSON 解析容错。

## 后续版本（规划）

- V2：PDF/网页接入 + 边加 `EXTRACTED / INFERRED` 置信标签 + 内容 hash 增量
- V3：实体去重与融合（向量召回 + LLM 判同）+ 半/全动态 schema
- V4：异步 pipeline + 图可视化 + 对外 MCP server（给 AI agent 当记忆层）
