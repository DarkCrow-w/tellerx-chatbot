# 1000 文档知识库功能与规模回测报告

> 历史报告：本文记录 2026-08-12 的 OpenSearch/BM25 阶段结果。当前 PostgreSQL +
> Elasticsearch 9.4.3 + 真实 Qwen Embedding/Rerank/Plus/Max 的正式结果请见
> [Elasticsearch 实施与在线回测报告](elasticsearch-implementation-and-regression-report.md)。

## 1. 结论

2026-08-12 在本地 PostgreSQL 17 与 OpenSearch 3.8.0（含 k-NN 插件）上完成了
1000 个物理文件的解析、版本治理、索引和 230 题 BM25 基线回测。

已实测通过：

- 1000 个文件均成功解析，无解析警告，形成 2550 个知识块。
- 210 个可回答问题的 Recall@5、Recall@10 均为 100%，其中包含 10 个双文档比较问题。
- 10 个 Deprecated 编号与 10 个不存在的精确缩写全部无证据拒答。
- Approved 版本优先率与 Excel 工作表/单元格范围定位准确率均为 100%。
- 本地检索 P95 为 14.005 ms，远低于 2 秒门槛。
- 离线证据约束答案链路 230/230 通过，状态、引用、事实和 Approved-only 均为 100%。
- 27 个单元测试通过，Ruff 全量检查通过。

未完成正式验收：Qwen Chat、`text-embedding-v4` 和 `qwen3-rerank` 当前均被百炼
以 HTTP 400 / `Arrearage` 拒绝。因此本报告不声称向量召回、Rerank 或真实 Qwen
答案质量已通过；账户恢复后必须执行第 6 节命令。

## 2. 语料构成

生成器固定使用 seed `20260812`，语料可完整重建，不依赖生成模型：

| 项目 | 数量 |
|---|---:|
| 物理文件 | 1000 |
| 逻辑文档 | 990 |
| Approved 版本 | 980 |
| Draft 冲突版本 | 10 |
| Deprecated 版本 | 10 |
| 项目 | 20 |
| 知识块 | 2550 |
| 可回答问题 | 210 |
| 不可回答问题 | 20 |

格式分布：Markdown 260、TXT 200、HTML 150、DOCX 160、XLSX 150、文本 PDF 80。
来源分布：普通上传 803、Confluence 导出 135、OneNote 导出 62；文档类型在业务规则、
系统设计、需求和运行手册之间均匀分布。
内容覆盖中英文业务实体、精确编号、审批金额与角色、服务超时、审计保留期、
生命周期代码、英文缩写、Excel 坐标和 Approved/Draft 版本冲突。

## 3. 导入性能

| 指标 | 结果 |
|---|---:|
| 总导入耗时（不含 Embedding） | 3.154 s |
| 吞吐 | 317.09 文件/s |
| 单文件解析 P50 | 0.176 ms |
| 单文件解析 P95 | 4.416 ms |
| 单文件解析最大值 | 31.476 ms |
| 解析警告 | 0 |

## 4. 检索结果

本表是完全关闭向量和 Rerank 后的保底能力，测试对象为 1000 个物理文件：

| 指标 | 结果 | 设计门槛 |
|---|---:|---:|
| Recall@1 | 95.24% | — |
| Recall@5 | 100% | — |
| Recall@10 | 100% | ≥ 90% |
| MRR | 0.9762 | — |
| 不可回答拒答率 | 100% | ≥ 90% |
| Approved 优先率 | 100% | — |
| Excel 来源定位准确率 | 100% | ≥ 90% |
| 检索 P95 | 14.005 ms | ≤ 2 s |

七类可回答问题（版本治理、精确编号、中文语义实体、跨语言、英文缩写、生命周期状态、双文档比较）的
Recall@5 均为 100%。这是合成基准，不替代业务人员对 50–100 个真实问题的人工验收。

### 离线答案编排验证

