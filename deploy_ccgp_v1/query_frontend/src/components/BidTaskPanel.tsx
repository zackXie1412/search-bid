import { useEffect, useRef, useState } from 'react'
import { ArrowUpRight, CheckCircle2, Info, Square, RotateCcw } from 'lucide-react'
import { actOnBidTask, getBidTask, listBidTasks, type BidTask } from '../bidApi'
import { regions } from '../data'

const statusNames: Record<string, string> = { draft: '待确认', queued: '排队中', preparing: '整理公司资料', running: '正在检标', stopping: '正在停止', stopped: '已停止', completed: '已完成', failed: '任务异常' }
const activeStatuses = new Set(['queued', 'preparing', 'running', 'stopping'])
const phaseNames: Record<string, string> = { documents: '解析上传资料', company_profile: '调用模型整理公司资料', announcement_read: '读取公告网页', qualification_check: '调用模型判断资格要求', qualification_validate: '核对条款覆盖与筛除证据' }

export function BidTaskPanel({ selectedId, onSelect, onActive, onManageKnowledge }: { selectedId: string; onSelect: (id: string) => void; onActive: (active: boolean) => void; onManageKnowledge?: () => void }) {
  const [tasks, setTasks] = useState<BidTask[]>([])
  const [task, setTask] = useState<BidTask | null>(null)
  const [page, setPage] = useState(1)
  const [error, setError] = useState('')
  const [busy, setBusy] = useState(false)
  const [refresh, setRefresh] = useState(0)
  const actionRequest = useRef<AbortController | null>(null)
  const generation = useRef(0)

  useEffect(() => {
    const controller = new AbortController()
    listBidTasks(controller.signal).then((data) => {
      if (controller.signal.aborted || !Array.isArray(data.items)) return
      setTasks(data.items)
      if (!selectedId && data.items[0]?.id) onSelect(data.items[0].id)
    }).catch((caught) => { if (!controller.signal.aborted) setError(caught instanceof Error ? caught.message : '读取检标任务失败') })
    return () => controller.abort()
  }, [selectedId, onSelect, refresh])

  useEffect(() => { setPage(1); setTask(null); onActive(false) }, [selectedId, onActive])
  useEffect(() => {
    if (!selectedId) return
    const controller = new AbortController()
    const id = ++generation.current
    let timer: ReturnType<typeof setTimeout>
    async function poll() {
      try {
        const latest = await getBidTask(selectedId, page, controller.signal)
        if (controller.signal.aborted || id !== generation.current) return
        setTask(latest)
        setError('')
        onActive(activeStatuses.has(latest.status))
        if (activeStatuses.has(latest.status)) timer = setTimeout(() => void poll(), 2000)
      } catch (caught) {
        if (controller.signal.aborted || id !== generation.current) return
        setError(caught instanceof Error ? caught.message : '读取检标任务失败')
        timer = setTimeout(() => void poll(), 4000)
      }
    }
    void poll()
    return () => { controller.abort(); clearTimeout(timer) }
  }, [selectedId, page, refresh, onActive])
  useEffect(() => () => actionRequest.current?.abort(), [])

  async function action(value: 'start' | 'stop' | 'retry') {
    if (!task || busy) return
    const taskId = task.id
    const id = generation.current
    const controller = new AbortController()
    actionRequest.current = controller
    setBusy(true)
    try {
      const updated = await actOnBidTask(taskId, value, controller.signal)
      if (controller.signal.aborted || id !== generation.current) return
      setTask(updated)
      setRefresh((n) => n + 1)
    } catch (caught) { if (!controller.signal.aborted && id === generation.current) setError(caught instanceof Error ? caught.message : '操作失败') }
    finally { if (!controller.signal.aborted) setBusy(false) }
  }

  return <div className="bid-task-panel">
    <div className="bid-task-heading"><h3>建议关注的标讯</h3>{tasks.length ? <label>检标任务<select aria-label="检标任务" value={selectedId} disabled={busy} onChange={(event) => onSelect(event.target.value)}>{tasks.map((item) => <option key={item.id} value={item.id}>{new Date(item.created_at).toLocaleString('zh-CN', { timeZone: 'Asia/Shanghai' })} · {item.total} 条 · {statusNames[item.status] || item.status}</option>)}</select></label> : null}</div>
    {error ? <p className="bid-error" role="alert">{error}</p> : null}
    {!task ? <p className="bid-empty">完成筛选并提交检标后，这里会列出未发现明确硬性冲突的项目</p> : <>
      <div className="bid-progress" role="status"><strong>{statusNames[task.status] || task.status} · 已处理 {task.completed} / {task.total}</strong><progress max={task.total || 1} value={task.completed} /><span>保留 {task.retained} · 其中待核实 {task.needs_review} · 明确筛除 {task.excluded} · 未完成判断 {task.failed}</span>{task.current_title ? <small>当前：{task.current_title}</small> : null}</div>
      <p className="bid-snapshot">本次条件：关键词 {task.filters.keyword || '全部'} · 地区 {regions.find(([, code]) => code === task.filters.region_code)?.[0] || '全部'} · 日期 {task.filters.start_date || '不限'} 至 {task.filters.end_date || '不限'} · 模型 {task.model_config.model}</p>
      {task.screening_rules?.skill_id ? <p className="bid-snapshot">资格初筛规则：{task.screening_rules.skill_name} · 版本 {task.screening_rules.skill_sha256.slice(0,8)} · 判断协议 {task.screening_rules.protocol_version} · 公告正文初筛</p> : task.legacy_screening ? <p className="bid-result-note">旧版流程：未执行新版资格覆盖检查。继续与重试沿用原规则；需要新版判断时请新建检标任务。</p> : null}
      {task.error ? <p className="bid-error" role="alert">{task.error}</p> : null}
      {task.diagnostic?.phase ? <p className="bid-warning" role="status"><Info size={14} />{phaseNames[task.diagnostic.phase] || '处理检标任务'}{task.diagnostic.attempt ? ` · 第 ${task.diagnostic.attempt} / ${task.diagnostic.max_attempts || 3} 次尝试` : ''}{task.diagnostic.status_code ? ` · HTTP ${task.diagnostic.status_code}` : ''}{task.diagnostic.elapsed_ms !== undefined ? ` · 累计 ${(task.diagnostic.elapsed_ms / 1000).toFixed(1)} 秒` : ''}{task.diagnostic.retrying ? ` · 临时故障，等待 ${task.diagnostic.retry_in_seconds} 秒后自动重试` : ''}</p> : null}
      {task.status === 'failed' && !task.has_profile ? <p className="bid-warning">{task.can_retry_preparation ? '公司资料整理未完成，尚未开始读取公告。资料已保存在本机，可直接重试或取消并清除资料。' : '公司资料整理未完成，尚未开始读取公告。原始资料已清理，请重新上传并提交检标。'}</p> : null}
      {task.failures.map((failure) => <p className="bid-warning" key={failure.message}><Info size={14} />{failure.message}（{failure.count} 条）</p>)}
      <div className="bid-task-actions">
        {activeStatuses.has(task.status) ? <button type="button" disabled={busy || task.status === 'stopping'} onClick={() => void action('stop')}><Square size={13} />停止检标</button> : null}
        {task.status === 'stopped' && task.has_profile && task.completed < task.total ? <button type="button" disabled={busy} onClick={() => void action('start')}>继续未完成项</button> : null}
        {task.failed > 0 && task.has_profile && !activeStatuses.has(task.status) ? <button type="button" disabled={busy} onClick={() => void action('retry')}><RotateCcw size={13} />重试失败项（会再次调用模型）</button> : null}
        {task.can_retry_preparation && task.status === 'failed' ? <><button type="button" disabled={busy} onClick={() => void action('retry')}><RotateCcw size={13} />重试公司资料整理（会再次调用模型）</button><button type="button" disabled={busy} onClick={() => void action('stop')}>取消任务并清除上传资料</button></> : null}
        {task.status === 'draft' ? <><span>此任务尚未开始，请重新上传资料并确认后创建新任务</span><button type="button" disabled={busy} onClick={() => void action('stop')}>取消草稿并清除上传资料</button></> : null}
      </div>
      <p className="bid-result-note">保留代表未发现有证据的硬性冲突；待核实项需进一步确认，不代表已满足全部资格要求。</p>
      {task.profile_warnings?.length ? <details className="bid-profile-warnings"><summary><Info size={14} />企业资料核对提示（{task.profile_warnings.length} 类）</summary><p>这些提示来自本次任务使用的企业档案，集中核对即可，不表示与每个项目资格冲突。</p><ul>{task.profile_warnings.map(text=><li key={text}>{text}</li>)}</ul>{onManageKnowledge?<button type="button" onClick={onManageKnowledge}>管理企业资料</button>:null}</details> : null}
      {task.has_legacy_decisions ? <p className="bid-result-note">历史任务已按当前规则整理展示，原始判断及资料快照保留；待核实标签和统计按当前规则计算。</p> : null}
      {task.items.length === 0 ? <p className="bid-empty">{activeStatuses.has(task.status) ? '正在逐条处理，保留结果会陆续显示' : task.failed === task.total ? '公告或模型未能完成判断，请查看上方原因并重试' : '当前没有已保留的标讯'}</p> : <ul className="bid-results">{task.items.map(({ notice, decision }) => <li key={notice.id}>
        <div className="bid-result-title"><a href={notice.canonical_url} target="_blank" rel="noreferrer">{notice.title}<ArrowUpRight size={14} /></a><span>{decision.verdict === 'needs_review' ? '待核实' : '建议关注'}</span></div>
        <small>{notice.region_raw || '未知'} · {new Date(notice.published_at).toLocaleString('zh-CN', { timeZone: 'Asia/Shanghai', hour12: false })} 北京时间</small><p>{decision.summary}</p>
        {decision.pending_checks.length ? <div className="bid-pending"><Info size={14} /><div><strong>本项目待核实事项</strong><ul>{decision.pending_checks.map((text, i) => <li key={i}>{text}</li>)}</ul></div></div> : null}
        <details><summary><CheckCircle2 size={14} />查看资格条件与判断依据</summary>
          {decision.reading ? <div className="bid-evidence"><strong>公告阅读范围：正文；附件未读取</strong><p>读取于 {new Date(decision.reading.read_at).toLocaleString('zh-CN', { timeZone: 'Asia/Shanghai', hour12: false })} 北京时间</p>{decision.reading.attachments.length ? <ul>{decision.reading.attachments.map((attachment, index) => <li key={index}>{attachment.url && /^https?:\/\//i.test(attachment.url) ? <a href={attachment.url} target="_blank" rel="noreferrer">{attachment.name}</a> : <span>{attachment.name}（无可用链接）</span>} · 附件未读取</li>)}</ul> : <p>未识别到可用附件入口；仅以正文已读取内容判断。</p>}</div> : null}
          {decision.coverage ? <div className="bid-evidence"><strong>已识别条款核对 {decision.coverage.checked} / {decision.coverage.identified}</strong><p>{decision.coverage.scope}；阅读缺口表示内容未知，不表示企业缺少资格。</p>{decision.coverage_gaps?.length ? <ul>{decision.coverage_gaps.map((gap,index)=><li key={index}>{gap.clause_id}：{gap.text}</li>)}</ul> : null}</div> : null}
          {decision.groups?.map(group=><div className="bid-evidence" key={group.id}><strong>{group.operator==='or'?'任一满足':group.operator==='and'?'同时满足':'关系待核对'} · {group.id}</strong><p>{group.context}</p><p>成员：{group.members.join('、')} · {group.assessment==='met'?'该条件组有资料支持':group.assessment==='conflict'?'明确冲突已验证':'待核实'}{!group.validated?' · 关系未完成校验':''}</p></div>)}
          {decision.requirements.map((requirement, i) => <div className="bid-evidence" key={i}><strong>{requirement.condition_id ? `${requirement.condition_id} · ` : ''}{requirement.category==='scoring'?'评分项 · ':''}{requirement.requirement} · {requirement.reference_only ? '参考说明' : requirement.not_applicable ? '不适用' : requirement.assessment === 'met' ? '资料支持' : requirement.assessment === 'conflict' ? '存在差异' : requirement.review_required === false ? '未作资格确认' : '待核实'}</strong><p>公告：{requirement.announcement_quote}</p><p>公司资料：{requirement.company_quote || '未找到明确证明'}</p>{requirement.evidence?.length ? <ul>{requirement.evidence.map((evidence,index)=><li key={index}>{evidence.filename || evidence.fact_id} · {evidence.location || evidence.source_id}：{evidence.quote}</li>)}</ul> : null}<p>{requirement.reason}</p>{requirement.evidence_validated !== undefined ? <p>引用校验：{requirement.evidence_validated?'对应出处已匹配':'未取得可用的对应证明'}{requirement.conflict_validated?' · 明确否定性证据已通过校验':''}</p> : null}</div>)}</details>
      </li>)}</ul>}
      <div className="bid-pages"><button type="button" disabled={page === 1 || busy} onClick={() => setPage((n) => n - 1)}>上一页</button><span>第 {page} 页 · 共 {Math.ceil(task.retained / 20)} 页</span><button type="button" disabled={!task.has_next || busy} onClick={() => setPage((n) => n + 1)}>下一页</button></div>
    </>}
  </div>
}
