import { render, screen, waitFor } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { afterEach, beforeEach, expect, it, vi } from 'vitest'
import { KnowledgePage } from './KnowledgePage'
import { AiBidCheck } from './AiBidCheck'

const config = {base_url:'https://model.example/v1',model:'test-model',protocol:'responses',api_key_configured:true}
const data = {kind:'fact', category:'资格声明',name:'民事责任能力',value:'能够独立承担民事责任',subject:'测试公司',occurrence:'actual',evidence_kind:'declaration',source_id:'S1',quote:'能够独立承担民事责任',applicable_date:'',industry:'',notes:'',needs_review:false}
const run = {id:'run1',generation:1,status:'awaiting_review',pages:10,total_batches:2,completed_batches:2,failed_batches:0,pending_entries:1,error:'',limitations:[],config,diagnostic:{}}
const document = {id:'doc1',name:'历史投标.pdf',size:1024,active:1,run,runs:[run]}
const extracted = {id:'entry1',run_id:'run1',document_id:'doc1',generation:1,run_status:'awaiting_review',filename:'历史投标.pdf',active:1,status:'pending',data,original:data,source:{location:'第3页',quote:data.quote,visual:false},updated_at:'2026-10-09'}
const company = {id:'default',name:'测试公司',document_count:1,revision:{id:'revision1',company_name:'测试公司',created_at:'2026-10-09',material_count:0,fact_count:1,records:[{data,conflict:false,sources:[{document_id:'doc1',filename:'历史投标.pdf',location:'第3页',quote:data.quote}]}]}}
let calls: {url:string;init?:RequestInit}[]
const response=(data:unknown,status=200)=>new Response(JSON.stringify(data),{status})
beforeEach(()=>{
  calls=[]
  vi.stubGlobal('fetch',vi.fn(async (url:string,init?:RequestInit)=>{
    calls.push({url,init})
    if(url==='/api/bid-check/rules')return response({skill_id:'qualification-screening',skill_name:'资格初筛',skill_sha256:'a'.repeat(64),protocol_version:2})
    if(url==='/api/ai/config')return response(config)
    if(url==='/api/knowledge/company')return response(company)
    if(url==='/api/knowledge/documents')return response({items:[document]})
    if(url.startsWith('/api/knowledge/entries'))return response({items:[extracted],total:1,page:1,has_next:false})
    if(url==='/api/knowledge/review')return response({revision_id:'revision2'})
    if(url==='/api/bid-check/tasks'&&init?.method==='POST')return response({id:'task1',total:8,estimated_calls:8})
    if(url==='/api/bid-check/tasks/task1/start')return response({id:'task1',status:'queued'})
    if(url.startsWith('/api/bid-check/tasks/task1?'))return response({id:'task1',filters:{keyword:'设备',region_code:'370000',start_date:'2026-10-07',end_date:'2026-10-07'},status:'completed',total:8,completed:8,retained:8,needs_review:0,excluded:0,failed:0,estimated_calls:8,has_profile:true,current_title:'',error:'',model_config:config,file_names:[],created_at:'2026-10-09',failures:[],items:[],page:1,has_next:false})
    return response({items:[]})
  }))
})
afterEach(()=>vi.unstubAllGlobals())

it('shows document evidence and edits with provenance retained, then supports batch confirmation',async()=>{
  const user=userEvent.setup();const back=vi.fn();render(<KnowledgePage onBack={back}/>)
  expect(await screen.findByText('民事责任能力')).toBeInTheDocument()
  expect(screen.getByText('历史投标.pdf')).toBeInTheDocument()
  await user.click(screen.getByText('查看出处与原始提取'))
  expect(screen.getByText(/第3页/)).toBeInTheDocument()
  await user.click(screen.getByRole('button',{name:'修改'}))
  await user.clear(screen.getByLabelText('提取内容'));await user.type(screen.getByLabelText('提取内容'),'人工确认后的能力')
  await user.click(screen.getByRole('button',{name:'保存并确认'}))
  await waitFor(()=>expect(calls.some(c=>c.url==='/api/knowledge/review')).toBe(true))
  const payload=JSON.parse(String(calls.find(c=>c.url==='/api/knowledge/review')?.init?.body))
  expect(payload.items[0].changes.value).toBe('人工确认后的能力')
  expect(payload.items[0].changes).not.toHaveProperty('quote')
  await user.click(screen.getByLabelText('选择 民事责任能力'));await user.click(screen.getByRole('button',{name:'确认所选'}))
  await user.click(screen.getByRole('button',{name:'返回找标'}));expect(back).toHaveBeenCalled()
})

