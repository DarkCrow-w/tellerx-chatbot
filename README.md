# TellerX Knowledge Chatbot — 纯源码版

本分支保留 React 前端、FastAPI 后端、本地自动化测试、独立初始数据库迁移与必要依赖和配置。Python 源码统一使用 `.txt` 后缀，包括包入口、测试和迁移；内容仍是 Python，阅读交付版不能直接启动后端。

## 目录

| 路径 | 内容 |
| --- | --- |
| `frontend/src/` | 聊天、流式进度、证据卡片、知识库管理与前端测试 |
| `frontend/index.html`、`frontend/vite.config.js` | 前端入口与构建配置 |
| `app/` | 后端接口、用例、检索、入库、模型与数据访问源码（`.txt`） |
| `tests/` | 后端本地测试（`.txt`），使用测试替身或内存 SQLite |
| `alembic/`、`alembic.ini` | 数据库初始迁移与迁移配置 |
| `config/models.yaml`、`.env.example` | 模型注册表与不含真实凭据的环境模板 |
| `pyproject.toml`、`package.json`、`package-lock.json` | 安装、测试和构建依赖 |

评估脚本、评估数据、设计文档及依赖独立 PostgreSQL 的集成测试已移除。运行数据、构建产物、虚拟环境、依赖目录和真实 `.env` 不属于交付内容。

## 恢复 Python 后缀

需要运行时，先复制本分支到独立目录，再在复制目录根路径执行下列命令。仅转换三个源码目录；不会重命名业务文档或第三方依赖。

```bash
python3 - <<'PY'
from pathlib import Path

sources = [path for root in ("app", "tests", "alembic")
           for path in Path(root).rglob("*.txt")]
conflicts = [path.with_suffix(".py") for path in sources
             if path.with_suffix(".py").exists()]
if conflicts:
    raise SystemExit(f"目标文件已存在，未执行转换：{conflicts}")
for path in sources:
    path.rename(path.with_suffix(".py"))
print(f"已恢复 {len(sources)} 个 Python 文件")
PY
```

文件内的 Python 导入路径与工具配置保持运行时写法，无需修改。`alembic/script.py.mako` 是迁移生成模板，不是 `.py` 源文件，因此保留原名。

## 安装与配置

要求 Python 3.12 或更新版本、Node.js `^20.19.0` 或 `>=22.12.0`。运行后端还需 PostgreSQL、pgvector 0.7.0 或更新版本及 `pg_trgm` 扩展，以及可用的 OpenAI 兼容模型服务。

恢复后缀后执行：

```bash
python3 -m venv .venv
source .venv/bin/activate
python -m pip install -e '.[dev]'
npm ci
cp .env.example .env
```

编辑 `.env` 中的 `DATABASE_URL`、`MODEL_API_BASE_URL` 和 `MODEL_API_KEY`；数据库密码中的特殊字符须 URL 编码。不要提交真实凭据。

生成模型由 `config/models.yaml` 配置，当前为 `glm-5.2`；Embedding 默认使用 `qwen3-embedding`、2560 维。重排默认关闭。数据与原文件默认保存在 `.local-data/knowledge`。

## 数据库初版

唯一迁移是 `alembic/versions/0001_initial.txt`，恢复后缀后为 `.py`；`down_revision = None`。

该初版固定了原迁移链截至 `0006_hierarchical_retrieval` 的最终结构，直接创建 21 张业务表，包括文档版本、章节树、向量缓存、入库任务、查询追踪和搜索投影。保留全文搜索、三元组索引、`halfvec(2560)`、HNSW 索引及当前批准版本的唯一性约束。迁移不导入应用模型，也不包含历史数据回填。

**仅用于新建空数据库。** 现有数据库应继续使用原开发分支的迁移链；不要对旧数据库直接运行此初版或仅修改版本号冒充升级。本次整理没有修改已有业务数据库，也没有导出业务数据。

将 `.env` 的 `DATABASE_URL` 指向新建数据库，再执行：

```bash
alembic upgrade head
```

数据库需允许创建表、索引及所需扩展；若应用账号不能创建扩展，由管理员提前安装 `vector` 和 `pg_trgm`。回滚会删除全部业务表，保留可能共享的扩展。

## 启动

```bash
npm run local
```

默认前端端口为 5173，后端端口为 8000。也可分别执行 `npm run backend` 和 `npm run dev`。后端启动时会执行数据库迁移，因此运行前务必完成新数据库配置。

支持文档上传、版本管理、全库或指定文档检索、流式处理进度、引用原文、章节上下文和证据文档下载。解析、向量化及索引发布由后端后台任务处理。

## 本地测试与构建

恢复后缀后执行：

```bash
python -m pytest -q
ruff check app tests/test_initial_migration.py
npm test
npm run build
```

这些测试不要求真实模型服务或外部数据库。初始迁移测试以 PostgreSQL 方言离线生成 SQL，检查建表、索引和回滚边界；前端测试包含请求协议、状态规则和组件渲染。构建产物写入被忽略的 `app/static/`。

本次合并迁移另在临时 PostgreSQL + pgvector 环境中验证：旧六步迁移链与新初版的 191 个字段、45 个约束、88 个索引及扩展一致，并通过回滚后重新建库验证。临时数据库仅用于验证，不属于交付内容。
