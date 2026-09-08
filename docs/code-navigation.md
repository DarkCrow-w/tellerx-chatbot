# 代码阅读与维护指南

这次整理以 `8371848` 为起点，保留现有接口、检索规则、证据预算和数据库结构。先沿主流程阅读，再进入对应规则文件，会比逐行阅读整个项目更容易。

## 一次问答怎么走

1. `app/api/routes/chat.py` 接收请求。
2. `app/application/chat_service.py` 进入问答用例。
3. `app/services/answering.py` 准备证据、调用模型、校验并保存结果。成功、拒答和模型失败统一在一个位置保存。
4. `app/services/retrieval.py` 决定文档范围，编排各召回阶段。
5. `app/integrations/search.py` 执行 PostgreSQL 查询，保持项目、权限和版本过滤。

### 检索规则去哪改

| 要改的内容 | 文件 | 说明 |
|---|---|---|
| 文本规范化、分词、业务主体识别 | `app/knowledge/search_text.py` | 不连接数据库，不调用模型 |
| 查询计划中的主体、事实及场景 | `app/services/business_queries.py` | 构建业务文档内的事实查询 |
| 融合分数、文档多样性、证据转换 | `app/services/candidate_ranking.py` | 输入候选，返回调整后的候选或证据 |
| 精确编号、主体过滤、相邻块和跨文档关联 | `app/services/candidate_selection.py` | 判断哪些候选可以进入证据集合 |
| 调用顺序、降级、候选预算 | `app/services/retrieval.py` | 编排规则和外部调用 |
| 回答 JSON 与原文引用校验 | `app/services/answer_contract.py` | 不可信的模型输出必须经过这里 |
| 补充业务名到下游编号的引用 | `app/services/answer_bridges.py` | 补引用，不新增或改写事实 |

候选字典沿用搜索适配器的数据协议：`hit._source` 是来源字段，`score` 是融合分数，`channels` 是命中的通道。最终通过 `to_evidence` 转成有明确字段的 `Evidence`。不要把融合分数当作向量相似度。

`_recall_planned_queries` 和 `_recall_references` 分别负责查询改写和编号扩展。主体过滤只在候选合并后执行；调整顺序可能改变正确事实能否保留下来。需要新增规则时，先确定它属于哪个阶段，再给它写一个表达用途的函数。

## 一份文档怎么入库

`app/services/ingestion.py` 管理任务阶段：解析、切块、向量化、写入和失败处理。

`app/repositories/ingestion_records.py` 负责具体写入：

- `remove_version_chunks` 删除当前版本已有的分块记录。
- `write_sections` 建立根章节和父子章节，返回章节 ID 表。
- `write_chunks` 建立分块、前后关系、父块和向量关联。

这些写入函数只写入和 flush，不自行 commit。调用方把分块与 Outbox 事件一起提交，保证索引器拿到事件时能读到完整数据。写入 Chunk 后先 flush，再写 ChunkEmbedding；这里的顺序由外键依赖决定。

项目和版本管理继续位于 `app/application/document_service.py` 与 `app/repositories/documents.py`。SQL 和事务相关操作保持在一起，避免维护删除范围时需要跨多个文件查找。

## 知识库页面怎么读

`frontend/src/KnowledgeManager.jsx` 负责页面状态和用户操作协调。展示与上传细节位于 `frontend/src/knowledge/`：

| 文件 | 职责 |
|---|---|
| `ProjectPanel.jsx` | 创建、选择和重命名知识库 |
| `DocumentRow.jsx` | 单份文档状态、操作入口和展开 |
| `VersionList.jsx` | 版本状态、下载、批准和废弃入口 |
| `BatchStatus.jsx` | 上传批次进度和结果 |
| `useDocumentUpload.js` | 批次开始、进度更新与停止请求 |
| `documentDisplay.js` | 文档状态文案、时间和警告显示 |
| `upload.js` | 文件准备及上传队列执行 |

停止批次会阻止后续文件开始上传，当前文件继续完成。页面展示状态有优先顺序：废弃、失败、处理中、可检索；不能直接根据某次旧任务成功就显示当前版本可检索。

## 修改后怎样验证

```sh
.venv/bin/python -m pytest -q
.venv/bin/ruff check app
npm test
npm run build
git diff --check
```

`tests/test_refactor_workflows.py` 覆盖无证据不调用模型、失败路径只保存一次、跨文档引用补全、重排后的编号补回、重复入库的稳定 ID 与章节关系，以及入库成功和失败阶段。

检索重构还可以运行以下对照。它从指定的可信 Git 版本加载旧代码，使用已有测试问题及查询计划。旧代码先记录数据库和 Embedding 返回，新代码必须使用相同调用及返回，逐项比较证据内容、顺序、分数和范围决策：

```sh
.venv/bin/python -m evaluation.stress50.refactor_parity --baseline 8371848
```

此脚本需要本地测试库、测试记录和模型配置；不调用回答生成。它证明测试输入下的检索行为一致，不能当作新一轮模型回答准确率。输出在被 Git 忽略的 `evaluation/generated/stress50/refactor-parity.jsonl`。

## 2026-09-08 验证记录

本地后端测试57项通过、1项需要独立PostgreSQL的测试跳过；前端14项通过，构建及Ruff检查通过。170道保存问法的检索行为与重构前一致。

重构后重新运行真实问答：169/170通过。Qwen日期版本完成127题后免费额度耗尽，剩余43题使用GLM-5.2，其中42题通过。唯一未通过项是只输入“青禾清算”的业务概览：GLM在1600 token预算下将全部输出token用于推理，finish_reason为length，正文为空；独立重试复现。该模型调用预算问题尚未调整，不能把本轮记为全通过。

完整本地记录位于evaluation/generated/stress50/refactor-real-final-20260908.md。生成结果按现有.gitignore排除；可通过real_regression.py与report_real_regression.py重新运行并汇总。
