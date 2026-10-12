# 找标系统：云端 PostgreSQL 从零部署与数据上线手册

> 目标：把中国政府采购公告的 URL、标题、发布时间、地区等列表数据保存到云端 PostgreSQL，供后续网站或 App 按地区、时间和关键词查询。
>
> 本文采用一条明确的实施路线：**Ubuntu 24.04 LTS 云服务器 + Docker Compose + PostgreSQL 16 + 本地 SQLite 缓冲 + HTTPS 数据写入 API**。
>
> 配套文档：[中国政府采购网静态分类列表采集方案](./ccgp_static_list_crawler_design.md)

## 0. 如何使用这份手册

不要一次执行完整篇文档。按照编号逐步完成，每一步都包含“完成标准”。只有当前步骤验收通过，才进入下一步。

实施分为五个阶段：

```text
阶段 A：准备云服务器
阶段 B：部署 PostgreSQL
阶段 C：创建表结构并验证查询
阶段 D：打通本地采集与云端同步
阶段 E：导入历史数据并正式上线
```

最终架构：

```text
本地电脑
┌──────────────────────────────────┐
│ 持久化 Playwright / HTTP 采集器   │
│              ↓                   │
│ SQLite 本地待同步队列             │
└──────────────┬───────────────────┘
               │ HTTPS 批量上传
               ▼
云服务器
┌──────────────────────────────────┐
│ Caddy/Nginx：HTTPS               │
│ 写入 API：校验、鉴权、批量 UPSERT │
│ 查询 API：地区、日期、关键词查询   │
│ PostgreSQL：正式数据源            │
│ 定时备份任务                      │
└──────────────┬───────────────────┘
               │
               ▼
        对象存储/异地备份
```

三个存储位置的职责：

| 位置 | 职责 | 是否是正式数据源 |
|---|---|---|
| 本地 SQLite | 采集缓冲、断网重试、断点续传 | 否 |
| 云端 PostgreSQL | 用户查询、正式业务数据 | 是 |
| 对象存储/异地磁盘 | PostgreSQL 备份 | 否，但用于灾难恢复 |

---

# 阶段 A：准备云服务器

## 第 1 步：购买并初始化云服务器

### 1.1 推荐配置

初期 10 万至数百万条公告无需高配置：

```text
操作系统：Ubuntu Server 24.04 LTS 64 位
CPU：2 vCPU
内存：2～4 GB，推荐 4 GB
系统盘：40 GB SSD 起
公网带宽：3～5 Mbps 起
地域：尽量靠近主要用户
备份：开启云盘快照
```

数据库、API 和反向代理可以先部署在同一台服务器。后续用户量增大时，再把 PostgreSQL 迁移到托管数据库或独立服务器。

### 1.2 配置云平台安全组

只开放：

| 端口 | 来源 | 用途 |
|---:|---|---|
| 22 | 仅你的固定 IP；没有固定 IP 时暂时开放后尽快收紧 | SSH |
| 80 | `0.0.0.0/0` 和 `::/0` | HTTP，仅用于跳转和证书申请 |
| 443 | `0.0.0.0/0` 和 `::/0` | HTTPS API |

不要开放：

```text
5432 PostgreSQL
6379 Redis
任何 Docker 内部服务端口
```

### 1.3 首次登录

在本地终端执行：

```bash
ssh root@<服务器公网IP>
```

更新系统：

```bash
apt update
apt upgrade -y
timedatectl set-timezone Asia/Shanghai
```

创建日常运维用户：

```bash
adduser deploy
usermod -aG sudo deploy
```

把你的 SSH 公钥配置到 `deploy` 用户后，另开一个终端确认下面的登录能成功：

```bash
ssh deploy@<服务器公网IP>
```

在新用户登录验证成功前，不要关闭当前 root 会话，也不要禁用 root 或密码登录，以免把自己锁在服务器外。

### 1.4 开启服务器防火墙

```bash
sudo apt install -y ufw
sudo ufw allow OpenSSH
sudo ufw allow 80/tcp
sudo ufw allow 443/tcp
sudo ufw deny 5432/tcp
sudo ufw enable
sudo ufw status verbose
```

### 完成标准

- [ ] 可以使用 SSH 密钥登录 `deploy` 用户。
- [ ] 云安全组只开放 22、80、443。
- [ ] `5432` 没有向公网开放。
- [ ] 服务器时区为 `Asia/Shanghai`。
- [ ] 云平台已开启自动快照或已制定快照计划。

---

## 第 2 步：安装 Docker 和 Compose

本文使用 Docker Compose，方便固定版本、备份、迁移和重建。

在服务器上执行：

```bash
sudo apt update
sudo apt install -y docker.io docker-compose-v2
sudo systemctl enable --now docker
sudo usermod -aG docker deploy
```

退出 SSH 后重新登录，让 Docker 用户组生效：

```bash
exit
ssh deploy@<服务器公网IP>
```

验证：

```bash
docker version
docker compose version
docker run --rm hello-world
```

### 完成标准

