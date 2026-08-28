# ame-kb

一个可本地运行的知识图谱构建器（V3）：扫描文档，使用 LLM 抽取实体与关系，原子写入 MySQL，再通过独立的实体消解步骤把跨文档的同一实体合并为 canonical Node。

V3 保留 V2 的多格式输入、半动态属性、内容 hash 增量和边置信度，并新增：

- 与名称解耦的稳定 Node Identity：`node:{uuid}`；
- 旧 `type:slug(name)` 标识到新 UUID 的兼容映射；
- 按文档替换贡献与内容 hash 的单事务写入；
- 同类型内的 exact/alias blocking、向量召回和灰区 LLM Judge；
- merge 审计、回滚、alias 查询，以及 legacy/loser ID 到 survivor 的重定向。

## 环境要求

- Python 3.9+
- MySQL 8.0
- OpenAI-compatible chat completion endpoint
- OpenAI-compatible embeddings endpoint；可以与 chat endpoint 共用 key 和 base URL，不需要单独部署向量数据库

## 安装

```bash
cd ame-kb
python3 -m pip install -e .
```

也可以不安装，使用 `PYTHONPATH=src python3 -m ame_kb.cli ...`，以确保加载的是当前工作区代码。

## 配置

复制 `.env.example` 为 `.env`，至少配置：

```dotenv
LLM_API_KEY=...
LLM_BASE_URL=https://.../v1
LLM_MODEL=...

# KEY 和 BASE_URL 未设置时回退到对应 LLM_*；模型和维度必须显式设置
EMBED_API_KEY=...
EMBED_BASE_URL=https://.../v1
EMBED_MODEL=...
EMBED_DIM=1024                   # 必须与 provider 实际输出维度一致

MYSQL_DSN=mysql+pymysql://user:pass@host:3306/kb?charset=utf8mb4
SOURCE_DIR=./data
# 可选：同一图中区分不同来源根目录；配置后应保持稳定
SOURCE_ID=local-docs

GRAPH_NO=default
GRAPH_VERSION=1

# 必须满足 0 <= LOW < HIGH <= 1
RESOLVE_LOW_THRESHOLD=0.75
RESOLVE_HIGH_THRESHOLD=0.92
RESOLVE_CANDIDATE_TOPK=10
```

`.env` 已被 `.gitignore` 忽略。首次初始化需要目标数据库的建表和变更表权限；日常 ingest/resolve 还需要读写权限。

## 初始化与迁移

新库：

```bash
python3 -m ame_kb.cli init-db
```

`init-db` 会检查连通性、应用基础 DDL、确保 V3 schema 已存在，并写入固定 ontology 种子。

已有 V1/V2 数据库按以下顺序升级；执行前先备份：

```bash
# 1. 先安装/更新代码，再补齐 V3 schema
python3 -m ame_kb.cli init-db

# 2. 把旧 type:slug(name) ID 迁移为 node:{uuid}，同步边端点并保留 legacy 映射
python3 -m ame_kb.cli migrate-identity

# 3. 迁移后做一次只读检查
python3 -m ame_kb.cli query "已知实体名" --no-relations
```

不要在 `migrate-identity` 前用 V3 ingest 写入旧库。迁移命令会输出迁移的节点、边、alias 和贡献记录数量；重复运行应只处理尚未迁移的数据。
V1/V2 的聚合属性没有逐文档归因：若一个旧节点/边引用多篇文档，迁移只回填结构和各文档 ref，不把整份聚合属性复制到每篇贡献。迁移后应对这些来源执行一次全量 `ingest --force`，再以真实文档重建可撤销的属性贡献。

## Ingest → Resolve → Query

把 `.md`、`.txt`、`.pdf` 或 `.html` 文件放入 `SOURCE_DIR`，然后运行：

