import { act, render, screen, waitFor } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { afterEach, describe, expect, it, vi } from 'vitest'
import { BidTaskPanel } from './BidTaskPanel'
import { AiBidCheck } from './AiBidCheck'

const filters = { keyword: '设备', regionCode: '370000', timeRange: '自定义' as const, startDate: '2026-10-07', endDate: '2026-10-07' }
const task = {
  id: 'task1', status: 'completed', total: 8, completed: 8, retained: 1, excluded: 6, needs_review: 1, failed: 1,
  filters: { keyword: '设备', region_code: '370000', start_date: '2026-10-07', end_date: '2026-10-07' },
  model_config: { base_url: 'https://model.example/v1', model: 'gpt-test', protocol: 'responses' },
  created_at: '2026-10-09T00:00:00Z', file_names: ['company.txt'], error: '', current_title: '', has_profile: true, page: 1, has_next: false,
  failures: [{ message: '公告访问受限（403）', count: 1 }],
  items: [{ notice: { id: 1, title: '建议关注的真实公告', canonical_url: 'https://www.ccgp.gov.cn/cggg/dfgg/gkzb/test.htm', region_raw: '山东', published_at: '2026-10-07T10:00:00+08:00' }, decision: { verdict: 'needs_review', summary: '未发现明确冲突，税收证明需核实', pending_checks: ['补充税收证明'], requirements: [{ requirement: '税收证明', mandatory: true, announcement_quote: '需税收证明', assessment: 'unknown', company_quote: '', company_fact_ids: [], reason: '资料未提及' }] } }],
}
const response = (data: unknown, status = 200) => new Response(JSON.stringify(data), { status })
afterEach(() => vi.unstubAllGlobals())