- [ ] `docker version` 能显示客户端和服务端版本。
- [ ] `docker compose version` 正常。
- [ ] `hello-world` 运行成功。
- [ ] 日常使用 Docker 不需要 `sudo`。

---

# 阶段 B：部署 PostgreSQL

## 第 3 步：创建项目目录

在服务器上执行：

```bash
sudo mkdir -p /opt/zhaobiao/{migrations,scripts,backups,imports}
sudo chown -R deploy:deploy /opt/zhaobiao
cd /opt/zhaobiao
```

最终目录结构：

```text
/opt/zhaobiao/
├── compose.yaml
├── .env
├── migrations/
│   ├── 001_roles.sql
│   ├── 002_schema.sql
│   └── 003_regions.sql
├── scripts/
│   ├── migrate.sh
│   ├── backup-postgres.sh
│   └── restore-test.sh
├── backups/
└── imports/
```

### 完成标准

- [ ] `/opt/zhaobiao` 属于 `deploy` 用户。
- [ ] 四个子目录均存在。

---

## 第 4 步：创建数据库密码文件

生成一个高强度随机密码：

```bash
openssl rand -base64 36
```

在密码管理器中保存密码。然后创建 `/opt/zhaobiao/.env`：

```dotenv
POSTGRES_DB=ccgp
POSTGRES_USER=ccgp_admin
POSTGRES_PASSWORD=把这里替换为刚才生成的随机密码
TZ=Asia/Shanghai
```

限制文件权限：

```bash
chmod 600 /opt/zhaobiao/.env
```

注意：

- `.env` 不要提交到 Git。
- 不要把真实密码写到文档、聊天记录或代码中。
- 管理员密码只用于迁移和运维，API 不使用该密码。

### 完成标准

- [ ] `.env` 权限是 `600`。
- [ ] 密码已保存在密码管理器。
- [ ] 项目 `.gitignore` 包含 `.env`、备份和导入文件。

建议的 `.gitignore`：

```gitignore
.env
backups/
imports/
*.dump
*.csv
```

---

## 第 5 步：创建 PostgreSQL Compose 配置

创建 `/opt/zhaobiao/compose.yaml`：

```yaml
services:
  postgres:
    image: postgres:16-bookworm
    container_name: zhaobiao-postgres
    restart: unless-stopped
    env_file:
      - .env
    environment:
      POSTGRES_INITDB_ARGS: "--encoding=UTF8 --locale=C.UTF-8"
    ports:
      - "127.0.0.1:5432:5432"
    volumes:
      - postgres_data:/var/lib/postgresql/data
    healthcheck:
      test: ["CMD-SHELL", "pg_isready -U $${POSTGRES_USER} -d $${POSTGRES_DB}"]
      interval: 10s
      timeout: 5s
      retries: 10
      start_period: 20s
    logging:
      driver: json-file
      options:
        max-size: "10m"
        max-file: "5"
    networks:
      - backend

volumes:
  postgres_data:

networks:
  backend:
    name: zhaobiao-backend
```

关键安全设置：

```yaml
ports:
  - "127.0.0.1:5432:5432"
```

这表示 PostgreSQL 只监听服务器本机，不接受公网直接连接。

检查配置：

```bash
cd /opt/zhaobiao
docker compose config
```

启动数据库：

```bash
docker compose up -d postgres
docker compose ps
docker compose logs --tail=100 postgres
```

验证健康状态：

```bash
docker inspect --format '{{.State.Health.Status}}' zhaobiao-postgres
```

预期输出：

```text
healthy
```

### 完成标准

- [ ] `docker compose config` 无错误。
- [ ] PostgreSQL 容器状态为 `healthy`。
- [ ] `ss -lntp | grep 5432` 显示监听 `127.0.0.1:5432`，不是 `0.0.0.0:5432`。
- [ ] 从外部网络无法访问服务器的 5432 端口。

---

## 第 6 步：创建最小权限数据库角色

系统至少使用三种角色：

| 角色 | 用途 | 权限 |
|---|---|---|
| `ccgp_admin` | 数据库迁移和运维 | 管理员，禁止业务代码使用 |
| `ingest_writer` | 写入 API | 查询、插入、更新，不允许删除 |
| `api_reader` | 用户查询 API | 只读 |

创建 `/opt/zhaobiao/migrations/001_roles.sql`：

```sql
do $$
begin
  if not exists (select 1 from pg_roles where rolname = 'notices_readonly') then
    create role notices_readonly nologin;
  end if;
  if not exists (select 1 from pg_roles where rolname = 'notices_ingest') then
    create role notices_ingest nologin;
  end if;
  if not exists (select 1 from pg_roles where rolname = 'api_reader') then
    create role api_reader login inherit;
  end if;
  if not exists (select 1 from pg_roles where rolname = 'ingest_writer') then
    create role ingest_writer login inherit;
  end if;
end
$$;

grant notices_readonly to api_reader;
grant notices_ingest to ingest_writer;
```

执行：

```bash
cd /opt/zhaobiao
docker compose exec -T postgres \
  psql -v ON_ERROR_STOP=1 -U ccgp_admin -d ccgp \
  < migrations/001_roles.sql
```

