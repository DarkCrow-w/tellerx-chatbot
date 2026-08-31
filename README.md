# TellerX Knowledge Chatbot

基于公司 OpenAI 兼容模型网关、PostgreSQL 全文检索和 pgvector 的证据优先企业知识库。系统支持中英混合 Word、Excel、Markdown、HTML 和文本型 PDF，回答必须通过服务器端原文引用校验。

Docker 镜像使用确定性原生解析器：`python-docx`、`openpyxl`、BeautifulSoup 和
`pypdf`。项目不安装 Docling 的本地 OCR/视觉模型、PyTorch 或 CUDA 运行时。

内部向量检索使用 `qwen3-embedding`，Chat 使用 `qwen3.5-122B`，两者均通过
`openai.OpenAI` SDK 和系统证书信任库访问公司 Endpoint。向量必须输出 1024 维；
模型、维度和预处理版本共同生成 Embedding fingerprint，并进入
PostgreSQL 搜索行；向量查询只使用 fingerprint 匹配的 pgvector 数据，避免不同向量空间混用。
内部环境没有专用 Rerank 模型，本分支不会发送 Rerank 请求，候选结果使用 BM25、
pgvector 和 RRF 融合排序。

切换向量模型后，已有文档需要重新生成向量。服务启动后执行：

```bash
docker compose exec api knowledge-reindex
```

命令会复用对象存储中的持久向量，事务性写入全文、精确词和 pgvector 搜索行并逐版本验证；
如果目标 fingerprint 没有缓存，才调用 Embedding 模型生成缺失向量。临时只重建 BM25 可加
`--bm25-only`，但这不能通过正式混合检索验收。

完整文档：

- [生产代码重构与 100 文档回归报告](evaluation/reports/production-refactoring-regression-report.md)
- [生产代码架构与维护约定](docs/production-code-architecture.md)
- [使用、开发与运维手册](docs/usage-and-operations-guide.md)
- [公司 OpenAI 兼容模型网关配置](docs/internal-openai-model-api.md)
- [PostgreSQL + pgvector + 全文检索架构设计](docs/postgresql-pgvector-fulltext-design.md)
- [pgvector 迁移与运行手册](docs/pgvector-migration-and-operations.md)
- [历史 Elasticsearch 实施与在线回测报告](evaluation/reports/elasticsearch-implementation-and-regression-report.md)
- [历史 PostgreSQL + OpenSearch 设计](docs/knowledge-base-chatbot-design.md)
- [历史 OpenSearch 千文档基准报告](evaluation/reports/benchmark-1k-report.md)

## 本地启动

1. 确保 `Qwen/Qwen token.txt` 中只有模型网关 Token；该文件已被 Git 忽略。
   Docker 构建上下文也会排除整个 `Qwen` 目录，token 仅在容器运行时通过 secret 挂载。
2. 复制配置：

   ```bash
   cp .env.example .env
   ```

3. 启动：

   ```bash
   docker compose up --build
   ```

4. 打开 <http://localhost:8000>，API 文档位于 <http://localhost:8000/docs>。

前端使用 React + Vite。开发界面时，可在后端运行后另开终端启动热更新服务：

```bash
npm install
npm run dev
```

打开 <http://localhost:5173>。提交或直接用 FastAPI 运行前，执行 `npm run build`，
构建结果会写入 `app/static/`，Docker 构建也会自动完成此步骤。

上传文档：

```bash
curl -F 'file=@./example.docx' \
  -F 'project=TellerX' \
  -F 'document_type=system-design' \
  -F 'lifecycle_status=approved' \
  -F 'version_label=1.0' \
  http://localhost:8000/api/v1/documents
```

查询返回的 `job_id` 可用于查看导入进度：

```bash
curl http://localhost:8000/api/v1/ingestion-jobs/JOB_ID
```

## 模型网关连通性诊断

诊断会产生少量 API 用量，只在需要时显式运行：

```bash
docker compose run --rm api model-diagnostics
```

