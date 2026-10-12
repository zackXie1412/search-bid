# 找标系统端到端开发顺序：从现有服务器到10万条数据上线

> 这份文档解决一个核心问题：**已经有云服务器以后，到底应该先部署数据库、先写爬虫，还是先抓数据？**
>
> 明确答案：**先确定数据结构并在云端部署数据库骨架，再用少量页面开发爬虫；打通本地缓冲与云端同步后，先试运行1000条，最后才启动10万条正式采集。**
>
> 不建议先把10万条全部爬到本地后再设计数据库，也不建议数据库刚部署好就立刻跑5000页。

配套文档：

- [静态分类列表采集方案](./ccgp_static_list_crawler_design.md)
- [云端 PostgreSQL 部署与数据上线手册](./ccgp_cloud_postgresql_step_by_step.md)

---

## 1. 先看完整执行顺序

```text
第 0 步  检查现有服务器是否满足部署条件
    ↓
第 1 步  冻结第一版数据合同
    ↓
第 2 步  在云服务器部署 PostgreSQL
    ↓
第 3 步  创建表、索引、角色和备份
    ↓
第 4 步  插入少量样本并验证查询
    ↓
第 5 步  在本地建立采集项目和 SQLite 缓冲
    ↓
第 6 步  开发单页解析器，只测试 1～3 页
    ↓
第 7 步  开发可停止、可恢复的分页采集器
    ↓
第 8 步  开发云端批量写入 API
    ↓
第 9 步  打通“本地 SQLite → HTTPS API → PostgreSQL”
    ↓
第 10 步  完成 1000 条端到端试运行
    ↓
第 11 步  开发并验证用户查询 API
    ↓
第 12 步  分批执行 10 万条历史初始化
    ↓
第 13 步  切换为长期增量采集
    ↓
第 14 步  持续监控、备份和数据质量检查
```

### 为什么这个顺序更稳

如果先爬10万条再设计数据库，最常见的返工包括：

- 日期最初保存成字符串，后来才发现需要范围查询。
- 地区只保存原文，后来无法统一查询“广西”和“广西壮族自治区”。
- URL 没有规范化，导入后出现大量重复。
- 没有唯一键，重复运行爬虫产生多份相同公告。
- 本地 CSV 字段和云端表字段不一致，需要重新清洗。
- 一开始没有保存来源页，后续无法审计某条数据从哪里来。

因此应先确定数据库约束和输入格式，再开发爬虫输出同样的字段。

---

## 2. 三个运行位置及职责

开发前先明确每段程序在哪里运行：

| 位置 | 运行内容 | 保存内容 |
|---|---|---|
| 你的本地电脑 | Playwright、HTTP采集器、同步客户端 | SQLite待同步队列、采集断点、临时日志 |
| 云服务器 | PostgreSQL、写入API、查询API、HTTPS代理 | 正式业务数据、API日志 |
| 异地存储 | 对象存储、另一台服务器或云快照 | PostgreSQL备份 |

正式数据链路：

```text
CCGP 静态列表页
      ↓
本地采集器
      ↓
本地 SQLite
      ↓ HTTPS 批量上传
云端写入 API
      ↓ UPSERT
PostgreSQL
      ↓ SELECT
云端查询 API
      ↓
用户
```

关键原则：

1. PostgreSQL 是唯一正式数据源。
2. SQLite 是本地采集缓冲，不直接供用户查询。
3. 本地爬虫不直接连接公网 PostgreSQL。
4. 用户也不直接连接 PostgreSQL，只访问查询 API。
5. 备份文件不能代替在线数据库，但必须能够恢复数据库。

---

# 第 0 步：检查现有服务器

## 0.1 这一步在哪里做

云服务器。

## 0.2 需要确认什么

建议环境：

```text
Ubuntu Server 22.04/24.04 LTS
2 vCPU
2～4 GB 内存
40 GB 以上 SSD
可 SSH 登录
有公网 IP
可以配置安全组
```

登录服务器：

```bash
ssh <用户名>@<服务器IP>
```

检查：

```bash
cat /etc/os-release
uname -m
free -h
df -h
ip addr
```

检查端口：

```bash
sudo ss -lntp
```

云安全组只需要准备：

```text
22：SSH，尽量只允许你的 IP
80：HTTP，用于证书申请和跳转
443：HTTPS API
```

不要开放：