分别为登录角色设置随机密码。进入交互式 `psql`：

```bash
docker compose exec postgres psql -U ccgp_admin -d ccgp
```

在 `psql` 中执行：

```text
\password api_reader
\password ingest_writer
\q
```

把两个密码分别保存到密码管理器。不要给两个账户设置相同密码。

### 完成标准

- [ ] 四个角色均存在。
- [ ] `api_reader` 和 `ingest_writer` 使用不同的强密码。
- [ ] 管理员密码没有写进应用代码。

验证角色：

```bash
docker compose exec postgres \
  psql -U ccgp_admin -d ccgp \
  -c "select rolname, rolcanlogin, rolsuper from pg_roles where rolname in ('notices_readonly','notices_ingest','api_reader','ingest_writer');"
```

---

# 阶段 C：创建表结构并验证查询

## 第 7 步：创建数据库表、约束和索引

创建 `/opt/zhaobiao/migrations/002_schema.sql`：

```sql
begin;

create extension if not exists pg_trgm;

create schema if not exists app authorization ccgp_admin;
revoke all on schema app from public;

create table if not exists app.regions (
  code text primary key,
  name text not null unique,
  aliases text[] not null default '{}',
  created_at timestamptz not null default now()
);

create table if not exists app.notices (
  id bigint generated always as identity primary key,
  source text not null default 'ccgp',
  source_notice_id text,
  canonical_url text not null,
  title text not null,
  published_at timestamptz not null,
  region_code text references app.regions(code),
  region_raw text,
  notice_type text,
  category text,
  source_list_url text not null,
  first_seen_at timestamptz not null default now(),
  last_seen_at timestamptz not null default now(),
  updated_at timestamptz not null default now(),
  constraint notices_url_unique unique (canonical_url),
  constraint notices_title_not_empty check (length(btrim(title)) > 0),
  constraint notices_url_http check (canonical_url ~ '^https?://')
);

create unique index if not exists notices_source_notice_id_uidx
  on app.notices (source, source_notice_id)
  where source_notice_id is not null;

create index if not exists notices_published_idx
  on app.notices (published_at desc, id desc);

create index if not exists notices_region_published_idx
  on app.notices (region_code, published_at desc, id desc);

create index if not exists notices_region_raw_published_idx
  on app.notices (region_raw, published_at desc, id desc);

create index if not exists notices_title_trgm_idx
  on app.notices using gin (title gin_trgm_ops);

create table if not exists app.ingest_batches (
  id bigint generated always as identity primary key,
  batch_key text not null unique,
  received_count integer not null check (received_count >= 0),
  inserted_count integer not null default 0 check (inserted_count >= 0),
  updated_count integer not null default 0 check (updated_count >= 0),
  source_name text not null default 'local-crawler',
  created_at timestamptz not null default now()
);

grant usage on schema app to notices_readonly, notices_ingest;

grant select on app.notices, app.regions to notices_readonly;

grant select, insert, update on app.notices to notices_ingest;
grant select on app.regions to notices_ingest;
grant insert, select, update on app.ingest_batches to notices_ingest;
grant usage, select on all sequences in schema app to notices_ingest;

commit;
```

这里故意不对未来新建的所有表设置宽泛的默认权限。以后每次增加表，都在对应迁移文件里显式授予最小权限，避免查询账户意外读取审计表或内部表。

执行迁移：

```bash
docker compose exec -T postgres \
  psql -v ON_ERROR_STOP=1 -U ccgp_admin -d ccgp \
  < migrations/002_schema.sql
```

为什么当前不分表：

- 10 万至数百万行对 PostgreSQL 单表来说很小。
- 现有索引足以支持地区、时间和标题搜索。
- 只有达到数千万乃至上亿行、维护成本明显升高后，才评估按时间分区。

### 完成标准

- [ ] `app.notices`、`app.regions`、`app.ingest_batches` 存在。
- [ ] `pg_trgm` 已启用。
- [ ] URL 唯一约束存在。
- [ ] 地区+时间复合索引存在。
- [ ] 标题 trigram GIN 索引存在。

检查：

```bash
docker compose exec postgres \
  psql -U ccgp_admin -d ccgp \
  -c "\dt app.*"

docker compose exec postgres \
  psql -U ccgp_admin -d ccgp \
  -c "\di app.*"
```

---

## 第 8 步：导入省级地区字典

创建 `/opt/zhaobiao/migrations/003_regions.sql`：

