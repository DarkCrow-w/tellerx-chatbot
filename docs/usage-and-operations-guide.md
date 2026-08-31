# TellerX 企业知识库 Chatbot 使用、开发与运维手册

本文档对应当前仓库代码，覆盖系统功能、代码结构、配置、启动与关闭、API、项目命令、Docker 运维、测试、备份及常见故障。

> 适用范围：当前版本是本地/内网单机版。系统已经保留 ACL 数据结构，但尚未接入登录认证和真实权限校验。请勿直接暴露到公网。

## 1. 系统能做什么

TellerX 将业务文档解析、切块并写入 PostgreSQL；全文检索使用 PostgreSQL FTS，向量检索使用 pgvector，然后由 Qwen Rerank 和 Qwen 生成模型回答问题。每项结论必须引用知识库中的原文；引用校验失败或证据不足时，系统会拒绝猜测。

当前支持：

- Word：`.docx`、`.doc`
- Excel：`.xlsx`、`.xls`、`.xlsm`
- Markdown：`.md`、`.markdown`
- HTML：`.html`、`.htm`
- 文本：`.txt`、`.csv`
- 文本型 PDF：`.pdf`
- Confluence 导出的 HTML 或 Markdown
- OneNote 导出的 DOCX、PDF 或 HTML

当前不支持：

- 原生 OneNote `.one`
- 扫描件 PDF 的 OCR
- 加密文档
- 在线同步 Microsoft 365、OneNote 或 Confluence
- 自动执行排障操作的 Agent

`.doc` 和 `.xls` 会通过 LibreOffice 转换；只有 Docker 的解析 Worker 镜像包含
LibreOffice Writer/Calc，API、Indexer 和迁移镜像不携带该大型依赖。`.xlsm` 只读取内容，
不执行宏。DOCX、Excel、HTML、Markdown 和文本型 PDF 使用确定性原生解析器，不安装
Docling 的本地 OCR/视觉模型、PyTorch 或 CUDA 运行时。

## 2. 系统架构与处理流程

```mermaid
flowchart LR
    B["浏览器 / Vite"] --> A["FastAPI API"]
    A --> AS["AnswerService"]
    AS --> S["混合检索与证据编排"]
    S --> O["PostgreSQL FTS + pgvector"]
    S --> Q1["Qwen Embedding / Rerank"]
    AS --> R["模型路由器"]
    R --> Q2["Qwen Plus / Max"]

    U["文档上传"] --> P["PostgreSQL 任务表"]
    P --> W["解析 Worker"]
    W --> C["解析与切块"]
    C --> PG["PostgreSQL + Outbox"]
    PG --> I["Indexer"]
    I --> O
```

文档导入链路：

1. API 保存原文件，并用 SHA-256 判断重复内容。
2. API 创建文档版本和 `queued` 导入任务。
3. Worker 从 PostgreSQL 任务表领取任务。
4. 解析器提取正文、标题路径、页码、工作表和单元格范围。
5. 切块器生成约 450 tokens、最大 650 tokens 的知识块。
6. Qwen Embedding 为知识块生成 1024 维向量。
7. Worker 在一个 PostgreSQL 事务中写入 Chunk、向量清单和 Outbox 事件。
8. 单实例 Indexer 幂等写 PostgreSQL 检索投影，核验数量和 manifest 后才发布版本。

问答链路：

1. BM25 和向量检索各召回候选知识块。
2. RRF 融合并应用文档状态、标题、缩写和精确标识符等信号。
3. Qwen Rerank 对候选二次排序。
4. 服务端按证据复杂度确定使用 Plus 或 Max。
5. 模型输出结构化结论、引用 ID 和原文短句。
6. 服务端核验引用是否存在、原文是否匹配。
7. 校验失败或证据不足时返回 `insufficient_evidence`，不让模型自由补充。

## 3. 目录与代码解析

```text
TellerxChatBot/
├── app/                         Python 后端
│   ├── main.py                  FastAPI 入口、CORS、静态页面
│   ├── api/                     HTTP 路由和接口适配器
│   ├── contracts/               请求、响应和应用 DTO
│   ├── services/                问答、入库、索引和模型路由用例
│   ├── knowledge/               解析、切块和证据值对象
│   ├── integrations/            Qwen、PostgreSQL 检索投影和对象存储
│   ├── db/                      SQLAlchemy 会话与 PostgreSQL 模型
│   ├── core/                    配置和 ApplicationContainer 组合根
│   ├── jobs/                    后台解析 Worker 与索引 Worker
│   ├── commands/                生产诊断与 Reindex
│   └── static/                  Vite 构建后的前端资源
├── frontend/                    React 前端源码（API、状态、组件、存储分层）
├── alembic/                     数据库迁移
├── config/models.yaml           生成模型注册表和额度
├── docs/                        架构、设计和运维文档
├── evaluation/                  评测代码、数据集、脚本和报告
├── tests/                       Python 自动化测试
├── Dockerfile                   前端和后端多阶段镜像
├── docker-compose.yml           本地服务编排
├── pyproject.toml               Python 依赖和可执行命令
├── package.json                 前端依赖和 npm 命令
└── .env.example                 配置模板
```

### 3.1 后端核心模块

`app/main.py`

- 创建 FastAPI 应用。
- 注册 `/health/*` 和 `/api/v1/*` 路由。
- 提供 `/static` 静态资源和生产页面 `/`。
- 配置允许来源；生产 UI 与 API 默认同源。

`app/api/router.py` 与 `app/api/routes/`