describe('bid checking flow', () => {
  it('shows retained and review items with failure counts and evidence', async () => {
    vi.stubGlobal('fetch', vi.fn().mockImplementation(async (url: string) => response(url.includes('?limit') ? { items: [task] } : task)))
    render(<BidTaskPanel selectedId="task1" onSelect={vi.fn()} onActive={vi.fn()} />)
    expect(await screen.findByRole('link', { name: '建议关注的真实公告' })).toHaveAttribute('href', task.items[0].notice.canonical_url)
    expect(screen.getByText('补充税收证明')).toBeInTheDocument()
    expect(screen.getByRole('status')).toHaveTextContent('明确筛除 6')
    expect(screen.getByText(/公告访问受限/)).toBeInTheDocument()
    const user = userEvent.setup()
    await user.click(screen.getByText('查看资格条件与判断依据'))
    expect(screen.getByText('公司资料：未找到明确证明')).toBeVisible()
    await user.click(screen.getByRole('button', { name: /重试失败项/ }))
  })

  it('selects the most recent persisted task after refresh', async () => {
    vi.stubGlobal('fetch', vi.fn().mockImplementation(async () => response({ items: [task] })))
    const select = vi.fn()
    render(<BidTaskPanel selectedId="" onSelect={select} onActive={vi.fn()} />)
    await waitFor(() => expect(select).toHaveBeenCalledWith('task1'))
  })

  it('shows company warnings once separately from each project and opens knowledge management', async () => {
    const warning='部分档案主体为“其他公司”，请核对材料归属。主体关系不明不等于资格冲突。'
    const legacy={...task,has_legacy_decisions:true,profile_warnings:[warning],items:[task.items[0],{notice:{...task.items[0].notice,id:2,title:'第二个公告'},decision:{...task.items[0].decision,verdict:'retain',summary:'未发现明确冲突',pending_checks:[]}}]}
    vi.stubGlobal('fetch',vi.fn(async(url:string)=>response(url.includes('?limit')?{items:[legacy]}:legacy)))
    const manage=vi.fn();const user=userEvent.setup()
    render(<BidTaskPanel selectedId="task1" onSelect={vi.fn()} onActive={vi.fn()} onManageKnowledge={manage}/>)
    await screen.findByRole('link',{name:'第二个公告'})
    expect(screen.getAllByText('本项目待核实事项')).toHaveLength(1)
    expect(screen.getByText(/历史任务已按当前规则整理展示/)).toBeInTheDocument()
    expect(screen.getByText(warning)).not.toBeVisible()
    await user.click(screen.getByText('企业资料核对提示（1 类）'))
    expect(screen.getAllByText(warning)).toHaveLength(1)
    expect(screen.getByText(warning)).toBeVisible()
    await user.click(screen.getByRole('button',{name:'管理企业资料'}));expect(manage).toHaveBeenCalledOnce()
  })

  it('shows preparation diagnostics and retries saved company materials', async () => {
    const failed = { ...task, status: 'failed', has_profile: false, can_retry_preparation: true, completed: 0, retained: 0, failed: 0, items: [], failures: [], error: '服务商网关超时（HTTP 504）；已尝试 3 次', diagnostic: { phase: 'company_profile', attempt: 3, max_attempts: 3, status_code: 504, elapsed_ms: 183000 } }
    const fetch = vi.fn().mockImplementation(async (url: string) => response(url.includes('?limit') ? { items: [failed] } : url.endsWith('/retry') ? { ...failed, status: 'queued', error: '', diagnostic: {} } : failed))
    vi.stubGlobal('fetch', fetch)
    const user = userEvent.setup()
    render(<BidTaskPanel selectedId="task1" onSelect={vi.fn()} onActive={vi.fn()} />)
    expect(await screen.findByText(/资料已保存在本机/)).toBeInTheDocument()
    expect(screen.getByText(/调用模型整理公司资料/)).toHaveTextContent('第 3 / 3 次尝试 · HTTP 504 · 累计 183.0 秒')
    await user.click(screen.getByRole('button', { name: /重试公司资料整理/ }))
    expect(fetch).toHaveBeenCalledWith('/api/bid-check/tasks/task1/retry', expect.objectContaining({ method: 'POST' }))
  })

  it('explains that older preparation failures need reupload and shows automatic retry state', async () => {
    const failed = { ...task, status: 'failed', has_profile: false, can_retry_preparation: false, failed: 0, items: [], diagnostic: { phase: 'company_profile', attempt: 2, max_attempts: 3, retrying: true, retry_in_seconds: 8 } }
    vi.stubGlobal('fetch', vi.fn().mockImplementation(async (url: string) => response(url.includes('?limit') ? { items: [failed] } : failed)))
    render(<BidTaskPanel selectedId="task1" onSelect={vi.fn()} onActive={vi.fn()} />)
    expect(await screen.findByText(/原始资料已清理/)).toBeInTheDocument()
    expect(screen.getByText(/等待 8 秒后自动重试/)).toBeInTheDocument()
    expect(screen.queryByRole('button', { name: /重试公司资料整理/ })).not.toBeInTheDocument()
  })

  it('ignores a late response from an old task and aborts its poll', async () => {
    let finish!: (value: Response) => void
    let oldSignal!: AbortSignal
    vi.stubGlobal('fetch', vi.fn().mockImplementation((url: string, options: RequestInit) => {
      if (url.includes('?limit')) return Promise.resolve(response({ items: [] }))
      if (url.includes('/old?')) { oldSignal = options.signal!; return new Promise((resolve) => { finish = resolve }) }
      return Promise.resolve(response({ ...task, id: 'new' }))
    }))
    const select = vi.fn(); const active = vi.fn()
    const { rerender } = render(<BidTaskPanel selectedId="old" onSelect={select} onActive={active} />)
    rerender(<BidTaskPanel selectedId="new" onSelect={select} onActive={active} />)
    await screen.findByRole('link', { name: '建议关注的真实公告' })
    expect(oldSignal.aborted).toBe(true)
    await act(async () => finish(response({ ...task, items: [{ ...task.items[0], notice: { ...task.items[0].notice, title: '旧结果不得覆盖' } }] })))
    expect(screen.queryByText('旧结果不得覆盖')).not.toBeInTheDocument()
  })

  it('confirms all eight results before multipart upload and start', async () => {
    const calls: { url: string; options: RequestInit }[] = []
    vi.stubGlobal('fetch', vi.fn().mockImplementation(async (url: string, options: RequestInit) => {
      calls.push({ url, options })
      if (url === '/api/bid-check/rules') return response({skill_id:'qualification-screening',skill_name:'资格初筛',skill_sha256:'a'.repeat(64),protocol_version:2})
      if (url === '/api/ai/config') return response({ ...task.model_config, api_key_configured: true })
      if (url.includes('?limit')) return response({ items: [] })
      if (url === '/api/bid-check/tasks' && options.method === 'POST') return response({ id: 'task1', total: 8, estimated_calls: 9 })
      return response(task)
    }))
    const user = userEvent.setup()
    render(<AiBidCheck filters={filters} totalCount={8} queryReady />)
    await user.upload(screen.getByLabelText('上传公司信息'), new File(['公司资料'], 'company.txt', { type: 'text/plain' }))
    await user.click(screen.getByRole('button', { name: 'AI 检标' }))
    expect(await screen.findByRole('region', { name: '确认检标范围' })).toHaveTextContent('预计调用约 9 次')
    expect(calls.some((call) => call.options.method === 'POST')).toBe(false)
    await user.click(screen.getByRole('button', { name: '确认并开始检标' }))
    await screen.findByRole('link', { name: '建议关注的真实公告' })
    const upload = calls.find((call) => call.url === '/api/bid-check/tasks' && call.options.method === 'POST')!
    const body = upload.options.body as FormData
    expect(body.get('keyword')).toBe('设备')
    expect(body.get('start_date')).toBe('2026-10-07')
    expect(body.getAll('files')).toHaveLength(1)
    expect(calls.some((call) => call.url.endsWith('/task1/start'))).toBe(true)
  })

  it('does not submit while search is incomplete or keyword is unsubmitted', async () => {
    vi.stubGlobal('fetch', vi.fn().mockImplementation(async () => response({ items: [] })))
    const user = userEvent.setup()
    const { rerender } = render(<AiBidCheck filters={filters} totalCount={8} queryReady keywordDirty />)
    await user.upload(screen.getByLabelText('上传公司信息'), new File(['test'], 'company.txt'))
    expect(screen.getByRole('button', { name: 'AI 检标' })).toBeDisabled()
    expect(screen.getByText('关键词尚未提交，请先搜索')).toBeInTheDocument()
    rerender(<AiBidCheck filters={filters} totalCount={0} queryReady={false} />)
    expect(screen.getByRole('button', { name: 'AI 检标' })).toBeDisabled()
  })
})

