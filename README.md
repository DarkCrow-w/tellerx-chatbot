# TellerX Knowledge Chatbot（本地核心版）

这是无需 Docker 的内部本地运行分支，只包含 React 前端、FastAPI 后端、数据库迁移和运行所需配置。

本地开发模式把文档解析、切块、Embedding 和 PostgreSQL 搜索索引发布放在 FastAPI 后台任务中，因此只需要启动两个进程：

- Python 后端：FastAPI、业务服务和文档入库任务；
- Node 前端：React + Vite 开发服务器。

## 1. 环境要求

- Python 3.12；
- Node.js `^20.19.0` 或 `>=22.12.0`；
- 可以访问公司 IKP PostgreSQL Service；
- PostgreSQL 已安装 `vector` 和 `pg_trgm` 扩展；
- 可以访问公司 OpenAI 兼容 SDK Endpoint。

PostgreSQL 必须安装 pgvector 0.7.0 或更高版本。项目使用 `halfvec(2560)` 和 HNSW
索引，以避开 `vector` 类型 HNSW 最多 2000 维的限制。模型固定使用：

- Embedding：`qwen3-embedding`；
- Chat：`qwen3.5-122B`；
- Rerank：关闭。

支持的文件格式为 `.docx`、`.xlsx`、`.xlsm`、`.md`、`.txt`、`.html`、文本型 `.pdf` 和 `.csv`。旧版 `.doc`、`.xls` 需要先另存为现代格式；扫描 PDF 需要先完成 OCR。

## 2. 安装

在项目根目录创建 Python 虚拟环境：

```bash
python3.12 -m venv .venv
source .venv/bin/activate
python -m pip install --upgrade pip
python -m pip install -e .
```

安装前端依赖：

```bash
npm install
```

复制本地配置：

```bash
cp .env.example .env
```

在 `.env` 中直接填写公司模型网关 Token。`.env` 已被 Git 忽略，不要把真实 Token 写入其他配置或提交到仓库。

## 3. 配置 IKP PostgreSQL

编辑 `.env` 中的 `DATABASE_URL`。项目在 IKP 内运行时应使用 PostgreSQL Service 的 Internal Endpoint，而不是普通 HTTP Ingress，例如：

```env
DATABASE_URL=postgresql+psycopg://tellerx_app:URL编码后的密码@postgresql.namespace.svc.cluster.local:5432/tellerx?sslmode=require
```

如果程序运行在公司电脑而不是 IKP Pod 内，必须先确认电脑能够解析并访问这个 Internal Endpoint。不能访问时，需要公司 VPN、PostgreSQL TCP Endpoint 或经过批准的端口转发。

数据库账号至少要能创建和使用项目表、索引。首次迁移还需要已经存在以下扩展：

```sql
CREATE EXTENSION IF NOT EXISTS vector;
CREATE EXTENSION IF NOT EXISTS pg_trgm;
```

如果应用账号没有安装扩展的权限，请让数据库管理员预先安装。

## 4. 配置公司模型接口

编辑 `.env`：

```env
MODEL_API_BASE_URL=https://公司的SDK-Endpoint/v1
MODEL_API_KEY="公司模型网关Token"
EMBEDDING_MODEL=qwen3-embedding
EMBEDDING_DIMENSIONS=2560
MODEL_REGISTRY_PATH=config/models.yaml
RERANK_ENABLED=false
```

诊断模型连通性会产生少量模型调用：

```bash
model-diagnostics
```

从旧版 1024 维结构升级时，请先把 `.env` 中的 `EMBEDDING_DIMENSIONS` 改为
`2560`，再执行 `alembic upgrade head`。迁移会保留关键词搜索数据，但会清除
不兼容的旧向量。迁移完成后执行以下命令，为已有文档重新生成 2560 维向量：

```bash
knowledge-reindex
```

重建完成前已有文档仍可使用关键词检索；新上传文档不受影响。

## 5. 启动前后端

确保 Python 虚拟环境已激活，然后在项目根目录执行：

```bash
npm run local
```

这条命令会同时启动：

- 后端：<http://127.0.0.1:8000>；
- API 文档：<http://127.0.0.1:8000/docs>；
- 前端：<http://localhost:5173>。

