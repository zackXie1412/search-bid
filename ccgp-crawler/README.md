# CCGP 公开招标采集控制台

本项目提供仅监听 `127.0.0.1` 的可视化采集控制台。数据源固定为中国政府采购网搜索页 `search.ccgp.gov.cn/bxsearch`，查询参数固定 `bidType=1`，只保存页面类型文字严格等于“公开招标公告”的结果。

每条公告只保存以下字段：

| 字段 | 说明 |
| --- | --- |
| `canonical_url` | 规范化后的详情 URL，也是唯一键 |
| `title` | 公告标题 |
| `published_at` | 带 `+08:00` 的发布时间 |
| `region_raw` | 搜索结果页显示的地区原文 |
| `region_code` | 省级六位行政区划代码，无法映射时为 `null` |
| `category` | 根据详情 URL 中的 `/zygg/` 或 `/dfgg/` 得到 |
| `source_list_url` | 含切片日期与页码的完整搜索 URL |

不打开详情页，不提取采购人、预算、来源编号等字段。原静态分类页 CLI 仍保留为备用通道，但控制台不调用它。

## 本机安装

```bash
cd /Users/zackxie/Lingxi/数据库设计/找标/ccgp-crawler
python3 -m venv .venv
.venv/bin/python -m pip install -e .
cd frontend
npm install
npm run build
cd ..
```

运行真实采集控制台：

```bash
.venv/bin/ccgp-console
```

