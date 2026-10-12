# 中国政府采购网静态分类列表采集方案

> 适用目标：只采集公告列表中的 **详情 URL、发布时间、公告标题**，不打开详情页，不下载附件，不解析预算、采购人、代理机构等正文信息。
>
> 推荐路线：优先使用 `www.ccgp.gov.cn` 的静态分类列表页，通过低频 HTTP 请求和确定性 HTML 解析完成采集；只有在必须使用搜索关键词、地区、时间等组合筛选时，才把持久化 Playwright 作为补充。

## 1. 结论摘要

当前已验证两种访问行为：

- `search.ccgp.gov.cn/bxsearch` 对普通 `curl/requests` 请求可能返回 HTTP 200，但正文是“频繁访问”软封禁页。
- 真实 Chrome + 持久化 Playwright 可以正常打开搜索结果，第 2 页也成功提取了 20 条公告链接。
- `www.ccgp.gov.cn/cggg/...` 的分类列表页是服务端生成的 HTML，列表项直接包含标题、相对链接和发布时间，适合用普通 HTTP 解析。

因此，批量采集不应该让 Jev 或 Playwright 逐条点击。推荐使用以下链路：

```text
www.ccgp.gov.cn 静态分类列表页
        ↓
低频 HTTP 获取 HTML
        ↓
解析 URL、日期、标题
        ↓
SQLite/PostgreSQL 去重和断点保存
        ↓
本地检索、导出和后续业务处理
```

如果每页约 20 条公告，10 万条约需要读取 5000 页。静态列表方案按保守的 1～3 秒/页（包含礼貌间隔和解析）估算，通常需要数小时到十小时；实际时间取决于网站响应、冷却和重复数据，不能把估算当作服务端承诺。

## 2. 范围与非目标

### 2.1 本方案包含

每条公告保存：

| 字段 | 说明 |
|---|---|
| `notice_url` | 公告详情页规范 URL，唯一键 |
| `title` | 公告标题，优先取链接的 `title` 属性 |
| `published_at` | 列表页展示的发布时间 |
| `category` | 列表入口，例如 `zygg`、`dfgg` |
| `list_page` | 采集来源列表页 |
| `source_id` | 从 URL 中提取的站点公告编号（如存在） |
| `first_seen_at` | 首次发现时间 |
| `last_seen_at` | 最近一次在列表中看到的时间 |
| `raw_row_text` | 可选，保留列表行原文，便于排错 |

### 2.2 本方案不包含

- 不打开公告详情页。
- 不下载附件。
- 不解析预算金额、项目编号、采购人、代理机构等详情字段。
- 不使用代理池、频繁更换 IP、验证码破解或其他规避访问控制的方式。
- 不把自然语言 Agent 用作每一页的主循环。

## 3. 数据源与分页规则

### 3.1 推荐入口

已观察到的中央公告入口：

```text
https://www.ccgp.gov.cn/cggg/zygg/index.htm
https://www.ccgp.gov.cn/cggg/zygg/index_1.htm
https://www.ccgp.gov.cn/cggg/zygg/index_2.htm
```

地方公告入口通常为：

```text
https://www.ccgp.gov.cn/cggg/dfgg/index.htm
https://www.ccgp.gov.cn/cggg/dfgg/index_1.htm
```

分页规律是：

| 逻辑页码 | URL 文件名 |
|---:|---|
| 0 | `index.htm` |
| 1 | `index_1.htm` |
| 2 | `index_2.htm` |
| N | `index_N.htm` |

不同分类可能存在自己的分页边界，不能假设所有分类页数相同。程序应以“页面没有有效公告项”“连续页面全部是已入库旧数据”或“请求返回 404/空列表”为停止条件。

### 3.2 列表项结构

典型列表项类似：

```html
<li>
  <a href="./gkzb/202609/t20260918_27356711.htm"
     title="长江水利委员会机关服务中心（本级）...公开招标公告">
    长江水利委员会机关服务中心（本级）...公开招标公告
  </a>
  <em rel="bxlx">公开招标</em>
  发布时间：<em>2026-09-18 17:21</em>
  地域：<em>湖北</em>
  采购人：<em>长江水利委员会机关服务中心（本级）</em>
</li>
```