后端启动前会自动执行 `alembic upgrade head`。数据库迁移失败时，前端仍可能启动，但后端会明确退出；优先检查 `DATABASE_URL`、网络、TLS 和数据库权限。

使用 `Ctrl+C` 会同时停止前后端。

后端日志默认输出到当前终端，包含 HTTP 请求 ID、状态码、耗时，以及数据库迁移、
文档解析、Embedding、索引发布、检索和 Chat 调用等关键阶段。IKP 部署时直接由平台
采集标准输出即可，不需要在应用内配置日志文件。临时排障可在 `.env` 中设置：

```env
LOG_LEVEL=DEBUG
```

每个 HTTP 响应都会返回 `X-Request-ID`；出现问题时可用该值在日志中串联同一次请求。
日志不会记录模型 Token、问题正文、Prompt 或文档正文。

如果 8000 端口已被占用，可以整体改用其他后端端口，Vite 代理会自动同步：

```bash
TELLERX_API_PORT=18001 npm run local
```

也可以分开启动，便于分别查看日志：

```bash
# 终端 1
source .venv/bin/activate
tellerx-backend

# 终端 2
npm run dev
```

如果已经由管理员执行过迁移，可以跳过启动时迁移：

```bash
tellerx-backend --skip-migrations
```

## 6. 上传和使用文档

打开前端后，从左侧进入“知识库管理”：

1. 新建或选择一个知识库；
2. 点击“选择文件”上传一个或多个文档，或点击“选择文件夹”导入完整目录；
3. 等待页面中的任务状态变成“可检索”；
4. 返回问答页，在“知识范围”中选择对应知识库后提问。

文件夹导入会保留根目录以内的相对路径，用于区分不同目录下的同名文件。每次最多并行构建两份文档；单份失败不会阻断其余文件。管理页还可以查看历史版本、重试失败任务、上传新版本、下载、废弃或软删除文档。

管理页提供两种不可恢复的知识库级操作，执行前必须输入完整知识库名称确认：

- “清理删除残留”只物理回收已经软删除的文档、版本、分块、任务、解析产物和不再被其他文档引用的向量缓存，仍在使用的文档不受影响；
- “删除知识库”会执行相同清理，并删除知识库本身。

普通单文档和批量“删除所选”仍是软删除，便于重新上传同一逻辑文档；软删除后的文档不会再被全量重建、索引修复或入库 Worker 处理。需要回收它们占用的磁盘和数据库空间时，使用“清理删除残留”或“删除知识库”。共享原文件或向量仍被其他知识库引用时不会被误删。

需要调试接口时仍可打开 Swagger：

<http://127.0.0.1:8000/docs>

调用 `POST /api/v1/documents`，或执行：

```bash
curl -X POST 'http://127.0.0.1:8000/api/v1/documents' \
  -F 'file=@/absolute/path/example.docx' \
  -F 'project=TellerX' \
  -F 'document_type=business-document' \
  -F 'lifecycle_status=approved' \
  -F 'version_label=1.0'
```

返回的 `job_id` 可用于查询处理状态：

```bash
curl 'http://127.0.0.1:8000/api/v1/ingestion-jobs/JOB_ID'
```

本地模式不需要单独启动 Worker 或 Indexer。处理状态到达 `succeeded` 后，文档才可以被检索。

## 7. 前端构建

开发时直接使用 `npm run dev`。如需让 FastAPI 同时提供构建后的前端：

```bash
npm run build
tellerx-backend
```

构建结果写入 `app/static/`。未构建前端时，访问后端根路径会跳转到 `/docs`，不会影响 Vite 前端。

## 8. 核心目录

```text
app/                 FastAPI 后端和业务代码
frontend/            React 前端源码
config/models.yaml   公司 Chat 模型清单
alembic/             PostgreSQL 数据库迁移
.env.example         本地配置模板
pyproject.toml       Python 依赖和命令入口
package.json         前端依赖和启动命令
```

`.env`、`.local-data/`、`.venv/` 和 `node_modules/` 均不会提交到 Git。

## 9. 开发检查

```bash
python -m unittest discover -s tests
npm test
npm run build
ruff check app
```

这些检查不调用公司模型接口；完整验收仍应在已配置 PostgreSQL 和模型 Token 的环境中上传真实文档并执行一次问答。
