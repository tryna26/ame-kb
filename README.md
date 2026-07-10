# ame-kb

面向 AI Agent 的 Knowledge Recall / Memory 服务内核（当前 V6.1）：多源文档 → LLM 抽取实体与关系 → MySQL 图存储 → 全文/向量混合召回 → 多图谱版本化增量构建。

目前已交付 V1～V5，以及 V6.1 的图谱注册、文件清单、版本继承和安全的请求级图谱上下文。异步 Pipeline、REST/MCP 和 UI 仍在后续 V6 阶段。

核心表设计借鉴自 oceanai_site：
- `kg_domain_entity`：类型定义层（schema），V1 由固定 schema 写入种子
- `kg_graph_node`：实例节点
- `kg_graph_edge`：实例边
- `kg_doc_version`（V2）：文档内容 SHA-256 快照，用于增量跳过未变文档
- `kg_graph` / `kg_graph_file`（V6.1）：图谱版本生命周期和跨版本文件清单

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

GRAPH_NO=default                # 默认图谱；CLI --graph-no 可按请求覆盖
GRAPH_VERSION=1                # 默认版本；托管图谱不指定时只解析最新 ACTIVE 版本
```

`.env` 已在 `.gitignore` 中，不会被提交。

### 数据库权限提示

首次 `init-db` 需要账号对目标库有 `CREATE / INSERT / UPDATE / DELETE` 权限。
阿里云 RDS 的默认账号常被裁剪权限，需在控制台「账号管理」给目标库授「读写」。

## 使用

```bash
# 1. 探测连通性 + 建所有表（sql/*.sql 按序执行）+ 写入固定 schema 种子（幂等，可重复跑）
python3 -m ame_kb.cli init-db

# 2. 干跑：抽取并打印结果，不写库、不记 hash（用于检查抽取质量）
python3 -m ame_kb.cli ingest --dry-run

# 3. 真正入库（增量：内容未变的文档自动跳过）
python3 -m ame_kb.cli ingest

# 3b. 强制重抽（忽略 hash）
python3 -m ame_kb.cli ingest --force

# 4. 按名查实体 + 一跳直接关系
python3 -m ame_kb.cli query "Ada"

# 5. 跨文档实体融合（V5）：向量/全文找相似候选 → LLM 判同 → 合并
python3 -m ame_kb.cli resolve --dry-run   # 只打印判同结果，不写库
python3 -m ame_kb.cli resolve             # 真正融合（记别名 + 可回滚审计）
python3 -m ame_kb.cli resolve --type Person   # 只融合某类型

# 5b. 回滚某次合并（按 merge_id，从审计快照还原）
python3 -m ame_kb.cli rollback-merge <merge_id>

# 5c. 查看某主实体的别名（验证「别名搜到主实体」）
python3 -m ame_kb.cli alias-of "Ada Lovelace"

# 辅助：查看某 type/name 的业务键（graph_node_no）
python3 -m ame_kb.cli node-no Person "Ada Lovelace"
```

### 多图谱版本化工作流（V6.1）

```bash
# 注册托管图谱，记录输出的 graph_no（例如 graph_a1b2c3d4）
python3 -m ame_kb.cli create-graph --name "Agent Memory"

# 维护该图谱的文件清单
python3 -m ame_kb.cli --graph-no graph_a1b2c3d4 add-file ./data
python3 -m ame_kb.cli --graph-no graph_a1b2c3d4 list-files

# 首次填充 v1；后续运行自动派生 vN+1
python3 -m ame_kb.cli --graph-no graph_a1b2c3d4 ingest