it('blocks only an unsuccessful batch and shows upload guidance',async()=>{
  vi.stubGlobal('fetch',vi.fn(async(url:string)=>url==='/api/knowledge/company'?response({...company,name:'',revision:null}):url==='/api/ai/config'?response(config):url==='/api/knowledge/documents'?response({items:[]}):response({items:[{...extracted,run_status:'awaiting_review',batch_status:'failed',reviewable:false}],total:1,page:1,has_next:false})))
  render(<KnowledgePage onBack={()=>undefined}/>)
  await screen.findByText('民事责任能力')
  expect(screen.getByRole('button',{name:'上传文件'})).toBeDisabled()
  expect(screen.getByRole('button',{name:'确认入库'})).toBeDisabled()
  expect(screen.getByLabelText('选择 民事责任能力')).toBeDisabled()
  expect(screen.getByText('本条所在批次尚未成功完成，请先重试该批次。')).toBeInTheDocument()
})

it.each(['failed','running','stopped'])('allows reviewing a successful batch while the document is %s',async runStatus=>{
  const base=globalThis.fetch
  const incomplete={...run,status:runStatus,completed_batches:1,failed_batches:runStatus==='failed'?1:0,error:runStatus==='failed'?'部分批次未完成，请重试失败批次':''}
  const candidate={...extracted,run_status:runStatus,batch_status:'done',reviewable:true,data:{...data,needs_review:true,notes:'请核对原件'}}
  vi.stubGlobal('fetch',vi.fn(async(url:string,init?:RequestInit)=>url==='/api/knowledge/documents'?response({items:[{...document,run:incomplete,runs:[incomplete]}]}):url.startsWith('/api/knowledge/entries')?response({items:[candidate],total:1,page:1,has_next:false}):base(url,init)))
  const user=userEvent.setup();render(<KnowledgePage onBack={()=>undefined}/>)
  await screen.findByText('民事责任能力')
  expect(screen.getByRole('button',{name:'确认入库'})).toBeEnabled()
  expect(screen.getByText('需核实：请核对原件')).toBeInTheDocument()
  expect(screen.getByText('本条所在批次已完成，可先确认入库；文档其余批次仍待完成。')).toBeInTheDocument()
  await user.click(screen.getByRole('button',{name:'修改'}))
  expect(screen.getByLabelText('保留待核实标记')).toBeChecked()
  await user.click(screen.getByRole('button',{name:'保存并确认'}))
  await waitFor(()=>expect(calls.some(c=>c.url==='/api/knowledge/review')).toBe(true))
  expect(JSON.parse(String(calls.find(c=>c.url==='/api/knowledge/review')?.init?.body)).items[0].changes.needs_review).toBe(true)
  await user.click(screen.getByLabelText('选择 民事责任能力'))
  await user.click(screen.getByRole('button',{name:'确认所选'}))
  await waitFor(()=>expect(calls.filter(c=>c.url==='/api/knowledge/review')).toHaveLength(2))
  expect(screen.getByRole('button',{name:runStatus==='failed'?'重试未完成批次':runStatus==='stopped'?'继续拆解':'停止'})).toBeEnabled()
})

it('shows service errors and preserves editable data when a review fails',async()=>{
  const base=globalThis.fetch
  vi.stubGlobal('fetch',vi.fn(async(url:string,init?:RequestInit)=>url==='/api/knowledge/review'?response({detail:'存储暂时不可用'},503):base(url,init)))
  const user=userEvent.setup();render(<KnowledgePage onBack={()=>undefined}/>)
  await screen.findByText('民事责任能力');await user.click(screen.getByRole('button',{name:'确认入库'}))
  expect(await screen.findByRole('alert')).toHaveTextContent('存储暂时不可用')
})