- `router.py` 只组合路由；具体接口按 documents、chat、operations、health 分组。
- HTTP 层处理参数、状态码和响应模型，不实现检索和模型调用算法。
- 上传只负责保存文件和创建任务，耗时解析由 Worker 完成。
- Qwen 付费诊断故意不通过 HTTP 开放，只允许本地 CLI 显式执行。

`app/knowledge/parsers.py` 与 `app/knowledge/chunking.py`

- DOCX 使用 python-docx，HTML 使用 BeautifulSoup，Markdown 使用标题分段器。
- 文本型 PDF 直接使用 pypdf，当前不安装或执行 OCR/视觉模型。
- Excel 使用 openpyxl，保留工作表、表头、公式/缓存值和单元格坐标。
- 普通文本带少量重叠；表格按逻辑行切分并重复表头。

`app/services/ingestion.py` 与 `app/jobs/ingestion_worker.py`

- Worker 使用 `FOR UPDATE SKIP LOCKED` 并发安全领取任务，无需 Redis 或 Kafka。
- Worker 只提交 PostgreSQL Chunk、向量清单和 Outbox，不直接发布搜索索引；单实例
  Indexer 按事件顺序完成 PostgreSQL 检索投影写入、核对和版本切换。
- 任务状态通常经过 `queued -> running -> succeeded`，失败为 `failed`。
- 阶段包括 `starting`、`parsing`、`embedding`、`indexing`、`complete` 和 `failed`。
- `ALLOW_BM25_ONLY=true` 时，Embedding 不可用仍可创建纯文本索引，但这不代表正式混合检索验收通过。

`app/services/retrieval.py` 与 `app/integrations/search.py`

- `retrieval.py` 负责混合召回、RRF、标识符覆盖和 Rerank 排序策略。
- `search.py` 只负责 PostgreSQL FTS/pgvector 查询、过滤和检索投影读写。
- 默认 BM25 与向量各召回 50 个块。
- RRF 融合后将最多 30 个候选发送给 `qwen3-rerank`。
- 默认选出 8 个证据块，并保留精确编号、缩写和中英文实体覆盖。
- 优先检索 `approved`；证据过少时可补充并标记 `draft`；默认排除 `deprecated`。

`app/services/answer_contract.py`、`app/services/answering.py` 与
`app/services/model_router.py`

- Prompt 构造、证据预算和引用校验是无数据库/网络副作用的独立安全契约。
- 单文档直接查询优先 Plus；跨文档、比较、冲突等复杂问题优先 Max。
- 所有 Max 因额度、权限或服务故障不可用时，普通交互可受控降级到一次 Plus；响应中的
  `route_tier` 和 `model_id` 始终显示实际选择。固定模型评测绝不跨模型或跨档位降级。
- 固定模型评测时禁用自动切换。
- 每项结论必须提供有效 chunk ID 和可在知识块中找到的原文短句。
- 生成模型使用量、耗时、结果、Prompt 版本记录在 `model_usage`。
- 本地估算使用量达到 90% 后，不再向该模型分配普通新请求。

`app/integrations/qwen.py`

- 从文件读取 API Key，不从源码读取。
- 使用进程级 HTTP 连接池，并在 API、Worker、Indexer 优雅退出时显式关闭。
- Chat 使用兼容模式接口；Embedding 和 Rerank 使用百炼接口。
- 对连接错误和部分临时 HTTP 状态做有限重试，不无限消耗额度。
- 错误信息只暴露安全元数据，不打印 token 或完整供应商响应正文。

### 3.2 数据存储职责

- PostgreSQL：项目、文档、不可变版本、知识块、导入任务、outbox、索引代际、对话、反馈、模型用量和 ACL 结构。
- uploads Docker 卷：原始文件、规范化解析产物和可复用的 Embedding 向量。
- PostgreSQL `chunk_search_index`：可重建且事务维护的全文与向量检索投影。
- 浏览器 `localStorage`：前端最近 24 个对话的显示副本和主题设置。

PostgreSQL 和原始文件是事实来源；`chunk_search_index` 与业务表保存在同一 PostgreSQL 持久卷中，并可从 Chunk 与持久向量重建。浏览器历史并不是服务端对话的完整管理界面，换浏览器后不会自动显示旧历史。

### 3.3 前端当前功能与边界

前端源码按职责拆分为 `api.js`、`storage.js`、`components.jsx`、`App.jsx` 和最小
`main.jsx`；UI 组件不直接发起 HTTP 请求。

React 页面目前提供：

- 提问和连续对话。
- 全部项目或单项目检索范围选择。
- `answered`、`conflict`、`insufficient_evidence` 的可视状态。
- 实际模型和路由档位显示。
- 可展开的原文证据、页码、工作表和单元格位置。
- 最近对话、复制回答、明暗主题。

当前页面没有文档上传/管理界面，上传、删除和任务状态查询需使用 API 或 Swagger。页面上的点赞/点踩只更新本地界面提示，尚未调用 `/feedback`；需要持久化反馈时请直接调用 API。

## 4. 首次启动前准备

### 4.1 前置软件

推荐方式只要求：

- Docker Desktop，支持 `docker compose`
- 至少约 2 GB 可用内存；大规模导入和并行评测建议 4 GB 以上
- 可访问阿里云百炼 API 的网络

开发前端还需要 Node.js 22；本地运行 Python 后端需要 Python 3.12。

### 4.2 准备 Qwen 凭证

凭证文件固定为：

```text
Qwen/Qwen token.txt
```

文件中只放一行 API Key，不要加变量名、引号或说明文字。设置权限：

```bash
cd /Users/cliff/Documents/TellerxChatBot
chmod 600 "Qwen/Qwen token.txt"
```

该目录已被 Git 和 Docker 构建上下文排除。Compose 只在容器运行时将文件只读挂载到 `/run/secrets/model_api_key`。不要把 token 写进 `.env`、代码、截图或日志。

