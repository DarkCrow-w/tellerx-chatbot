# 跨文档复杂知识检索与问答评测报告

## 1. 评测目标

验证知识库能否在文档结构不规则、信息分散、存在历史版本和草稿干扰的情况下：

1. 从不同格式的两到三份文档中找到同一业务对象的关联证据。
2. 根据当前生效且已批准的证据回答，避免采用废弃版本或会议草稿。
3. 返回覆盖结论所需的全部来源，而不是只引用其中一份文档。
4. 对知识库中不存在的业务对象严格拒答。

## 2. 测试语料

- 项目：`Codex-CrossDoc-20-Test`
- 业务知识簇：20 个
- 文档：120 份
- 格式：Markdown、DOCX、XLSX、HTML、PDF、TXT 各 20 份
- 生命周期：100 份 approved，20 份 draft
- 问题：100 个可回答问题，10 个不可回答问题
- 每个可回答问题需要联合 2 到 3 份文档

每个业务知识簇包含：业务需求入口、策略架构、参数矩阵、路由 API、批准的变更通知和未批准会议草稿。文档间通过业务 ID、策略 ID、变更 ID、接口路径、失败码和降级队列形成关联。

语料定义见：

- `evaluation/generated/crossdoc-20/generation-summary.json`
- `evaluation/generated/crossdoc-20/relation-graph.json`
- `evaluation/generated/crossdoc-20/questions.jsonl`

## 3. 检索结果

最终检索对全部 110 个问题执行真实 Elasticsearch、Embedding 和 Qwen Rerank 流程。

| 指标 | 结果 |
|---|---:|
| 可回答问题 | 100 |
| Recall@5 | 100% |
| Recall@10 | 100% |
| 知识库外问题正确拒绝检索 | 100% |
| Excel 来源位置准确率 | 100% |
| approved/current 优先率 | 100% |
| 本地检索 P95 | 721.265 ms |

五种问题类型的 Recall@5 均为 100%：

- 三文档业务操作链路
- 三文档治理和生效链路
- 三文档失败及降级链路
- 跨区域参数比较
- 新旧版本优先级判断

详细结果：`evaluation/generated/crossdoc-20/retrieval-v1-r1.json`。

## 4. 端到端回答结果

使用固定模型 `qwen3.7-plus-2026-05-26`，选取每类 6 题，共 30 个可回答问题，并加入 6 个知识库外问题。每题经过完整检索、Rerank、回答生成和服务端引用验证。

| 指标 | 结果 |
|---|---:|
| 回答状态准确率 | 100% |
| 答案关键内容准确率 | 100% |
| 必需来源引用完整率 | 100% |
| 6 个无证据问题拒答率 | 100% |
| 端到端 P95 | 15.209 s |

详细结果：`evaluation/generated/crossdoc-20/answer-report-qwen3.7-plus-2026-05-26.json`。

这里的 100% 是当前固定语料和本次固定模型运行的结果，不代表任意生产问题都能达到绝对 100%。上线后仍应持续补充真实业务评测集，并在模型、Prompt、Embedding 或索引版本变化时重新运行基线。

## 5. 持久化一致性

最终运行时检查：

| 对象 | PostgreSQL | Elasticsearch |
|---|---:|---:|
| 逻辑文档 | 120 | 按内容块索引 |
| 当前可检索版本 | 120 | 120 份文档对应内容块 |
| 当前内容块 | 300 | 300 |

PostgreSQL 保存原始事实、版本、生命周期和块内容；Elasticsearch 保存可重建的检索索引。HTML 解析器多次升级产生的旧版本仍保存在 PostgreSQL 中，但只标记当前版本为可检索，检索不会混入被替代版本。

## 6. 关键改造

- 从首轮证据发现关联业务 ID、策略 ID、变更 ID、错误码和队列 ID。
- 执行第二阶段关联检索，并只扩展已证明相关的文档。
- 使用文档多样性约束，避免前几个结果全部来自同一文件。
- 优先 approved、current 和明确标注当前生效的内容。
- 保留 Excel 工作表、单元格范围以及 PDF 页码。
- 对跨文档结论补齐必需引用，并验证引用原文真实存在。
- 对引用中的省略号仅允许修复为同一来源内的连续原文。
- 证据不足时不升级模型猜测，直接返回 `insufficient_evidence`。

## 7. 可复现命令

生成语料：

```bash
node evaluation/scripts/generate-crossdoc-corpus.mjs --force
```

通过公开 API 上传：

```bash
python evaluation/scripts/upload_evaluation_corpus.py \
  evaluation/generated/crossdoc-20 \
  --base-url http://localhost:8000
```

运行全部检索问题：

```bash
python -m evaluation.benchmark.cli retrieve evaluation/generated/crossdoc-20
```

运行端到端回答样本：

```bash
python -m evaluation.benchmark.cli answers evaluation/generated/crossdoc-20 \
  --limit 30 \
  --model qwen3.7-plus-2026-05-26
```

验收门槛未通过时，`retrieve` 和 `answers` 命令会以非零状态码退出。

## 8. 最终结论

当前实现已经能够对本组复杂交叉文档进行稳定的多跳检索，并在端到端样本中给出内容和引用均正确的回答。对于知识库外对象，系统在生成模型调用前拒答。该能力由确定性的关联检索、版本治理、Rerank 和服务端引用校验共同保证，而不是依赖模型自行猜测文档关系。