```sql
insert into app.regions(code, name, aliases) values
('110000', '北京', array['北京市']),
('120000', '天津', array['天津市']),
('130000', '河北', array['河北省']),
('140000', '山西', array['山西省']),
('150000', '内蒙古', array['内蒙古自治区']),
('210000', '辽宁', array['辽宁省']),
('220000', '吉林', array['吉林省']),
('230000', '黑龙江', array['黑龙江省']),
('310000', '上海', array['上海市']),
('320000', '江苏', array['江苏省']),
('330000', '浙江', array['浙江省']),
('340000', '安徽', array['安徽省']),
('350000', '福建', array['福建省']),
('360000', '江西', array['江西省']),
('370000', '山东', array['山东省']),
('410000', '河南', array['河南省']),
('420000', '湖北', array['湖北省']),
('430000', '湖南', array['湖南省']),
('440000', '广东', array['广东省']),
('450000', '广西', array['广西壮族自治区']),
('460000', '海南', array['海南省']),
('500000', '重庆', array['重庆市']),
('510000', '四川', array['四川省']),
('520000', '贵州', array['贵州省']),
('530000', '云南', array['云南省']),
('540000', '西藏', array['西藏自治区']),
('610000', '陕西', array['陕西省']),
('620000', '甘肃', array['甘肃省']),
('630000', '青海', array['青海省']),
('640000', '宁夏', array['宁夏回族自治区']),
('650000', '新疆', array['新疆维吾尔自治区']),
('710000', '台湾', array['台湾省']),
('810000', '香港', array['香港特别行政区']),
('820000', '澳门', array['澳门特别行政区'])
on conflict (code) do update set
  name = excluded.name,
  aliases = excluded.aliases;
```

执行：

```bash
docker compose exec -T postgres \
  psql -v ON_ERROR_STOP=1 -U ccgp_admin -d ccgp \
  < migrations/003_regions.sql
```

验证：

```bash
docker compose exec postgres \
  psql -U ccgp_admin -d ccgp \
  -c "select count(*) from app.regions;"
```

预期为 34 条。网站出现无法映射的地区文字时，`region_code` 保持 `NULL`，但必须保留 `region_raw`，不能丢弃整条公告。

### 完成标准

- [ ] 地区表有 34 条省级记录。
- [ ] “广西壮族自治区”等别名能够映射到统一地区代码。
- [ ] 未知地区不会导致公告写入失败。

---

## 第 9 步：插入样本数据

进入管理员 `psql`：

```bash
docker compose exec postgres psql -U ccgp_admin -d ccgp
```

插入三条样本：

```sql
insert into app.notices (
  source_notice_id,
  canonical_url,
  title,
  published_at,
  region_code,
  region_raw,
  notice_type,
  category,
  source_list_url
) values
(
  'test-001',
  'https://www.ccgp.gov.cn/test/test-001.htm',
  '湖北省某医院医疗设备公开招标公告',
  '2026-09-19 10:00:00+08',
  '420000',
  '湖北',
  '公开招标',
  'zygg',
  'https://www.ccgp.gov.cn/cggg/zygg/index.htm'
),
(
  'test-002',
  'https://www.ccgp.gov.cn/test/test-002.htm',
  '广东省某学校信息化服务采购公告',
  '2026-09-18 09:30:00+08',
  '440000',
  '广东',
  '竞争性磋商',
  'dfgg',
  'https://www.ccgp.gov.cn/cggg/dfgg/index.htm'
),
(
  'test-003',
  'https://www.ccgp.gov.cn/test/test-003.htm',
  '湖北某单位服务器采购项目成交公告',
  '2026-09-17 14:00:00+08',
  '420000',
  '湖北',
  '成交公告',
  'dfgg',
  'https://www.ccgp.gov.cn/cggg/dfgg/index.htm'
);
```

退出：

```text
\q
```

### 完成标准

- [ ] 三条样本都写入成功。
- [ ] 重复执行同一 URL 时唯一约束会阻止重复记录。
- [ ] `published_at` 在数据库中是 `timestamptz`。

---

## 第 10 步：验证三类核心查询

### 10.1 按地区和时间查询

```sql
select id, title, canonical_url, published_at, region_raw
from app.notices
where region_code = '420000'
  and published_at >= '2026-09-01 00:00:00+08'
  and published_at <  '2026-10-01 00:00:00+08'
order by published_at desc, id desc
limit 20;
```

时间范围使用左闭右开：

```text
>= 开始日期 00:00:00
<  下一天或下个月 00:00:00
```

### 10.2 按中文关键词查询

```sql
select id, title, canonical_url, published_at, region_raw
from app.notices
where title ilike '%医疗设备%'
order by published_at desc, id desc
limit 20;
```

`pg_trgm` 能为包含搜索提供索引支持。当前规模不需要立即部署 Elasticsearch。

### 10.3 地区、时间和关键词组合查询

```sql
select id, title, canonical_url, published_at, region_raw
from app.notices
where region_code = '420000'
  and published_at >= '2026-09-01 00:00:00+08'
  and published_at <  '2026-10-01 00:00:00+08'
  and title ilike '%医疗设备%'
order by published_at desc, id desc
limit 20;
```

### 10.4 验证执行计划

```sql
explain (analyze, buffers)
select id, title, canonical_url, published_at, region_raw
from app.notices
where region_code = '420000'
  and published_at >= '2026-09-01 00:00:00+08'
  and published_at <  '2026-10-01 00:00:00+08'
order by published_at desc, id desc
limit 20;
```

当前只有三条样本时，PostgreSQL 可能选择顺序扫描，这是正常现象；数据量增大后再观察索引使用情况。

### 完成标准