it('creates a bid task from confirmed knowledge without sending files or N+1 preparation',async()=>{
  const user=userEvent.setup();render(<AiBidCheck filters={{keyword:'设备',regionCode:'370000',timeRange:'自定义',startDate:'2026-10-07',endDate:'2026-10-07'}} queryReady totalCount={8} onManageKnowledge={()=>undefined}/>)
  await screen.findByText('使用已确认企业知识库')
  expect(screen.queryByLabelText('上传公司信息')).not.toBeInTheDocument()
  await user.click(screen.getByRole('button',{name:'AI 检标'}))
  expect(await screen.findByRole('region',{name:'确认检标范围'})).toHaveTextContent('预计调用约 8 次')
  await user.click(screen.getByRole('button',{name:'确认并开始检标'}))
  await waitFor(()=>expect(calls.some(c=>c.url==='/api/bid-check/tasks'&&c.init?.method==='POST')).toBe(true))
  const form=calls.find(c=>c.url==='/api/bid-check/tasks'&&c.init?.method==='POST')!.init!.body as FormData
  expect(form.get('knowledge_revision_id')).toBe('revision1');expect(form.has('files')).toBe(false)
})

it('does not treat an empty revision as confirmed company information',async()=>{
  const base=globalThis.fetch
  vi.stubGlobal('fetch',vi.fn(async(url:string,init?:RequestInit)=>url==='/api/knowledge/company'?response({...company,revision:{...company.revision,fact_count:0,records:[]}}):base(url,init)))
  const user=userEvent.setup();const manage=vi.fn();render(<AiBidCheck queryReady totalCount={8} onManageKnowledge={manage}/>)
  await screen.findByText('还没有已确认资料，请先上传、拆解并确认企业资料。')
  expect(screen.getByRole('button',{name:'AI 检标'})).toBeDisabled()
  await user.click(screen.getByRole('button',{name:'管理企业资料'}));expect(manage).toHaveBeenCalled()
})

it('rereads a zero-batch failed file without model confirmation and keeps it disabled',async()=>{
  const base=globalThis.fetch
  const failed={...run,status:'failed',pages:0,total_batches:0,completed_batches:0,error:'转换超时',diagnostic:{}}
  vi.stubGlobal('fetch',vi.fn(async(url:string,init?:RequestInit)=>{
    if(url==='/api/knowledge/documents')return response({items:[{...document,active:0,name:'大型.docx',run:failed,runs:[failed]}]})
    if(url==='/api/knowledge/documents/doc1/action'){calls.push({url,init});return response({})}
    return base(url,init)
  }))
  const user=userEvent.setup();render(<KnowledgePage onBack={()=>undefined}/>)
  await user.click(await screen.findByRole('button',{name:'重新读取文件'}))
  expect(await screen.findByRole('status')).toHaveTextContent('读取完成后再点击开始拆解')
  expect(screen.queryByRole('region',{name:'确认文档拆解'})).not.toBeInTheDocument()
  expect(JSON.parse(String(calls.find(c=>c.url.endsWith('/doc1/action'))?.init?.body))).toEqual({action:'retry'})
  expect(screen.getByRole('button',{name:'启用'})).toBeInTheDocument()
})

it('shows live reading phase and structural coverage with reference-only Word pages',async()=>{
  const base=globalThis.fetch
  const reading={...run,status:'indexing_running',pages:0,total_batches:0,diagnostic:{phase:'image_prepare',mode:'docx_direct',paragraphs:8593,tables:31,images:960,blocks:1418,processed:500,total:1418,reference_pages:1204,elapsed_ms:6300}}
  vi.stubGlobal('fetch',vi.fn(async(url:string,init?:RequestInit)=>url==='/api/knowledge/documents'?response({items:[{...document,run:reading,runs:[reading]}]}):base(url,init)))
  render(<KnowledgePage onBack={()=>undefined}/>)
  expect(await screen.findByText(/准备图片 · 6.3 秒 · 已处理 500\/1418 内容块/)).toBeInTheDocument()
  expect(screen.getByText(/31 表格 · 960 图片引用 · Word 参考 1204 页/)).toBeInTheDocument()
  expect(screen.getByRole('button',{name:'停止'})).toBeEnabled()
})

