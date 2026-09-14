# 知识库扫描功能：文件修改清单

## 范围与统计

本文整理“直接上传文档到 MinIO，再在前端扫描对应知识库并解析”的文件改动。

- 开发分支：`codex/source-only-txt`。
- 功能代码已包含在提交 [`0c35751`](https://github.com/DarkCrow-w/tellerx-chatbot/commit/0c35751) 中。
- 扫描功能共涉及 **16 个文件：新增 6 个、修改 10 个**，包括代码、数据库迁移、测试和文档。
- 下文行号以 `0c35751` 为准，后续代码修改可能使行号变化。

这里的“新增/修改”按扫描功能开发前的工作区状态区分。例如，`s3_storage.py` 在前一轮 MinIO 接入中已经建立，本轮只增加对象列表能力。`0c35751` 将两轮功能及原有模型配置、诊断改动一起提交，共包含 41 个文件，不能把整份提交都算作扫描功能改动。本清单文档本身也不计入上述 16 个文件。

## 新增文件（6 个）

| 文件 | 主要位置 | 新增内容 |
| --- | --- | --- |
| [app/application/scan_service.py](../app/application/scan_service.py) | `scan_prefix()`，18 行；`latest_scan()`，22 行；`start_scan()`，39 行；`ignored_reason()`，68 行；`KnowledgeScanner`，86 行 | 扫描核心逻辑：生成 `imports/<知识库ID>/` 前缀、创建任务、防止同库重复扫描、识别中断任务、过滤文件、复用上传和解析流程、持久化进度及失败明细。 |
| [app/api/routes/scans.py](../app/api/routes/scans.py) | `snapshot()`，16 行；`run_scan()`，33 行；GET 路由，41 行；POST 路由，57 行 | 增加查询扫描状态与发起后台扫描的 HTTP 接口，并为后台处理创建独立数据库会话。 |
| [alembic/versions/0002_knowledge_scans.py](../alembic/versions/0002_knowledge_scans.py) | `upgrade()`，13 行；`downgrade()`，45 行 | 创建 `knowledge_scans` 表和索引；同一知识库最多有一个运行中的扫描。回滚时删除扫描表。 |
| [frontend/src/knowledge/ScanPanel.jsx](../frontend/src/knowledge/ScanPanel.jsx) | `ScanPanel`，5 行；轮询请求，19 行；启动请求，35 行；界面，46 行 | 显示 MinIO 目录、扫描按钮、当前文件、成功/跳过/失败数量及明细；运行时每 2.5 秒查询进度；加载失败时可重试。 |
| [tests/test_knowledge_scan.py](../tests/test_knowledge_scan.py) | 整个文件 | 覆盖任务互斥、过期任务重试、文件过滤、中文子目录、分页列举、单文件失败不中断、列表读取失败，以及扫描表升级和回滚。 |
| [docs/knowledge-scan.md](knowledge-scan.md) | 整个文件 | 记录使用步骤、目录规则、内容去重、版本处理、失败重试、接口、部署要求和本机验证情况。 |

## 修改文件（10 个）

| 文件 | 修改位置 | 本轮改动 |
| --- | --- | --- |
| [app/db/models.py](../app/db/models.py) | `KnowledgeScan`，442 行 | 新增扫描任务 ORM 模型：知识库 ID、任务状态、已处理/导入/跳过/失败数量、当前对象、明细、错误和时间；定义运行任务的唯一索引。 |
| [app/integrations/s3_storage.py](../app/integrations/s3_storage.py) | `list_objects(prefix)`，93 行 | 新增按前缀分页读取对象列表的能力，使用 `list_objects_v2` 和 continuation token，不遍历整个 bucket。 |
| [app/application/document_service.py](../app/application/document_service.py) | `UploadDocumentCommand.project_id`，78 行；`upload()`，295 行；`_purge_project()`，692 行 | 上传命令支持按稳定的知识库 ID 定位项目，避免知识库改名时误导入；扫描运行期间禁止物理清理或删除对应知识库。原有按名称上传方式保留。 |
| [app/api/routes/documents.py](../app/api/routes/documents.py) | 导入，13 行；路由注册，35 行 | 导入并注册 `scans_router`，使扫描接口挂载到现有 API 路由下。 |
| [frontend/src/KnowledgeManager.jsx](../frontend/src/KnowledgeManager.jsx) | 导入，34 行；组件挂载，340 行 | 在选中的知识库管理区域嵌入 `ScanPanel`；知识库切换时重新挂载，扫描进度更新时刷新文档列表。 |
| [frontend/src/api.js](../frontend/src/api.js) | `getKnowledgeScan()`，244 行；`startKnowledgeScan()`，248 行 | 新增查询和启动扫描的请求封装，知识库 ID 使用 URL 编码。 |
| [frontend/src/styles.css](../frontend/src/styles.css) | 410–416 行 | 增加扫描区域、标题、按钮、可选中的目录路径、明细以及窄屏布局样式。 |
| [frontend/src/knowledge/api.test.js](../frontend/src/knowledge/api.test.js) | 扫描接口测试，60 行 | 验证请求始终指向选中的知识库，路径正确编码，启动扫描使用 POST。 |
| [tests/test_initial_migration.py](../tests/test_initial_migration.py) | 初始建表断言，31 行；回滚断言，44 行 | 从冻结的 `0001_initial` 测试断言中排除新表 `knowledge_scans`；新表由独立的 `0002` 迁移测试覆盖，不修改初始迁移文件。 |
| [README.md](../README.md) | 284 行 | 增加扫描功能介绍及使用指南链接。 |

`document_service.py` 有三个改动位置，但统计时只算一个文件。

## 新增接口

| 方法与路径 | 行为 |
| --- | --- |
| `GET /api/v1/projects/{project_id}/scan` | 返回是否支持扫描、bucket、导入目录前缀以及最近一次任务状态。 |
| `POST /api/v1/projects/{project_id}/scan` | 返回 HTTP 202 并启动后台任务；同一知识库已有运行任务时返回已有任务。 |

扫描任务持久化到数据库，刷新页面仍可查看。后台进程退出后不自动续跑；一小时无进展的任务会在查询时标记为中断，之后可以重新扫描。

## 扫描处理流程

1. 用户将文档放入 `tellerx-knowledge/imports/<知识库ID>/`，允许包含子目录。
2. 后端仅列举该知识库前缀下的对象，跳过隐藏、系统、不支持格式和超限文件。
3. 按相对路径识别逻辑文档，读取实际内容计算 SHA-256；不把 ETag 当作内容哈希。
4. 新文件或内容更新的文件复制到已有不可变存储区，再进入现有解析、向量化和索引发布流程。
5. 内容相同且已解析成功的版本跳过解析；失败任务可以重试。单个文件失败不会阻断后面的文件。
6. 前端轮询并展示进度、结果和最近 100 条跳过/失败明细。

同路径覆盖会保留历史版本；从导入目录删除源文件不会同步删除知识库记录。直接导入的源文件由用户管理，扫描不会删除它们。

## 本机执行过的操作

以下是部署与验证操作，不是额外源码文件改动：

- 保存升级前数据库备份 `.local-data/minio-migration/before-knowledge-scan.dump`。
- 核对旧数据库应用列与搜索结构后，将旧迁移标记 `0006_hierarchical_retrieval` 对齐到仓库合并后的 `0001_initial`，再升级到 `0002_knowledge_scans`。其他数据库不能未经核对照搬 stamp 操作。
- 执行前端生产构建，生成被 Git 忽略的 `app/static` 产物。
- 通过真实 MinIO 验证新增导入、重复跳过、同名更新、历史版本下载、子目录、中文名称、知识库隔离及坏文件修复重试。
- 在真实前端点击扫描按钮，检查目录和结果显示；验收后关闭临时启动的后端，MinIO 服务保留。

## 验证结果

功能验收时：后端 **142 项测试**通过，前端 **28 项测试**通过，前端生产构建、Ruff 和 `git diff --check` 通过。本次整理清单仅新增文档，不修改功能代码。
