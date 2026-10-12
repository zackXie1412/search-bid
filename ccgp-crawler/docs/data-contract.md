# CCGP 列表采集数据合同（当前版）

本合同只覆盖列表采集层。只保留列表页标记为公开招标或邀请招标的公告；每条公告输出以下 7 个业务字段：

| 字段 | 类型 | 约束 |
| --- | --- | --- |
| `canonical_url` | string | 公告详情页绝对 URL；去掉 fragment；`www.ccgp.gov.cn` 使用 HTTPS；非空 |
| `title` | string | 优先使用链接 `title` 属性，否则使用链接文本；保留来源文字；非空 |
| `published_at` | string | 列表行发布时间；ISO 8601；带 `+08:00` 时区；无法解析的行不入库 |
| `region_raw` | string/null | 列表行“地域/地区”原文；缺失时 `null` |
| `region_code` | string/null | 可识别的省级地区映射成与 `app.regions` 一致的六位代码；未知或跨地区时 `null` |
| `category` | string | 列表入口分类，如 `zygg`、`dfgg` |
| `source_list_url` | string | 当前公告所在列表页的绝对 URL |

公告类型只用于列表行过滤，不输出/保存 `notice_type`；标签缺失时仅在路径属于 `gkzb`/`yqzb` 且标题以“公开招标公告”/“邀请招标公告”结尾时保留。非招标公告不会入库。以下内容不在当前采集范围：详情页、附件、采购人、代理机构、预算和来源编号。

SQLite 的 `sync_status`、重试次数、checkpoint、页运行记录和域名冷却状态只是本地运行控制数据，不属于公告业务 payload。后续云端 API 应复用本合同，不要求爬虫直接连接 PostgreSQL。