`knowledge-benchmark answers-offline` 使用确定性、禁止联网的证据响应器，仍经过正式
`AnswerService` 的检索、Plus/Max 规则路由、JSON 解析、引用 ID 校验、原文连续短句校验、
状态治理、消息持久化和严格拒答代码。它验证系统编排，不评价 Qwen 的语言能力。

| 指标 | 结果 |
|---|---:|
| 问题数 | 230 |
| 状态准确率 | 100% |
| 引用准确率 | 100% |
| 预期事实包含率 | 100% |
| Approved-only 准确率 | 100% |
| Plus / Max 路由次数 | 200 / 10 |
| 答案编排 P95 | 11.587 ms |

首轮该门禁发现一个 PDF 抽取问题：视觉换行把 `TX-0929-READY` 拆成
`TX-0929-R\nEADY`，文档命中但精确引用无法成立。解析器现在只重连 ASCII 标识符内部的
视觉换行，并新增 PDF 回归测试；重新解析全部 1000 文件后 230/230 通过。

### 离线混合检索机械验证

`knowledge-benchmark hybrid-offline` 为 2550 个块生成确定性的 1024 维特征哈希向量，
实际经过 OpenSearch k-NN、BM25、RRF 和本地二次排序。该模式只验证向量索引与混合编排，
不能代替 Qwen Embedding/Rerank 的语义质量验收。

| 指标 | 结果 |
|---|---:|
| 向量索引知识块 | 2550 |
| Recall@1 | 95.24% |
| Recall@5 / Recall@10 | 100% / 100% |
| 不可回答拒答率 | 100% |
| 检索 P95 | 20.865 ms |

首轮机械 Rerank 仅按通用词相似度排序，反而把强实体命中挤出前八，Recall@10 只有
72.86%。修复后 Rerank 保留精确编号、实体名和缩写硬信号，再跑达到 100%。这一结果
说明加入向量和 Rerank 不天然提高准确率，正式 Qwen 混合门禁必须独立通过，不能沿用
BM25 成绩。

### 生产导入管道与公开 API 验证

`load-production-offline` 让 1000 个任务逐一经过生产 `IngestionService`，包括 PostgreSQL
`SKIP LOCKED` 领取、解析、Embedding 不可用降级、Chunk 重建、OpenSearch 索引、任务进度
和完成状态提交。它使用离线故障 Provider，不产生 Qwen 调用。

| 指标 | 结果 |
|---|---:|
| Ingestion jobs | 1000 |
| 成功 / 失败 | 1000 / 0 |
| 生成知识块 | 2550 |
| 总耗时 | 44.56 s |
| 任务领取 P95 | 3.733 ms |
| 单任务处理 P95 | 93.785 ms |

`api-smoke-offline` 再选择六种格式各一个文件，通过公开 HTTP API 验证上传、任务查询、
重复上传幂等、来源查看和原始文件下载；所有检查通过，下载内容与原始文件逐字节一致。

项目隔离另用 200 个单文档问题逐题带入真实 `project_id` 过滤：预期文档命中率和返回
证据项目隔离率均为 100%，没有从其他 19 个项目泄漏证据。

## 5. 回测发现并修复的问题

1. 精确编号防幻觉过滤错误地用 `set.issubset(string)` 比较完整编号和单个字符，
   导致真实编号也被过滤。现改为完整子串匹配并加入回归测试。
2. 编号在长问题中会被 BM25 高频词稀释。索引新增 `identifiers` keyword 字段，
   精确编号作为强召回信号；不存在的编号仍返回空证据。
3. 相似业务名在 1000 文档下容易互相干扰。检索器以确定性规则抽取中文业务实体、
   英文实体和缩写，使用短语加权，不调用额外大模型、不消耗额度。
4. OpenSearch 基础安装不含 k-NN 插件会导致向量 mapping 创建失败；本地环境已安装
   与 OpenSearch 3.8.0 匹配的 `opensearch-knn` 3.8.0.0。Docker 镜像仍应在构建阶段固定插件版本。
