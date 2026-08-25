# 生产代码重构与 100 文档回归报告

报告日期：2026-08-20

## 1. 验收结论

本轮生产化重构与回归测试通过。后端已按接口层、应用编排层、领域校验层和基础设施层拆分，前端已从单文件实现拆为页面、组件、API 与本地状态模块；Docker 镜像按服务职责拆分，API 不再携带 LibreOffice。

本轮在保留 PostgreSQL、Elasticsearch 和原始文档持久卷的情况下，持续导入并验证了两组各 100 份的测试文档：共 200 份文档、200 个成功任务和 612 个可检索文档块。最终 25 个混合格式检索用例全部命中首位，7 个真实 API 问答与拒答用例全部通过。

这说明当前代码可以作为生产落地的工程基线。正式接入公司用户前，仍需完成公司 SSO/身份认证、ACL 强制过滤、TLS、外部备份恢复演练和基于真实业务问题的人工验收；这些属于部署和组织集成工作，不应由测试语料结果替代。

## 2. 重构范围

### 2.1 后端结构

- `app/` 根目录只保留 ASGI 入口，后端按 `api`、`contracts`、`services`、`knowledge`、
  `integrations`、`db`、`core`、`jobs` 和 `commands` 分层。
- `app/core/container.py` 是唯一应用组合根，集中构造并缓存数据库、检索、模型和回答服务。
- `app/services/answer_contract.py` 集中管理证据提示词、结构化回答解析、引用校验和拒答文本。
- `app/services/answering.py` 只负责检索、模型路由、生成、校验、追踪和受控降级的应用流程。
- 新增可执行架构边界测试，阻止内部层反向依赖 API、命令或后台任务入口。
- 为配置、解析、切块、索引、模型路由、存储、Worker 和 Indexer 增加模块级说明与关键约束注释。
- Qwen Provider 复用长连接 HTTP Client，并在 API、Worker 与 Indexer 退出时显式关闭资源。
- 启动配置增加生产环境保护，避免弱数据库密码、调试配置或不安全密钥配置进入正式环境。

### 2.2 前端结构

- `frontend/src/main.jsx` 缩减为 9 行挂载入口。
- 页面状态和交互进入 `App.jsx`。
- 展示组件进入 `components.jsx`。
- HTTP 调用进入 `api.js`，本地会话持久化进入 `storage.js`。
- 修复全局键盘事件监听器的生命周期，避免重复绑定和过期闭包。

### 2.3 构建与依赖

- Python 直接依赖固定到本轮实际验证版本，降低未来构建漂移。
- Dockerfile 拆为通用 `runtime` 与仅供文档 Worker 使用的 `worker-runtime`。
- API、Indexer 和迁移镜像约 438 MB；Worker 镜像约 1.09 GB。
- API 镜像不包含 `soffice`；Worker 保留 LibreOffice，用于旧版 `.doc`/`.xls` 转换。

## 3. 自动化质量门禁

| 检查 | 结果 |
|---|---:|
| Ruff 静态检查 | 通过 |
| Pytest | 48 / 48 通过（含 3 个架构边界测试） |
| 前端 Vite 构建 | 通过 |
| Docker Compose 配置校验 | 通过 |
| Docker 多阶段镜像构建 | 通过 |
| Git diff 空白错误检查 | 通过 |
| 数据库迁移 | `0003_elasticsearch_persistence (head)` |

前端生产包包含 1,797 个模块，主 JavaScript 约 207.58 kB，gzip 后约 66.52 kB；CSS 约 12.53 kB，gzip 后约 3.54 kB。

分层迁移完成后又使用已持久化语料抽查了 Markdown、TXT、HTML、DOCX、XLSX 各一个
问题，5 个问题的 Top-1 文件全部正确，单次混合检索耗时为 0.359–0.532 秒。随后通过
真实 `/api/v1/chat` 验证：已知问题返回两条合法引用，不存在的 `KBR-9999` 不调用生成
模型并正确返回 `insufficient_evidence`。

## 4. 持久文档与索引一致性

| 测试项目 | 文档 | 版本 | 文档块 | 可检索文档 | 格式 |
|---|---:|---:|---:|---:|---|
| `Codex-Upload-100-Test` | 100 | 100 | 260 | 100 | Markdown、TXT、HTML、DOCX、XLSX 各 20 |
| `Codex-Chaos-100-Test` | 100 | 100 | 352 | 100 | DOCX 17、HTML 17、Markdown 17、PDF 16、TXT 17、XLSX 16 |
| 合计 | 200 | 200 | 612 | 200 | 6 种格式 |

