# PostgreSQL + pgvector 120 份交叉文档验收报告

## 1. 验收结论

2026-08-25 在独立 Docker Compose 验证环境中完成了 PostgreSQL 全文检索、
pgvector 向量检索、Qwen Embedding、Qwen Rerank 和 Qwen 生成回答的端到端验收。

本轮语料包含 120 份刻意设置为不规则、跨格式、跨文档关联且带有干扰值的文档。
全量检索门禁和抽样生成回答门禁均通过；PostgreSQL 容器重启后，文档索引和向量数量
保持一致。

## 2. 环境与语料

- 分支：`codex/postgresql-pgvector-fts`
- 数据库镜像：`pgvector/pgvector:0.8.1-pg16`
- 生成模型：`qwen3.7-plus-2026-05-26`
- Embedding：`text-embedding-v4`，1024 维
- Rerank：`qwen3-rerank`
- 语料目录：`evaluation/generated/crossdoc-20`
- 文档数量：120
- 文档格式：DOCX、HTML、Markdown、PDF、TXT、XLSX 各 20 份
- 业务组数量：20；每组 6 份文档相互引用
- 文档内容包含：正式值、退役值、会议候选值、跨语言名称、策略 ID、路由 ID、
  变更单、Excel 矩阵、错误码和降级队列
- 入库结果：120 成功、0 失败、0 重复
- PostgreSQL 检索块：320
- pgvector 向量：320

## 3. 全量检索结果

全量评测共 110 个问题，其中 100 个可回答，10 个应拒答。每类跨文档问题各 20 个：

- 三文档业务操作查询
- 三文档治理查询
- 三文档失败路径查询
- 跨区域矩阵比较
- 正式版本优先级查询
- 不存在业务对象的拒答查询

| 指标 | 结果 | 门禁 |
|---|---:|---:|
| Recall@5 | 100% | 通过 |
| Recall@10 | 100% | ≥ 90% |
| 不可回答问题拒答率 | 100% | ≥ 90% |
| Excel 来源定位准确率 | 100% | ≥ 90% |
| 答案所需事实覆盖率 | 100% | ≥ 95% |
| Qwen Rerank 成功 | 100 / 100 | 无失败 |
| 检索 P50 | 691.075 ms | 记录项 |
| 检索 P95 | 1,053.171 ms | ≤ 2,000 ms |
| 检索最大耗时 | 1,129.486 ms | 记录项 |

该数据集是多文档关联评测，最终答案通常需要 2–4 份来源。`Recall@1` 不作为门禁：
首位结果常是包含最终数值的矩阵、API 或变更单，而基准的主文档字段是负责建立关联的
业务需求索引。系统在前 5 个证据中保留完整来源集合，并由服务端校验最终引用。

## 4. Qwen 端到端回答结果

为控制免费生成额度，最终回答门禁抽取 20 个可回答问题和 4 个不可回答问题。

| 指标 | 结果 | 门禁 |
|---|---:|---:|
| 回答状态准确率 | 100% | ≥ 90% |
| 引用来源覆盖准确率 | 100% | ≥ 95% |
| 事实内容准确率 | 100% | ≥ 98% |
| 回答 P50 | 14,830.352 ms | 记录项 |
| 回答 P95 | 19,708.631 ms | Qwen 外部延迟单独记录 |
| 回答最大耗时 | 24,422.835 ms | 记录项 |

回答必须同时通过以下服务端规则才会返回：

1. 模型输出满足固定 JSON 契约。
2. 每项事实均包含知识库证据 ID。
3. 引用短句能够在对应证据块中精确定位或通过受限格式修复定位。
4. 引用对应当前数据库中的有效文档版本。
5. 跨文档回答保留“业务对象到策略、矩阵、路由或变更单”的映射证据。
6. 两次生成仍无法通过校验时严格拒答，不返回未经验证的结论。

## 5. 持久性验证

在全部 120 份文档入库后重启 PostgreSQL 容器，重启前后结果如下：

| 检查项 | 重启前 | 重启后 |
|---|---:|---:|
| `chunk_search_index` 行数 | 320 | 320 |
| 非空向量数 | 320 | 320 |
| API 数据库健康检查 | ready | ready |
| API 搜索健康检查 | ready | ready |

验证环境使用具名 Docker volume，重启未重新上传文档、未重算向量。结果证明文档元数据、
切块、全文检索字段和 pgvector 向量均由 PostgreSQL 持久维护。

## 6. 已修正的问题

初始实现中，Rerank 更倾向选择含最终值的下游文档，偶尔不会保留负责证明关联关系的
业务需求文档。最终实现增加了确定性的 provenance bridge：

- 只有同时命中问题主体和正式下游标识符的已批准证据才可成为桥接块。
- 业务需求、术语注册表、映射表等文档优先于偶然同时包含标识符的变更记录。
- 桥接只补充来源，不新增或修改模型结论。
- 所有补充引用继续接受数据库版本与原文短句校验。

对应回归测试已加入测试套件，当前共 92 项测试全部通过。

## 7. 复现命令

以下命令使用独立验证 Compose 文件，不会覆盖日常开发数据库：

```bash
export QWEN_API_KEY_SECRET_FILE="/absolute/path/to/Qwen token.txt"
export TELLERX_ENV_FILE=.env.example

docker compose -p tellerx-pgvector-gate -f docker-compose.verify.yml up -d --build

docker compose -p tellerx-pgvector-gate -f docker-compose.verify.yml run --rm \
  -v "$PWD/evaluation:/app/evaluation" api \
  knowledge-benchmark retrieve /app/evaluation/generated/crossdoc-20

docker compose -p tellerx-pgvector-gate -f docker-compose.verify.yml run --rm \
  -v "$PWD/evaluation:/app/evaluation" api \
  knowledge-benchmark answers /app/evaluation/generated/crossdoc-20 \
  --limit 20 --model qwen3.7-plus-2026-05-26

docker compose -p tellerx-pgvector-gate -f docker-compose.verify.yml stop
```

评测原始输出位于被 `.gitignore` 排除的 `evaluation/generated/crossdoc-20`，避免将大量
生成语料和运行结果提交到生产源码；本报告保存可审计的验收摘要。
