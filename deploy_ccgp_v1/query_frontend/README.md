# 灵犀找标查询前端

查询本机已爬取的公开招标公告。选择地区或发布时间即刷新结果；关键词通过搜索按钮或回车提交。无需 API Key，不使用模拟数据。

## 运行

先启动采集服务（已有 LaunchAgent 时无需重复启动）：

```bash
cd ../../ccgp-crawler
.venv/bin/ccgp-console
```

再启动查询前端：

```bash
cd ../deploy_ccgp_v1/query_frontend
npm install
npm run dev
```

打开 http://127.0.0.1:5174/。Vite 将 `/api` 代理到本机 `127.0.0.1:8765`，接口直接只读查询采集服务配置的 SQLite，默认 `ccgp-crawler/runtime/ccgp.sqlite3`。修改后端代码后需要重启采集服务。

## 查询

`GET /api/v1/notices` 支持 `region_code`、`start_date`、`end_date`、`keyword`、`limit`、`cursor`。默认每页 6 条，上限 100 条；日期包含开始日和结束日，按北京时间解释；空格分隔的关键词必须同时出现在标题中。

返回 `{ items, next_cursor, limit, total_count }`，单条包含 `id`、`title`、`canonical_url`、`published_at`、`region_code`、`region_raw`。`total_count` 是当前筛选条件下的全部匹配记录数，不受分页游标影响；页面标题下显示结果总数，翻页按钮旁显示总页数（按每页 6 条向上取整）。结果按发布时间和本地 rowid 倒序排列。筛选变化后从第一页重新查询；翻页继续使用已提交的筛选条件和游标。并发筛选取消旧请求，过期响应不会覆盖新结果。

今天、昨天按北京时间整天查询。“近一周”“近一个月”“近三个月”“近六个月”“近一年”分别查询今天之前的 7、30、90、180、365 个完整自然日，不包含今天，也不按当月或自然月计算：开始日为今天减 N 天，结束日为昨天。例如北京时间 2026-10-08 的“近一个月”为 2026-09-08 至 2026-10-07（含两端）。预设和自定义日期互斥。空结果只代表本地没有匹配记录，不会触发重新采集。

目前采集范围仅为公开招标公告，未采集采购单位，页面不展示采购单位。地区为空时显示“未知”。此接口只用于本机测试；云端查询 API 的认证保持原有设计。

## 模型配置与连接测试

点击页面右上角「模型配置」，填写服务商的 Base URL、模型 ID、API Key 和调用协议。默认官方地址 `https://api.openai.com/v1` 与 Responses；兼容平台可选择 Chat Completions。填写 API 根地址，不要添加 `/responses` 或 `/chat/completions`。保存不发起模型调用；测试使用当前表单，仅发送固定短文本，可能产生少量 API 费用，不发送公司资料或公告。成功表示选定模型返回了有效文本。

「DeepSeek」预设自动填入官方地址 `https://api.deepseek.com`、模型 `deepseek-flash`、Chat Completions，清空尚未保存的密钥输入。更换服务商需填入对应的 API Key 后测试和保存。DeepSeek 的检标调用使用 JSON Output、`max_tokens`，关闭默认 thinking；结果仍按本地 schema 和证据校验。Flash 支持文字和图片资料，`deepseek-v4-pro` 仅用于文字资料。也可手工填写兼容服务商提供的 DeepSeek 模型 ID（以 `deepseek-` 开头会应用该协议适配）。

API Key 仅由本机后端保存和使用，读取配置只返回是否已配置。留空保留已有密钥；「清除密钥」需要点击保存才会持久生效。更换 Base URL 时需输入新密钥或明确清除原密钥。关闭弹窗清除页面中的密钥输入；配置保存在 `ccgp-crawler/runtime/ai_config.json`，权限 0600，采用文件锁和原子替换。该文件包含本机明文密钥，请勿复制或提交到版本库。

接口为 `GET /api/ai/config`、`PUT /api/ai/config`、`POST /api/ai/test`。后两者接受 `{ base_url, model, protocol, api_key?, clear_api_key? }`；协议为 `responses` 或 `chat_completions`。连接测试限时 30 秒，不重试，返回 `{ ok, model, protocol, elapsed_ms, message }`。参数无效返回 422，配置存储异常返回 503，上游连接失败返回 `ok: false`。

后续指令型 Skill 放入 `ccgp-crawler/skills/bid_check/<skill-id>/SKILL.md`，加载接口与中文模板见该目录 README。当前只提供配置、连接测试和 Skill 扩展入口。

## 验证

页面底部「AI 检标」接受公司资料并检查当前最近一次成功查询的全部结果。先显示费用与资料发送目标的确认面板，确认后后台任务处理全部匹配公告，不受分页限制。只有有公告和公司资料证据支持的强制资格冲突才筛除；未提及和缺证明保留并标为待核实。查询未完成、结果为空或关键词未提交时不可检标。

“建议关注的标讯”展示保留结果、匹配依据和待核实事项；明确筛除的项目不列出。任务选择器支持刷新恢复历史任务，处理期间显示进度，可停止后继续，失败条目单独重试。原始资料整理成功后清理，本机保留资料事实、来源文本和结果以供恢复及重试；公司资料整理失败会保留本机上传文件，支持直接重试或取消清除；旧版本已清理的任务需要重新上传。页面展示失败阶段、HTTP 状态码、次数、耗时及自动重试状态。临时故障最多尝试 3 次，持续模型故障暂停批量任务。

后端接口：创建任务使用 multipart POST /api/bid-check/tasks，字段 region_code、start_date、end_date、keyword、files、expected_config（确认的 base_url/model/protocol）。创建返回待确认任务；POST /{id}/start 开始；GET /{id}?page=1&page_size=20 返回进度和分页的保留结果；POST /{id}/stop 停止；POST /{id}/retry 重试失败项；GET /api/bid-check/tasks 读取最近任务。全部接口复用 8765 和现有 Vite 代理。检标不加载 Skill，后续入口保留。

```bash
npm test
npm run build
```

后端相关测试：在 `ccgp-crawler` 下运行 `.venv/bin/python -m pytest -q tests/test_notice_query.py tests/test_api.py`。测试使用临时数据库并关闭后台采集。