最终检查结果：

- 200 个导入任务全部成功。
- PostgreSQL 中共有 612 个当前文档块。
- Elasticsearch 读写 alias 指向同一物理索引，共有 612 条索引记录。
- `missing_embeddings=0`、`pending_events=0`、`dead_events=0`、`sync_differences=0`。
- Elasticsearch 集群状态为 `green`。
- 整个验证过程没有重建或删除现有持久卷。

## 5. 检索回归

在 `Codex-Upload-100-Test` 中选取 Markdown、TXT、HTML、DOCX、XLSX 各 5 个问题，共 25 个混合检索用例：

| 指标 | 结果 |
|---|---:|
| Recall@10 | 100% |
| MRR | 1.000 |
| Top-1 命中 | 25 / 25 |
| 平均检索延迟 | 0.369 秒 |
| P95 检索延迟 | 0.466 秒 |

该结果验证的是受控测试语料上的工程回归，不等同于真实业务知识准确率。正式验收仍应使用 50–100 个由业务人员标注的真实问题。

## 6. 端到端问答回归

| 场景 | 结果 | 实际模型 | 引用数 | 延迟 |
|---|---|---|---:|---:|
| Markdown 规则阈值与审批人 | 通过 | Plus | 2 | 6.171 秒 |
| TXT 超时与保留期 | 通过 | Plus | 2 | 5.538 秒 |
| HTML 生命周期状态 | 通过 | Plus | 1 | 3.932 秒 |
| DOCX 规则阈值与审批人 | 通过 | Plus | 2 | 6.028 秒 |
| XLSX 四字段联合查询 | 通过 | Plus | 4 | 9.344 秒 |
| 不存在知识的严格拒答 | 通过 | 未调用生成模型 | 0 | 0.219 秒 |
| 跨文档比较 | 通过 | Max 不可用后受控降级 Plus | 2 | 8.423 秒 |

合计 7 / 7 通过，端到端 P95 为 9.344 秒。来源详情接口能够返回 Excel 工作表 `Approved Rules` 和单元格范围 `A1:B13`；原文下载接口、项目列表接口和生产前端静态资源也均已验证。

## 7. Max 模型异常与处理结果

当前百炼账号调用两个已启用的 Max 模型时返回 `403 AllocationQuota.FreeTierOnly`；另外两个注册表中的 Max 名称返回模型参数错误并保持禁用。这是供应商侧额度/模型可用性状态，不是本地检索失败。

重构后的行为如下：

- 跨文档复杂问题仍首先确定性路由到 Max。
- 仅当所有 Max 均不可用、证据已充分且请求不是固定模型评测时，执行一次有完整引用约束的 Plus 降级。
- 响应和查询追踪同时记录 `requested_tier=max` 与 `actual_tier=plus`，不会冒充 Max 成功。
- 固定模型评测禁止自动降级，确保基线结果可重复。
- 如果生成失败或引用校验仍不通过，系统严格拒答，不根据模型常识补全。

该逻辑已增加自动化测试，并通过真实跨文档问题验证。

## 8. 安全与运行检查

- `Qwen/Qwen token.txt` 被 `.gitignore` 排除。
- token 文件权限为 `600`。
- token 不进入 Docker 构建上下文、镜像、日志或诊断输出。
- API、Worker、Indexer 正常运行；迁移容器成功退出。
- `/health/ready` 返回数据库和 Elasticsearch 均可用。
- 最后 300 行 API、Worker、Indexer 日志中未发现 Traceback、未处理异常、Critical 或 5xx。

## 9. 复核命令

```bash
.venv/bin/ruff check app evaluation tests
.venv/bin/pytest -q
npm run build
docker compose config --quiet
docker compose build api worker indexer migrate
docker compose up -d --force-recreate api worker indexer
docker compose ps -a
curl -fsS http://localhost:8000/health/ready
curl -fsS http://localhost:8000/api/v1/index/status
git diff --check
```

完整代码分层和依赖方向见《生产代码架构与维护约定》，启动、关闭、API 与运维命令见《使用、开发与运维手册》。