- [ ] 地区筛选结果正确。
- [ ] 日期边界正确。
- [ ] 中文关键词能够命中标题。
- [ ] 三个条件组合查询正确。

---

## 第 11 步：验证最小权限

先确认只读账户能够查询。为了不在命令历史中暴露密码，进入容器后由 `psql` 提示输入密码，或通过 API 容器内部网络验证。

需要验证的权限矩阵：

| 操作 | `api_reader` | `ingest_writer` |
|---|---:|---:|
| SELECT notices | 允许 | 允许 |
| INSERT notices | 拒绝 | 允许 |
| UPDATE notices | 拒绝 | 允许 |
| DELETE notices | 拒绝 | 拒绝 |
| DROP TABLE | 拒绝 | 拒绝 |

如果任一业务账户可以删除表或成为超级用户，不要进入下一步。

检查：

```sql
select rolname, rolsuper, rolcreatedb, rolcreaterole
from pg_roles
where rolname in ('api_reader', 'ingest_writer');
```

预期三个管理字段全部为 `false`。

### 完成标准

- [ ] 查询账户只能读。
- [ ] 写入账户不能删除公告。
- [ ] 两个业务账户都不是超级用户。

---

## 第 12 步：建立数据库备份

创建 `/opt/zhaobiao/scripts/backup-postgres.sh`：

```bash
#!/usr/bin/env bash
set -euo pipefail

PROJECT_DIR="/opt/zhaobiao"
BACKUP_DIR="/opt/zhaobiao/backups"
STAMP="$(date +%Y%m%d_%H%M%S)"

cd "$PROJECT_DIR"
set -a
. "$PROJECT_DIR/.env"
set +a

mkdir -p "$BACKUP_DIR"

docker compose exec -T postgres \
  pg_dump \
  --username "$POSTGRES_USER" \
  --dbname "$POSTGRES_DB" \
  --format custom \
  --no-owner \
  --no-acl \
  > "$BACKUP_DIR/ccgp_${STAMP}.dump"

find "$BACKUP_DIR" -type f -name 'ccgp_*.dump' -mtime +14 -delete
```

设置权限并手动执行一次：

```bash
chmod 700 /opt/zhaobiao/scripts/backup-postgres.sh
/opt/zhaobiao/scripts/backup-postgres.sh
ls -lh /opt/zhaobiao/backups
```

设置定时任务：

```bash
crontab -e
```

每天凌晨 2:30 备份：

```cron
30 2 * * * /opt/zhaobiao/scripts/backup-postgres.sh >> /opt/zhaobiao/backups/backup.log 2>&1
```

同一块云盘上的备份不属于完整灾难恢复。至少再做一项：

- 同步到云对象存储。
- 同步到另一台服务器。
- 使用云平台的自动磁盘快照。

### 完成标准

- [ ] 手工备份成功并生成非空 `.dump` 文件。
- [ ] 每日定时任务已配置。
- [ ] 至少有一份备份位于服务器之外。
- [ ] 备份保留周期已确定。

---

## 第 13 步：执行一次恢复演练

只有实际恢复成功的备份才是可信备份。

创建临时恢复库：

```bash
cd /opt/zhaobiao
docker compose exec postgres \
  createdb -U ccgp_admin ccgp_restore_test
```

选择一个备份文件：

```bash
ls -1t /opt/zhaobiao/backups/ccgp_*.dump | head -1
```

恢复到临时库：

```bash
docker compose exec -T postgres \
  pg_restore \
  -U ccgp_admin \
  -d ccgp_restore_test \
  --no-owner \
  --no-acl \
  < /opt/zhaobiao/backups/<具体备份文件名>.dump
```

验证：

```bash
docker compose exec postgres \
  psql -U ccgp_admin -d ccgp_restore_test \
  -c "select count(*) from app.notices;"
```

确认恢复结果后再删除测试库：

```bash
docker compose exec postgres \
  dropdb -U ccgp_admin ccgp_restore_test
```

注意：最后一条命令会删除明确命名的测试数据库，只能在验证目标数据库名为 `ccgp_restore_test` 后执行，不能误填生产数据库名。

### 完成标准

- [ ] 备份可以恢复到独立测试库。
- [ ] 恢复后的表和记录数正确。
- [ ] 恢复演练时间和结果已有记录。

---

# 阶段 D：打通本地采集与云端同步

## 第 14 步：设计本地 SQLite 缓冲队列

本地爬虫不应只写云端。先写入 SQLite，再同步到云端；网络或 API 中断时不会丢数据。

本地 SQLite 表：

```sql
create table if not exists pending_notices (
  notice_url text primary key,
  source_notice_id text,
  title text not null,
  published_at text not null,
  region_raw text,
  region_code text,
  notice_type text,
  category text,
  source_list_url text not null,
  sync_status text not null default 'pending'
    check (sync_status in ('pending', 'syncing', 'synced', 'failed')),
  retry_count integer not null default 0,
  last_error text,
  created_at text not null,
  updated_at text not null,
  synced_at text
);

create index if not exists pending_notices_status_idx
  on pending_notices(sync_status, created_at);
```