```text
5432：PostgreSQL
6379：Redis
```

## 0.3 本步骤的产出

记录以下信息，但不要把密码提交到 Git：

```text
服务器 IP：
操作系统：
CPU/内存/磁盘：
SSH 用户：
是否有域名：
是否安装 Docker：
云安全组开放端口：
```

## 0.4 完成标准

- [ ] 能稳定 SSH 登录。
- [ ] 磁盘至少有 20 GB 可用空间。
- [ ] 安全组没有开放 5432。
- [ ] 服务器可以访问 Docker 镜像源或软件仓库。

如果服务器不是 Ubuntu，也可以继续，但后续安装命令需要按系统调整。

---

# 第 1 步：冻结第一版数据合同

## 1.1 这一步为什么必须在爬虫之前

“数据合同”定义爬虫输出什么、数据库接收什么、查询 API 返回什么。如果字段没有先固定，采集完成后很容易返工。

## 1.2 第一版必须采集的字段

虽然用户最终只看 URL、日期、标题和地区，采集层还应保留少量审计字段：

| 字段 | 必填 | 示例 | 用途 |
|---|---:|---|---|
| `canonical_url` | 是 | `https://www.ccgp.gov.cn/...htm` | 公告唯一键 |
| `title` | 是 | `某医院医疗设备公开招标公告` | 关键词查询 |
| `published_at` | 是 | `2026-09-19T10:30:00+08:00` | 时间筛选 |
| `region_raw` | 否 | `湖北` | 保留网站原文 |
| `region_code` | 否 | `420000` | 标准化地区筛选 |
| `source_notice_id` | 否 | `27357383` | 来源编号和辅助去重 |
| `notice_type` | 否 | `公开招标` | 后续类型筛选 |
| `category` | 是 | `zygg` | 中央/地方分类来源 |
| `source_list_url` | 是 | 列表页 URL | 数据审计 |
| `first_seen_at` | 云端生成 | 当前时间 | 首次发现时间 |
| `last_seen_at` | 云端生成 | 当前时间 | 最近发现时间 |

## 1.3 字段规则

### URL

- 相对 URL 必须转绝对 URL。
- 删除 `#fragment`。
- `www.ccgp.gov.cn` 链接统一使用 HTTPS。
- 规范化后的 URL 是最终唯一键。

### 标题

- 优先读取链接 `title` 属性，避免页面文本中的省略号。
- 去掉首尾空格。
- 标题不能为空。
- 不对标题做翻译或改写，保留来源文字。

### 时间

- 采集器输出 ISO 8601 且包含时区。
- 示例：`2026-09-19T10:30:00+08:00`。
- PostgreSQL 存储类型使用 `timestamptz`。
- 无法解析的日期不能伪造，应记录为采集错误并保留原始行。

### 地区

- `region_raw` 保存页面原文。
- `region_code` 保存标准化行政区代码。
- 无法映射时允许 `region_code = NULL`，但不能丢弃整条公告。

## 1.4 统一 JSON 格式

爬虫、SQLite、同步 API 使用相同结构：

```json
{
  "canonical_url": "https://www.ccgp.gov.cn/cggg/dfgg/gkzb/202609/t20260919_12345678.htm",
  "title": "湖北省某医院医疗设备公开招标公告",
  "published_at": "2026-09-19T10:30:00+08:00",
  "region_raw": "湖北",
  "region_code": "420000",
  "source_notice_id": "12345678",
  "notice_type": "公开招标",
  "category": "dfgg",
  "source_list_url": "https://www.ccgp.gov.cn/cggg/dfgg/index.htm"
}
```

## 1.5 本步骤的产出

建议在代码仓库中创建：

```text
docs/data-contract.md
schemas/notice.schema.json
```

JSON Schema 至少验证：

- URL 格式。
- 标题最小长度为 1。
- `published_at` 是 `date-time`。
- 地区代码为 6 位数字或 `null`。
- `category` 只能是允许的分类值。

## 1.6 完成标准

- [ ] 字段名和格式已经固定。
- [ ] URL、时间和地区的标准化规则明确。
- [ ] 爬虫输出与 API 输入使用同一个 JSON 格式。
- [ ] 后续新增字段通过数据库迁移实现，不直接手工改生产表。

在本步骤完成前，不启动正式历史采集。

---

# 第 2 步：先在服务器部署 PostgreSQL

