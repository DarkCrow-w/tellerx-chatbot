# MinIO 验证记录

验证日期：2026-09-11。运行环境：当前 Mac、Docker 29.7.2、单节点 MinIO。

## 已完成

- 固定官方源码构建并启动，端口仅绑定 127.0.0.1；容器健康。
- 镜像摘要：`sha256:b724d924f50bfae068472ca6800d299ab031c80fb1df642696a9e1e7b175739d`。
- 6 个原始文档、6 个解析产物、126 个向量缓存，共 138 条引用完成源/目标 SHA-256 校验；重复复制及 S3 导出均通过。
- 容器强制重建后，全部旧文档版本通过现有下载接口读取，哈希与数据库一致。
- 真实 MinIO 16 路并发重复上传只复用相同内容，冲突内容被拒绝，匿名 GET 返回 403；临时文件正常清理。
- MinIO 停止时存储就绪为 false，存取返回 HTTP 503；重启后恢复。
- 中文 Markdown 上传、模型向量化、解析产物保存、索引发布、下载和真实问答均通过。回答状态为 answered，含正确验证编号和引用。独立验证项目已清理，模型调用审计记录保留。
- 后端 131 项测试通过，前端 27 项测试通过；Ruff 和 git diff --check 通过。

## 当前状态与备份

当前工作区 `.env` 已启用 `STORAGE_BACKEND=s3`。MinIO 保持运行。验证时通过 TestClient 执行真实后端生命周期；未额外启动常驻 chatbot HTTP 服务。下次按原流程启动 chatbot 即使用 MinIO。

原本地文件仍在 `.local-data/source-only-knowledge`。切换前数据库备份、原配置及路径映射同时保存在 `/Users/cliff/TellerXData/minio/migration`。工作区 `.local-data/minio-migration` 保存校验清单、导出文件和端到端日志。凭证保存在 `/Users/cliff/TellerXData/minio`，未写入版本控制。

这是本机单节点功能验证，不代表多人负载或异机灾难恢复验证。未重建原有搜索索引或重新生成原有向量。

## 重复执行

```bash
PYTHONPATH=. .venv/bin/python deploy/minio/verify_live.py
```

该命令需要可用的数据库、MinIO 和模型服务；会创建并清理独立测试项目，产生少量模型调用记录。