### 4.3 创建配置

```bash
cd /Users/cliff/Documents/TellerxChatBot
cp .env.example .env
docker compose config
```

如果 `.env` 已存在，不要直接覆盖；先比较：

```bash
diff -u .env.example .env
```

`docker compose config` 用于检查 Compose 和环境变量语法。其输出不应包含 Qwen token，因为 token 不在 `.env` 中。

## 5. 推荐启动与关闭方式：全部使用 Docker

### 5.1 第一次启动

后台构建并启动全部服务：

```bash
cd /Users/cliff/Documents/TellerxChatBot
docker compose up --build -d
```

如果希望在当前终端直接观察全部日志，可以去掉 `-d`；此时按 `Ctrl+C` 会停止本次 Compose 服务：

```bash
docker compose up --build
```

查看状态：

```bash
docker compose ps
docker compose logs --tail=100 migrate api worker indexer postgres
```

`migrate` 是一次性数据库迁移服务，显示 `Exited (0)` 表示升级成功；API、Worker 和
Indexer 只有在它成功完成后才会启动。若它非零退出，先查看
`docker compose logs migrate`，不要绕过迁移直接启动应用进程。

等待 `postgres` 健康且 `migrate` 成功后，检查系统：

```bash
curl -fsS http://localhost:8000/health/live
curl -fsS http://localhost:8000/health/ready
```

可访问：

- Web 页面：<http://localhost:8000>
- Swagger API 文档：<http://localhost:8000/docs>
- OpenAPI JSON：<http://localhost:8000/openapi.json>
- PostgreSQL 检索状态：<http://localhost:8000/api/v1/index/status>

一次性 Migrate 容器先执行 `alembic upgrade head`。Worker 负责解析、切块与 Embedding；
Indexer 消费 PostgreSQL outbox，只有检索投影写入和计数校验成功后任务才会变为
`succeeded`、版本才会变为 `searchable`。

### 5.2 日常启动

容器已经创建但被 `stop` 后：

```bash
docker compose start
```

如果容器尚未创建，或 Compose/环境配置有变化：

```bash
docker compose up -d
```

代码、Python 依赖、前端依赖或 Dockerfile 变化后：

```bash
docker compose up --build -d
```

### 5.3 日常关闭

只停止服务，保留容器、网络和全部数据：

```bash
docker compose stop
```

停止并删除容器和项目网络，但保留 PostgreSQL 与 uploads 两个命名卷的数据：

```bash
docker compose down
```

下次都可以使用 `docker compose up -d` 恢复。

### 5.4 危险关闭命令

```bash
docker compose down -v
```

`-v` 会删除 PostgreSQL 和 uploads 命名卷，即文档记录、对话、全文/向量索引及上传文件。除非明确要清空本地系统且已有备份，否则不要执行。

## 6. 前后端分别开发

### 6.1 Docker 后端 + Vite 前端热更新（推荐开发方式）

终端 1：运行完整后端：

```bash
cd /Users/cliff/Documents/TellerxChatBot
docker compose up -d --build
docker compose logs -f api worker indexer
```

终端 2：运行前端：

```bash
cd /Users/cliff/Documents/TellerxChatBot
npm ci
npm run dev
```

打开 <http://localhost:5173>。Vite 会把 `/api` 和 `/health` 代理到 `http://localhost:8000`。

关闭前端使用 `Ctrl+C`；关闭 Docker 后端使用：

```bash
docker compose stop
```

前端构建检查：

```bash
npm run build
npm run preview
```

`npm run build` 输出到 `app/static/`。Docker 镜像构建时也会自动执行一次前端构建。

### 6.2 全部源码运行

默认 Compose 没有把 PostgreSQL 的 5432 端口暴露到宿主机。如果要让本地 Python 直接连接 Compose PostgreSQL，可临时创建一个仅开发使用的 `docker-compose.local.yml`：

```yaml
services:
  postgres:
    ports:
      - "5432:5432"
```

启动基础设施：

```bash
docker compose -f docker-compose.yml -f docker-compose.local.yml up -d postgres
```

创建 Python 环境：

```bash
python3.12 -m venv .venv
source .venv/bin/activate
python -m pip install --upgrade pip
python -m pip install -e '.[dev,quality]'
```

为宿主机进程配置地址。注意这些值与容器内 `.env` 的主机名不同：

```bash
export DATABASE_URL='postgresql+psycopg://knowledge:knowledge@localhost:5432/knowledge'
export SEARCH_BACKEND='postgresql-pgvector-fts'
export POSTGRES_SEARCH_TABLE='chunk_search_index'
export STORAGE_ROOT="$PWD/.local-data/uploads"
export MODEL_API_KEY_FILE="$PWD/Qwen/Qwen token.txt"
export MODEL_REGISTRY_PATH="$PWD/config/models.yaml"
mkdir -p "$STORAGE_ROOT"
```

执行迁移：

```bash
alembic upgrade head
```

终端 1，启动 API：

```bash
source .venv/bin/activate
uvicorn app.main:app --host 0.0.0.0 --port 8000 --reload
```

终端 2，重新导出同一组环境变量，然后启动 Worker：

```bash
source .venv/bin/activate
knowledge-worker
```

终端 3，启动前端：

```bash
npm ci
npm run dev
```

三个前台进程都用 `Ctrl+C` 关闭，基础设施使用：

```bash
docker compose -f docker-compose.yml -f docker-compose.local.yml stop postgres
```

不要同时运行 Docker `worker` 和本地 `knowledge-worker`，除非你确实要测试多个 Worker；它们会竞争同一任务队列。

## 7. 文档上传与日常使用