## 2.1 回答“先爬还是先部署数据库”

正式顺序是：

```text
先部署 PostgreSQL 骨架
→ 再写爬虫
→ 先抓少量样本
→ 打通同步
→ 最后正式抓10万条
```

这里的“先部署数据库”不意味着数据库一好就开始全量采集，而是先确保：

- 表结构正确。
- 唯一约束正确。
- 时间和地区能查询。
- 备份可以恢复。
- 爬虫明确要输出什么。

## 2.2 部署方式

推荐 Docker Compose + PostgreSQL 16。详细命令见：

[云端 PostgreSQL 部署与数据上线手册](./ccgp_cloud_postgresql_step_by_step.md)

最小要求：

```text
PostgreSQL 16
持久化 Docker volume
restart: unless-stopped
健康检查
5432 只绑定 127.0.0.1 或 Docker 内网
每日备份
```

不要把数据库端口直接暴露到公网。

## 2.3 数据库角色

至少三个账户：

| 账户 | 使用者 | 权限 |
|---|---|---|
| `ccgp_admin` | 迁移脚本和运维人员 | 建表、改表、备份恢复 |
| `ingest_writer` | 写入 API | SELECT、INSERT、UPDATE；不能 DELETE |
| `api_reader` | 查询 API | 只能 SELECT |

应用程序不能使用 `ccgp_admin`。

## 2.4 本步骤的产出

```text
/opt/zhaobiao/compose.yaml
/opt/zhaobiao/.env
PostgreSQL 持久化 volume
三个数据库账户
数据库健康检查
```

## 2.5 完成标准

- [ ] PostgreSQL 容器状态健康。
- [ ] 重启服务器后数据库自动启动。
- [ ] 5432 不对公网开放。
- [ ] 三个账户已经创建且密码不同。
- [ ] 应用账户不是超级用户。

---

# 第 3 步：创建正式表结构、索引和备份

## 3.1 这一步在哪里做

数据库迁移文件在代码仓库维护，通过管理员账户应用到云端 PostgreSQL。

## 3.2 必要表

第一版至少有：

```text
app.notices          正式公告
app.regions          地区字典
app.ingest_batches   同步批次幂等记录
```

本地 SQLite 的队列表不属于云端 PostgreSQL。

## 3.3 notices 表的必要约束

```sql
create extension if not exists pg_trgm;

create table app.notices (
  id bigint generated always as identity primary key,
  source text not null default 'ccgp',
  source_notice_id text,
  canonical_url text not null unique,
  title text not null check (length(btrim(title)) > 0),
  published_at timestamptz not null,
  region_code text references app.regions(code),
  region_raw text,
  notice_type text,
  category text not null,
  source_list_url text not null,
  first_seen_at timestamptz not null default now(),
  last_seen_at timestamptz not null default now(),
  updated_at timestamptz not null default now()
);
```

## 3.4 必要索引

```sql
create index notices_published_idx
  on app.notices (published_at desc, id desc);

create index notices_region_published_idx
  on app.notices (region_code, published_at desc, id desc);

create index notices_title_trgm_idx
  on app.notices using gin (title gin_trgm_ops);
```

当前不做月份分表，也不部署 Elasticsearch。10万条至数百万条短标题，PostgreSQL 单表足够。

## 3.5 先做备份再放正式数据

在正式数据进入前完成：

1. `pg_dump` 每日自动备份。
2. 备份保留 7～14 天。
3. 备份同步到服务器外部。
4. 恢复到测试数据库并验证记录。

数据库部署成功不等于备份成功。只有实际完成过恢复演练，备份才可信。

## 3.6 本步骤的产出

```text
migrations/001_roles.sql
migrations/002_schema.sql
migrations/003_regions.sql
scripts/backup-postgres.sh
一次成功恢复演练记录
```

## 3.7 完成标准

- [ ] 数据表通过迁移脚本创建。
- [ ] URL 唯一约束生效。
- [ ] 地区+时间索引存在。
- [ ] 标题 trigram 索引存在。
- [ ] 每日备份任务存在。
- [ ] 备份已经成功恢复到测试库一次。

---

# 第 4 步：先用手工样本验证数据库，不运行爬虫

## 4.1 为什么先插样本

这一步把数据库问题和爬虫问题隔离开。如果手工样本都不能正常查询，不应让爬虫开始写数据。

## 4.2 插入样本

