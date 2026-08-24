# TellerX Knowledge Chatbot

基于 Qwen API、Elasticsearch 和 PostgreSQL 的证据优先企业知识库。系统支持中英混合 Word、Excel、Markdown、HTML 和文本型 PDF，回答必须通过服务器端原文引用校验。

Docker 镜像使用确定性原生解析器：`python-docx`、`openpyxl`、BeautifulSoup 和
`pypdf`。项目不安装 Docling 的本地 OCR/视觉模型、PyTorch 或 CUDA 运行时。

向量检索使用百炼 `qwen3.7-text-embedding`（控制台名称：Qwen3.7-通用文本向量），
输出维度为 1024。模型、维度和预处理版本共同生成 Embedding fingerprint，并进入
Elasticsearch 物理索引名称，避免不同向量空间混用。

切换向量模型后，已有文档需要重新生成向量。服务启动后执行：

```bash
docker compose exec api knowledge-reindex
```

命令会复用对象存储中的持久向量，写入并验证新的物理索引，再原子切换读写别名；
如果目标 fingerprint 没有缓存，才调用 Qwen 生成缺失向量。临时只重建 BM25 可加
`--bm25-only`，但这不能通过正式混合检索验收。

完整文档：

- [生产代码重构与 100 文档回归报告](docs/production-refactoring-regression-report.md)
- [生产代码架构与维护约定](docs/production-code-architecture.md)
- [使用、开发与运维手册](docs/usage-and-operations-guide.md)
- [PostgreSQL + Elasticsearch 目标架构设计](docs/postgresql-elasticsearch-knowledge-base-design.md)
- [Elasticsearch 实施与在线回测报告](docs/elasticsearch-implementation-and-regression-report.md)
- [历史 PostgreSQL + OpenSearch 设计](docs/knowledge-base-chatbot-design.md)
- [历史 OpenSearch 千文档基准报告](docs/benchmark-1k-report.md)

## 本地启动

1. 确保 `Qwen/Qwen token.txt` 中只有百炼 API Key；该文件已被 Git 忽略。
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

## Qwen 连通性诊断

诊断会产生少量 API 用量，只在需要时显式运行：

```bash
docker compose run --rm api qwen-diagnostics
```

可使用 `--skip-chat`、`--skip-embedding` 或 `--skip-rerank` 单独跳过检查。诊断永远不输出 token。

该 token 在首次诊断时已验证可用的生成模型为：

- `qwen3.7-plus-2026-05-26`
- `qwen3.7-plus`
- `qwen3.7-max-2026-05-20`
- `qwen3.7-max`

`qwen3.7-max-2026-05-17` 和 `qwen3.7-max-preview` 当前返回模型参数错误，已在注册表中保留但禁用。
复杂问题优先 Max；若全部 Max 临时不可用，非固定评测请求会执行一次有完整证据约束的
Plus 降级，响应会显示实际模型和档位。固定模型评测不会自动切换。

2026-08-13 已重新验证 Chat、Embedding 和 Rerank 全部成功，并完成真实 Elasticsearch +
Qwen 千文档混合检索以及 Plus/Max 固定快照回答回归。精确结果见实施与在线回测报告。

## 评测

项目内置可重复的 1000 份混合格式压力语料生成器，生成内容位于
`evaluation/generated/`（不提交 Git）：

```bash
knowledge-benchmark generate \
  --output evaluation/generated/benchmark-1k \
  --count 1000 --questions 200 --seed 20260812 --force

# BM25-only 基线；即使 Qwen 暂时不可用也可验证解析、治理、检索和拒答
knowledge-benchmark load evaluation/generated/benchmark-1k --reset --no-embedding
knowledge-benchmark retrieve evaluation/generated/benchmark-1k --no-vector --no-rerank
knowledge-benchmark answers-offline evaluation/generated/benchmark-1k
knowledge-benchmark hybrid-offline evaluation/generated/benchmark-1k
knowledge-benchmark load-production-offline evaluation/generated/benchmark-1k --reset
knowledge-benchmark api-smoke-offline evaluation/generated/benchmark-1k
knowledge-benchmark project-filter-offline evaluation/generated/benchmark-1k

# 正式混合检索与 Qwen 答案回归（先确保 qwen-diagnostics 全部成功）
knowledge-benchmark index-existing evaluation/generated/benchmark-1k
knowledge-benchmark retrieve evaluation/generated/benchmark-1k
knowledge-benchmark answers evaluation/generated/benchmark-1k \
  --limit 20 --model qwen3.7-plus-2026-05-26
```

也可在账户恢复后使用一条命令执行全部 Qwen 门禁。诊断不通过时脚本会立即停止：

```bash
scripts/run-qwen-1k-gate.sh evaluation/generated/benchmark-1k
```

最近一次千文档实测结果见
[docs/elasticsearch-implementation-and-regression-report.md](docs/elasticsearch-implementation-and-regression-report.md)。

### 业务评测

将 `evaluation/sample.jsonl` 替换为业务真实题目后，固定快照运行。Docker 镜像不包含
宿主机的 `evaluation/`，需要显式挂载：

```bash
docker compose run --rm \
  -v "$PWD/evaluation:/app/evaluation" \
  api knowledge-eval /app/evaluation/sample.jsonl \
  --model qwen3.7-plus-2026-05-26 \
  --output /app/evaluation/plus-baseline.jsonl
```

固定 `--model` 时不会自动切换模型，便于可重复回归。

## 开发测试

```bash
python -m pip install -e '.[dev]'
pytest
ruff check app tests
```

本地 Docker 不可用时，可用 SQLite 和 mock 服务运行单元测试；完整链路仍需 PostgreSQL 和 Elasticsearch。
