# ame-kb 路线图（Knowledge Recall / Agent Memory）

本项目定位不是单纯的"知识图谱构建系统"，而是 **Knowledge Recall / Agent Memory**：
图谱的价值不只在存节点和边，而在服务自然语言召回——

```
用户问题
  -> 搜索节点/边
  -> 找到图锚点
  -> 边命中补端点
  -> MySQL 精确展开邻居（多跳）
  -> Ref 拉原文
  -> 返回给 AI agent
```

主参考链路是 general_recall 的混合召回：`RetrieveNodesWithExpansion` 先并行召回节点和边，
再拼候选池、重排、展开邻居。

## 参考项目根路径

- **general_recall（主参考）**：`/Users/bytedance/GolandProjects/general_recall`
- **本项目**：`/Users/bytedance/PycharmProjects/ame-kb`

## 总体功能地图（7 模块）

1. **Ingest** —— 数据接入：md/txt/PDF/html/URL/code
2. **Normalize** —— 异构源归一：统一成 Document + Line + Chunk
3. **Extraction** —— 抽取：实体、关系、Ref、description
4. **Resolution** —— 实体对齐去重：同名、同义、字段融合
5. **Storage** —— 图存储：MySQL 节点表、边表、schema 表、文档表、chunk 表、索引表
6. **Recall/Search** —— 混合召回：节点/边/chunk 索引、候选池、重排、邻居展开、多跳、多 query、重试阶梯（**核心模块**）
7. **UI/API/MCP** —— 展示、接口、Agent 记忆层

---

## 版本进度总览

| 版本 | 主题 | 状态 |
|---|---|---|
| V1 | 最小可用闭环（ingest → extract → MySQL → 按名查询） | ✅ 已交付 |
| V2 | 多源接入（pdf/html）+ 半动态属性 + hash 增量 + 边置信 | ✅ 已交付 |
| V3 | 全混合召回：FULLTEXT(ngram) + 向量 cosine + RRF + 10 步召回 + Ref 取原文 | ✅ 已交付 |
| V4 | 可切换搜索后端 + doc chunk + 多 query + 多跳 + 重试阶梯 + URL 接入 | ✅ 已交付 |
| V5 | 实体对齐与融合 + 动态 schema | 🔴 计划 |
| V6 | 规模化 pipeline + Agent 接口(REST/MCP) + UI | 🔴 计划 |
| V7 | 高级能力（多租户 / schema 演化 / 图算法 / 图库迁移） | 🔴 可选 |

---

## V1 — 最小可用闭环 ✅

**目标**：证明"文档进来、图建出来、能按名查到并查关系"。

| 能力 | 本项目文件 | 借鉴 |
|---|---|---|
| 本地 md/txt 按行号读取 | `src/ame_kb/ingest.py` | oceanai `addLineNumbers` |
| 固定 schema + LLM 单步抽取 | `src/ame_kb/extract.py`、`schema.py`、`prompts/extract_v1.txt` | oceanai 抽取 prompt |
| MySQL 三表（domain_entity/node/edge） | `sql/001_init.sql`、`models.py` | oceanai_site 三表设计 |
| node_no=type:slug(name) 去重 | `src/ame_kb/store.py` | — |
| 按名查询 + 一跳关系 | `src/ame_kb/query.py` | — |

---

## V2 — 多源接入 + 半动态属性 ✅

| 能力 | 本项目文件 | 借鉴 |
|---|---|---|
| md/txt/pdf/html loader | `src/ame_kb/sources.py` | oceanai「异构源归一成 doc」；`pypdf` / `trafilatura` |
| 半动态属性（properties JSON 兜底） | `extract.py: validate`、`prompts/extract_v2.txt` | `general_recall/core/entity/byterag_store.go:642` extractRAGTextFromProperties |
| 内容 hash 增量跳过 | `store.py`、`sql/002_doc_version.sql` | oceanai `saveIfChanged` |
| 边置信标签 EXTRACTED/INFERRED/AMBIGUOUS | `extract.py` | Graphify 边 provenance |

---

## V3 — 全混合召回 ✅

**目标**：从"能按名查"升级为"自然语言能搜到并查原文"。

