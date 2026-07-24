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
| V5 | 实体对齐与融合 + 固定两层本体（属性半动态） | ✅ 核心已交付（代码源后移） |
| V6 | 版本化 + 规模化 pipeline + Agent 接口(REST/MCP) + UI | 🟡 V6.4 已交付（REST + Web UI；MCP 待做） |
| V7 | 高级能力（多租户 / schema 演化 / 图算法 / 图库迁移） | 🔴 可选 |

---

## V1 — 最小可用闭环 ✅

**目标**：证明"文档进来、图建出来、能按名查到并查关系"。

| 能力 | 本项目文件 | 借鉴 |
|---|---|---|
| 本地 md/txt 按行号读取 | `src/ame_kb/ingest.py` | oceanai `addLineNumbers` |
| 固定 schema + LLM 单步抽取 | `src/ame_kb/extract.py`、`schema.py`、`prompts/extract_v1.txt` | oceanai 抽取 prompt |
| MySQL 三表（domain_entity/node/edge） | `sql/schema.sql`、`models.py` | oceanai_site 三表设计 |
| node_no=type:spec:slug(name)（Asset）/ type:slug(name)（其它）去重 | `src/ame_kb/store.py` | — |
| 按名查询 + 一跳关系 | `src/ame_kb/query.py` | — |

---

## V2 — 多源接入 + 半动态属性 ✅

| 能力 | 本项目文件 | 借鉴 |
|---|---|---|
| md/txt/pdf/html loader | `src/ame_kb/sources.py` | oceanai「异构源归一成 doc」；`pypdf` / `trafilatura` |
| 半动态属性（properties JSON 兜底） | `extract.py: validate`、`prompts/extract_v2.txt` | `general_recall/core/entity/byterag_store.go:642` extractRAGTextFromProperties |
| 内容 hash 增量跳过 | `store.py`、`sql/schema.sql`（kg_doc_version） | oceanai `saveIfChanged` |
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
| FULLTEXT(ngram) + 向量 JSON + description 列 | `sql/schema.sql`（kg_search_index）、`models.py` | — |
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
| **doc chunk 持久化 + 索引**（按 doc sha256 增量，delete-by-filter 重建） | `src/ame_kb/docchunk.py`、`sql/schema.sql`（kg_doc_chunk） | `byterag_store.go:204` DocChunkUpToDate、`:522` delete-by-filter |
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

## V5 — 实体对齐与融合 + 固定两层本体（属性半动态）✅（核心验收已完成）

**目标**：从"抽出一堆点"变成"稳定的知识实体"。向量在这里从"搜索入口"升级为"实体治理基建"。

### 功能范围