### 7.1 上传文档

Web 页面当前不包含上传入口。可在 Swagger 的 `POST /api/v1/documents` 中上传，或使用：

```bash
curl -sS -X POST 'http://localhost:8000/api/v1/documents' \
  -F 'file=@/absolute/path/system-design.docx' \
  -F 'project=TellerX' \
  -F 'document_type=system-design' \
  -F 'lifecycle_status=approved' \
  -F 'version_label=1.0' \
  -F 'owner=Business Architecture'
```

`project` 不存在时自动创建。`lifecycle_status` 只能是：

- `approved`：正式文档，优先检索。
- `draft`：草稿，仅在正式证据不足时补充。
- `deprecated`：已废弃，默认不参与检索。

可选字段 `effective_at` 使用 ISO 8601，例如 `2026-08-12T00:00:00+08:00`；`supersedes_document_id` 用于记录被替代文档。

响应示例：

```json
{
  "document_id": "doc-id",
  "version_id": "version-id",
  "job_id": "job-id",
  "duplicate": false
}
```

同一项目、同一文件名、同一 SHA-256 再次上传会返回 `duplicate: true`，不会重复创建版本。

### 7.2 查看导入状态

```bash
curl -sS 'http://localhost:8000/api/v1/ingestion-jobs/JOB_ID'
```

成功示例：

```json
{
  "id": "job-id",
  "document_id": "doc-id",
  "version_id": "version-id",
  "status": "succeeded",
  "stage": "complete",
  "progress": 100,
  "error_message": null,
  "warnings": []
}
```

如果状态长时间为 `queued`，先检查 Worker：

```bash
docker compose ps worker
docker compose logs --tail=200 worker
```

重试已完成或失败的任务：

```bash
curl -sS -X POST 'http://localhost:8000/api/v1/ingestion-jobs/JOB_ID/retry'
```

### 7.3 提问

在 Web 页面选择知识范围后输入问题即可。`Enter` 发送，`Shift+Enter` 换行，`Cmd/Ctrl+K` 新建对话。

也可调用 API：

```bash
curl -sS -X POST 'http://localhost:8000/api/v1/chat' \
  -H 'Content-Type: application/json' \
  -d '{
    "question": "TellerX 的交易审批流程有哪些关键节点？",
    "project_ids": [],
    "conversation_id": null
  }'
```

`project_ids` 为空表示检索全部项目。后续问题可复用返回的 `conversation_id`。正式固定模型评测可添加：

```json
"pinned_model": "qwen3.7-plus-2026-05-26"
```

普通交互不建议固定模型，让路由器根据证据复杂度和额度选择。

自然语言问法的主题抽取、聚焦检索、追踪与排障方式详见
[多种问题表述识别机制](query-phrasing-handling.md)。

### 7.4 删除文档

```bash
curl -i -X DELETE 'http://localhost:8000/api/v1/documents/DOCUMENT_ID'
```

该操作先提交数据库软删除和 outbox tombstone，Indexer 随后从 PostgreSQL 检索投影删除各版本。
原始文件、解析产物和向量不会立即物理删除，以支持审计和恢复。

## 8. API 功能说明

除健康检查外，业务 API 前缀是 `/api/v1`。

| 方法 | 路径 | 功能 | 常见响应 |
|---|---|---|---|
| `GET` | `/health/live` | API 进程存活检查 | 200 |
| `GET` | `/health/ready` | PostgreSQL、FTS 扩展和 pgvector 就绪检查 | 200/503 |
| `GET` | `/api/v1/projects` | 项目列表 | 200 |
| `POST` | `/api/v1/documents` | 上传文档、版本和导入任务 | 202/413/415/422 |
| `GET` | `/api/v1/ingestion-jobs/{id}` | 查询导入状态 | 200/404 |
| `POST` | `/api/v1/ingestion-jobs/{id}/retry` | 重新排队已结束任务 | 202/404/409 |
| `GET` | `/api/v1/documents/{id}/versions` | 查看文档版本与技术状态 | 200/404 |
| `POST` | `/api/v1/document-versions/{id}/approve` | 发布已验证版本 | 200/404/409 |
| `POST` | `/api/v1/document-versions/{id}/deprecate` | 废弃版本并发出删除事件 | 200/404 |
| `POST` | `/api/v1/chat` | 检索并生成带引用回答 | 200/422/502/503 |
| `GET` | `/api/v1/sources/{chunk_id}` | 获取完整知识块和定位元数据 | 200/404 |
| `GET` | `/api/v1/documents/{id}/download` | 下载最新或指定版本原文件 | 200/404 |
| `DELETE` | `/api/v1/documents/{id}` | 软删除文档并移除检索数据 | 204/404 |
| `POST` | `/api/v1/feedback` | 持久化回答反馈 | 201/404/422 |
| `GET` | `/api/v1/models/usage` | 查看生成模型本地额度估算 | 200 |
| `GET` | `/api/v1/index/status` | 查看 alias、outbox 和索引状态 | 200 |
| `POST` | `/api/v1/admin/indexes/reconcile` | 对账；`repair=true` 时修复 | 200 |
| `POST` | `/api/v1/internal/diagnostics/qwen` | 返回禁用提示，不执行付费诊断 | 200 |

### 8.1 项目列表

```bash
curl -sS 'http://localhost:8000/api/v1/projects'
```

返回项目的 `id` 和 `name`，其中 `id` 可用于 Chat 的 `project_ids`。

### 8.2 查看原文来源

```bash
curl -sS 'http://localhost:8000/api/v1/sources/CHUNK_ID'
```

返回完整知识块，以及文件名、文档版本、生命周期、标题路径、页码、工作表和单元格范围。