对于当前目标，只需要读取：

1. `a[href]` 的 `href`，并使用列表页 URL 解析为绝对 URL。
2. `a[title]` 的 `title`，没有时再使用链接文本。
3. 列表行文本中的 `发布时间：...`。

## 4. 推荐技术栈

### 4.1 最小版本

- Python 3.11 或更高版本。
- `httpx`：HTTP 请求、超时和连接复用。
- `beautifulsoup4` + `lxml`：HTML 解析。
- SQLite：单机、可断点、足够支撑 10 万至数百万条列表记录。

安装：

```bash
python3 -m venv .venv
source .venv/bin/activate
python -m pip install httpx beautifulsoup4 lxml
```

### 4.2 何时使用 Playwright

仅在以下情况下使用持久化 Playwright：

- 必须调用 `search.ccgp.gov.cn` 的关键词或复杂筛选。
- 服务器对搜索页要求浏览器会话，普通 HTTP 无法获取结果。
- 页面改版导致静态入口暂时不可用，需要人工确认页面结构。

即使使用 Playwright，也应该批量读取当前页的所有链接，而不是让 Agent 逐条点击。

## 5. 数据库设计

### 5.1 SQLite 表结构

```sql
CREATE TABLE IF NOT EXISTS notices (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    source_id TEXT,
    notice_url TEXT NOT NULL,
    title TEXT NOT NULL,
    published_at TEXT,
    category TEXT NOT NULL,
    list_page TEXT NOT NULL,
    raw_row_text TEXT,
    first_seen_at TEXT NOT NULL,
    last_seen_at TEXT NOT NULL,
    content_hash TEXT,
    UNIQUE(notice_url)
);

CREATE INDEX IF NOT EXISTS idx_notices_published_at
    ON notices(published_at);

CREATE INDEX IF NOT EXISTS idx_notices_source_id
    ON notices(source_id);

CREATE TABLE IF NOT EXISTS crawl_checkpoint (
    category TEXT PRIMARY KEY,
    next_page INTEGER NOT NULL DEFAULT 0,
    last_success_at TEXT,
    last_notice_url TEXT,
    status TEXT NOT NULL DEFAULT 'running',
    error_message TEXT
);

CREATE TABLE IF NOT EXISTS crawl_events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    category TEXT NOT NULL,
    page_no INTEGER,
    url TEXT,
    event_type TEXT NOT NULL,
    message TEXT,
    created_at TEXT NOT NULL
);
```

### 5.2 URL 规范化

列表中可能出现相对 URL、`http://` URL 或带锚点的 URL。建议：

- 使用 `urljoin(list_page, href)` 解析相对路径。
- 删除 URL fragment（`#...`）。
- 将 `http://www.ccgp.gov.cn/` 统一为 `https://www.ccgp.gov.cn/`，但保留 `raw_url` 便于审计时可选。
- 以规范化后的 `notice_url` 建唯一索引。

## 6. 软封禁与请求策略

### 6.1 软封禁识别

不能只看 HTTP 状态码，因为网站可能返回 HTTP 200 但正文是封禁页：

```python
BLOCK_MARKERS = (
    "您的访问过于频繁",
    "频繁访问",
    "请稍后再试",
    "事件ID",
)

def is_soft_block(status_code: int, body: str) -> bool:
    if status_code in {403, 429, 503}:
        return True
    sample = body[:20_000]
    return any(marker in sample for marker in BLOCK_MARKERS)
```

### 6.2 访问节奏

初始参数建议：

```text
并发数：1
连接超时：20 秒
读取超时：30 秒
成功请求间隔：2～5 秒随机抖动
普通网络错误：最多重试 3 次
软封禁：立即停止该域名任务
```

成功运行一段时间后，再根据实际响应逐步调整。不要一开始就开几十个并发。

### 6.3 冷却策略

建议按域名保存冷却状态：