it('shows identity-only history as retained without pending UI and preserves unrelated review issues',async()=>{
  const identity={...task.items[0],decision:{...task.items[0].decision,verdict:'retain',summary:'未发现有证据的硬性资格冲突，建议关注',pending_checks:[],requirements:[{...task.items[0].decision.requirements[0],reason:'本项未提供可展示的独立资格判断依据',review_required:false}]}}
  const clean={...task,needs_review:0,has_legacy_decisions:true,profile_warnings:[],items:[identity]}
  vi.stubGlobal('fetch',vi.fn(async(url:string)=>response(url.includes('?limit')?{items:[clean]}:clean)))
  render(<BidTaskPanel selectedId="task1" onSelect={vi.fn()} onActive={vi.fn()}/>)
  await screen.findByRole('link',{name:'建议关注的真实公告'})
  expect(screen.queryByText('本项目待核实事项')).not.toBeInTheDocument()
  expect(screen.getByRole('status')).toHaveTextContent('其中待核实 0')
  expect(screen.getByText('建议关注')).toBeInTheDocument()
  await userEvent.setup().click(screen.getByText('查看资格条件与判断依据'))
  expect(screen.getByText(/未作资格确认/)).toBeVisible()
  expect(screen.queryByText(/主体名称不一致/)).not.toBeInTheDocument()
})