手工准备 5～10 条覆盖不同情况的数据：

```text
湖北 + 医疗设备
湖北 + 服务器
广东 + 信息化服务
无标准地区代码
相同日期不同 ID
```

插入后验证：

```sql
select id, title, canonical_url, published_at, region_raw
from app.notices
where region_code = '420000'
  and published_at >= '2026-09-01 00:00:00+08'
  and published_at < '2026-10-01 00:00:00+08'
  and title ilike '%医疗设备%'
order by published_at desc, id desc
limit 20;
```

## 4.3 验证 UPSERT

对同一个 `canonical_url` 再提交一次更新后的标题：

```sql
insert into app.notices (...)
values (...)
on conflict (canonical_url) do update set
  title = excluded.title,
  last_seen_at = now(),
  updated_at = now();
```

预期：

- 总记录数不增加。
- 原记录被更新。
- 不生成重复 URL。

## 4.4 验证账户权限

| 操作 | `api_reader` | `ingest_writer` |
|---|---:|---:|
| SELECT | 成功 | 成功 |
| INSERT | 失败 | 成功 |
| UPDATE | 失败 | 成功 |
| DELETE | 失败 | 失败 |
| DROP TABLE | 失败 | 失败 |

## 4.5 完成标准

- [ ] 地区、日期、关键词组合查询正确。
- [ ] 同一个 URL 重复插入不会产生重复记录。
- [ ] 两个业务账户权限符合设计。
- [ ] 备份中包含样本数据且可以恢复。

通过以后，才开始写爬虫。

---

# 第 5 步：在本地建立采集项目和 SQLite 缓冲

## 5.1 这一步在哪里做

你的本地电脑。当前测试证明本机 Chrome 和持久化 Playwright 可以正常访问搜索页，因此第一版采集器优先放本地运行。

## 5.2 推荐目录

```text
ccgp-crawler/
├── pyproject.toml
├── .env.example
├── .gitignore
├── README.md
├── src/
│   └── ccgp_crawler/
│       ├── config.py
│       ├── models.py
│       ├── normalize.py
│       ├── parser.py
│       ├── fetcher.py
│       ├── crawler.py
│       ├── local_store.py
│       ├── sync_client.py
│       └── cli.py
├── migrations/
│   └── sqlite_001.sql
├── tests/
│   ├── fixtures/
│   ├── test_parser.py
│   ├── test_normalize.py
│   └── test_local_store.py
└── runtime/
    ├── crawler.sqlite3
    ├── html_failures/
    └── logs/
```

`runtime/` 不提交 Git。

## 5.3 SQLite 必要表

### pending_notices

保存待同步公告：

```sql
create table pending_notices (
  canonical_url text primary key,
  source_notice_id text,
  title text not null,
  published_at text not null,
  region_raw text,
  region_code text,
  notice_type text,
  category text not null,
  source_list_url text not null,
  sync_status text not null default 'pending',
  retry_count integer not null default 0,
  last_error text,
  created_at text not null,
  updated_at text not null,
  synced_at text
);
```

### crawl_checkpoint

保存采集位置：

```sql
create table crawl_checkpoint (
  category text primary key,
  next_page integer not null default 0,
  last_success_at text,
  last_notice_url text,
  status text not null default 'idle',
  error_message text
);
```

### crawl_page_runs

保存每个列表页的审计结果：

```sql
create table crawl_page_runs (
  id integer primary key autoincrement,
  category text not null,
  page_no integer not null,
  page_url text not null,
  http_status integer,
  row_count integer not null default 0,
  new_count integer not null default 0,
  duplicate_count integer not null default 0,
  blocked integer not null default 0,
  elapsed_ms integer,
  error_message text,
  started_at text not null,
  finished_at text
);
```

## 5.4 完成标准

- [ ] 项目可以在本地创建和初始化 SQLite。
- [ ] 重复写同一个 URL 不会产生两条记录。
- [ ] checkpoint 能更新和恢复。
- [ ] `.env`、SQLite文件和日志不会提交 Git。

---

# 第 6 步：先开发单页解析器，只测试1～3页

## 6.1 不要一开始开发完整翻页

先把一个静态 HTML 页面解析正确，再增加网络请求和分页。这样可以快速区分：

- HTML选择器错误。
- URL规范化错误。
- 日期正则错误。
- 网络访问错误。
- 软封禁错误。

## 6.2 开发顺序