```text
第一次软封禁：至少暂停 30 分钟
同一批次再次封禁：暂停 2 小时
连续出现：停止当天任务并报警
```

冷却期间禁止多个 worker 继续请求同一域名。冷却状态应该写入数据库或独立状态文件，而不是只保存在进程内存中。

## 7. 解析与采集代码骨架

下面的代码只处理列表页，不访问详情页。它包含 URL、标题、日期提取、软封禁识别和基础重试逻辑，适合作为第一个可运行版本的起点。

```python
from __future__ import annotations

import hashlib
import random
import re
import sqlite3
import time
from datetime import datetime, timezone
from urllib.parse import urldefrag, urljoin, urlsplit, urlunsplit

import httpx
from bs4 import BeautifulSoup


BASE = "https://www.ccgp.gov.cn"
BLOCK_MARKERS = ("您的访问过于频繁", "频繁访问", "请稍后再试", "事件ID")
TIME_RE = re.compile(
    r"发布时间\s*[:：]?\s*(\d{4}-\d{2}-\d{2}(?:\s+\d{2}:\d{2}(?::\d{2})?)?)"
)


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def list_url(category: str, page_no: int) -> str:
    filename = "index.htm" if page_no == 0 else f"index_{page_no}.htm"
    return f"{BASE}/cggg/{category}/{filename}"


def normalize_url(page_url: str, href: str) -> str:
    absolute = urljoin(page_url, href.strip())
    absolute, _fragment = urldefrag(absolute)
    parts = urlsplit(absolute)
    scheme = "https" if parts.netloc.lower() == "www.ccgp.gov.cn" else parts.scheme
    return urlunsplit((scheme, parts.netloc, parts.path, parts.query, ""))


def source_id_from_url(url: str) -> str | None:
    match = re.search(r"_([0-9]{6,})\.htm$", url)
    return match.group(1) if match else None


def parse_list_page(page_url: str, html: str) -> list[dict]:
    soup = BeautifulSoup(html, "lxml")
    rows = []

    # 当前页面使用 c_list_bid；保留 a[href] 兜底，避免小范围样式改版导致全空。
    candidates = soup.select("ul.c_list_bid li") or soup.select("li")
    seen = set()

    for li in candidates:
        link = li.select_one('a[href]')
        if not link:
            continue

        url = normalize_url(page_url, link.get("href", ""))
        if "/cggg/" not in url or not url.lower().endswith(".htm"):
            continue
        if url in seen:
            continue
        seen.add(url)

        title = (link.get("title") or link.get_text(" ", strip=True)).strip()
        row_text = li.get_text(" ", strip=True)
        time_match = TIME_RE.search(row_text)
        published_at = time_match.group(1) if time_match else None

        if not title:
            continue

        rows.append({
            "notice_url": url,
            "source_id": source_id_from_url(url),
            "title": title,
            "published_at": published_at,
            "list_page": page_url,
            "raw_row_text": row_text,
        })

    return rows


def fetch_page(client: httpx.Client, url: str) -> str:
    for attempt in range(3):
        try:
            response = client.get(url)
            body = response.text
        except httpx.HTTPError:
            if attempt == 2:
                raise
            time.sleep(2 ** attempt)
            continue

        if is_soft_block(response.status_code, body):
            raise RuntimeError(f"soft block detected: {url}")
        response.raise_for_status()
        return body

    raise RuntimeError(f"failed to fetch: {url}")


def is_soft_block(status_code: int, body: str) -> bool:
    return status_code in {403, 429, 503} or any(
        marker in body[:20_000] for marker in BLOCK_MARKERS
    )


def save_rows(db: sqlite3.Connection, rows: list[dict]) -> int:
    timestamp = now_iso()
    inserted = 0
    for row in rows:
        content_hash = hashlib.sha256(
            f"{row['title']}\n{row['published_at']}".encode("utf-8")
        ).hexdigest()
        cursor = db.execute(
            """
            INSERT INTO notices(
                source_id, notice_url, title, published_at, category,
                list_page, raw_row_text, first_seen_at, last_seen_at, content_hash
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(notice_url) DO UPDATE SET
                title=excluded.title,
                published_at=excluded.published_at,
                last_seen_at=excluded.last_seen_at,
                content_hash=excluded.content_hash
            """,
            (
                row["source_id"], row["notice_url"], row["title"],
                row["published_at"], row["category"], row["list_page"],
                row["raw_row_text"], timestamp, timestamp, content_hash,
            ),
        )
        inserted += cursor.rowcount > 0
    db.commit()
    return inserted


def crawl_category(db: sqlite3.Connection, category: str, start_page: int = 0):
    headers = {
        "User-Agent": "ccgp-list-research/1.0 (+contact your team)"
    }
    with httpx.Client(
        headers=headers,
        follow_redirects=True,
        timeout=httpx.Timeout(connect=20, read=30, write=20, pool=20),
    ) as client:
        page_no = start_page
        while True:
            page_url = list_url(category, page_no)
            html = fetch_page(client, page_url)
            rows = parse_list_page(page_url, html)

            if not rows:
                print(f"stop: no rows at {page_url}")
                break

            for row in rows:
                row["category"] = category
            save_rows(db, rows)

            db.execute(
                """
                INSERT INTO crawl_checkpoint(category, next_page, last_success_at,
                                             last_notice_url, status)
                VALUES (?, ?, ?, ?, 'running')
                ON CONFLICT(category) DO UPDATE SET
                    next_page=excluded.next_page,
                    last_success_at=excluded.last_success_at,
                    last_notice_url=excluded.last_notice_url,
                    status='running',
                    error_message=NULL
                """,
                (category, page_no + 1, now_iso(), rows[-1]["notice_url"]),
            )
            db.commit()

            print(f"{category} page={page_no} rows={len(rows)}")
            page_no += 1
            time.sleep(random.uniform(2.0, 5.0))


if __name__ == "__main__":
    # 实际项目中应先执行建表 SQL，再从 checkpoint 恢复 start_page。
    db = sqlite3.connect("ccgp_notices.sqlite3")
    crawl_category(db, "zygg", start_page=0)
    crawl_category(db, "dfgg", start_page=0)
```