| 能力 | 本项目文件 | 借鉴（general_recall） |
|---|---|---|
| 10 步混合召回 | `src/ame_kb/recall.py` | `domain/service/recall/node_retriever.go:261`（并行召回 :292 / 端点入池 :343 / 池重排 :364 / 邻居展开 :389） |
| RRF 融合 | `src/ame_kb/rrf.py` | `utils/rrf.go:20`；`core/reranker/rrf_reranker.go` |
| searchable_text = name+description+properties | `src/ame_kb/searchindex.py` | `core/entity/byterag_store.go:346,362` |
| Ref → 原文行（范围展开、相邻≤3合并、±window） | `recall.py`（step 9） | `domain/service/recall/general_retriever.go:580`；`application/recall/method/general.go` + `doc_lines.go` |
| FULLTEXT(ngram) + 向量 JSON + description 列 | `sql/003_recall.sql`、`models.py` | — |
| embedding 客户端（批量、独立 endpoint） | `src/ame_kb/embed.py` | `extract.py` OpenAI client 写法 |

**10 步召回流程**（V3 单 query / 单跳，V4 在此基础上扩展）：

```
recall(query):
  1. 搜节点索引（FULLTEXT + 向量，各带 min-score，RRF 融合）
  2. 搜边索引
  3. candidate_node_no = 节点命中 ∪ 边 source ∪ 边 target（去重保序）
  4. 候选池限定集内对原 query 重排
  5. 取 topK seeds
  6. MySQL 查 seeds 一跳边
  7. 批量取邻居节点详情（避免 N+1）
  8. 邻居按原 query 重排（neighborTopK）
  9. 解析 Ref，读取原文行
 10. 返回 seeds + neighbors + evidence lines
```

---

## V4 — 可切换后端 + chunk + 多 query + 多跳 + 重试阶梯 ✅

**目标**：从"能搜"变成"相对搜得干净"，并把向量后端抽象成可换组件。

### 已交付能力

| 条目 | 本项目文件 | 借鉴地址 |
|---|---|---|
| **HybridIndex 后端抽象**（search/upsert/delete） | `src/ame_kb/searchbackend/base.py`、`factory.py` | `general_recall/domain/service/recall/ports.go`（IByteRAGRetriever 端口范式） |
| **MySQL 后端**（FULLTEXT ngram + JSON cosine + RRF，默认/参考实现） | `searchbackend/mysql.py` | V3 recall 通道下沉；`byterag_store.go` |
| **Redis 后端**（RediSearch FT + VECTOR KNN + RRF，`SEARCH_BACKEND=redis`） | `searchbackend/redis.py` | Redis Stack `FT.CREATE ... VECTOR`；TAG 过滤范式 `core/entity/filter_dsl_test.go` |
| **纯向量数学**（cosine，避免 import 环） | `src/ame_kb/vecmath.py` | — |
| **doc chunk 切片**（滑窗+重叠，记录 line_start/end） | `src/ame_kb/chunker.py` | `core/entity/sync_spec_doc_chunk.go:130` splitTextIntoChunks |
| **doc chunk 持久化 + 索引**（按 doc sha256 增量，delete-by-filter 重建） | `src/ame_kb/docchunk.py`、`sql/004_v4.sql`（kg_doc_chunk） | `byterag_store.go:204` DocChunkUpToDate、`:522` delete-by-filter |
| **doc_chunk 第三通道兜底**（不受 graph_scope 约束） | `recall.py: _doc_chunk_channel` | `skills/knowledge-recall/references/sources.md:100`（④b） |
| **多 query 改写 + RRF 合并** | `src/ame_kb/queryexpand.py` | `application/recall/method/rag_retrieve.go:208`；`sources.md:80` |
| **多跳邻居展开**（frontier BFS + visited 防环 + 缺口驱动，≤N 跳） | `recall.py: _expand_neighbors` | `sources.md:134`（③b）；`domain/service/recall/graph_explorer.go:202` traversalRetrieve + `:691` markExploredEntityIDs |
| **重试阶梯**（高阈值 → 降分 → 去 workspace；graph_scope 永不放宽） | `recall.py: _retry_ladder / RecallTier` | `sources.md:86-97` |
| **graph_scope / workspace_scope**（SearchFilters；workspace 暂 dormant） | `searchbackend/base.py: SearchFilters` | `sources.md:31-39`；`core/entity/retrieve_params.go` |
| **URL 接入**（单 URL / manifest，记录 origin_url 溯源） | `cli.py: ingest-url`、`ingest.py: load_url/read_url_list` | `application/recall/method/general.go:164`（origin_url 溯源思路） |
| **reindex**（节点/边/chunk 全量重建索引） | `src/ame_kb/reindex.py` | — |

### V4 配置项（`.env` / `config.py`）