```bash
# 只检查抽取结果；不写图数据或文档 hash
python3 -m ame_kb.cli ingest --dry-run

# 增量写入。单个文档的贡献替换和 hash 更新在同一事务提交
python3 -m ame_kb.cli ingest

# 忽略未变 hash，强制重新抽取并替换该文档的旧贡献
python3 -m ame_kb.cli ingest --force

# 先观察本轮实体消解判断，不执行 merge
python3 -m ame_kb.cli resolve --dry-run

# 可限定类型和本轮最多扫描的 seed Node 数
python3 -m ame_kb.cli resolve --type Person --limit 10

# 查询 canonical name 或 alias，并展示 survivor 的一跳关系
python3 -m ame_kb.cli query "Ada"
```

`ingest` 只持久化文档里的 Mention，不做跨文档合并。`resolve` 才执行全局实体消解：

1. 只在相同 Entity Type 内生成候选；每个 Node 的候选数由 `RESOLVE_CANDIDATE_TOPK` 控制。
2. exact name/alias 命中直接合并。
3. 否则计算向量相似度：`score >= HIGH` 自动合并。
4. `LOW <= score < HIGH` 才调用 LLM Judge；`score < LOW` 保持为不同 Node。
5. merge 后 loser 软删除并指向 survivor；边重定向到 survivor，同时清理自环和重复边。

查询只返回 active canonical survivor。旧 ID、loser ID 和 alias 都会解析到 survivor，因此历史引用仍可用于关系查询。

## Identity、alias 与回滚命令

```bash
# 按现有 type/name（也接受精确 alias）查询已存储 UUID；不再计算 type:slug(name)
python3 -m ame_kb.cli node-no Person "Ada Lovelace"

# 查看 canonical Node 的 aliases；参数可用 UUID 或精确名称
python3 -m ame_kb.cli alias-of node:00000000-0000-0000-0000-000000000000

# 使用 merge 审计记录中的 merge_id 撤销一次 merge
python3 -m ame_kb.cli rollback-merge MERGE_ID
```

Node UUID 是持久身份，不能由名称推导。`node-no` 现在是数据库 lookup；无匹配或同类型下结果不唯一时会失败并列出候选。

## 数据与消解语义

- 类型仍由 `src/ame_kb/schema.py` 的固定 ontology 约束；额外节点属性保存在 `properties`。
- 每条边的 `confidence` 为 `EXTRACTED`、`INFERRED` 或 `AMBIGUOUS`。
- 文档贡献是重 ingest 的事实来源：内容变化时替换该文档的旧贡献，再重算受影响的节点和边。
- merge 采用 survivor-wins：survivor 的非空属性优先，loser 补空缺；alias 和来源取并集，重复边保留更高置信度。
- 同一 `(graph_no, graph_version)` 的 ingest、merge、rollback 和 identity migration 通过数据库写锁串行化，避免贡献聚合丢更新和反向锁序死锁；不同图版本仍可并行。
- embedding 由 `resolve` 懒计算并缓存于 MySQL；候选相似度在进程内计算。这里没有 Redis、Milvus、FAISS 或本地 embedding 模型依赖。

设计背景见 [CONTEXT.md](CONTEXT.md)、[ADR-0001](docs/adr/0001-node-identity-decoupled-from-name.md) 和 [ADR-0002](docs/adr/0002-embeddings-without-vector-store.md)。

## 测试与验收边界

离线测试：

```bash
PYTHONPATH=src python3 -m pytest tests/ -q
```

离线测试使用 mock/内存数据库时，不代表以下外部能力已经验证：真实 MySQL 8 DDL 和事务、真实 embeddings/chat endpoint、RDS 权限与网络，以及生产数据迁移。对目标环境至少执行：

```bash
python3 -m ame_kb.cli init-db
python3 -m ame_kb.cli migrate-identity
python3 -m ame_kb.cli ingest --dry-run
python3 -m ame_kb.cli resolve --dry-run --limit 3
python3 -m ame_kb.cli query "已知实体名"
```

`ingest --dry-run` 会调用抽取 LLM；`resolve --dry-run` 可能调用 embeddings 和灰区 Judge，但不执行 merge。请用非生产样本先验证 endpoint、模型维度和阈值，再对正式图运行写入命令。

## 后续方向

- 代码源 tree-sitter 确定性抽取
- 异步 pipeline 与图可视化
- 对外 MCP server