it('confirms OCR cloud submission with the number of pending pictures before extraction',async()=>{
  const base=globalThis.fetch
  const ready={...run,status:'uploaded',completed_batches:0,pending_images:6}
  const ocr={enabled:true,provider:'paddle_aistudio',model:'PP-OCRv5',access_token_configured:true}
  vi.stubGlobal('fetch',vi.fn(async(url:string,init?:RequestInit)=>{
    if(url==='/api/ocr/config')return response(ocr)
    if(url==='/api/knowledge/documents')return response({items:[{...document,run:ready,runs:[ready]}]})
    if(url.endsWith('/doc1/action')){calls.push({url,init});return response({})}
    return base(url,init)
  }))
  const user=userEvent.setup();render(<KnowledgePage onBack={()=>undefined}/>)
  await user.click(await screen.findByRole('button',{name:'开始拆解'}))
  expect(await screen.findByRole('region',{name:'确认文档拆解'})).toHaveTextContent('待识别 6 张图片')
  expect(screen.getByText(/图片会提交到飞桨官方云端/)).toBeInTheDocument()
  await user.click(screen.getByRole('button',{name:'确认并开始拆解'}))
  await waitFor(()=>expect(calls.some(c=>c.url.endsWith('/doc1/action'))).toBe(true))
  expect(JSON.parse(String(calls.find(c=>c.url.endsWith('/doc1/action'))?.init?.body)).expected_ocr).toEqual({enabled:true,provider:'paddle_aistudio',model:'PP-OCRv5'})
})

it('shows partial OCR issues and OCR evidence while allowing successful entry confirmation',async()=>{
  const base=globalThis.fetch
  const partial={...run,ocr:{total:6,processed:6,failed:1,issues:[{source_id:'S2',location:'正文第2段 · 图片2',status:'empty',error:'未识别出文字'}]},diagnostic:{phase:'extraction_complete'}}
  vi.stubGlobal('fetch',vi.fn(async(url:string,init?:RequestInit)=>url==='/api/knowledge/documents'?response({items:[{...document,run:partial,runs:[partial]}]}):url.startsWith('/api/knowledge/entries')?response({items:[{...extracted,source:{...extracted.source,ocr:true,ocr_text:'营业执照 测试公司'}}],total:1,page:1,has_next:false}):base(url,init)))
  const user=userEvent.setup();render(<KnowledgePage onBack={()=>undefined}/>)
  expect(await screen.findByText(/拆解完成，部分图片待核实/)).toBeInTheDocument()
  await user.click(screen.getByText('查看 OCR 待核实位置'));expect(screen.getByText(/正文第2段 · 图片2：未识别出文字/)).toBeInTheDocument()
  await user.click(screen.getByText('查看出处与原始提取'));expect(screen.getByText('营业执照 测试公司')).toBeInTheDocument()
  expect(screen.getByRole('button',{name:'确认入库'})).toBeEnabled()
})

const summaryItem={id:'summary-entry',status:'pending',updated_at:'2026-10-10',data:{...data,kind:'material',name:'营业执照',value:'已发现实际营业执照',subject_role:'supplier',evidence_kind:'proof',certificates:[]},original:{...data,value:'原始汇总'},entry_ids:['entry1'],sources:[{entry_id:'entry1',document_id:'doc1',filename:'历史投标.pdf',location:'第3页',quote:'营业执照',ocr:true}]}
const summary={id:'summary1',status:'awaiting_review',error:'',total_parts:2,completed_parts:2,skill:{id:'company-materials',version:'v1'},config:{model:'test-model'},coverage:{document_id:'doc1',filename:'历史投标.pdf',run_id:'run1',total_batches:3,completed_batches:2,gaps:[{batch:3,status:'failed',locations:['第9页'],error:'引用失败'}],ocr_gaps:[{source_id:'S3',location:'第9页图片',status:'empty',error:'识别为空'}]},items:[summaryItem,{...summaryItem,id:'summary-fact',data:{...summaryItem.data,kind:'fact',name:'履约能力',value:'企业声明具备履约能力'}}]}