### A. 保存一页 HTML fixture

只请求一个列表页，将 HTML 保存到：

```text
tests/fixtures/zygg_index.html
```

### B. 离线开发 parser

输入：HTML字符串和来源 URL。

输出：公告对象列表。

测试要求：

- 能提取约20条公告。
- 标题优先取 `title` 属性。
- URL变成绝对 HTTPS URL。
- 日期包含 `+08:00`。
- 地区原文保留。
- 导航栏链接不会混入公告数据。

### C. 单页联网测试

联网访问第一页一次，将解析结果打印为 JSON Lines，但先不启动连续翻页。

### D. 三页测试

只访问第0、1、2页，检查：

- 每页约20条。
- 三页 URL 无重复或重复原因可解释。
- 日期按页面顺序基本递减。
- 没有“频繁访问”页面。

## 6.3 软封禁检测

所有解析前都检查：

```python
BLOCK_MARKERS = (
    "您的访问过于频繁",
    "频繁访问",
    "请稍后再试",
    "事件ID",
)
```

状态码200但正文包含上述文字，也必须认定失败并停止。

## 6.4 完成标准

- [ ] 离线 fixture 测试稳定通过。
- [ ] 三页联网测试能得到有效公告。
- [ ] URL、标题、日期、地区抽样正确。
- [ ] 软封禁页面不会被当作空数据或正常页面。
- [ ] 解析失败的 HTML 会保存用于排错。

通过之前，不运行50页或更多页面。

---

# 第 7 步：开发可停止、可恢复的分页采集器

## 7.1 分页采集流程

每页严格执行：

```text
读取 checkpoint
→ 构造当前列表页 URL
→ 单次 HTTP 请求
→ 检测软封禁
→ 解析公告
→ 在一个 SQLite 事务中写入公告
→ 写入 crawl_page_runs
→ 更新 next_page
→ 提交事务
→ 随机等待
→ 下一页
```

如果公告写入成功但 checkpoint 尚未提交，重启后会重新抓当前页；由于 URL 是唯一键，不会产生重复数据。

## 7.2 必要 CLI 参数

建议提供：

```bash
python -m ccgp_crawler crawl \
  --category zygg \
  --start-page 0 \
  --max-pages 3 \
  --delay-min 2 \
  --delay-max 5
```

必须支持：

```text
--category
--start-page
--max-pages
--delay-min
--delay-max
--resume
--dry-run
```

`--dry-run` 只解析和打印，不写 SQLite。

## 7.3 停止条件

出现任一情况立即停止当前分类：

- 软封禁。
- 连续3次网络失败。
- 页面选择器无法提取任何公告。
- 当前页与上一页 URL 集合完全相同。
- 用户发送终止信号。
- 达到 `--max-pages`。

不能因为单页为空就直接认定历史数据已经结束；先记录页面和错误，再由规则判断。

## 7.4 访问节奏

初始设置：

```text
并发：1
页间随机等待：2～5秒
网络失败：指数退避，最多3次
软封禁：立即停止，不自动换IP
```

## 7.5 完成标准

- [ ] `--max-pages 3` 只访问三页。
- [ ] Ctrl+C后已完成页面不会丢失。
- [ ] 再次运行 `--resume` 能从checkpoint继续。
- [ ] 重复运行不会制造重复URL。
- [ ] 每页都有审计日志。
- [ ] 软封禁后不会继续翻页。

---

# 第 8 步：开发云端批量写入 API

## 8.1 为什么不让本地爬虫直接连接 PostgreSQL

直接开放5432会增加安全风险，也不利于校验、限流和审计。正确方式：

```text
本地同步客户端 → HTTPS API → PostgreSQL
```

## 8.2 API接口

```http
POST /internal/v1/notices/batch
Authorization: Bearer <INGEST_API_KEY>
Content-Type: application/json
```

请求：

```json
{
  "batch_key": "zygg-000001-000100-sha256前12位",
  "items": []
}
```

单批建议100～500条，最大请求体建议1 MB。

## 8.3 API处理顺序

```text
验证 API Key
→ 验证 batch_key
→ 验证 items 数量
→ 校验每条数据格式
→ 查询 batch_key 是否已处理
→ 开启数据库事务
→ 批量 UPSERT notices
→ 写入 ingest_batches
→ 提交事务
→ 返回统计结果
```

## 8.4 幂等规则