5. OpenSearch alias 404 响应曾可能把 `status` 当作索引名删除；现只处理含 `aliases`
   结构的真实索引，并有回归测试。
6. Qwen 全模型不可用时，回答服务原先可能抛服务器错误；现严格降级为
   `insufficient_evidence`，不泄露 provider 错误详情，也不生成无引用结论。
7. 诊断输出现在只暴露组件、模型、HTTP 状态与安全错误码，不输出 token 或响应正文。
8. 评测报告区分“请求 Rerank”和“实际 Rerank 生效”；API 失败后静默降级会使正式门禁
   返回非零退出码，不允许把 RRF 结果误报成 Rerank 验收通过。
9. 精确编号保护原先会要求一个块同时包含查询中的所有编号，导致跨文档比较被误拒答；
   现在要求所有编号分别被完整证据集合覆盖，并拒绝任一编号不存在的部分回答。
10. 二次排序必须保留编号、实体名和缩写等确定性硬信号；离线混合验证证明仅按模糊
    相似度重排可能显著降低召回。
11. 批量基准不能代替生产 Worker 验证；新增 1000 个真实 ingestion job 的规模门禁和
    六格式 HTTP API 冒烟回测。
12. `project_ids` 过滤需独立验证；新增 200 题跨 20 项目的隔离门禁。
13. 完整唯一实体命中时删除无关候选文档，防止单文档问题因检索噪声误升级 Max；在不
    降低准确率的前提下，离线路由由 Plus/Max 124/86 改善为 200/10。
14. `model_usage` 增加 `prompt_version` 并贯穿成功、失败调用记录；Alembic 0001→0002
    已分别在现有千文档数据库和全新 PostgreSQL 数据库验证通过。

## 6. 账户恢复后的强制复测

先确认三类接口全部成功：

```bash
qwen-diagnostics
```

然后建立真实 Qwen 向量索引并跑完整混合召回：

```bash
knowledge-benchmark index-existing evaluation/generated/benchmark-1k
knowledge-benchmark retrieve evaluation/generated/benchmark-1k
```

混合检索必须维持 Recall@10 ≥ 90%、不可回答拒答率 ≥ 90%、Excel 定位 ≥ 90%。
Embedding 按内容 hash 持久缓存，若中途失败可断点续传，避免重复消耗额度。

最后分别固定 Plus 与 Max 快照进行答案验证：

```bash
knowledge-benchmark answers evaluation/generated/benchmark-1k \
  --limit 20 --model qwen3.7-plus-2026-05-26
knowledge-benchmark answers evaluation/generated/benchmark-1k \
  --limit 20 --model qwen3.7-max-2026-05-20
```

答案层至少要求引用准确率 ≥ 95%、无证据结论率 ≤ 2%、端到端 P95 ≤ 10 秒。
该合成集通过后仍需运行独立、人工标注的真实业务题集。

上述步骤可合并为一条安全恢复命令；它会在诊断失败时立即停止：

```bash
scripts/run-qwen-1k-gate.sh evaluation/generated/benchmark-1k
```

## 7. 原始结果

可重建语料及机器可读结果位于：

- `evaluation/generated/benchmark-1k/generation-summary.json`
- `evaluation/generated/benchmark-1k/load-report.json`
- `evaluation/generated/benchmark-1k/retrieval-v0-r0.json`
- `evaluation/generated/benchmark-1k/answer-pipeline-offline.json`
- `evaluation/generated/benchmark-1k/index-offline-hybrid-report.json`
- `evaluation/generated/benchmark-1k/retrieval-offline-hybrid.json`
- `evaluation/generated/benchmark-1k/production-ingestion-offline.json`
- `evaluation/generated/benchmark-1k/api-smoke-offline.json`
- `evaluation/generated/benchmark-1k/project-filter-offline.json`
- `evaluation/generated/benchmark-1k/questions.jsonl`

目录被 Git 忽略，避免向仓库提交约千份二进制测试文件；生成器、测试和本报告会提交。