it('confirms document ownership and manually organizes stored extraction without reparsing',async()=>{
  const confirm=vi.spyOn(window,'confirm').mockReturnValue(true)
  const user=userEvent.setup();render(<KnowledgePage onBack={()=>undefined}/>)
  await user.click(await screen.findByLabelText('确认为本企业历史资料 历史投标.pdf'))
  await waitFor(()=>expect(calls.some(c=>c.url.endsWith('/doc1/ownership'))).toBe(true))
  expect(JSON.parse(String(calls.find(c=>c.url.endsWith('/doc1/ownership'))?.init?.body))).toEqual({confirmed:true})
  await user.click(screen.getByRole('button',{name:'整理已有结果'}))
  await waitFor(()=>expect(calls.some(c=>c.url.endsWith('/doc1/summarize'))).toBe(true))
  expect(JSON.parse(String(calls.find(c=>c.url.endsWith('/doc1/summarize'))?.init?.body))).toEqual({action:'start',expected_config:{base_url:config.base_url,model:config.model,protocol:config.protocol}})
  expect(calls.some(c=>c.url.endsWith('/doc1/action'))).toBe(false)
  confirm.mockRestore()
})

it('shows merged material and capability candidates with coverage, edits and confirms all while raw audit remains',async()=>{
  const base=globalThis.fetch
  vi.stubGlobal('fetch',vi.fn(async(url:string,init?:RequestInit)=>url==='/api/knowledge/documents'?response({items:[{...document,summary}]}):base(url,init)))
  const user=userEvent.setup();render(<KnowledgePage onBack={()=>undefined}/>)
  expect(await screen.findByText('已发现实际营业执照')).toBeInTheDocument()
  expect(screen.getByText(/未处理内容未知/)).toBeInTheDocument()
  await user.click(screen.getByText('查看覆盖缺口'))
  expect(screen.getByText(/第 3 批 · failed：第9页 引用失败/)).toBeInTheDocument()
  await user.click(screen.getByText('查看关联出处（1）'))
  expect(screen.getByText(/第3页 · OCR/)).toBeInTheDocument()
  await user.click(screen.getByRole('button',{name:'修改汇总'}))
  await user.clear(screen.getByLabelText('汇总内容'));await user.type(screen.getByLabelText('汇总内容'),'人工核对：有营业执照')
  await user.click(screen.getByRole('button',{name:'保存汇总并确认'}))
  await waitFor(()=>expect(calls.some(c=>c.url==='/api/knowledge/summaries/review')).toBe(true))
  expect(JSON.parse(String(calls.find(c=>c.url==='/api/knowledge/summaries/review')?.init?.body)).items[0].changes.value).toBe('人工核对：有营业执照')
  await user.click(screen.getByRole('button',{name:'能力摘要'}))
  expect(screen.getByText('企业声明具备履约能力')).toBeInTheDocument()
  await user.click(screen.getByRole('button',{name:'确认全部汇总'}))
  await waitFor(()=>expect(calls.filter(c=>c.url==='/api/knowledge/summaries/review')).toHaveLength(2))
  expect(JSON.parse(String(calls.filter(c=>c.url==='/api/knowledge/summaries/review')[1]?.init?.body)).items).toHaveLength(2)
  await user.click(screen.getByText('展开原始逐批记录'))
  expect(screen.getByRole('button',{name:'确认入库'})).toBeEnabled()
})

it.each(['running','failed','stopped'])('controls independent summary lifecycle while %s',async state=>{
  const base=globalThis.fetch
  vi.stubGlobal('fetch',vi.fn(async(url:string,init?:RequestInit)=>url==='/api/knowledge/documents'?response({items:[{...document,summary:{...summary,status:state,error:state==='failed'?'汇总认证失败':'',items:[]}}]}):base(url,init)))
  const user=userEvent.setup();render(<KnowledgePage onBack={()=>undefined}/>)
  const action=state==='running'?'stop':state==='failed'?'retry':'start'
  await user.click(await screen.findByRole('button',{name:state==='running'?'停止汇总':state==='failed'?'重试汇总':'继续汇总'}))
  await waitFor(()=>expect(calls.some(c=>c.url==='/api/knowledge/summaries/summary1/action')).toBe(true))
  expect(JSON.parse(String(calls.find(c=>c.url==='/api/knowledge/summaries/summary1/action')?.init?.body)).action).toBe(action)
  expect(screen.queryByRole('button',{name:'确认全部汇总'})).not.toBeInTheDocument()
})