### 8.3 下载指定版本

最新版本：

```bash
curl -fL 'http://localhost:8000/api/v1/documents/DOCUMENT_ID/download' \
  -o downloaded-document
```

指定版本：

```bash
curl -fL 'http://localhost:8000/api/v1/documents/DOCUMENT_ID/download?version_id=VERSION_ID' \
  -o downloaded-document
```

### 8.4 提交反馈

`message_id` 来自 Chat 响应，`rating` 可为 `correct`、`incorrect`、`missing_source` 或 `irrelevant_source`：

```bash
curl -sS -X POST 'http://localhost:8000/api/v1/feedback' \
  -H 'Content-Type: application/json' \
  -d '{
    "message_id": "ASSISTANT_MESSAGE_ID",
    "rating": "correct",
    "comment": "结论和引用均准确"
  }'
```

### 8.5 查看模型额度

```bash
curl -sS 'http://localhost:8000/api/v1/models/usage'
```

这是依据 API 返回 token 的本地估算，用于路由；阿里云控制台仍是额度最终依据。Embedding 和 Rerank 不计入这组生成模型的 1M token 注册额度。

### 8.6 Chat 响应字段

- `status`：`answered`、`insufficient_evidence` 或 `conflict`。
- `answer`：最终文本。
- `claims`：结构化结论和引用 chunk ID。
- `sources`：已校验的引用详情。
- `model_id`：实际使用的生成模型；未生成时可能为空。
- `route_tier`：`plus` 或 `max`。
- `conversation_id`：继续对话时复用。
- `message_id`：提交反馈时使用。
- `trace_id`：排查单次请求时使用。

所有准确字段和在线调试以 <http://localhost:8000/docs> 为准。

## 9. 环境配置

常用配置位于 `.env`：

| 变量 | 默认/示例 | 说明 |
|---|---|---|
| `APP_ENV` | `development` | 运行环境标识 |
| `DATABASE_URL` | `...@postgres:5432/knowledge` | PostgreSQL 连接串 |
| `SEARCH_BACKEND` | `postgresql-pgvector-fts` | 当前检索后端；启动时严格校验 |
| `POSTGRES_SEARCH_TABLE` | `chunk_search_index` | FTS 与向量检索投影表 |
| `POSTGRES_SEARCH_SCHEMA_VERSION` | `1` | 检索结构代际 |
| `PGVECTOR_HNSW_EF_SEARCH` | `200` | HNSW 查询候选深度 |
| `STORAGE_ROOT` | `/data/knowledge` | 原文件存储根目录 |
| `MODEL_API_KEY_FILE` | `/run/secrets/model_api_key` | 公司模型网关 token 文件路径 |
| `MODEL_API_BASE_URL` | 公司 SDK Endpoint | OpenAI 兼容 API 根地址 |
| `MODEL_API_JSON_MODE_ENABLED` | `true` | 是否发送标准 JSON Object 响应格式 |
| `EMBEDDING_MODEL` | `qwen3-embedding` | 向量模型 ID |
| `EMBEDDING_DIMENSIONS` | `1024` | 向量维度，必须匹配 pgvector 结构 |
| `EMBEDDING_PREPROCESS_VERSION` | `normalized-text-v1` | 向量预处理版本 |
| `RERANK_ENABLED` | `false` | 内部环境禁用专用重排，使用 RRF |
| `MODEL_REGISTRY_PATH` | `/app/config/models.yaml` | 生成模型注册表 |
| `ALLOW_BM25_ONLY` | `true` | Embedding 失败时允许纯 BM25 导入/检索 |
| `RUN_INLINE_INGESTION` | `false` | 是否由 API 后台任务处理导入；Compose 应保持 false |

可按需添加的高级配置：

| 变量 | 默认值 | 说明 |
|---|---:|---|
| `MODEL_API_TIMEOUT_SECONDS` | `60` | 单次模型网关请求超时 |
| `MODEL_API_MAX_RETRIES` | `2` | SDK 临时错误最大重试次数 |
| `PARSER_BACKEND` | `native` | 当前固定使用确定性原生解析器 |
| `WORKER_POLL_SECONDS` | `2` | Worker 无任务时轮询间隔 |
| `INDEX_RECONCILE_INTERVAL_SECONDS` | `3600` | Indexer 自动 manifest 对账/修复周期；`0` 禁用 |
| `MAX_UPLOAD_BYTES` | `104857600` | 上传上限，默认 100 MB |
| `CHUNK_TARGET_TOKENS` | `450` | 目标块大小 |
| `CHUNK_MAX_TOKENS` | `650` | 最大块大小 |
| `CHUNK_OVERLAP_TOKENS` | `60` | 普通文本重叠 |
| `RETRIEVAL_TOP_K` | `50` | 每路初始召回数 |
| `RERANK_CANDIDATES` | `30` | Rerank 候选数 |
| `EVIDENCE_TOP_K` | `8` | 最终证据块数 |
| `VECTOR_MIN_SIMILARITY` | `0.25` | 向量最低相似度初值；正式值由评测标定 |
| `SEMANTIC_QUERY_UNDERSTANDING_ENABLED` | `true` | 对自然语言问题启用受限查询规划 |
| `QUERY_PLAN_CACHE_SIZE` | `500` | 查询计划进程内缓存条数 |
| `QUERY_PLAN_CACHE_TTL_SECONDS` | `3600` | 查询计划缓存秒数 |
| `PROMPT_VERSION` | `grounded-qa-v1` | Prompt 审计版本 |
| `CORS_ORIGINS` | `["http://localhost:8000"]` | JSON 数组格式的允许来源 |

修改 `.env` 后重建对应容器配置：

```bash
docker compose up -d --force-recreate api worker indexer
```

