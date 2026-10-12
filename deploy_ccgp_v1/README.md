# CCGP 云端写入服务

本目录包含 PostgreSQL 16、内部批量写入 API 和只读用户查询 API。数据库及 API 端口均只绑定服务器 `127.0.0.1`，不直接向公网开放。

## 部署

服务器目录为 `/opt/zhaobiao-ccgp-v1`。配置以下未跟踪文件：

```text
.env
secrets/ingest_password
secrets/reader_password
secrets/ingest_api_key
secrets/cursor_secret
```

密钥文件权限应为 `0600`。应用迁移后启动：

```bash
openssl rand -hex 32 > secrets/cursor_secret
chmod 600 secrets/reader_password secrets/ingest_api_key secrets/cursor_secret
```

游标签名密钥至少为 32 个字符。然后应用迁移并启动服务：

```bash
docker compose exec -T postgres \
  psql -v ON_ERROR_STOP=1 -U "$POSTGRES_USER" -d "$POSTGRES_DB" \
  < migrations/006_ingest_batch_hash.sql
docker compose exec -T postgres \
  psql -v ON_ERROR_STOP=1 -U "$POSTGRES_USER" -d "$POSTGRES_DB" \
  < migrations/007_query_api_keys.sql
docker compose up -d --build ingest-api
docker compose up -d --build query-api
docker compose ps
curl -fsS http://127.0.0.1:18080/healthz
curl -fsS http://127.0.0.1:18081/healthz
```

API 提供公开于本机隧道的 `GET /healthz`，以及需要 Bearer 密钥的 `GET /internal/v1/healthz` 和 `POST /internal/v1/notices/batch`。单批最多 500 条，请求体最大 1 MB。

查询 API 提供 `GET /api/v1/notices`，使用 `X-API-Key`、地区/日期/关键词筛选和签名游标分页：

```bash
./scripts/create-query-api-key.sh reporting-service
curl -H 'X-API-Key: <key>' \
  'http://127.0.0.1:18081/api/v1/notices?region_code=420000&start_date=2026-09-01&end_date=2026-09-30&limit=20'
```

密钥可按创建时显示的前缀管理：

```bash
./scripts/manage-query-api-key.sh disable ccgp_0123456789a
./scripts/manage-query-api-key.sh enable ccgp_0123456789a
./scripts/manage-query-api-key.sh revoke ccgp_0123456789a
```

生产环境应将 `Caddyfile.example` 中的域名替换为真实 API 域名，并由 Caddy 将 HTTPS 443 反向代理到 `127.0.0.1:18081`。不要直接暴露 18081 或 PostgreSQL 端口。

本地测试：

```bash
python3 -m venv .venv-query-api
. .venv-query-api/bin/activate
pip install -r query_api/requirements-dev.txt
PYTHONPATH=query_api pytest -q query_api/tests
```

## 本地隧道

```bash
ssh -N -L 18765:127.0.0.1:18080 ubuntu@175.178.172.158
curl -fsS http://127.0.0.1:18765/healthz

ssh -N -L 18766:127.0.0.1:18081 ubuntu@175.178.172.158
curl -fsS http://127.0.0.1:18766/healthz
```

试运行后执行：

```bash
./scripts/backup-postgres.sh
./scripts/restore-test.sh
```
