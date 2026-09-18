# ame-kb 启动文档

面向 AI Agent 的知识召回 / 记忆服务内核（当前 V6.4：REST API + Web UI）。
本文档说明如何在本地把服务跑起来并访问。

## 1. 前置条件

- Python 3.9+
- 一个可写的 MySQL 8.0 实例（本地或云端，如阿里云 RDS）
- 一个 OpenAI 兼容的 LLM / Embedding endpoint（key + base_url + model）

## 2. 安装

项目根目录已带 `.venv` 虚拟环境。若需重建：

```bash
cd /Users/bytedance/PycharmProjects/ame-kb
python3 -m venv .venv
.venv/bin/python -m pip install -e '.[api]'   # 含 REST API 依赖（fastapi/uvicorn）
```

> 只装内核不装 API：`pip install -e .`；带 Redis 后端：`pip install -e '.[api,redis]'`。

## 3. 配置

复制 `.env.example` 为 `.env` 并填写真实值（`.env` 已在 `.gitignore`，不会提交）：

```
LLM_API_KEY=...                 # LLM key
LLM_BASE_URL=https://.../v1     # OpenAI 兼容 endpoint
LLM_MODEL=...                   # 模型名

EMBED_API_KEY=...               # Embedding key（可与 LLM 相同）
EMBED_BASE_URL=https://.../v1
EMBED_MODEL=text-embedding-3-small

MYSQL_DSN=mysql+pymysql://user:pass@host:3306/ame_kb?charset=utf8mb4
SOURCE_DIR=./data               # 待扫描的文档目录

SEARCH_BACKEND=mysql            # mysql（默认）或 redis
GRAPH_NO=default
GRAPH_VERSION=1
```

## 4. 初始化数据库（仅首次 / 有变更时）

`init-db` 幂等，会执行 `sql/schema.sql` 建表并写入固定 schema 种子：

```bash
.venv/bin/python -m ame_kb.cli init-db
```

**什么时候需要重新跑**：

- 首次启动、或换了 `MYSQL_DSN`（新库 / 新实例）
- 拉到新增的 `sql/*.sql` 迁移
- 库被清空或建表曾失败

**不需要重新跑**：同一个库上已成功初始化过、且 `sql/` 无新增迁移，直接进第 5 步。

> 阿里云 RDS 提示：`init-db` 需要账号对目标库有 `CREATE / INSERT / UPDATE / DELETE` 权限，
> 默认账号常被裁剪权限，需在控制台「账号管理」授「读写」。

## 5. 启动服务

```bash
.venv/bin/python -m ame_kb.cli serve                       # 默认 127.0.0.1:8000
.venv/bin/python -m ame_kb.cli serve --host 0.0.0.0 --port 8080   # 对外 / 换端口
```

## 6. 访问地址

启动后（默认端口 8000）：

- Web UI：http://127.0.0.1:8000/
- API 文档（Swagger）：http://127.0.0.1:8000/docs
- 静态资源目录：http://127.0.0.1:8000/ui

换了 `--port` 就把地址中的端口替换为对应值。

## 7. 灌数据（可选，让召回有内容）

把 `.md` / `.txt` / `.pdf` / `.html` 放进 `SOURCE_DIR`（默认 `./data`），然后二选一：

```bash
# 方式 A：同步入库（增量，内容未变的文档自动跳过）
.venv/bin/python -m ame_kb.cli ingest
.venv/bin/python -m ame_kb.cli ingest --force        # 强制重抽

# 方式 B：后台任务（另开一个终端常驻 worker）
.venv/bin/python -m ame_kb.cli worker
.venv/bin/python -m ame_kb.cli enqueue-ingest        # 入队
.venv/bin/python -m ame_kb.cli task-status task_<id> # 查进度
```

也可通过 API 上传 + 入队：`POST /graphs/{graph_no}/upload` → `POST /graphs/{graph_no}/ingest`。

## 8. 常用命令速查

```bash
.venv/bin/python -m ame_kb.cli search "Ada 做过什么？"   # 混合召回
.venv/bin/python -m ame_kb.cli query "Ada"               # 按名查实体 + 一跳关系
.venv/bin/python -m ame_kb.cli list-graphs               # 列出托管图谱
.venv/bin/python -m ame_kb.cli list-tasks                # 列出后台任务
```

## 9. 主要 REST 接口

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| POST | `/search` | 混合召回，默认带 session trace |
| GET | `/graphs` | 列出图谱 |
| POST | `/graphs` | 创建托管图谱 |
| GET | `/graphs/{graph_no}/entities?name=` | 按名查实体 |
| GET | `/graphs/{graph_no}/entities/{node_no}/relations` | 实体关系 |
| GET | `/graphs/{graph_no}/snapshot` | 图谱快照 |
| POST | `/graphs/{graph_no}/upload` | 上传文件 |
| POST | `/graphs/{graph_no}/ingest` | 触发后台 ingest |
| GET | `/tasks` / `/tasks/{task_no}` | 任务列表 / 详情 |
| POST | `/tasks/{task_no}/retry` | 人工重试任务 |