状态流转：

```text
pending → syncing → synced
             ↓
           failed → pending（延迟重试）
```

程序意外退出后，应把长时间停留在 `syncing` 的记录恢复为 `pending`。

### 完成标准

- [ ] 每抓完一页先写 SQLite 并提交事务。
- [ ] URL 是本地唯一键。
- [ ] 云端暂时不可用时，采集数据仍会保留。
- [ ] 同步任务可以从失败位置继续。

---

## 第 15 步：开发云端写入 API

不要把 PostgreSQL `5432` 暴露给本地爬虫。推荐接口：

```http
POST /internal/v1/notices/batch
Authorization: Bearer <INGEST_API_KEY>
Content-Type: application/json
```

请求：

```json
{
  "batch_key": "zygg-20260919-page-12-a1b2c3",
  "items": [
    {
      "source_notice_id": "27357383",
      "url": "https://www.ccgp.gov.cn/cggg/zygg/jzxcs/202609/t20260918_27357383.htm",
      "title": "武汉理工大学某项目竞争性磋商公告",
      "published_at": "2026-09-18T17:48:25+08:00",
      "region_raw": "湖北",
      "region_code": "420000",
      "notice_type": "竞争性磋商",
      "category": "zygg",
      "source_list_url": "https://www.ccgp.gov.cn/cggg/zygg/index.htm"
    }
  ]
}
```

成功响应：

```json
{
  "batch_key": "zygg-20260919-page-12-a1b2c3",
  "received": 20,
  "inserted": 18,
  "updated": 2,
  "rejected": 0
}
```

API 必须完成：

1. 验证 API Key。
2. 限制请求体，例如最多 500 条或 1 MB。
3. 验证 URL 域名、标题非空、时间格式和字符串长度。
4. 根据地区别名填充 `region_code`；无法识别时保留 `region_raw`。
5. 在一个短事务中批量 UPSERT。
6. 使用 `batch_key` 保证批次重试幂等。
7. 返回插入、更新和拒绝数量。
8. 日志不得输出数据库密码和完整 API Key。

核心 UPSERT：

```sql
insert into app.notices (
  source,
  source_notice_id,
  canonical_url,
  title,
  published_at,
  region_code,
  region_raw,
  notice_type,
  category,
  source_list_url
) values (...)
on conflict (canonical_url) do update set
  title = excluded.title,
  published_at = excluded.published_at,
  region_code = excluded.region_code,
  region_raw = excluded.region_raw,
  notice_type = excluded.notice_type,
  category = excluded.category,
  source_list_url = excluded.source_list_url,
  last_seen_at = now(),
  updated_at = now();
```

一次提交 20～500 条，不要每条开启一次数据库连接和事务。

### 数据库连接方式

如果 API 也在 Compose 中，数据库主机使用服务名：

```text
postgresql://ingest_writer:<密码>@postgres:5432/ccgp
```

不是：

```text
localhost:5432
```

API 使用连接池，初始设置 5～10 个连接即可。不要为每个 HTTP 请求新建数据库连接。

### 完成标准

- [ ] 未带 API Key 的请求返回 401 或 403。
- [ ] 相同批次重复提交不会制造重复公告。
- [ ] 单个无效项目不会静默污染数据库。
- [ ] 写入 API 使用 `ingest_writer`，不使用 `ccgp_admin`。
- [ ] API 使用连接池。

---

## 第 16 步：配置域名和 HTTPS

准备一个域名，例如：

```text
api.example.com
```

在 DNS 服务商添加 A 记录，指向云服务器公网 IP。

推荐使用 Caddy 或 Nginx 反向代理。公开网络只访问 HTTPS API，不直接访问 PostgreSQL。

需要验证：

```text
https://api.example.com/health
```

健康检查返回示例：

```json
{
  "status": "ok",
  "database": "ok"
}
```

健康检查不要暴露：

- 数据库地址和密码。
- PostgreSQL完整版本信息。
- 异常堆栈。
- API Key。

### 完成标准

- [ ] DNS 已解析到正确服务器。
- [ ] HTTPS 证书有效。
- [ ] HTTP 自动跳转 HTTPS。
- [ ] 写入 API 只能通过有效 API Key 调用。
- [ ] 5432 端口仍未开放。

---

## 第 17 步：打通本地同步流程

同步任务每次读取一批 `pending` 数据：

```text
读取 100～500 条 pending
→ 生成稳定 batch_key
→ 标记为 syncing
→ POST 到写入 API
→ 成功后标记为 synced
→ 失败则记录错误并稍后重试
```

推荐重试策略：

```text
第 1 次失败：1 分钟后
第 2 次失败：5 分钟后
第 3 次失败：30 分钟后
第 4 次及以后：停止自动重试并报警
```

HTTP 响应处理：

| 状态 | 处理 |
|---|---|
| 200/201 | 标记已同步 |
| 400/422 | 数据问题，记录错误，人工检查 |
| 401/403 | 认证问题，停止任务，不要连续重试 |
| 429 | 遵守 `Retry-After` |
| 500/502/503 | 指数退避重试 |
| 网络超时 | 使用相同 `batch_key` 重试 |