打开 [http://127.0.0.1:8765](http://127.0.0.1:8765)。首次启动会创建 `runtime/ccgp.sqlite3` 和 `runtime/chrome-profile`。采集时会打开可见的持久化 Chrome；不要同时手动使用这个专用窗口访问 CCGP。

用于离线 UI 验证的假采集器：

```bash
CCGP_FAKE_CRAWLER=1 .venv/bin/ccgp-console
```

## 任务行为

- 日期按北京时间解释，历史任务按天切片。
- 每天从 `page_index=1` 递增，所有任务共享持久化站点节流：页面之间随机等待 15～25 秒，每 20 次实际导航额外休息 3～5 分钟。
- 不同任务之间随机等待 15～20 分钟；同一任务手动暂停后从断点继续不等待该任务间隔，但仍遵守页面间隔、批次休息、站点冷却和每日上限。节流状态写入 SQLite，服务重启不能绕过等待。
- 北京时间每天最多启动 240 次导航请求；达到上限时允许当前任务完成，但不再启动后续任务。
- 站点冷却期间不会发出请求，冷却结束后需要用户确认继续；等待均可被“停止”操作中断。
- 同时只运行一个任务；新任务进入队列。
- 点击停止后，当前页会先原子提交，再进入已停止状态。
- 继续任务从 SQLite 中记录的“日期 + 下一页”恢复。
- `canonical_url` 唯一去重，重复抓取会更新已有记录。
- `page.goto` 等待首响超过 60 秒时自动重试 2 次，分别退避 10 秒和 30 秒；重试耗尽后才停止并保留断点。
- HTTP 异常、重复页、异常空页、日期错配和频繁访问提示不自动重试，会立即停止并保留断点。
- 页面使用 SSE 更新；断线后自动重连，并通过任务详情接口恢复状态。

## 每日增量

自动增量固定在北京时间 `02:00`，每次抓取今天和昨天。错过执行时间后，服务启动会依据最近成功日期补排缺失任务；调度日期有唯一键，重启不会重复创建同一个自动任务。

要让网页关闭后仍持续运行，安装用户级 LaunchAgent：

```bash
./scripts/install-launch-agent.sh
```

卸载时配置会移入废纸篓：

```bash
./scripts/uninstall-launch-agent.sh
```

日志位于 `runtime/logs/`。安装脚本只监听 `127.0.0.1:8765`，用户登录后自动启动。

## 本地标讯查询

`GET /api/v1/notices` 无需 API Key，直接只读查询本机 SQLite 中已采集的公开招标公告。支持省级 `region_code`、包含两端整天的北京时间 `start_date` / `end_date`、空格分隔且同时匹配标题的 `keyword`，以及 `limit` / `cursor` 分页；默认每页 6 条，最多 100 条。

响应为 `{ items, next_cursor, limit }`，单条字段为 `id`（本地 rowid）、`title`、`canonical_url`、`published_at`、`region_code`、`region_raw`。按发布时间和 rowid 倒序排列，游标只能继续相同筛选条件。参数错误返回 422，无效游标返回 400，数据库不可用返回 503。接口包含中央、地方以及所有同步状态的记录，不启动采集、不改变同步状态。

查询页面位于 `../deploy_ccgp_v1/query_frontend`，运行 `npm run dev` 后访问 http://127.0.0.1:5174/。修改本服务代码后需要重启服务，页面使用 Vite 代理连接 8765 端口。

## 检标模型配置

查询页面右上角的「模型配置」支持官方 OpenAI 和兼容平台。填写 Base URL、服务商支持的模型 ID、API Key，并选择 Responses（默认）或 Chat Completions。保存不会调用模型；「测试连接」使用当前表单发送固定短文本，30 秒总超时，无自动重试，不包含公司资料或标讯。

- `GET /api/ai/config`：返回 `{ base_url, model, protocol, api_key_configured }`，不返回密钥。
- `PUT /api/ai/config`：接受 `{ base_url, model, protocol, api_key?, clear_api_key? }`，保存后返回公开配置。密钥省略、空值或空字符串均保留原密钥；明确清除使用 `clear_api_key: true`。
- `POST /api/ai/test`：接受同一配置表单，测试但不保存；返回 `{ ok, model, protocol, elapsed_ms, message }`。换地址时不得直接复用原密钥，必须提供新密钥或明确清除。参数错误 422；本机配置存储故障 503；上游连接失败返回 `ok: false`。

配置位于 `runtime/ai_config.json`（包含本机明文密钥，权限 0600，文件锁与原子替换），不进入版本库、公开响应或日志。后续检标可复用 `app.state.ai_config_store` 和 `app.state.ai_client`。指令型 Skill 入口与中文模板见 [skills/bid_check/README.md](skills/bid_check/README.md)，当前注册表为空。

## 测试

```bash
.venv/bin/python -m pytest -q
cd frontend && npm test && npm run build
```

后端测试使用离线 fixture；前端完整交互可通过假采集器验证。真实站点烟测应限制为 1～2 页，避免高频访问。

## 同步到云端

工作台右侧的“云端同步”面板会显示本地 SQLite 的 `pending/syncing/synced/failed` 数量、SSH 隧道状态和批次进度。点击“上传待同步数据”后，后台以 200 条为一批上传所有 `pending` 数据。

普通上传不会选取 `synced` 或 `failed` 记录。数据校验失败的记录保留为 `failed`，只有点击“重试失败数据”才会重新进入队列。云端同时通过 `canonical_url` 唯一约束和批次 payload hash 防止重复数据。

### 1. 配置 SSH 公钥

写入 API 只监听云服务器本机端口，不对公网开放。先确保以公钥无交互登录：

```bash
ssh -i ~/.ssh/id_ed25519 -o BatchMode=yes ubuntu@175.178.172.158 true
```

如果私钥路径不同，安装隧道时用 `--identity` 指定。不要把 SSH 密码放入脚本、plist 或命令参数。
公钥登录验证成功后，应轮换曾在聊天或其他明文渠道中出现过的 SSH 密码。

### 2. 安装常驻隧道

```bash
./scripts/install-ingest-tunnel.sh
curl -fsS http://127.0.0.1:18765/healthz
```

安装脚本会先检查私钥、公钥登录和本地 `18765` 端口，然后安装 `com.lingxi.ccgp-ingest-tunnel` LaunchAgent。隧道固定为 `127.0.0.1:18765 -> 175.178.172.158:18080`，断线后由 launchd 重启。卸载：

```bash
./scripts/uninstall-ingest-tunnel.sh
```

### 3. 配置 API Key

将云端 ingest API Key 写入未跟踪文件，不要在此处生成新 key：本地值必须与云端 `secrets/ingest_api_key` 一致。

```bash
mkdir -p runtime
read -s "INGEST_KEY?请输入现有 ingest API Key: "
printf '%s' "$INGEST_KEY" > runtime/ingest_api_key
unset INGEST_KEY
chmod 600 runtime/ingest_api_key
```

`runtime/ingest_api_key` 已被 `.gitignore` 忽略。密钥只由本地 FastAPI 读取，不会返回给前端或写入日志。

### 4. 启动工作台并上传

LaunchAgent 模板已注入：

```text
CCGP_INGEST_URL=http://127.0.0.1:18765
CCGP_INGEST_API_KEY_FILE=<project>/runtime/ingest_api_key
```

重新安装工作台后台服务，再打开页面：

```bash
./scripts/install-launch-agent.sh
open http://127.0.0.1:8765
```

当面板显示“已连接”时，点击“上传待同步数据”。上传中每 1 秒刷新，结束后恢复每 5 秒刷新。断开隧道不会删除 SQLite 数据。

CLI 仍可用于受控的小批量验证：

```bash
.venv/bin/python -m ccgp_crawler sync \
  --db runtime/ccgp.sqlite3 --batch-size 20 --max-items 20
```

网络错误会把本批恢复为 `pending`；`401/403` 会立即停止；`422` 数据校验失败会标记为 `failed`，避免自动无限重试。

## 备用静态列表 CLI

旧通道仍可小范围运行：

```bash
.venv/bin/python -m ccgp_crawler crawl \
  --db runtime/crawler.sqlite3 \
  --category zygg \
  --max-pages 1
```

任务表迁移见 `migrations/sqlite_003_jobs.sql`。程序会自动迁移旧 SQLite，并保留已有公告与旧 checkpoint。

## AI 检标

完成地区、日期或关键词查询并上传公司资料后，点击“AI 检标”。系统会冻结本次筛选的全部公开招标公告（包括未翻到的页），显示模型调用次数和费用提示，确认后在后台逐条读取公告原文并分析。只有公告明确要求的强制条件与公司资料明确证据发生冲突时才筛除；资料未提及、缺证明或需要现场核验的项目保留，并显示“待核实”。公告访问受限、模型异常和输出无效会进入失败数，不会被当成不符合。

任务保存在 `runtime/bid_check/tasks.sqlite3`，刷新或关闭页面后可在“建议关注的标讯”任务选择器恢复。可以停止后继续未完成项，或重试失败项；失败重试会再次调用模型。原始公司文件仅在任务临时目录中保存，权限为 0600，资料整理成功后清理。模型配置沿用 `/api/ai/config`，公告检标接口为 `/api/bid-check/tasks`、`/start`、`/stop`、`/retry` 和任务详情接口。

两种协议共用本机资料读取：TXT/PDF/DOCX/XLSX 提取文字，图片、扫描页和文档内的视觉资料通过模型图像输入识别；DOC/XLS 由本机 LibreOffice 转换。图表或矢量图片需转换成 PDF 页面图像，未计算的 Excel 公式需在表格软件中计算保存。资料超出单次处理范围时明确拒绝，不截断或忽略页面。当前公告原文必须能从中国政府采购网读取；403、验证码或正文缺失会明确提示并支持重试。

模型故障处理：检标中的 HTTP 408/500/502/503/504、连接异常和超时最多尝试 3 次，间隔 3 秒、8 秒。认证、额度及参数错误不自动重试，连接测试仍只请求一次。持续服务故障暂停批量检标，可稍后重试，不重做成功公告。资料整理失败时保留本机上传文件，支持“重试公司资料整理”；成功或取消时清除原文件。旧任务已清理的文件无法恢复，需要重新上传。页面显示处理阶段、HTTP 状态、次数和累计耗时；日志仅记录任务 ID、阶段和安全诊断，不含密钥、资料或上游原始内容。

DeepSeek 可在页面模型配置中使用预设：Base URL `https://api.deepseek.com`、模型 `deepseek-flash`、Chat Completions。适配使用 DeepSeek 的 JSON Output（`response_format: {"type":"json_object"}`）和 `max_tokens`，并关闭默认 thinking 以降低资料整理等待时间；模型输出仍由本地 schema 校验。参考 DeepSeek 官方文档：<https://api-docs.deepseek.com/>、<https://api-docs.deepseek.com/guides/json_mode>。

## 企业材料与能力汇总

企业知识库位于 `runtime/knowledge`，原件、逐批提取、人工审核与版本快照保留。

- 在“企业知识库”勾选“确认为本企业历史资料”，仅把该文件中的供应商自身材料用于本企业初筛；厂家与客户材料仍独立记录。
- 新拆解使用 `skills/knowledge_extract/company-materials/SKILL.md`，快照记录指令内容和 SHA-256 版本，继续任务沿用原指令。拆解完成后自动汇总成功部分；手动停止、认证或额度故障不触发自动汇总。
- 旧文件点击“整理已有结果”，使用成功批次与已保存正文/OCR文字，不重新读图、不重新OCR。输入、归属、覆盖与指令版本相同则复用已有汇总，不重复调用模型；失败任务可继续或重试。
- 默认查看“材料清单／能力摘要”。目录与实际正文关联，同类证件保留独立编号及多个出处；声明函材料与能力声明分别记录。局部未发现不是企业全局缺失。旧 `third_party` 标记须结合原文恢复，无法确定则保留 unknown。
- 汇总先作为候选，逐条修改、剔除或确认全部后进入新知识库版本。原始逐批审核入口保留。汇总采用后替代对应零散来源，未替换的已确认资料继续保留；人工修改及剔除记录不会被自动覆盖。
- 汇总任务有独立进度、停止、继续和重试，已完成分组保留；每组正文不超过 19,000 字符（为请求元数据预留空间），分组失败隔离，成功部分可审核，未完成分组单独重试。停止会取消当前模型请求；服务商已接收的调用可能继续处理。
- 知识库版本及检标任务快照包含 `material_support` 和 `coverage`：总批次、成功/失败/未处理批次、位置及OCR缺口。未处理内容视为未知，不推断材料缺失。已确认的实际材料/声明支持一般资格初筛，不因历史日期、OCR或编号不全单独待核实；公告额外等级、范围、金额、人数仍核对。
- 新版确认仅影响未来检标，历史知识库版本及已创建检标任务保持原快照。

扩展接口：`PUT /api/knowledge/documents/{id}/ownership`、`POST /api/knowledge/documents/{id}/summarize`、`GET /api/knowledge/summaries`、`POST /api/knowledge/summaries/{id}/action`、`POST /api/knowledge/summaries/review`。开始/重试需要提交当前 `expected_config`；summary action 支持 start/stop/retry。公开响应只包含模型名称与指令版本，不包含密钥。

相关回归：`tests/test_knowledge_summary.py`、`tests/test_knowledge.py`、前端 `KnowledgePage.test.tsx`。自动测试使用临时知识库和模拟模型；现有真实文件需用户点击整理，自动验收不会上传企业资料或调用付费模型。


知识库审核展示：默认原始审核队列排除有效已确认汇总采用的来源及目录/空白模板。可切换“全部原始记录与线索”追溯原文、关联材料和原始批次观察。确认/撤回、来源修改与剔除会重新计算关联，原始审核历史保持不变。新旧参考线索都不能单独确认入库，批量混合请求整批返回 422。当前版本仍含旧参考线索时启动会幂等生成排除线索的新版本，历史版本和任务快照保持不变。详情见 [知识库说明](docs/knowledge.md#审核展示与原始记录追溯)。

### 资格初筛协议 2

新检标任务默认加载 `skills/bid_check/qualification-screening/SKILL.md`，保存完整基础指令、Skill 指令、SHA-256 和协议版本。缺失或空 Skill 阻止创建。`GET /api/bid-check/rules` 仅返回规则元数据，任务列表不返回完整指令或公告正文。任务停止、继续、重试使用创建时规则。

公告成功读取后持久保存正文、段落、条款、逻辑组、读取时间、内容指纹和附件入口。模型失败重试复用已成功读取的正文；不下载附件或调用 OCR。正文和网页维持 120,000 字符及 5 MB 上限，超限失败而非截断。条款索引是保守规则定位：无法可靠解释的关系或例外保留待核实，不等同于完整法律审查。

每条公告一次结构化模型请求（临时故障可重试）。后端独立核对叶子条款引用、条件组成员、企业多出处引用和相关性。明确否定性筛除必须通过对象、当前时间、主体、来源及知识库保护标记检查。缺证照、数量等级差异、历史过期和 OCR 不清不能自动筛除；OR 任一分支有支持即可，所有分支有独立明确冲突才筛除。例外无法可靠验证时保留为待核实。普通附件仅展示未读取状态，明确引用附件的资格要求才产生来源缺口。

数据库采用增量可空字段 `tasks.rules_snapshot`、`tasks.evidence_index` 和 `items.notice_snapshot`。旧任务继续原协议并显示“未执行新版资格覆盖检查”，不补造公告覆盖结论，不改写历史知识库或任务。新版功能可用 `tests/test_bid_screening.py` 的模拟模型和临时数据库验证；自动验收不提交企业图片、不调用真实模型或 OCR。
