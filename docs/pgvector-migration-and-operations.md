# pgvector 迁移与运行手册

## 1. 迁移前备份

不要使用 `docker compose down -v`。先停止写入服务，再备份数据库和上传卷：

```bash
docker compose stop api worker indexer
mkdir -p backup
docker compose exec -T postgres pg_dump \
  -U knowledge -d knowledge -Fc > backup/pre-pgvector.dump
docker run --rm \
  -v tellerxchatbot_uploads:/source:ro \
  -v "$PWD/backup:/backup" \
  alpine sh -c 'cd /source && tar -czf /backup/pre-pgvector-uploads.tgz .'
shasum -a 256 backup/pre-pgvector.dump backup/pre-pgvector-uploads.tgz \
  > backup/pre-pgvector.sha256
```

确认 Qwen token 不在归档中：

```bash
tar -tzf backup/pre-pgvector-uploads.tgz | grep -Ei 'token|secret|credential' && exit 1 || true
```

## 2. 切换数据库镜像和迁移

`docker-compose.yml` 已固定 `pgvector/pgvector:0.8.1-pg16`，可以复用原 PostgreSQL 16 数据卷。

```bash
docker compose pull postgres
docker compose up -d postgres
docker compose run --rm migrate
```

核对扩展和迁移版本：

```bash
docker compose exec -T postgres psql -U knowledge -d knowledge -c \
  "SELECT extname, extversion FROM pg_extension WHERE extname IN ('vector','pg_trgm');"
docker compose exec -T postgres psql -U knowledge -d knowledge -c \
  "SELECT version_num FROM alembic_version;"
```

预期 Alembic head 为 `0004_postgresql_pgvector_fts`。

在迁移生产知识库之前，可先用专用临时数据库完成空库迁移和真实 SQL 门禁；脚本会拒绝在普通
数据库名上插入验证数据，并在结束时删除 `_verify` 数据库：

```bash
scripts/run-pgvector-integration.sh
```

## 3. 回填全文和向量

正常迁移复用既有持久化向量：

```bash
docker compose run --rm api knowledge-reindex
```

Embedding 权限暂不可用时，只回填全文检索：

```bash
docker compose run --rm api knowledge-reindex --bm25-only
```

后者只是开发降级模式，不是正式验收配置。

```bash
docker compose run --rm api knowledge-reconcile
docker compose run --rm api knowledge-reconcile --repair
```

只有首次对账无差异或修复后再次对账无差异，才可以启动服务。

## 4. 启动与关闭

```bash
# 首次或代码变更后
docker compose up -d --build
docker compose ps
curl -fsS http://localhost:8000/health/ready

# 日常停止和恢复，不删除数据
docker compose stop
docker compose start

# 删除容器但保留命名卷
docker compose down
```

严禁在无完整备份时执行 `docker compose down -v`。

## 5. 健康和索引检查

```bash
curl -fsS http://localhost:8000/health/live
curl -fsS http://localhost:8000/health/ready
curl -fsS http://localhost:8000/api/v1/index/status

docker compose exec -T postgres psql -U knowledge -d knowledge -P pager=off -c \
  "SELECT count(*) AS rows, count(embedding) AS vectors FROM chunk_search_index;"
docker compose exec -T postgres psql -U knowledge -d knowledge -P pager=off -c \
  "SELECT indexname, indexdef FROM pg_indexes WHERE tablename='chunk_search_index';"
```

## 6. 日志

```bash
docker compose logs --tail=200 api worker indexer migrate postgres
docker compose logs -f api worker indexer
```

重点关注 `PostgreSQL search verification failed`、manifest 差异、Embedding/Rerank 降级和
Outbox `dead` 事件。

## 7. 回滚

在新后端未通过验收前保留旧 Elasticsearch Git 分支、数据库备份和原始文件备份。代码回滚后，
使用迁移前 PostgreSQL 数据卷或在空库恢复 dump。不要在保存新数据的数据库上直接执行破坏性
Alembic downgrade；回滚演练必须在独立数据库完成。