需要两层幂等：

1. `batch_key` 唯一：同一批网络重试不会重复处理。
2. `canonical_url` 唯一：不同批次重复遇到同一公告也不会重复。

## 8.5 数据库连接

- API使用`ingest_writer`账户。
- 使用5～10个连接的连接池。
- 每个批次使用短事务。
- 不允许DELETE。
- 不在日志中打印数据库密码或完整API Key。

## 8.6 API测试顺序

1. 无API Key：应返回401/403。
2. 错误API Key：应返回401/403。
3. 空items：应返回400/422。
4. 非法URL：应拒绝。
5. 非法日期：应拒绝。
6. 20条有效样本：应成功。
7. 同批次重传：记录数不增加。
8. 新批次包含相同URL：记录数不增加，只更新字段。

## 8.7 完成标准

- [ ] API通过HTTPS访问。
- [ ] 5432依然没有开放。
- [ ] 批次和URL都具备幂等性。
- [ ] 写入使用最小权限账户。
- [ ] API使用数据库连接池。
- [ ] 失败响应包含可定位但不泄密的错误信息。

---

# 第 9 步：打通本地 SQLite 到云端 PostgreSQL

## 9.1 同步客户端工作流

```text
从SQLite取100～500条pending
→ 生成稳定batch_key
→ 标记为syncing
→ HTTPS提交
→ 成功后标记synced
→ 失败则记录last_error并增加retry_count
```

## 9.2 稳定 batch_key

同一批数据在重试时必须得到相同 `batch_key`。可以使用：

```text
category + 最小URL + 最大URL + URL排序后SHA256
```

不要使用每次都变化的随机UUID作为重试批次键。

## 9.3 状态恢复

程序启动时执行：

```text
把超过30分钟仍为syncing的记录重置为pending
```

因为上次程序可能在请求过程中崩溃。依靠批次幂等和URL唯一约束，重复提交是安全的。

## 9.4 重试规则

| 类型 | 处理 |
|---|---|
| 网络超时 | 使用相同batch_key重试 |
| 429 | 遵守Retry-After |
| 500/502/503 | 指数退避 |
| 400/422 | 标记数据失败并人工检查 |
| 401/403 | 立即停止，检查密钥，不连续重试 |

## 9.5 第一次端到端测试

只使用20条：

```text
抓取1页
→ SQLite出现20条pending
→ 同步客户端发送1批
→ 云端PostgreSQL出现20条
→ SQLite变为20条synced
→ 再次同步不新增记录
```

核对：

```sql
select count(*) from app.notices;

select canonical_url, count(*)
from app.notices
group by canonical_url
having count(*) > 1;
```

## 9.6 完成标准

- [ ] 20条数据走完整链路成功。
- [ ] 相同批次重试不会重复。
- [ ] 相同URL跨批次出现不会重复。
- [ ] 断网时数据留在SQLite。
- [ ] 恢复网络后能继续同步。

---

# 第 10 步：完成1000条端到端试运行

## 10.1 为什么不是直接10万条

1000条足以暴露：

- 日期格式差异。
- 地区无法映射。
- 标题截断。
- URL规范化错误。
- 页面重复。
- SQLite同步状态错误。
- API批次幂等错误。

修正1000条比清洗10万条便宜得多。

## 10.2 执行方式

按每页约20条计算，先抓50页：

```bash
python -m ccgp_crawler crawl \
  --category zygg \
  --max-pages 50 \
  --delay-min 2 \
  --delay-max 5 \
  --resume
```

然后启动同步：

```bash
python -m ccgp_crawler sync --batch-size 200
```

## 10.3 必须检查的数据指标

本地：

```text
抓取页面数
解析记录数
新URL数
重复URL数
日期解析失败数
地区映射失败数
pending/synced/failed数量
```

云端：

```sql
select count(*) from app.notices;

select min(published_at), max(published_at)
from app.notices;

select region_raw, count(*)
from app.notices
group by region_raw
order by count(*) desc;

select count(*)
from app.notices
where region_code is null;
```

## 10.4 人工抽样

随机抽100条：

- URL能否打开。
- 标题是否与列表页一致。
- 发布时间是否一致。
- 地区是否一致。
- 是否存在导航链接或新闻链接混入。

建议验收阈值：

```text
URL有效率 ≥ 99%
标题准确率 ≥ 99%
日期解析成功率 ≥ 99%
重复URL = 0
软封禁误入库 = 0
```