it('shows failed summary groups and allows successful candidates to be reviewed before retry',async()=>{
  const base=globalThis.fetch
  const partial={...summary,status:'partial',completed_parts:1,failed_parts:1,processed_parts:2,error:'1 个分组未完成，其他分组已处理',part_errors:[{part:2,status:'failed',names:['许可证'],locations:['第9页'],error:'汇总引用与已有来源不符'}]}
  vi.stubGlobal('fetch',vi.fn(async(url:string,init?:RequestInit)=>url==='/api/knowledge/documents'?response({items:[{...document,summary:partial}]}):base(url,init)))
  const user=userEvent.setup();render(<KnowledgePage onBack={()=>undefined}/>)
  expect(await screen.findByText(/汇总部分完成 · 已汇总 1\/2 组 · 未成功 1 组/)).toBeInTheDocument()
  await user.click(screen.getByText('查看汇总失败分组'))
  expect(screen.getByText(/第 2 组 · 许可证 · 第9页：汇总引用与已有来源不符/)).toBeInTheDocument()
  expect(screen.getByRole('button',{name:'确认汇总入库'})).toBeEnabled()
  await user.click(screen.getByLabelText('选择汇总 营业执照'))
  await user.click(screen.getByRole('button',{name:'确认所选汇总'}))
  await waitFor(()=>expect(calls.some(c=>c.url==='/api/knowledge/summaries/review')).toBe(true))
  await user.click(screen.getByRole('button',{name:'重试未完成分组'}))
  await waitFor(()=>expect(calls.some(c=>c.url==='/api/knowledge/summaries/summary1/action')).toBe(true))
  expect(JSON.parse(String(calls.find(c=>c.url==='/api/knowledge/summaries/summary1/action')?.init?.body)).action).toBe('retry')
})

it('offers read-only trace for summary-linked sources and directory clues, with current and historical notes separated',async()=>{
  const base=globalThis.fetch
  const linked={...extracted,reviewable:false,display_status:'summary_confirmed',current_material_status:'已发现实际营业执照',current_notes:'编号文字不清',local_observations:['本批未见正文'],summary_links:[{summary_id:'summary1',entry_id:'summary-entry',name:'营业执照',kind:'material',occurrence:'actual',value:'已发现实际营业执照'}],data:{...data,name:'执照原始出处',needs_review:true,notes:'本批未见正文；编号文字不清'}}
  const clue={...extracted,id:'clue',reviewable:false,reference_only:true,current_notes:'',current_material_status:'定位线索',local_observations:['仅目录提及，本批未见正文'],data:{...data,name:'目录执照线索',occurrence:'third_party',needs_review:true,notes:'仅目录提及，本批未见正文'}}
  vi.stubGlobal('fetch',vi.fn(async(url:string,init?:RequestInit)=>{
    if(url.startsWith('/api/knowledge/entries')){calls.push({url,init});return response({items:url.includes('view=trace')?[linked,clue]:[],total:url.includes('view=trace')?2:0,page:1,has_next:false})}
    if(url==='/api/knowledge/summaries/summary1')return response({...summary,status:'reviewed',items:[{...summaryItem,status:'accepted'}]})
    return base(url,init)
  }))
  const user=userEvent.setup();render(<KnowledgePage onBack={()=>undefined}/>)
  await user.click(await screen.findByRole('button',{name:'全部原始记录与线索'}))
  expect(await screen.findByText(/已通过汇总确认/,{selector:'span'})).toBeInTheDocument()
  expect(screen.getByRole('region',{name:'目录与模板线索'})).toBeInTheDocument()
  expect(screen.queryByRole('button',{name:'确认入库'})).not.toBeInTheDocument()
  expect(screen.queryByLabelText('选择 执照原始出处')).not.toBeInTheDocument()
  expect(screen.queryByLabelText('选择 目录执照线索')).not.toBeInTheDocument()
  expect(screen.getByText('需核实：编号文字不清')).toHaveClass('knowledge-warning')
  expect(screen.getByText('本批未见正文')).not.toHaveClass('knowledge-warning')
  expect(screen.getByText('定位线索')).toBeInTheDocument()
  expect(calls.filter(c=>c.url.includes('view=trace')).every(c=>!c.url.includes('status='))).toBe(true)
  await user.click(screen.getByRole('button',{name:'查看关联材料：营业执照'}))
  expect(await screen.findByRole('region',{name:'关联已确认汇总'})).toBeInTheDocument()
  expect(screen.queryByRole('button',{name:'确认汇总入库'})).not.toBeInTheDocument()
})