### 完成标准

- [ ] 本地插入 20 条样本后能同步到云端。
- [ ] 重复同步不会产生重复数据。
- [ ] 网络断开后数据仍在 SQLite。
- [ ] 网络恢复后可以继续同步。
- [ ] 本地和云端记录数能够核对。

---

# 阶段 E：历史导入与正式上线

## 第 18 步：先完成 1000 条试运行

不要直接开始 10 万条历史任务。第一批只采集约 1000 条：

1. 静态列表采集器抓取约 50 页。
2. 数据写入本地 SQLite。
3. 每批 100～500 条同步到写入 API。
4. 检查云端记录总数。
5. 抽样打开 100 个 URL。
6. 检查标题、地区、日期。
7. 测试用户查询接口。

质量检查 SQL：

```sql
-- 总记录数
select count(*) from app.notices;

-- URL 重复检查，预期返回 0 行
select canonical_url, count(*)
from app.notices
group by canonical_url
having count(*) > 1;

-- 空标题检查，预期为 0
select count(*)
from app.notices
where title is null or btrim(title) = '';

-- 日期范围
select min(published_at), max(published_at)
from app.notices;

-- 地区分布
select coalesce(region_raw, '未知') as region, count(*)
from app.notices
group by region_raw
order by count(*) desc;

-- 地区无法标准化的比例
select
  count(*) filter (where region_code is null) as unmapped,
  count(*) as total
from app.notices;
```

### 完成标准

- [ ] 1000 条试运行完成。
- [ ] URL 重复数为 0。
- [ ] 空标题数为 0。
- [ ] 日期和地区抽样准确。
- [ ] 未标准化地区能够被列出并修正规则。
- [ ] 备份任务在试运行期间正常执行。

---

## 第 19 步：运行 10 万条历史初始化

推荐分批，不要一口气持续翻 5000 页：

```text
每批：100～300 页
每页：约 20 条
每批：约 2000～6000 条
批次结束：检查封禁、错误率、同步积压和云端数量
```

每批记录：

```text
分类
起止页码
开始与结束时间
列表页成功数
新增 URL 数
重复 URL 数
解析失败数
软封禁次数
本地待同步数
云端已同步数
```

历史初始化期间：

- 搜索站点出现“频繁访问”立即停止并冷却。
- 本地 SQLite 队列积压过多时，暂停采集，优先同步。
- 每完成一批就核对数据库数量。
- 每完成一天任务就确认备份文件存在且非空。

导入大量数据后更新统计信息：

```sql
analyze app.notices;
```

检查统计信息：

```sql
select
  relname,
  last_vacuum,
  last_autovacuum,
  last_analyze,
  last_autoanalyze
from pg_stat_user_tables
where schemaname = 'app';
```

### 完成标准

- [ ] 云端有约 10 万条唯一公告。
- [ ] 本地 `pending/failed` 积压已清零或原因明确。
- [ ] URL、标题、日期、地区抽样通过。
- [ ] `ANALYZE` 已完成。
- [ ] 已生成可恢复的最终初始化备份。

---

## 第 20 步：开发用户查询 API

推荐接口：

```http
GET /api/v1/notices
  ?region_code=420000
  &start_date=2026-09-01
  &end_date=2026-09-30
  &keyword=医疗设备
  &limit=20
  &cursor=<游标>
```

后端将日期转换成带时区的左闭右开范围。

组合条件查询：

```sql
select id, title, canonical_url, published_at, region_code, region_raw
from app.notices
where region_code = $1
  and published_at >= $2::timestamptz
  and published_at < $3::timestamptz
  and title ilike '%' || $4 || '%'
order by published_at desc, id desc
limit $5;
```

下一页使用 `(published_at, id)` 游标：

```sql
select id, title, canonical_url, published_at, region_code, region_raw
from app.notices
where region_code = $1
  and published_at >= $2::timestamptz
  and published_at < $3::timestamptz
  and title ilike '%' || $4 || '%'
  and (published_at, id) < ($5::timestamptz, $6::bigint)
order by published_at desc, id desc
limit $7;
```

地区、日期和关键词都是可选参数时，后端应只拼接实际存在的 `WHERE` 条件，并始终使用参数绑定；不要通过字符串拼接把用户输入直接写入 SQL。为每种常用组合保留明确的条件，更容易让 PostgreSQL 使用合适索引。

不要使用深分页：

```sql
limit 20 offset 100000;
```

API 约束：

```text
limit 默认 20，最大 100
keyword 最大长度 100
日期范围最大跨度按产品需要限制
所有 SQL 使用参数绑定
增加 IP/用户级查询限流
响应只返回必要字段
```

查询 API 使用 `api_reader`，不能使用 `ingest_writer` 或 `ccgp_admin`。

### 完成标准

- [ ] 地区、日期、关键词可以任意组合。
- [ ] 使用游标分页。
- [ ] 查询 API 只使用只读账户。
- [ ] 所有 SQL 都使用参数绑定。
- [ ] API 有 limit 上限和请求限流。