```
SEARCH_BACKEND=mysql|redis      # 搜索后端切换（两者 RRF 融合结构一致）
REDIS_URL / REDIS_INDEX_PREFIX  # RediSearch 连接（仅 db 0 可建索引，前缀隔离 key）
EMBED_DIM=0                     # Redis VECTOR 维度，0=首次自动探测
CHUNK_SIZE=800 / CHUNK_OVERLAP=200 / DOC_CHUNK_TOPK=10
RECALL_MAX_QUERIES=1            # 1=仅原 query（等价 V3）；>1 触发 LLM 改写
RECALL_MAX_HOPS=1               # 1=V3 单跳；2=两跳…
RECALL_MIN_RESULTS=0            # 0=单趟（V3）；>0 触发重试阶梯
RETRY_STRICT_TEXT/EMBEDDING=0.8 # 阶梯首档阈值
```

> **默认全部退化到 V3 行为**（单 query / 单跳 / 无阶梯 / mysql 后端），
> 逐旋钮开启即可，保证向后兼容。

### V4 表 / 迁移

- `kg_doc_chunk`：`doc_no, chunk_no, chunk_index, content, origin_url, file_path, line_start, line_end, sha256, workspace_id`
- `kg_doc` 加 `origin_url` / `workspace_id`（后者 dormant，V7 多租户预留）
- `kg_search_index` 启用 `object_type=DOC_CHUNK`；加 `workspace_id`（dormant）

### V4 验收标准（已满足）

1. ✅ 同一问题能同时从 节点/边/doc_chunk 找证据
2. ✅ 多 query 能合并排序，边能补回纯向量搜不到的节点
3. ✅ 多跳能从一跳邻居发现新锚点、visited 不成环、可配置最大跳数
4. ✅ 命中不足时重试阶梯生效，graph_scope 不被放宽
5. ✅ Ref 原文覆盖率可观测
6. ✅ md/txt/PDF/HTML/URL 均能进图并被搜到
7. ✅ `SEARCH_BACKEND=redis|mysql` 可切换，RRF 融合结构一致

> **未做（留给后续）**：代码源 tree-sitter 结构化抽取（V4 已接 URL，但 code 源仍未接，见 V5/V6 补充）。

---

## V5 — 实体对齐与融合 + 动态 schema 🔴

**目标**：从"抽出一堆点"变成"稳定的知识实体"。向量在这里从"搜索入口"升级为"实体治理基建"。

### 功能范围

```
Resolution:
- normalized_name 精确查重（V3 slug 升级）
- alias 匹配
- HybridIndex 向量召回候选（复用 V4 后端，无需新组件）
- LLM 判定 same / related / different
- 字段并集融合 + Ref 合并
- 边重新挂到 canonical entity

Schema:
- 固定 schema -> 半动态 -> 动态；core fields 稳定，properties 承接长尾
- schema 字段归一化
- 代码源 tree-sitter（Function/Class/Module 节点，EXTRACTED 边）
```

### 实体融合流程

```
new_entity:
  1. normalized_name 精确查重
  2. alias 查重
  3. 向量找相似候选（HybridIndex.search）
  4. LLM 判断 same/related/different
  5. same -> merge（fields 并集、Ref 合并）
  6. 边改挂 canonical entity
```

### 借鉴地址

| 条目 | 借鉴 |
|---|---|
| slug/normalized_name 升级 | 本项目 `store.py`（现有 slug） |
| alias 匹配 | `general_recall/domain/service/knowledge_graph/query_test.go` |
| 向量找相似候选 | 复用 V4 `HybridIndex`；范式 `graph_explorer.go:340` ragRetrieveEntityIDs |
| LLM 判同 | 本项目 `extract.py` LLM 调用模式（新增判定 prompt） |
| 字段并集 / Ref 合并 | 本项目 `store.py: _merge_ref`（扩到字段并集） |
| 边改挂 canonical | `node_retriever.go` 端点映射逻辑 |
| 代码源 tree-sitter | 外部 `github.com/tree-sitter/tree-sitter` + `py-tree-sitter` |
| 动态 schema | 本项目 `schema.py`；`core/entity/retrieve_params.go` |

### V5 表 / 基建

- `kg_entity_alias`：`canonical_node_no, alias, source`
- `kg_merge_log`：合并/回滚审计（为 V6 人工修正铺路）
- 向量仍走 RediSearch（V4 `HybridIndex`）；若数据涨到百万级或需事务性合并/回滚，评估切 **pgvector** 实现（接口已就绪，改动局部）