Embedding 模型或维度变化时，必须执行全量重新索引，不能混用旧向量。

## 10. 模型注册表与路由

`config/models.yaml` 当前模型池：

| 模型 | 档位 | 优先级 | 当前启用 |
|---|---|---:|---|
| `qwen3.7-plus-2026-05-26` | Plus | 10 | 是 |
| `qwen3.7-plus` | Plus | 20 | 是 |
| `qwen3.7-max-2026-05-20` | Max | 10 | 是 |
| `qwen3.7-max-2026-05-17` | Max | 20 | 否 |
| `qwen3.7-max` | Max | 30 | 是 |
| `qwen3.7-max-preview` | Max | 40 | 否 |

每个模型配置 1,000,000 tokens 本地额度。`priority` 数字越小越优先。启用一个模型前，先运行该模型的诊断和固定模型回归。

修改注册表后重启 API：

```bash
docker compose restart api
```

Worker 不负责生成模型路由，但如果同时修改了 Embedding/Rerank 配置，也应重启 Worker。

## 11. 项目可执行命令

生产命令由 `pyproject.toml` 安装；质量工具通过 `python -m evaluation...` 从源码树运行：

| 命令 | 功能 |
|---|---|
| `knowledge-worker` | 启动导入 Worker |
| `knowledge-indexer` | 消费可靠索引 outbox |
| `knowledge-reconcile` | 对账事实表与 PostgreSQL 检索投影；可加 `--repair` |
| `model-diagnostics` | 显式检查 Chat、Embedding，并确认 Rerank 已禁用 |
| `python -m evaluation.business` | 运行 JSONL 业务问答评测 |
| `python -m evaluation.benchmark.cli` | 生成和运行千文档基准 |
| `knowledge-reindex` | 从事实库和持久向量构建、验证并切换新索引代际 |
| `python -m evaluation.smoke.pgvector` | 验证 FTS、pgvector、项目过滤和版本状态 |

Docker 内执行一次性命令的一般形式：

```bash
docker compose run --rm api COMMAND
```

在正在运行的 API 容器中执行的一般形式：

```bash
docker compose exec api COMMAND
```

### 11.1 模型网关连通性诊断

诊断会产生少量真实 API 用量：

```bash
docker compose run --rm api model-diagnostics
```

指定 Chat 模型：

```bash
docker compose run --rm api model-diagnostics \
  --chat-model qwen3.5-122B
```

按需跳过部分检查：

```bash
docker compose run --rm api model-diagnostics --skip-chat
docker compose run --rm api model-diagnostics --skip-embedding
```

命令退出码为 0 表示全部已选检查成功，1 表示至少一项失败。输出不会包含 token。

### 11.2 数据库迁移

```bash
docker compose exec api alembic current
docker compose exec api alembic history
docker compose exec api alembic upgrade head
```

Compose 的一次性 `migrate` 服务会在 API、Worker 和 Indexer 启动前运行 `upgrade head`；
API 本身不会抢跑迁移。`alembic downgrade` 会改变数据库结构，只应在维护窗口、确认迁移支持并完成备份后执行。

### 11.3 全量向量重建

切换 `EMBEDDING_MODEL` 或 `EMBEDDING_DIMENSIONS` 后：

```bash
docker compose run --rm api model-diagnostics --skip-chat
docker compose exec api knowledge-reindex
```

该命令优先复用对象存储中的持久向量，只有缺少当前 fingerprint 的向量时才调用
Embedding。它总是创建新的物理 generation，逐版本核验数量、全局数量和 manifest，全部通过后
才原子切换读写 alias；不会删除或覆盖当前在线索引。不要并发执行多个实例。

### 11.4 业务评测

JSONL 每行至少包含 `question`，可选 `id`、`project_ids`、`expected_document_ids`：

```json
{"id":"q-001","question":"审批超时如何处理？","project_ids":[],"expected_document_ids":["doc-id"]}
```

Docker 镜像没有复制本地 `evaluation/`，运行时需要挂载：

```bash
docker compose run --rm \
  -v "$PWD/evaluation:/app/evaluation" \
  api python -m evaluation.business /app/evaluation/datasets/business/sample.jsonl \
  --model qwen3.7-plus-2026-05-26 \
  --output /app/evaluation/plus-baseline.jsonl
```

固定 `--model` 会禁止自动切换，适合可重复回归。该评测会真实调用检索、Rerank 和生成模型。

### 11.5 千文档基准

这里的中英文内容是固定的合成考卷，与实际上传的业务文档相互独立。业务文档发生变化时
不需要修改这些模板；它们用于持续验证解析、检索、版本治理、拒答和引用能力有没有退化。
如果要验证真实业务内容，应另外维护带标准答案的业务数据集，并使用
`python -m evaluation.business` 运行。只有需要改变合成考卷覆盖的语言、字段或题型时，
才修改 `evaluation/benchmark/corpus.py`。

生成确定性语料：

```bash
python -m evaluation.benchmark.cli generate \
  --output evaluation/generated/benchmark-1k \
  --count 1000 \
  --questions 200 \
  --seed 20260812 \
  --force
```

主要子命令：

- `generate`：生成基准文档和问题。
- `load`：解析并载入语料；`--no-embedding` 禁用真实 Embedding。
- `index-existing`：对数据库已有 chunk 建索引。
- `retrieve`：检索评测；可用 `--no-vector`、`--no-rerank`。
- `answers`：固定模型运行在线答案评测。
- `answers-offline`：离线答案管线测试。
- `hybrid-offline`：本地特征哈希混合检索测试。
- `load-production-offline`：用生产解析链路离线载入。
- `api-smoke-offline`：上传、任务、来源和下载 API 冒烟测试。
- `project-filter-offline`：项目过滤测试。