---

## 第 21 步：生产上线前检查

### 安全

- [ ] 云安全组没有开放 5432。
- [ ] UFW 没有开放 5432。
- [ ] PostgreSQL 只绑定 `127.0.0.1` 或 Docker 内网。
- [ ] 业务应用没有使用管理员账户。
- [ ] `.env` 权限为 `600`，没有提交 Git。
- [ ] API 使用 HTTPS。
- [ ] 写入 API 有独立密钥和速率限制。
- [ ] 日志不记录密码或完整密钥。

### 数据质量

- [ ] URL 唯一约束已启用。
- [ ] 空标题为 0。
- [ ] 时间统一存储为 `timestamptz`。
- [ ] 地区原文始终保留。
- [ ] 未映射地区有监控。
- [ ] 抽样 URL 可正常打开。

### 可靠性

- [ ] PostgreSQL 容器自动重启。
- [ ] 每日数据库备份成功。
- [ ] 有服务器外备份。
- [ ] 已成功完成一次恢复演练。
- [ ] 本地 SQLite 可以在断网时继续缓冲。
- [ ] 同步 API 支持幂等重试。

### 性能

- [ ] API 使用数据库连接池。
- [ ] 地区+时间复合索引存在。
- [ ] 标题 trigram 索引存在。
- [ ] 用户查询使用游标分页。
- [ ] 大批量导入后执行过 `ANALYZE`。

只有上述关键检查全部通过，才向正式用户开放查询。

---

# 日常运维流程

## 每天检查

```bash
cd /opt/zhaobiao
docker compose ps
docker compose logs --since=24h --tail=200 postgres
ls -lh backups | tail
df -h
```

数据库检查：

```sql
select count(*) from app.notices;

select max(published_at), max(last_seen_at)
from app.notices;

select count(*)
from pg_stat_activity;
```

## 每周检查

- 备份是否持续生成。
- 云盘容量和 inode 是否充足。
- API 错误率和响应时间。
- 地区映射失败数量。
- 本地 SQLite 是否存在大量 `failed`。
- 查询慢日志和索引使用情况。

## 每月检查

- 做一次恢复演练。
- 更新 Ubuntu 安全补丁。
- 评估 PostgreSQL Docker 镜像补丁版本；先备份和测试，再升级。
- 轮换写入 API Key和过期账户密码。
- 检查对象存储备份可下载、可解密、可恢复。

---

# 常见问题

## 1. PostgreSQL 容器启动失败

检查：

```bash
cd /opt/zhaobiao
docker compose config
docker compose logs --tail=200 postgres
docker inspect zhaobiao-postgres
```

常见原因：

- `.env` 格式错误。
- 数据盘空间不足。
- 5432 被其他本地程序占用。
- 升级了不兼容的 PostgreSQL 主版本。

不要在没有备份的情况下删除数据卷重新初始化。

## 2. API 连接不到 PostgreSQL

如果 API 在同一个 Compose 网络：

```text
host 应为 postgres
port 应为 5432
```

如果 API 直接运行在服务器宿主机：

```text
host 应为 127.0.0.1
port 应为 5432
```

## 3. 查询越来越慢

依次检查：

1. 是否使用深度 `OFFSET`。
2. 是否缺少地区+时间索引。
3. 关键词是否使用前后 `%` 且缺少 trigram 索引。
4. 大批量导入后是否执行 `ANALYZE`。
5. API 是否每次请求都创建新数据库连接。
6. 是否返回了过多字段或一次返回过多记录。

## 4. 数据重复

数据库以 `canonical_url` 为最终唯一键。同步过程必须使用 UPSERT，不要先 SELECT 再 INSERT。

## 5. 云数据库临时不可用

停止云端同步，但本地采集结果继续写 SQLite。恢复后使用相同 `batch_key` 重试，不需要重新爬取。

## 6. 是否现在就需要 Elasticsearch

不需要。10 万到数百万条短标题，PostgreSQL `pg_trgm` 足够。只有明确出现以下需求时再评估：

- 复杂中文分词和同义词。
- 相关度排序要求明显提高。
- 多字段全文检索和聚合非常复杂。
- PostgreSQL 查询已通过执行计划证明确实成为瓶颈。

---

# 推荐的实际开发顺序

按下面的短清单执行，最不容易返工：

```text
1. 购买 Ubuntu 云服务器
2. 配置安全组和 SSH
3. 安装 Docker
4. 部署 PostgreSQL
5. 创建角色、表和索引
6. 插入 3 条样本
7. 验证地区、日期、关键词查询
8. 配置备份并完成恢复演练
9. 建立本地 SQLite 缓冲
10. 开发云端写入 API
11. 配置域名和 HTTPS
12. 打通 20 条样本同步
13. 试运行 1000 条
14. 分批初始化 10 万条
15. 开发只读查询 API
16. 完成上线检查后开放用户访问
```

整个过程中，PostgreSQL 是唯一正式数据源；SQLite 是采集缓冲；备份文件只用于恢复。不要把三者混作同一用途。