## 10.5 完成标准

- [ ] 约1000条数据已写入PostgreSQL。
- [ ] 本地和云端数量能够解释。
- [ ] 抽样准确率达到目标。
- [ ] 所有解析异常已有处理规则。
- [ ] 已执行一次数据库备份。
- [ ] 没有未解释的同步失败。

只有这一步完成，才允许开始10万条历史初始化。

---

# 第 11 步：开发并验证用户查询 API

## 11.1 为什么在10万条前先做查询API

先用1000条验证最终使用方式，可以发现索引、字段和分页设计是否满足产品需求。否则10万条导入完成后才发现查询不符合预期，又要改结构。

## 11.2 接口设计

```http
GET /api/v1/notices
  ?region_code=420000
  &start_date=2026-09-01
  &end_date=2026-09-30
  &keyword=医疗设备
  &limit=20
  &cursor=<游标>
```

## 11.3 查询规则

- 地区、开始日期、结束日期、关键词均可选。
- `limit`默认20，最大100。
- 日期使用左闭右开范围。
- SQL只拼接实际存在的条件。
- 所有用户参数通过数据库驱动绑定。
- 不把用户输入直接拼进SQL字符串。
- 使用`(published_at, id)`游标分页。
- 不使用深度OFFSET。

第一页：

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

下一页：

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

## 11.4 权限和连接

- 查询API只使用`api_reader`。
- 使用5～10个连接的连接池。
- 增加请求限流。
- 响应只返回必要字段。
- 错误信息不暴露SQL和数据库地址。

## 11.5 完成标准

- [ ] 地区、日期、关键词可以组合查询。
- [ ] 游标分页前后页无重复、无漏项。
- [ ] 查询API不能写数据库。
- [ ] 查询速度符合产品要求。
- [ ] URL、标题、日期和地区返回格式稳定。

---

# 第 12 步：分批执行10万条历史初始化

## 12.1 不要一次连续跑完5000页

按每页20条计算，10万条约5000页。建议拆成：

```text
每批100～300页
每批约2000～6000条
每批结束后暂停检查
```

## 12.2 每个批次的固定流程

### 批次开始前

- 检查云端PostgreSQL健康。
- 检查数据库备份是否正常。
- 检查本地SQLite剩余磁盘。
- 检查`pending/failed`积压。
- 记录分类和起止页码。

### 批次执行中

- 并发保持1。
- 页间等待2～5秒随机抖动。
- 每页完成后提交SQLite事务和checkpoint。
- 软封禁立即停止。
- 同步客户端可以独立运行，但不要无限堆积。

### 批次结束后

- 记录抓取页数和新URL数。
- 确认SQLite的pending最终降下来。
- 核对云端新增数量。
- 查询日期范围和地区分布。
- 抽样20～50条。
- 记录下一批起始页码。

## 12.3 批次记录模板

```text
批次编号：
分类：zygg / dfgg
起始页：
结束页：
开始时间：
结束时间：
请求成功页：
请求失败页：
解析总数：
新增URL：
重复URL：
日期失败：
地区未映射：
软封禁：
同步成功：
同步失败：
下一批起始页：
备注：
```

## 12.4 数据量核对

```sql
select count(*) from app.notices;

select count(distinct canonical_url) from app.notices;

select min(published_at), max(published_at)
from app.notices;

select category, count(*)
from app.notices
group by category;
```

`count(*)`应等于`count(distinct canonical_url)`。

## 12.5 大量导入后

```sql
analyze app.notices;
```

然后对真实查询执行：

```sql
explain (analyze, buffers)
select ...;
```

不要只凭感觉增加索引，先看真实查询计划。

## 12.6 完成标准

- [ ] 云端约有10万条唯一公告。
- [ ] 没有软封禁页面进入数据库。
- [ ] 本地同步积压已经清理。
- [ ] 数据总量、日期范围和分类分布合理。
- [ ] 抽样质量通过。
- [ ] 完成最终备份并验证文件非空。
- [ ] PostgreSQL统计信息已更新。

---

# 第 13 步：历史初始化后切换为增量采集

## 13.1 增量任务不要沿用历史翻页方式

历史初始化完成后，每次从最新页开始：

```text
从第0页开始
→ 写入新公告
→ 遇到已存在URL
→ 连续3页全部无新URL
→ 停止当前轮次
```