> 注意：示例中的 `CREATE TABLE` 语句需要在第一次运行前执行。生产代码还应把 `soft block` 事件写入 `crawl_events`，并在捕获异常时更新 checkpoint 状态。

## 8. 断点续爬与增量策略

### 8.1 断点续爬

每成功完成一页，立即提交：

- 分类名。
- 下一页页码。
- 最后一条公告 URL。
- 成功时间。
- 页面行数。

进程中断后，从 `next_page` 继续，不要从第一页重新开始。

### 8.2 增量采集

长期运行时，不应该每天重新遍历全部历史页。推荐：

1. 管理员每次操作的时候从第一页开始。
2. 解析最新公告并写入数据库。
3. 连续若干页全部是已存在的 URL 后停止。
4. 对历史页面只在人工补数或数据校验时读取。

“连续旧页停止”需要根据业务设定，例如连续 3 页全部没有新 URL 就停止。第一次历史初始化可以关闭该规则，完整抓取到空页为止。

### 8.3 数据一致性

采集期间列表会持续更新，可能出现页码内容移动。为减少漏项：

- 初次初始化按分类分别采集。
- URL 做唯一去重，不依赖页码去重。
- 增量任务重复检查前几页。
- 最后用 URL 数量、发布时间范围和重复率做审计。

## 9. 100000 条记录的时间预算

假设每页 20 条，约需 5000 页：

| 单页总耗时 | 理论耗时 | 加入重试、冷却和重复页后的规划时间 |
|---:|---:|---:|
| 3 秒 | 4.2 小时 | 5～8 小时 |
| 6 秒 | 8.3 小时 | 10～16 小时 |
| 12 秒 | 16.7 小时 | 18～30 小时 |

以上是静态列表页方案。若改回搜索域名 + Playwright，并且每页都等待更长时间，通常按 1～2 天规划更稳妥。

