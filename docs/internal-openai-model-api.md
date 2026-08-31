# 公司 OpenAI 兼容模型网关配置

## 运行模型

- Chat：`qwen3.5-122B`
- Embedding：`qwen3-embedding`
- Rerank：禁用；检索使用 PostgreSQL FTS、pgvector 和 RRF

## 环境变量

```dotenv
MODEL_API_KEY_FILE=/run/secrets/model_api_key
MODEL_API_BASE_URL=https://sdk-endpoint.example.internal/v1
MODEL_API_JSON_MODE_ENABLED=true
MODEL_API_TIMEOUT_SECONDS=60
MODEL_API_MAX_RETRIES=2
EMBEDDING_MODEL=qwen3-embedding
EMBEDDING_DIMENSIONS=1024
RERANK_ENABLED=false
MODEL_REGISTRY_PATH=/app/config/models.yaml
```

`MODEL_API_BASE_URL` 必须是 SDK 根地址，代码会通过 `openai.OpenAI` 调用
`chat.completions.create` 和 `embeddings.create`。Token 只从 Secret 文件读取，不写入
Git、镜像或日志。HTTP 客户端启用 HTTP/2，并通过 `truststore.SSLContext` 使用操作系统
或公司下发的证书信任链。

在 Docker/IKP 中，`truststore` 读取的是容器内的系统信任库，不会自动继承宿主机证书。
如果公司 Endpoint 使用内部 CA，需要由基础镜像统一安装 CA，或把 CA 挂载到容器并更新
容器信任库；不要在代码中设置 `verify=False`。

如果公司 Endpoint 不支持 `response_format={"type":"json_object"}`，设置：

```dotenv
MODEL_API_JSON_MODE_ENABLED=false
```

系统 Prompt 仍会要求返回 JSON，业务层继续执行结构和引用校验。

## Embedding 验收

当前 PostgreSQL 列类型是 `vector(1024)`。上线前必须运行：

```bash
model-diagnostics --skip-chat
```

输出维度必须为 1024。如果公司模型不能通过 `dimensions=1024` 返回 1024 维向量，不能
直接上线，需要新增 Alembic 迁移修改 pgvector 列和 HNSW 索引，并重新向量化全部文档。

## Chat 验收

```bash
model-diagnostics --skip-embedding --chat-model qwen3.5-122B
```

应看到 `chat: ok` 和 `rerank: disabled`。诊断失败时只输出 HTTP 状态和错误码，不输出
Token 或供应商响应正文。

## 兼容旧本地配置

配置层暂时兼容 `QWEN_API_KEY_FILE`、`QWEN_CHAT_BASE_URL`、
`QWEN_EMBEDDING_MODEL` 和 `QWEN_EMBEDDING_DIMENSIONS`，便于使用原百炼 Endpoint 验证
SDK 适配器。新内部环境应统一使用 `MODEL_API_*` 和 `EMBEDDING_*` 名称。