完整 BM25-only 示例：

```bash
python -m evaluation.benchmark.cli load evaluation/generated/benchmark-1k --reset --no-embedding
python -m evaluation.benchmark.cli retrieve evaluation/generated/benchmark-1k --no-vector --no-rerank
python -m evaluation.benchmark.cli answers-offline evaluation/generated/benchmark-1k
```

在线混合检索示例：

```bash
python -m evaluation.benchmark.cli index-existing evaluation/generated/benchmark-1k
python -m evaluation.benchmark.cli retrieve evaluation/generated/benchmark-1k
python -m evaluation.benchmark.cli answers evaluation/generated/benchmark-1k \
  --limit 20 \
  --model qwen3.7-plus-2026-05-26
```

一键 Qwen 门禁：

```bash
evaluation/scripts/run-qwen-1k-gate.sh evaluation/generated/benchmark-1k
```

> 严重警告：基准命令的 `--reset` 会清空其当前配置所连接数据库中的项目、文档等数据，并可能重建索引。必须使用隔离的评测数据库和索引，绝不能指向业务/生产环境。

## 12. npm、测试和质量命令

### 12.1 前端

```bash
npm ci             # 严格按 package-lock.json 安装
npm run dev        # Vite 热更新，默认 5173
npm run build      # 构建到 app/static
npm run preview    # 预览生产构建
```

日常和 CI 推荐 `npm ci`；只有主动更新依赖时才使用 `npm install` 并审查 lockfile 变化。

### 12.2 后端

```bash
source .venv/bin/activate
ruff check app evaluation tests
pytest -q
pytest --cov=app --cov-report=term-missing
```

综合提交前检查：

```bash
ruff check app evaluation tests
pytest -q
npm run build
docker compose config
```

单元测试可使用 SQLite 和 mock Qwen；完整链路仍需带 `vector`/`pg_trgm` 扩展的 PostgreSQL 和可用的 Qwen 权限。

## 13. Docker 命令速查

### 13.1 查看与日志

```bash
docker compose ps
docker compose images
docker compose logs --tail=200
docker compose logs -f api
docker compose logs -f worker
docker compose top
```

退出 `logs -f` 只需 `Ctrl+C`，不会停止容器。

### 13.2 启动、停止和重启

```bash
docker compose up -d
docker compose up --build -d
docker compose stop
docker compose start
docker compose restart api worker indexer
docker compose down
```

只启动部分服务：

```bash
docker compose up -d postgres
```

重建单个服务：

```bash
docker compose up -d --build --no-deps api
docker compose up -d --build --no-deps worker
docker compose up -d --build --no-deps indexer
```

`--no-deps` 不会运行数据库迁移，只适合确认没有新 Alembic 版本的代码变更。存在迁移时
应执行完整的 `docker compose up -d --build`。API、Worker、Indexer 与 Migrate 使用同一
构建内容，后端代码变化时通常应一起重建。

### 13.3 进入容器和执行命令

```bash
docker compose exec api sh
docker compose exec api python --version
docker compose exec postgres psql -U knowledge -d knowledge
docker compose exec postgres psql -U knowledge -d knowledge -c '\dx'
docker compose exec postgres psql -U knowledge -d knowledge -c \
  'SELECT count(*), count(embedding) FROM chunk_search_index;'
```

### 13.4 构建和拉取

```bash
docker compose build
docker compose build --no-cache migrate api worker indexer
docker compose pull postgres
```

升级基础镜像前先备份，并重新执行测试和 Qwen 回归，不要把 `latest` 式升级直接带入正式环境。

### 13.5 查看卷

```bash
docker compose config --volumes
docker volume ls
docker compose exec api du -sh /data/knowledge
```

避免使用全局 `docker system prune --volumes`，它可能清理其他项目的数据。

## 14. 备份与恢复

至少备份 PostgreSQL 和 uploads 卷；uploads 包含原文件、解析产物和可复用向量，PostgreSQL
包含业务事实、对话、任务和在线 FTS/pgvector 索引。同一 Embedding fingerprint 下重建检索
投影不需要再次消耗 Qwen 额度。生产环境应启用 PostgreSQL PITR，并把备份存放到数据库主机之外。

创建本次专用备份目录，避免重复运行时把目录嵌套或覆盖：

```bash
backup_dir="backup/$(date +%Y%m%d-%H%M%S)"
mkdir -p "$backup_dir"
```

备份 PostgreSQL：

```bash
docker compose exec -T postgres \
  pg_dump -U knowledge -d knowledge -Fc > "$backup_dir/knowledge.dump"
```

备份原文件：

```bash
docker compose cp api:/data/knowledge "$backup_dir/uploads"
```

验证备份文件存在且大小合理：

```bash
ls -lah "$backup_dir"
```

恢复 PostgreSQL 会覆盖/删除现有对象，必须在维护窗口确认目标数据库后执行：

```bash
docker compose stop api worker indexer
docker compose exec -T postgres \
  pg_restore -U knowledge -d knowledge --clean --if-exists < /path/to/knowledge.dump
docker compose start api worker indexer
```

原文件恢复建议先在空的或单独的恢复环境验证，不要直接覆盖未知内容。备份还应复制到 Docker 主机之外，单机磁盘损坏时本机备份并不可靠。

## 15. 常见故障排查

### 15.1 `/health/ready` 返回 503

```bash
docker compose ps
docker compose logs --tail=200 postgres migrate api
docker compose exec postgres psql -U knowledge -d knowledge -c '\dx'
docker compose exec postgres psql -U knowledge -d knowledge -c \
  "SELECT to_regclass('chunk_search_index');"
```