### V5 验收标准

1. 同一实体跨多文档出现不重复成多个主实体
2. 字段能融合，别名能搜到主实体
3. 合并错误可回滚
4. schema 从固定平滑过渡到半/动态，core fields 不漂

---

## V6 — 规模化 Pipeline + Agent 接口 + UI 🔴

**目标**：从单机脚本变成可长期运行的知识服务，Agent 真正能调用这套记忆层。

### 功能范围

```
Pipeline:
- 异步 ingest / extraction / indexing
- 进度可观测、失败重试、checkpoint、增量更新

API:
- REST API
- MCP server（Agent memory interface）

UI:
- 图可视化、搜索结果解释、原文证据展示、节点/边人工编辑（配合 V5 merge_log 回滚）
```

### 多跳策略（沿用 V4 实现，规模化时强化）

不做默认全图 BFS，按 knowledge-recall 方式：先跑一跳 → 看子问题证据缺口 →
从邻居挑相关 NodeName 当新 query → 再召回 → 维护 visited → 最多额外 2 跳。
（V4 已实现 `_expand_neighbors`，V6 增加 session 日志与可观测。）

### 借鉴地址

| 条目 | 借鉴 |
|---|---|
| 异步 pipeline / 同步状态 | `general_recall/core/entity/sync_job.go`、`sync_spec.go`、`sync_specs.go` |
| checkpoint / 增量 skip | `byterag_store.go:204` DocChunkUpToDate |
| recall session 日志 | `domain/service/recall/session.go`、`logger.go` |
| REST API | 本项目 `cli.py` → 新增 FastAPI 层 |
| MCP server | 参考 `skills/knowledge-recall/` 封装范式 |
| UI 图可视化 | 外部 Cytoscape.js / sigma.js / vis-network |

### V6 基建

- **Redis**：任务队列 / 进度 / checkpoint（与向量索引**分库或分实例**，避免资源竞争与驱逐）
- 任务表：`kg_task` / `kg_pipeline_run` / `kg_pipeline_step`（参考 `sync_job.go`）
- Cron：增量扫描、失败任务恢复、索引补偿

### V6 验收标准

1. 大批文档可后台处理，失败可重试、可断点续跑
2. 搜索结果可解释（seeds / 邻居 / 多跳路径 / 证据来源）
3. Agent 可通过 REST / MCP 调用召回
4. 图可视化能辅助人工修正实体/边

---

## V7 — 高级能力（可选，V6 跑稳后）🔴

| 条目 | 借鉴 |
|---|---|
| schema 自动演化 | `general_recall/core/entity/sync_spec.go`（spec 注册机制） |
| 多图版本继承 | 本项目 `graph_no / graph_version`（已有种子字段） |
| 权限隔离 / 多租户 | `workspace_id`（V4 已建 dormant 字段）；`sources.md:32` workspace_ids |
| 社区发现 / 图算法 | 外部 networkx / Neo4j GDS |
| 图数据库迁移 | 外部 Neo4j，**仅在 MySQL 边表频繁复杂路径 / 深层遍历明显卡顿时才考虑，非默认** |

---

## 向量后端与 Redis 使用总览

| 版本 | Redis 角色 | 说明 / 借鉴 |
|---|---|---|
| V3 | 无（向量存 MySQL JSON + 内存 cosine） | 小数据量够用 |
| V4 | **向量库（RediSearch，可选）** | 经 `HybridIndex` 抽象接入；`SEARCH_BACKEND=redis`；独立 db0、建议 noeviction+AOF |
| V5 | 向量库（实体去重复用同一索引） | 数据量 / 事务需求触顶时评估切 pgvector |
| V6 | **+ 任务队列 / 进度 / checkpoint** | 与向量索引分库分实例 |

> **核心设计原则**：向量/搜索后端始终藏在 `HybridIndex` 接口（`search / upsert / delete`）之后，
> MySQL / RediSearch / pgvector 三实现可切换，业务层（`recall.py`、融合逻辑、实体融合）
> 永不直接依赖具体后端。接口抽象范式借鉴 `general_recall/domain/service/recall/ports.go`。
>
> Redis 当向量库是**性价比选择**（零新增组件、内存低延迟、原生标签过滤），不是"最强向量库"；
> 真正逼迫换 pgvector 的信号是：向量涨到百万级 RAM 吃紧，或 V5 需要频繁事务性合并 / 回滚。
