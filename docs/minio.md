# 本地 MinIO 文件存储

文件、解析产物和向量缓存进入私有 bucket `tellerx-knowledge`；数据库和 pgvector/FTS 索引保持原位。该部署仅监听当前 Mac，不提供团队登录或公网访问。

## 安装和启动

从项目根目录执行，使用本项目 Python（不要依赖其他 checkout 的虚拟环境脚本）：

```bash
.venv/bin/python -m pip install -e '.[dev]'
python3 deploy/minio/bootstrap.py
```

初始化会创建 `/Users/cliff/TellerXData/minio/data`、权限为 0600 的 `admin.env` 和 `application.env`。已存在的凭证文件不会覆盖。应用账户只可读写、删除本 bucket 的对象；不要将管理员凭证配置给 chatbot。

- S3 endpoint：`http://127.0.0.1:9000`
- 对象浏览控制台：`http://127.0.0.1:9001`，登录凭证保存在 `admin.env`。
- 镜像：`tellerx-minio:7aac2a2c-77f82e18`，最终镜像内容摘要写入外部目录的 `image-digest.txt`。
- 源码提交：MinIO `7aac2a2c5b7c882e68c1ce017d8256be2feea27f`；mc `77f82e18b5401a65958f1619df6ebb994634bd88`。基础镜像也固定摘要。构建需要联网下载 Go 模块及系统依赖；保存已构建镜像可保证原样恢复。

官方社区仓库已归档：[官方状态](https://github.com/minio/minio)。此部署使用固定源码自行构建，不跟随 `latest`。

```bash
docker compose -f deploy/minio/compose.yaml ps
docker compose -f deploy/minio/compose.yaml logs --tail 50
docker compose -f deploy/minio/compose.yaml stop
docker compose -f deploy/minio/compose.yaml up -d --wait
```

`unless-stopped` 在 Docker 启动后恢复容器；主动 stop 后需手动 up。数据是主机绑定目录，删除容器不删除文件。不要手工编辑 MinIO 的内部数据目录。

## 配置 chatbot

`.env` 新增以下配置（凭证本身不放入仓库）：

```dotenv
STORAGE_BACKEND=s3
STORAGE_CREDENTIALS_FILE=/Users/cliff/TellerXData/minio/application.env
S3_ENDPOINT_URL=http://127.0.0.1:9000
S3_BUCKET=tellerx-knowledge
S3_REGION=us-east-1
STORAGE_TEMP_DIR=.local-data/storage-tmp
```

默认后端仍为 `local`。凭证可由 `S3_ACCESS_KEY`、`S3_SECRET_KEY` 显式配置，它们优先于文件。外部凭证文件只读取这两个字段，endpoint 和 bucket 由上述应用配置决定。更改配置后重启后端。

数据库对象地址是相对 key。解析文件只暂存于临时目录；下载通过现有后端地址返回，无需浏览器访问 MinIO。存储不可用时返回 503，不降级到本地。下载中途失联时连接会终止，客户端需重新下载。就绪接口 `/health/ready` 包含 `storage` 状态。

## 迁移现有文件

先保留 `STORAGE_BACKEND=local` 并添加凭证文件配置。暂停所有 chatbot 实例、CLI 写入和后台任务；确认入库任务已完成。`--offline` 是操作人的确认，不会代替停服。

```bash
.venv/bin/python -m app.commands.storage_migrate check --manifest .local-data/minio-migration/check.json
PYTHONPATH=. .venv/bin/python deploy/minio/backup_database.py .local-data/minio-migration/before-minio.dump
.venv/bin/python -m app.commands.storage_migrate copy --offline --manifest .local-data/minio-migration/copied.json
.venv/bin/python -m app.commands.storage_migrate verify --offline --manifest .local-data/minio-migration/verified.json
.venv/bin/python -m app.commands.storage_migrate normalize --offline --manifest .local-data/minio-migration/mapping.json
```

预检查对照数据库 SHA-256 验证每条文件引用，缺失或损坏立即失败；复制可重跑，条件写入不会覆盖不同内容。目标数据重新完整读取校验，不使用 ETag。normalize 只在目标验证成功后将历史绝对路径变成相对 key；清单保留旧路径和新 key。

最后设置 `STORAGE_BACKEND=s3`，重启并检查就绪、旧文档下载、新文档解析和问答引用。校验通过前不要恢复写入。旧文件及备份不自动清理。不重新生成向量或搜索索引。

## 备份与恢复

暂停所有写入后，创建一个新的备份目录（不要覆盖旧备份），运行 PostgreSQL 备份并通过 S3 导出所有数据库引用的文件：

```bash
PYTHONPATH=. .venv/bin/python deploy/minio/backup_database.py /Users/cliff/TellerXData/backups/manual-001/database.dump
.venv/bin/python -m app.commands.storage_migrate export --offline --local-root /Users/cliff/TellerXData/backups/manual-001/objects --manifest /Users/cliff/TellerXData/backups/manual-001/manifest.json
```

只有两条命令均成功才算完整备份。export 会根据数据库哈希校验对象，目标目录的同 key 不同内容会失败。数据库未引用的孤立对象不在业务备份内。将备份复制到另一块磁盘才能应对本机磁盘损坏。

恢复时保持停服，先用 `pg_restore` 将对应 dump 恢复到新建的空 PostgreSQL 数据库（保留原库，连接密码使用 PGPASSWORD 或 .pgpass，切勿放在命令行），将应用 DATABASE_URL 指向新库，然后执行：

```bash
.venv/bin/python -m app.commands.storage_migrate copy --offline --local-root /Users/cliff/TellerXData/backups/manual-001/objects --manifest .local-data/minio-migration/restored.json
```

目标 MinIO 必须使用空 bucket 或内容一致的 bucket。恢复验证完成后启动应用。本工具不创建自动备份任务。

## 回退

- 切换验证期间尚未恢复写入：设置 `STORAGE_BACKEND=local` 并使用原 `STORAGE_ROOT`；必要时恢复切换前数据库备份。
- 切换后已产生新数据：先停服，执行 `export --offline --local-root <原 STORAGE_ROOT> --manifest <新清单>`，完整校验并补齐新文件后再改为 local。继续使用当前数据库，不能恢复旧 dump 丢弃新增记录。

## 验证

```bash
.venv/bin/python -m pytest -q
.venv/bin/python -m ruff check app tests/test_object_storage.py deploy/minio
npm test
```

共同存储测试涵盖并发去重、碰撞拒绝、超限、缓存校验、路径安全、异常清理、下载断开和迁移损坏检测。真实 MinIO 验证结果另见 `docs/minio-validation.md`。