由于只采集三列，详情页不需要访问，实际瓶颈是列表分页和服务端访问频率，而不是解析 CPU。

## 10. 测试计划

### 10.1 单元测试

- 相对 URL 能否正确转绝对 URL。
- `http://www.ccgp.gov.cn` 是否统一为 HTTPS。
- `title` 属性缺失时能否回退到链接文本。
- `发布时间：` 中有秒、无秒、只有日期时是否都能解析。
- 同一个 URL 在同页多次出现时是否只保留一条。
- 无效链接、站内导航链接是否被过滤。

### 10.2 集成测试

先只运行：

```text
zygg 第 0 页
zygg 第 1 页
dfgg 第 0 页
```

验证：

- 每页能够提取公告。
- 数据库唯一索引生效。
- 断点后从指定页继续。
- 人为中断后不会丢失已提交页面。
- 模拟软封禁页面时任务会暂停，而不是循环重试。

### 10.3 验收标准

- 95% 以上列表行能解析出非空标题和 URL。
- URL 唯一性达到 100%。
- 采集失败可以通过 checkpoint 恢复。
- 软封禁出现后 1 分钟内停止继续请求。
- 日志能够定位到分类、页码、URL 和错误原因。
- 抽样打开 100 条 URL，标题和日期与列表页一致。

## 11. 监控与日志

每页至少记录：

```text
category
page_no
page_url
http_status
elapsed_ms
row_count
new_count
duplicate_count
parse_error_count
blocked
```

建议输出 JSON Lines，便于后续导入日志系统：

```json
{
  "category": "zygg",
  "page_no": 12,
  "row_count": 20,
  "new_count": 18,
  "duplicate_count": 2,
  "blocked": false,
  "elapsed_ms": 1840
}
```

需要告警的情况：

- 连续 3 页为空。
- 连续 3 次请求失败。
- 软封禁标记出现。
- 单页解析数量从约 20 条突然下降到 0～2 条。
- 页面结构改变导致标题或发布时间缺失率超过阈值。

## 12. 上线步骤

### 第一步：小范围验证

只抓一个分类的前 3 页，检查数据库内容和页面抽样。

### 第二步：加入断点和软封禁保护

确认进程中断后可以继续，软封禁后不会继续重试。

### 第三步：历史初始化

按分类分批执行，每批限制页数，例如每次 100～300 页。批次之间观察响应和封禁情况。

### 第四步：切换为增量任务

初始化完成后，每 30～60 分钟抓取最新页，连续旧页停止。

### 第五步：本地检索

用户查询直接访问 SQLite/PostgreSQL，不再实时请求政府采购网搜索页面。

## 13. 风险与合规边界

- 网站返回 404 的 `robots.txt` 不代表可以无限制批量抓取。
- 应遵守网站公开规则、服务条款和单位内部合规要求。
- 访问频率受限时应暂停，不通过代理池或不断更换 IP 规避。
- 生产环境应保留来源 URL、采集时间和原始列表行，保证数据可追溯。
- 如果业务需要短时间获取大量历史数据，应优先咨询网站是否提供官方接口、批量数据服务或授权渠道。

## 14. 后续扩展

当三列采集稳定后，再按优先级扩展：

1. 增加全文检索和标题分词。
2. 增加公告类型、地域等列表中已有字段。
3. 对新增 URL 异步排队抓详情页。
4. 增加数据变更检测和每日校验。
5. 在页面结构明显变化时，让 Playwright/Jev 做一次人工辅助探查，但不要让 Agent 承担主采集循环。

## 15. 最终建议

对于“100000 条 URL + 日期 + 标题”的目标，优先实现静态分类列表采集器：

```text
HTTPX + BeautifulSoup/lxml
SQLite 唯一索引
单线程低频访问
checkpoint 断点续爬
软封禁识别和冷却
本地搜索
```

持久化 Playwright 只作为搜索页或异常页面的备用方案，Jev 不作为批量翻页主方案。这样能显著降低模型调用、浏览器开销和运行时间，也更容易验证数据完整性。