`live` 正常但 `ready` 失败，通常表示 PostgreSQL 不可达、Alembic 迁移未完成、`vector` 或
`pg_trgm` 扩展缺失，或 `chunk_search_index` 尚未创建。先检查 `migrate` 日志和 `alembic current`。

### 15.2 页面无法打开或端口冲突

检查 8000 和开发模式下的 5173：

```bash
lsof -nP -iTCP:8000 -sTCP:LISTEN
lsof -nP -iTCP:5173 -sTCP:LISTEN
```

若修改 Compose 端口映射，Vite 代理目标也需同步修改 `frontend/vite.config.js`。

### 15.3 导入任务一直 queued

```bash
docker compose ps worker
docker compose ps indexer
docker compose logs --tail=300 worker indexer
docker compose restart worker
docker compose restart indexer
```

确认 Worker、Indexer 与 API 使用相同 `DATABASE_URL`、`SEARCH_BACKEND`、uploads 卷和模型配置；
同时查看 `GET /api/v1/index/status` 的 pending/dead outbox 数量。

该接口还返回 `physical_index`、`embedding_fingerprint` 和 `missing_embeddings`。正式验收前
`pending_events`、`dead_events`、`sync_differences`、`missing_embeddings` 都应为 `0`；
开发环境允许的 BM25-only 数据必须在 Embedding 服务恢复后用 `knowledge-reindex` 补齐。

### 15.4 导入 succeeded 但有 warnings

查看任务的 `warnings`。当 `ALLOW_BM25_ONLY=true` 且 Embedding 调用失败时，任务可能以 BM25-only 完成；这时文本搜索可用，但不是正式混合检索状态。恢复 Qwen 后应诊断并重新索引。

### 15.5 模型网关返回 400、429、额度或模型错误

```bash
docker compose run --rm api model-diagnostics
curl -sS http://localhost:8000/api/v1/models/usage
```

- 400 且错误码为账户欠费/额度状态：到百炼控制台处理账户，而不是更换或打印 token。
- 429：等待限流窗口，不要手工无限循环重试。
- 模型参数错误：确认模型 ID、区域和账户权限，再决定是否在 `config/models.yaml` 启用。
- Chat 不可用时，复杂问题会先尝试受控 Plus 降级；若生成模型池仍不可用则严格拒答。
  BM25-only 只解决检索降级，不能代替生成回答验收。

### 15.6 文档解析失败

- 原生 `.one`：先从 OneNote 导出 DOCX、PDF 或 HTML。
- 扫描 PDF：当前无 OCR，先使用受控 OCR 工具生成文本型 PDF。
- `.doc`/`.xls`：确认解析任务由 Docker `worker` 执行；只有 `worker-runtime` 镜像包含
  LibreOffice Writer/Calc。
- 加密文件：先在合规环境解密为允许格式。
- Excel 宏：不会执行，业务结果如果依赖运行宏，应先保存已计算值。

### 15.7 前端改动没有显示

开发模式确认访问 5173。生产页面使用 `app/static`，需要重新构建/重建：

```bash
npm run build
docker compose up -d --build api
```

必要时硬刷新浏览器缓存。

### 15.8 检索不到刚上传的文档

1. 查询任务是否 `succeeded`。
2. 确认生命周期不是 `deprecated`。
3. 确认页面项目筛选与上传项目一致。
4. 查看 Worker 是否出现解析或 BM25-only warning。
5. 切换过 Embedding 模型时确认已执行 `knowledge-reindex`。

## 16. 安全与上线注意事项

- 当前没有用户登录和生效的文档 ACL，不应直接公网部署。
- 正式环境应接入公司身份系统、Secret Manager、TLS、审计日志和备份策略。
- 生产环境应更换示例数据库密码，并限制 PostgreSQL 网络访问。
- 不记录模型网关 token、原文全文或供应商完整错误响应到公共日志。
- `MODEL_API_KEY_FILE` 应保持只读 secret 文件方式。
- 上传接口在上线前应增加认证、恶意文件检查、租户/项目权限和访问频率限制。
- 本地 Compose 使用开发数据库口令且不发布 5432 端口；生产必须使用 Secret Manager、TLS、最小权限账号和网络隔离。
- 模型、Embedding、Prompt 或检索参数变化后，应运行同一套人工标注回归。

## 17. 推荐日常操作清单

启动：

```bash
cd /Users/cliff/Documents/TellerxChatBot
docker compose up -d
docker compose ps
curl -fsS http://localhost:8000/health/ready
```

上传后：

```bash
curl -sS http://localhost:8000/api/v1/ingestion-jobs/JOB_ID
docker compose logs --tail=100 worker
```

关闭：

```bash
docker compose stop
```

版本升级前：

```bash
mkdir -p backup
docker compose exec -T postgres \
  pg_dump -U knowledge -d knowledge -Fc > backup/knowledge.dump
docker compose cp api:/data/knowledge backup/uploads
```

升级后：

```bash
docker compose up --build -d
curl -fsS http://localhost:8000/health/ready
docker compose run --rm api model-diagnostics
```

诊断会产生少量模型用量；仅在首次配置、模型切换、权限变化或排障时运行。

## 18. 相关文档

- [PostgreSQL FTS + pgvector 系统设计文档](postgresql-pgvector-fulltext-design.md)
- [pgvector 迁移与运维说明](pgvector-migration-and-operations.md)
- [历史 Elasticsearch 实施与在线回测报告](../evaluation/reports/elasticsearch-implementation-and-regression-report.md)
- [历史 OpenSearch 千文档基准报告](../evaluation/reports/benchmark-1k-report.md)
- [项目 README](../README.md)