it('shows frozen screening rules, alternative groups, source gaps and separate evidence citations', async () => {
  const current = {...task,legacy_screening:false,screening_rules:{skill_id:'qualification-screening',skill_name:'资格初筛',skill_sha256:'a'.repeat(64),protocol_version:2},items:[{...task.items[0],decision:{...task.items[0].decision,protocol_version:2,reading:{scope:'announcement_body_only',read_at:'2026-10-12T02:00:00Z',sha256:'b'.repeat(64),attachments:[{name:'采购文件',url:'https://www.ccgp.gov.cn/files/example.pdf',read:false},{name:'无链接附件',url:null,read:false}]},coverage:{identified:3,checked:2,scope:'仅核对已识别条款，非完整法律审查'},coverage_gaps:[{clause_id:'C3',text:'资格来源缺口：相关附件未读取'}],groups:[{id:'G1',operator:'or',members:['C1.1','C1.2'],context:'许可证 A 或 B',assessment:'met',validated:true}],requirements:[{...task.items[0].decision.requirements[0],condition_id:'C3',evidence_validated:true,evidence:[{fact_id:'K1',source_id:'K1:S2',quote:'食品经营许可证',filename:'历史投标.docx',location:'表格 2'}]}]}}]}
  vi.stubGlobal('fetch',vi.fn(async(url:string)=>response(url.includes('?limit')?{items:[current]}:current)))
  render(<BidTaskPanel selectedId="task1" onSelect={vi.fn()} onActive={vi.fn()}/>)
  expect(await screen.findByText(/资格初筛规则：资格初筛/)).toHaveTextContent('判断协议 2')
  await userEvent.setup().click(screen.getByText('查看资格条件与判断依据'))
  expect(screen.getByText('已识别条款核对 2 / 3')).toBeVisible()
  expect(screen.getByText('任一满足 · G1')).toBeVisible()
  expect(screen.getByRole('link',{name:'采购文件'})).toHaveAttribute('href','https://www.ccgp.gov.cn/files/example.pdf')
  expect(screen.getByText('无链接附件（无可用链接）')).toBeVisible()
  expect(screen.getByText('历史投标.docx · 表格 2：食品经营许可证')).toBeVisible()
  expect(screen.queryByText(/未执行新版资格覆盖检查/)).not.toBeInTheDocument()
})

it('explains old task continuation without inventing coverage',async()=>{
  const old={...task,legacy_screening:true}
  vi.stubGlobal('fetch',vi.fn(async(url:string)=>response(url.includes('?limit')?{items:[old]}:old)))
  render(<BidTaskPanel selectedId="task1" onSelect={vi.fn()} onActive={vi.fn()}/>)
  expect(await screen.findByText(/未执行新版资格覆盖检查/)).toHaveTextContent('继续与重试沿用原规则')
  expect(screen.queryByText(/已识别条款核对/)).not.toBeInTheDocument()
})

it('labels supplementary references and inapplicable branches inside the existing detail layout', async()=>{
  const current={...task,items:[{...task.items[0],decision:{...task.items[0].decision,summary:'未发现明确硬性资格冲突，建议关注；有 1 项资格需核实。',pending_checks:['食品经营许可证或食品生产许可证：对应证明材料尚需核对'],requirements:[{...task.items[0].decision.requirements[0],requirement:'分支机构授权',not_applicable:true,review_required:false},{...task.items[0].decision.requirements[0],requirement:'附件模板说明',reference_only:true,review_required:false}]}}]}
  vi.stubGlobal('fetch',vi.fn(async(url:string)=>response(url.includes('?limit')?{items:[current]}:current)))
  const {container}=render(<BidTaskPanel selectedId="task1" onSelect={vi.fn()} onActive={vi.fn()}/>)
  await screen.findByRole('link',{name:'建议关注的真实公告'})
  expect(container.querySelectorAll('.bid-pending li')).toHaveLength(1)
  expect(screen.queryByText(/分支机构授权/)).not.toBeVisible()
  await userEvent.setup().click(screen.getByText('查看资格条件与判断依据'))
  expect(screen.getByText('分支机构授权 · 不适用')).toBeVisible()
  expect(screen.getByText('附件模板说明 · 参考说明')).toBeVisible()
  expect(container.querySelectorAll('.bid-evidence')).toHaveLength(2)
})