不要每天重新遍历5000页。

## 13.2 调度频率

初期建议：

```text
每30～60分钟运行一次
每次最多检查10～30页
同一时刻只允许一个分类任务运行
```

如果公告更新频率不高，可以延长到每2小时。

## 13.3 防止任务重叠

可以使用：

- 本地文件锁。
- SQLite锁表。
- 操作系统单实例服务。

如果上一轮仍在运行，新一轮应跳过，不要并发翻相同页面。

## 13.4 完成标准

- [ ] 增量任务只访问最新若干页。
- [ ] 连续旧页会停止。
- [ ] 同一轮不会并发运行两份。
- [ ] 新公告在目标时间内进入PostgreSQL。
- [ ] 失败后可以从本地SQLite继续同步。

---

# 第 14 步：上线后的日常监控

## 14.1 采集监控

每天至少看：

```text
成功页面数
失败页面数
软封禁次数
新增URL数量
解析失败数量
地区未映射数量
本地pending/failed数量
最近成功采集时间
```

## 14.2 云端监控

```text
PostgreSQL容器状态
数据库磁盘使用率
连接数
API错误率
查询P95响应时间
最近备份时间
备份文件大小
```

## 14.3 数据质量监控

```sql
-- 最近一天新增数
select count(*)
from app.notices
where first_seen_at >= now() - interval '1 day';

-- 空地区比例
select
  count(*) filter (where region_code is null) as unmapped,
  count(*) as total
from app.notices;

-- 最近公告时间
select max(published_at), max(last_seen_at)
from app.notices;
```

## 14.4 备份监控

- 每天确认备份生成。
- 每月恢复一次测试库。
- 至少一份备份离开当前服务器。
- 升级PostgreSQL前手工备份并做恢复确认。

## 14.5 完成标准

- [ ] 采集、同步、API和数据库都有日志。
- [ ] 关键异常有告警。
- [ ] 每日备份可用。
- [ ] 每月有恢复演练记录。

---

# 什么时候可以开始正式爬取

下面全部通过后，才开始10万条任务：

- [ ] 数据合同已固定。
- [ ] PostgreSQL已部署。
- [ ] 表、索引和角色已创建。
- [ ] URL唯一约束已验证。
- [ ] 数据库备份已成功恢复一次。
- [ ] 本地SQLite缓冲已完成。
- [ ] 单页和三页解析测试通过。
- [ ] 爬虫支持checkpoint和resume。
- [ ] 软封禁检测有效。
- [ ] 写入API已通过鉴权和幂等测试。
- [ ] 20条端到端同步成功。
- [ ] 1000条试运行质量通过。
- [ ] 查询API能够满足地区、时间、关键词查询。

只要其中任一项没有完成，就不要启动全量5000页任务。

---

# 推荐开发日程

以下是单人开发的参考节奏，不是硬性工期：

| 时间 | 主要工作 | 当天验收结果 |
|---|---|---|
| 第1天 | 数据合同、PostgreSQL、表结构 | 样本数据可查询 |
| 第2天 | SQLite、离线解析器、单页测试 | 三页解析正确 |
| 第3天 | 分页、断点、软封禁、日志 | 可安全抓50页 |
| 第4天 | 写入API、同步客户端 | 20条端到端成功 |
| 第5天 | 1000条试运行和修正 | 质量指标通过 |
| 第6天 | 查询API和游标分页 | 用户查询正常 |
| 后续 | 分批历史初始化 | 约10万条完成 |

如果第5天的1000条测试暴露问题，应先修复并重新试运行，不要为了赶进度直接开始全量任务。

---

# 最终执行结论

你的情况是“服务器已经存在，但爬虫和数据库尚未正式上线”，最合适的顺序是：

```text
先定义数据格式
→ 先在服务器部署PostgreSQL
→ 用手工样本验证数据库
→ 再在本地开发爬虫
→ 先爬1页、3页、50页
→ 开发写入API
→ 打通SQLite到PostgreSQL
→ 试运行1000条
→ 验证查询API
→ 分批爬取10万条
→ 切换增量更新
```

最重要的两个控制点：

1. **数据库先于全量爬取完成设计和验证。**
2. **1000条端到端试运行先于10万条历史初始化。**

这样能够把结构设计、解析、同步和查询问题提前暴露在小数据量阶段，避免10万条完成后再整体返工。