# 默认只查询最新 ACTIVE 版本；也可显式固定历史版本
python3 -m ame_kb.cli --graph-no graph_a1b2c3d4 search "Ada 做过什么？"
python3 -m ame_kb.cli --graph-no graph_a1b2c3d4 --graph-version 1 search "Ada 做过什么？"
```

派生新版本时，未变化文档的节点、边、原文和搜索索引会直接继承，embedding 不会重新计算；只有新增/变化文档调用 LLM。构建中的 `BUILDING` 版本不会成为默认查询版本，中断构建可在下一次 `ingest` 时续跑。图谱选择通过请求/任务级 `GraphContext` 隔离，不修改进程级环境变量。

把你的 `.md` / `.txt` / `.pdf` / `.html` 文件放进 `SOURCE_DIR`（默认 `./data`）即可被扫描抽取。

## 抽取机制

- **多源接入（V2）**：`src/ame_kb/sources.py` 按后缀分发 loader，把 md/txt/pdf/html 统一转成纯文本 `Document`（借鉴 oceanai「异构源先归一化成 doc 再抽」）。PDF 用 `pypdf`，网页正文用 `trafilatura`。下游抽取路径与源格式无关。
- **固定类型 + 半动态属性（V2）→ 半动态 schema（V5）**：节点/边**类型**的种子仍在 `src/ame_kb/schema.py` 手写：
  - 节点：`Person / Organization / Project / Document / Concept`
  - 边：`works_for / authored / part_of / mentions / related_to`
  - V5 起，抽取时的「有效 schema」= 种子类型 ∪ `kg_domain_entity` 中已注册的类型（`schema.load_types_from_db`）。开启 `SCHEMA_DYNAMIC=true` 后，LLM 提议的新节点类型不再直接丢弃，而是入库前自动写入 `kg_domain_entity` 注册（`schema.register_type`），后续运行即生效。默认关闭（保守）。
  - 但节点**属性**放开：schema 声明的字段之外，LLM 抽到的额外属性也全部保留进 `properties` JSON 兜底（不再像 V1 那样按白名单丢弃）。
- **填空式抽取**：prompt（`src/ame_kb/prompts/extract_v2.txt`）把固定类型注入，让 LLM 只做「填空」——只能输出上述类型，不能发明新类型。
- **边置信标签（V2）**：每条边带 `confidence ∈ {EXTRACTED, INFERRED, AMBIGUOUS}`（借鉴 Graphify），存入 `kg_graph_edge.properties`。LLM 抽取默认 `INFERRED`；入库前校验非法置信值会被丢弃。
- **行号来源**：文档每行加 `[N] ` 前缀（借鉴 oceanai `addLineNumbers`），LLM 在 `source` 里回填行范围，便于溯源。
- **入库校验**：越界的 `type` / `label` / 不合法端点 / 非法 `confidence` 会被丢弃（`extract.py: validate`）。
- **去重**：`graph_node_no = type:slug(name)`，精确同名在写入时天然合并（upsert）。跨文档「同实体不同写法」（如 `Ada` vs `Ada Lovelace`）由 V5 的 `resolve` 融合处理。
- **增量（V2）**：入库前算 `sha256(正文)` 与 `kg_doc_version` 中最新 hash 比对，未变则跳过抽取（借鉴 oceanai `saveIfChanged`）。hash 在**成功入库后**才记录，抽取失败不会污染缓存。`--force` 可绕过。

## 实体融合（V5）

`resolve` 把跨文档指向同一现实实体、但名字不同的节点合并为一个 canonical 主实体（`src/ame_kb/resolve.py`）：

- **候选召回**：对每个节点用其 `name + description + properties` 走 `HybridIndex.search`（向量 KNN + 全文，RRF 融合，复用 V4 检索栈）拿相似候选，排除自身。范式借鉴 general_recall 的双通道候选召回。
- **LLM 判同**：`prompts/resolve_v5.txt` 让 LLM 判 `same / related / different`，只有 `same` 才合并；判 `same` 时返回「更完整的规范名」（借鉴 Graphiti「is_duplicate 时返回最完整全名」）。LLM/JSON 失败一律保守判 `different`，不冒进合并。
- **字段并集融合**：`properties` 主实体优先、被合并方补空缺；`ref`（溯源行号）按 doc_id 并集；`description` 取更完整的一个。
- **边重挂 canonical**：被合并节点上的边端点改挂到主实体（借鉴 general_recall `node_retriever`）。重挂后若与既有边 `edge_no` 撞键则合并并去重；塌成自环的边丢弃。
- **别名 + 可回滚审计**：被合并方的名字（及被改名时主实体的旧名）写入 `kg_entity_alias`，reindex 时并进主实体的 `searchable_text`，于是**别名也能搜到主实体**；每次合并把 loser 全量 + 边端点原值快照进 `kg_merge_log`，`rollback-merge <merge_id>` 可完整还原。

## 数据库结构

见 `sql/*.sql`（`init-db` 按序执行）。所有表均带 `graph_no / graph_version`，为后续多版本演进预留。V5 新增 `kg_entity_alias`（别名 → 主实体）与 `kg_merge_log`（合并审计快照，支持回滚）。

## 测试

```bash
python3 -m pytest tests/ -q
```

离线测试（不连 DB/LLM）覆盖：schema 校验、半动态属性保留、node_no 去重、JSON 解析容错、源分发、内容 hash、边置信默认值与校验。V5 追加实体融合/回滚全链路；V6.1 追加 ACTIVE 版本隔离、异步 GraphContext 隔离、增量版本投影，以及 MySQL/Redis 索引跨版本复制和 embedding 保留。

## 后续版本（规划）

- ~~V5：跨文档实体去重与融合（向量召回 + LLM 判同）+ 别名可回滚 + 半动态 schema~~（已完成）
- V6.1：多图谱版本化增量构建 + ACTIVE 版本隔离 + 请求级 GraphContext（已完成）
- V6.2～V6.4：规模化异步 pipeline（Redis 任务队列 / checkpoint）+ REST/MCP server + 图可视化 UI
- V7（可选）：schema 自动演化、多租户权限隔离、图数据库迁移（Neo4j）、社区发现