可使用 `--skip-chat` 或 `--skip-embedding` 单独跳过检查。诊断永远不输出 token，
并会明确输出 `rerank: disabled`。内部模型清单位于 `config/models.yaml`；本地使用百炼
验证 SDK 时，可显式覆盖 Endpoint、模型和 `MODEL_REGISTRY_PATH=config/models.dashscope-local.yaml`。

2026-08-13 的 Chat、Embedding、Rerank 和 Elasticsearch 回归结果仅作为迁移前历史基线。
pgvector 架构必须在目标机器完成迁移、`knowledge-reindex`、`knowledge-reconcile` 和同题集
回归后才能正式验收，不能直接沿用历史搜索指标。

## 评测

评测代码与生产包严格分离，目录约定和入口见
[`evaluation/README.md`](evaluation/README.md)。运行前安装 `.[dev,quality]` 依赖。

项目内置可重复的 1000 份混合格式压力语料生成器，生成内容位于
`evaluation/generated/`（不提交 Git）：

```bash
python -m evaluation.benchmark.cli generate \
  --output evaluation/generated/benchmark-1k \
  --count 1000 --questions 200 --seed 20260812 --force

# BM25-only 基线；即使 Qwen 暂时不可用也可验证解析、治理、检索和拒答
python -m evaluation.benchmark.cli load evaluation/generated/benchmark-1k --reset --no-embedding
python -m evaluation.benchmark.cli retrieve evaluation/generated/benchmark-1k --no-vector --no-rerank
python -m evaluation.benchmark.cli answers-offline evaluation/generated/benchmark-1k
python -m evaluation.benchmark.cli hybrid-offline evaluation/generated/benchmark-1k
python -m evaluation.benchmark.cli load-production-offline evaluation/generated/benchmark-1k --reset
python -m evaluation.benchmark.cli api-smoke-offline evaluation/generated/benchmark-1k
python -m evaluation.benchmark.cli project-filter-offline evaluation/generated/benchmark-1k

# 正式混合检索与模型答案回归（先确保 model-diagnostics 全部成功）
python -m evaluation.benchmark.cli index-existing evaluation/generated/benchmark-1k
python -m evaluation.benchmark.cli retrieve evaluation/generated/benchmark-1k
python -m evaluation.benchmark.cli answers evaluation/generated/benchmark-1k \
  --limit 20 --model qwen3.7-plus-2026-05-26
```

也可在账户恢复后使用一条命令执行全部 Qwen 门禁。诊断不通过时脚本会立即停止：

```bash
evaluation/scripts/run-qwen-1k-gate.sh evaluation/generated/benchmark-1k
```

迁移前的千文档实测结果见
[历史 Elasticsearch 回测报告](evaluation/reports/elasticsearch-implementation-and-regression-report.md)；它不是当前
pgvector 后端的验收结果。

### 业务评测

将 `evaluation/datasets/business/sample.jsonl` 替换为业务真实题目后，固定快照运行。Docker 镜像不包含
宿主机的 `evaluation/`，需要显式挂载：

```bash
docker compose run --rm \
  -v "$PWD/evaluation:/app/evaluation" \
  api python -m evaluation.business /app/evaluation/datasets/business/sample.jsonl \
  --model qwen3.7-plus-2026-05-26 \
  --output /app/evaluation/plus-baseline.jsonl
```

固定 `--model` 时不会自动切换模型，便于可重复回归。

## 开发测试

```bash
python -m pip install -e '.[dev,quality]'
pytest
ruff check app evaluation tests
npm run build

# 专用临时库：空库迁移 + PostgreSQL FTS + pgvector HNSW 集成验证
evaluation/scripts/run-pgvector-integration.sh

# 独立 Compose 项目：120 份交叉文档、110 题、模型网关与重启持久性门禁
MODEL_API_KEY_SECRET_FILE='/path/to/model API token.txt' \
  evaluation/scripts/run-crossdoc-pgvector-gate.sh
```

本地 Docker 不可用时，可用 SQLite 和 mock 服务运行单元测试；完整链路仍需安装 `vector` 和
`pg_trgm` 扩展的 PostgreSQL。推荐直接使用 Compose 中固定版本的 pgvector 镜像。