```
Resolution:
- normalized_name 精确查重（V3 slug 升级，后续增强）
- alias 匹配
- HybridIndex 向量召回候选（复用 V4 后端，无需新组件）
- LLM 判定 same / related / different
- 字段并集融合 + Ref 合并
- 边重新挂到 canonical entity

本体（两层固定，非动态）:
- kg_graph_node 收敛为结构层（type 恒为 ENTITY）；kg_domain_entity 为本体层，
  type 存 Asset/Relation/Event/Behavior，Asset 再带 entity_spec 原型
  Mission/Solution/Implementation/ServiceInstance/Artifact，两层以 graph_node_no 关联
- 类型/原型是固定闭集（schema.py 纯静态，不查库、不可发明新类型）；
  只有节点/边的 properties 属性半动态（本体外的额外属性进 JSON 兜底）
- schema 字段归一化（后续增强）
- 代码源 tree-sitter（Function/Class/Module 节点，EXTRACTED 边，后续增强）
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
| 固定两层本体 | 本项目 `schema.py`（纯静态闭集）、`store.py: load_domain_map` |

### V5 表 / 基建

- `kg_entity_alias`：`canonical_node_no, alias, source`
- `kg_merge_log`：合并/回滚审计（为 V6 人工修正铺路）
- 向量仍走 RediSearch（V4 `HybridIndex`）；若数据涨到百万级或需事务性合并/回滚，评估切 **pgvector** 实现（接口已就绪，改动局部）

### V5 验收标准

1. ✅ 同一实体跨多文档出现不重复成多个主实体
2. ✅ 字段能融合，别名能搜到主实体
3. ✅ 合并错误可回滚
4. ✅ 本体两层固定（结构层 ENTITY + 本体层 Asset/Relation/Event/Behavior），core fields 不漂，属性走 properties 兜底

> `normalized_name` 独立字段、schema 字段归一化和代码源 tree-sitter 尚未实现，作为后续独立增强项，不阻塞上述 V5 核心验收。

---

## V6 — 版本化 + 规模化 Pipeline + Agent 接口 + UI 🟡

**目标**：从单机脚本变成可长期运行的知识服务，Agent 真正能调用这套记忆层。

### V6.1 — 多图谱版本化基础 ✅

- `kg_graph` 管理 `BUILDING / ACTIVE / FROZEN` 生命周期，默认查询只解析最新 `ACTIVE` 版本。
- `kg_graph_file` 保存跨版本文件清单；新增、删除、修改文件后自动派生 `vN+1`。
- 未变文档投影节点、边、Ref、原文、chunk、schema、alias 和搜索索引；只对变化/新增文档调用 LLM。
- MySQL / Redis 搜索后端均按 `graph_no + graph_version` 隔离，并原样复制已有 embedding。
- 中断留下的 `BUILDING` 版本可续跑；无变化默认不制造空版本。
- 图谱选择改为请求/任务级 `GraphContext`，为并发 REST/MCP 和 worker 消除进程环境变量竞争。

### V6.2 — 持久化后台 Pipeline ✅

- `kg_task / kg_pipeline_run / kg_pipeline_step` 分别记录任务、版本构建运行和逐文档 checkpoint。
- 同图谱活动任务通过 `active_key` 去重；worker 使用行锁原子认领，支持多 worker 竞争。
- 文档开始、成功、跳过、失败均独立提交 checkpoint；重试时已持久化文档由 `kg_doc.sha256` 跳过，不重复调用 LLM。
- 版本投影使用 `kg_graph.projection_done` 独立 checkpoint，覆盖“全部文档都变化、投影不产生 doc”的空投影续跑边界。
- 自动重试带预算和延迟；耗尽后进入 `FAILED`，可通过 `retry-task` 人工恢复并保留成功步骤。
- worker lease 过期自动回收，异常退出后任务可重新入队续跑。
- `task-status / list-tasks` 提供任务、run、文档步骤三级进度与错误观测。
- MySQL 保存权威状态；Redis 是可选唤醒队列，失败时回退数据库轮询，并与 RediSearch 分 DB/实例。

### V6.3 — Service 层 + REST API + 召回可观测 🟡（MCP/UI 待做）

- 新增 `service.py` 薄 facade：CLI 与 REST 共用，统一在每次调用处套请求/任务级 `graph_context`（解析 graph_no + 最新 ACTIVE 版本），底层 module 函数保持不动、行为不变。
- `recall` 增加 `RecallSession` trace（`trace=False` 默认零开销）：记录 embedding 可用性、改写后的多 query、每 query 池大小、重试 tier、逐跳邻居数、node/edge/doc_chunk 各通道命中量。CLI `search --trace` 打印，REST 默认返回，作为给 Agent 的召回解释。
- 新增 `api.py`（FastAPI，可选依赖 `pip install ame-kb[api]`）：`/search`、`/graphs`、`/graphs/{no}/entities`、`/graphs/{no}/ingest`、`/tasks`、`/tasks/{no}`、`/tasks/{no}/retry` 等；每请求经 `graph_context` 隔离，并发安全。CLI `serve` 启动。
- 测试 `test_v63`：service 图上下文解析/不泄漏、recall trace 字段、REST 端点（TestClient，service mock）。全量 85 passed。
- 待做：MCP server（Agent memory interface）、UI 图可视化。

### V6.4 — Web UI（单页静态）✅

- 新增 `src/ame_kb/web/index.html`：零构建、零前端依赖的单页应用，由现有 FastAPI 直接挂载（`GET /` 返回页面，`/ui` 挂静态目录），套壳调用 V6.3 REST。
- 四条主流程全覆盖：
  - **提问**：调 `/search`，渲染命中实体 / 邻居 / 关系 / 原文证据 / 文档片段，并把 `RecallSession` trace（向量可用性、改写 query 数、重试 tier、各通道命中量）作为「为什么召回到这些」展示给用户。
  - **知识库选择/创建**：顶栏下拉列出 `/graphs`，`+新建` 调 `create-graph`，全局切换当前库（每个 graph_no = 一个知识库容器）。
  - **喂数据 + 任务进度**：浏览器拖拽上传文档 → 一键 `ingest` 入队 → 任务页轮询 `/tasks` 看逐版本构建进度，`FAILED` 可一键 `retry`（支持自动刷新）。
  - **实体浏览**：按名查 `/entities`，点开看其 `/relations` 直接关系。
- 新增上传通道打通「浏览器无法交出服务器路径」这一缺口：
  - `api.py` 加 `POST /graphs/{no}/upload`（multipart）。
  - `service.upload_files()` 把上传字节落到 `SOURCE_DIR`（manifest 根）再复用路径式 `add_file`，doc_no 派生与 CLI 完全一致；带路径穿越防护（`../x` 压成 basename 并锁在根目录内）、后缀白名单校验。
  - `pyproject.toml`：api extra 增加 `python-multipart`，打包纳入 `ame_kb.web/*.html`。
- 验证：86 tests passed；真实 `serve` 起服务后 `GET /`、`/ui/index.html` 均 200，`/upload` 路由在 openapi 可见；`upload_files` 的正常写入 / 路径穿越拦截 / 非法后缀拒绝均已用 mock 覆盖。
- 说明：当前为**无鉴权**单页（任何人可读写所有知识库），"每个人管自己的库"的身份/归属层放在 V6.5 + V7。图可视化（Cytoscape/sigma）与节点/边人工编辑仍待做。

### 功能范围

```
Pipeline:
- ✅ 后台 ingest / extraction / indexing
- ✅ 进度可观测、失败重试、checkpoint、增量更新

API:
- ✅ Service 层（CLI/REST 共用，请求级 graph_context 隔离）
- ✅ REST API（召回 + 管线全套）
- MCP server（Agent memory interface）

UI:
- ✅ 单页 Web（提问 / 建库 / 上传+ingest / 任务进度 / 实体浏览 + 召回 trace）
- 图可视化、节点/边人工编辑（配合 V5 merge_log 回滚）
```

### 多跳策略（沿用 V4 实现，规模化时强化）

不做默认全图 BFS，按 knowledge-recall 方式：先跑一跳 → 看子问题证据缺口 →
从邻居挑相关 NodeName 当新 query → 再召回 → 维护 visited → 最多额外 2 跳。
（V4 已实现 `_expand_neighbors`，V6.3 已加 session trace 与可观测。）

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

- ✅ **Redis**：可选任务唤醒队列，与向量索引**分库或分实例**；权威状态仍在 MySQL
- ✅ 任务表：`kg_task` / `kg_pipeline_run` / `kg_pipeline_step`（参考 `sync_job.go`）
- ✅ worker lease：失败任务恢复和 checkpoint 续跑
- Cron：定时增量扫描、周期索引补偿（后续运维增强）

### V6 验收标准

1. ✅ 大批文档可后台处理，失败可重试、可断点续跑
2. ✅ 返回 seeds / 邻居 / 证据来源 + session trace（V6.3 已加）
3. 🟡 Agent 可通过 REST 调用召回（V6.3 已交付 REST；MCP 待做）
4. ✅ Web UI 可提问 / 建库 / 上传+ingest / 看任务进度 / 浏览实体（V6.4 已交付；图可视化人工修正待做）

---

## 上线完备性差距分析（"每个人建库并提问"的公网产品）

> 目标形态：任何人注册后创建/修改自己的知识库并提问。当前内核（ingest → 图 → 召回 → REST → UI）已通，
> 但从"能 demo"到"能放公网给陌生人用"，缺的主要是**身份、隔离、安全、成本、运维**这五类非功能能力。
> 下面把差距按优先级排成 V6.5 → V6.6 → V8，越靠前越是"不做就不能公开"的硬门槛。

### 🔴 P0 — 不做就不能公开（V6.5：账号与隔离）

产品化的第一硬门槛。现在 UI/REST 完全无鉴权，任何人可读写所有 graph。

| 差距 | 说明 | 落点 |
|---|---|---|
| 用户账号 | 注册/登录/会话；密码哈希或 OAuth（GitHub/Google 第三方登录最省心） | 新 `kg_user` 表 + auth 中间件 |
| 图谱归属 | `kg_graph` 加 `owner_id`；`list_graphs` / 所有 graph 操作按 owner 过滤 | `schema.sql` + `graphs.py` + `service.py` |
| 越权防护 | 每个 `/search`、`/upload`、`/ingest`、`/tasks/*` 校验"这个 graph 是不是你的"，否则 A 能读写 B 的库 | API 依赖注入 `current_user` |
| API 鉴权 | REST 全量加 token/session；Agent 走 API Key（区别于网页会话） | `api.py` |
| 隔离粒度决策 | 方案 A（graph 级归属，轻量，推荐）vs 方案 B（启用全链路 `workspace_id`，彻底但工作量大） | 见 V7 多租户 |

### 🔴 P0 — 上传即代码执行/滥用面（V6.5：输入与安全）

公网上传是最大攻击面，必须收口。

| 差距 | 说明 |
|---|---|
| 上传限额 | 单文件大小、单库文件数、单用户总配额；否则一个人能撑爆磁盘/DB |
| 文件类型硬校验 | 现在只看后缀白名单，需校验真实 MIME/魔数（防伪装的可执行内容）；PDF/HTML 解析器 CVE 面要盯 |
| SSRF 防护 | `ingest-url` 让服务器去 fetch 任意 URL——公网必须拦内网地址（169.254/10./127. 等），否则被当跳板 |
| LLM 成本护栏 | 每次 ingest/召回都烧 LLM/embedding token。需按用户限流 + 配额 + 熔断，否则一个人能刷爆你的 API 账单 |
| Prompt 注入 | 上传文档内容会进抽取/召回 prompt，可能挟持 LLM 输出。至少做输出结构校验（已有部分 validate） |

### 🟠 P1 — 好用度与信任（V6.6：产品体验）

有了这些才是"好用的产品"，而不只是"能用的工具"。

| 差距 | 说明 |
|---|---|
| 库管理闭环 | 删除库、重命名、删除单个文档（现在 UI 只能加不能删）、版本历史查看/回滚 |
| 实体人工修正 | 配合 V5 `merge_log`：UI 里合并/拆分/编辑节点与边、回滚错误合并（V6 验收里列了但没做） |
| 召回质量反馈 | 用户对答案点赞/踩 → 沉淀评测集，驱动召回调参（否则无法闭环优化质量） |
| 图可视化 | Cytoscape/sigma 展示子图，让用户"看见"自己的知识结构（V6 一直挂着的待做项） |
| 引用透明 | 答案已带原文证据行，但需要更清晰的"这句话来自哪个文档哪一行"的溯源 UI |
| 空状态引导 | 新用户第一次进来该看到什么（示例库、上传引导、提问示例） |
| MCP server | 让 Claude/Cursor 等 Agent 直接把它当记忆层调用（V6.3 就列的待做，是差异化卖点） |

### 🟠 P1 — 能放公网跑（V6.6：部署与运维）

| 差距 | 说明 |
|---|---|
| 容器化部署 | Dockerfile + docker-compose（app + MySQL + Redis 一键起）；现在只有本地 venv 跑法 |
| 配置与密钥管理 | 生产的 LLM key / DB 密码走 secret，不进代码；多环境配置 |
| 健康检查/监控 | `/health`、结构化日志、错误上报（Sentry 类）、召回延迟与 LLM 花费看板 |
| 数据备份 | MySQL 定时备份、Redis 持久化策略（RediSearch 索引可重建但要有预案） |
| 速率限制/防刷 | 网关层限流、验证码/防注册滥用 |
| HTTPS/域名 | 反向代理（nginx/caddy）+ TLS |

### 🟡 P2 — 规模化与合规（V8：长期）

| 差距 | 说明 |
|---|---|
| 计费 | 若要商业化：用量计量、套餐、支付 |
| 数据合规 | 用户数据删除权（GDPR 式）、隐私政策、数据导出 |
| 多租户彻底隔离 | 启用全链路 `workspace_id`（V4 已建 dormant 字段），或按库物理分片 |
| 规模化后端 | 向量涨到百万级切 pgvector；MySQL 边表深度遍历卡顿时评估 Neo4j（均已在设计中预留接口） |
| 团队/协作 | 库共享、成员权限、组织概念 |

### 建议的最小上线路径（MVP for public）

> 只做到"能安全地让陌生人各自建库提问"，砍掉一切非必需：

1. **V6.5**（P0 全部）：第三方登录 + `owner_id` 归属 + 越权校验 + 上传限额 + LLM 配额/限流 + SSRF 拦截。
2. **V6.6 精简**：Docker 一键部署 + `/health` + HTTPS + 删除库/文档 + 基础监控。
3. 其余（图可视化、MCP、计费、协作）**上线后按反馈迭代**，不阻塞首发。

---

## V7 — 高级能力（可选，V6 跑稳后）🔴

| 条目 | 借鉴 |
|---|---|
| 本体演化（新增元类型/原型，当前为固定闭集） | `general_recall/core/entity/sync_spec.go`（spec 注册机制） |
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
