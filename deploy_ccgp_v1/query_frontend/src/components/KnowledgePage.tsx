import {KnowledgeSummaryPanel} from './KnowledgeSummaryPanel'
import { useEffect, useRef, useState } from 'react'
import { ArrowLeft, BookOpen, FileText, UploadCloud } from 'lucide-react'
import {getOCRConfig,type OCRConfig} from '../ocrApi'
import { getAIConfig, type AIConfig } from '../aiApi'
import { getKnowledgeCompany, knowledgeRequest, knowledgeCategories, type KnowledgeCompany, type KnowledgeDocument, type KnowledgeEntry, type KnowledgeEntries, type KnowledgeSummary, type SummaryLink } from '../knowledgeApi'

const diagnosticNames: Record<string,string> = {ocr:'OCR 图片识别',indexing:'准备读取',docx_read:'直接读取 DOCX',image_prepare:'准备图片',office_convert:'Office 转换兜底',indexing_complete:'读取完成',extracting:'AI 拆解',extraction_complete:'拆解完成'}
function ReadingStatus({doc}:{doc:KnowledgeDocument}){
  const info=doc.run.diagnostic||{}
  return <>{info.phase==='ocr'?<p role="status">OCR 已处理 {info.ocr_processed||0}/{info.ocr_total||0} 张 · 待核实 {info.ocr_failed||0} 张{info.ocr_current_source?<small>当前：{info.ocr_current_source}</small>:null}</p>:null}{doc.run.ocr?.failed?<><p className="knowledge-warning">{['awaiting_review','reviewed'].includes(doc.run.status)?'拆解完成，部分图片待核实':'部分图片待核实'} · {doc.run.ocr.failed} 张；成功批次的条目可先确认入库</p><details><summary>查看 OCR 待核实位置</summary>{doc.run.ocr.issues.map(issue=><p key={issue.source_id}>{issue.location}：{issue.error}</p>)}</details></>:null}{info.phase?<small>{diagnosticNames[info.phase]||info.phase}{info.elapsed_ms!==undefined?` · ${(info.elapsed_ms/1000).toFixed(1)} 秒`:''}{['docx_read','image_prepare'].includes(info.phase||'')&&info.processed!==undefined?` · 已处理 ${info.processed}${info.total?`/${info.total}`:''} ${info.phase==='docx_read'?'段落':'内容块'}`:''}</small>:null}{info.mode?<p>{info.mode==='docx_direct'?'DOCX 直接读取':info.mode==='office_convert'?'Office 转换读取':'文件读取'} · {info.blocks??doc.run.pages} {info.mode==='docx_direct'?'内容块':'页/内容块'}{info.paragraphs!==undefined?` · ${info.paragraphs} 段落 · ${info.tables||0} 表格 · ${info.images||0} 图片引用`:''}{info.reference_pages?` · Word 参考 ${info.reference_pages} 页`:''}</p>:null}{info.unreadable?.length?<details className="knowledge-warning"><summary>未能读取的内容（{info.unreadable.length}）</summary>{info.unreadable.map((value,i)=><p key={i}>{value}</p>)}</details>:null}</>
}
const names: Record<string,string> = {indexing:'准备解析',indexing_running:'定位文档内容',uploaded:'待开始',queued:'排队中',running:'正在拆解',stopping:'正在停止',stopped:'已停止',failed:'拆解异常',awaiting_review:'待确认',reviewed:'已审核'}
const active = new Set(['indexing','indexing_running','queued','running','stopping'])
const occurrences: Record<string,string> = {actual:'实际出现',mention:'仅提及',template:'空白模板',third_party:'旧主体标记（出现方式待恢复）',unknown:'出现方式待确认'}
const evidence: Record<string,string> = {proof:'证明材料',declaration:'文件声明',inference:'推断',unknown:'未明确'}

const canReview = (entry: KnowledgeEntry) => !entry.reference_only && !entry.summary_links?.length && (entry.reviewable ?? (entry.batch_status ? entry.batch_status === 'done' : ['awaiting_review','reviewed'].includes(entry.run_status)))

function EntryCard({entry, disabled, onReview, onShowSummary}: {entry: KnowledgeEntry; disabled: boolean; onShowSummary: (link:SummaryLink)=>void; onReview: (items: {id: string; status: string; changes?: unknown}[]) => Promise<boolean>}) {
  const [editing, setEditing] = useState(false)
  const [draft, setDraft] = useState(entry.data)
  useEffect(() => {setDraft(entry.data); setEditing(false)}, [entry.id, entry.updated_at])
  const reviewable = canReview(entry)
  const linked=Boolean(entry.summary_links?.length)
  const readonly=Boolean(entry.reference_only||linked)
  const notes=entry.current_notes??entry.data.notes
  const locked = disabled || !reviewable
  return <article className="knowledge-entry">
    <div className="knowledge-entry-heading"><strong>{entry.data.name}</strong><span>{entry.data.kind === 'material' ? '材料名称' : '企业事实'} · {entry.data.category} · {linked ? '已通过汇总确认' : entry.reference_only ? '定位线索' : entry.status === 'accepted' ? '已确认' : entry.status === 'rejected' ? '已剔除' : '待确认'}</span></div>
    {!entry.reference_only?<p>{entry.data.value}</p>:null}
    <small>主体：{entry.data.subject || '未知'} · {occurrences[entry.data.occurrence]} · {evidence[entry.data.evidence_kind]} · {entry.data.applicable_date || '时间未注明'}{entry.data.industry ? ` · ${entry.data.industry}` : ''}</small>
    {entry.current_material_status?<p>{entry.current_material_status}</p>:null}
    {entry.summary_links?.map(link=><button key={link.entry_id} disabled={disabled} onClick={()=>onShowSummary(link)}>查看关联{link.kind==='material'?'材料':'能力'}：{link.name}</button>)}
    {notes?<p className={entry.data.needs_review?'knowledge-warning':''}>{entry.data.needs_review?'需核实：':''}{notes}</p>:!readonly&&entry.data.needs_review&&entry.current_notes===undefined?<p className="knowledge-warning">需核实：适用主体、时间或证据尚需确认</p>:null}
    {entry.local_observations?.length?<details><summary>原始批次观察（当时的局部记录）</summary>{entry.local_observations.map((note,i)=><p key={i}>{note}</p>)}</details>:null}
    <details><summary>查看出处与原始提取</summary><p>{entry.filename} · {entry.source.location} · 第 {entry.generation} 次拆解</p><blockquote>{entry.source.quote}</blockquote>{entry.source.ocr?<><small>来源：OCR 识别文字，需核对原件</small><pre className="ocr-test-text">{entry.source.ocr_text}</pre></>:null}<p>原始提取：{entry.original.name} — {entry.original.value}</p><a href={`/api/knowledge/documents/${entry.document_id}/original`} target="_blank" rel="noreferrer">下载原件核对</a></details>
    {readonly?null:editing ? <div className="knowledge-edit">
      <label>材料／事实名称<input value={draft.name} maxLength={200} onChange={e=>setDraft({...draft,name:e.target.value})}/></label>
      <label>提取内容<textarea value={draft.value} maxLength={3000} onChange={e=>setDraft({...draft,value:e.target.value})}/></label>
      <label>分类<select value={draft.category} onChange={e=>setDraft({...draft,category:e.target.value})}>{knowledgeCategories.map(c=><option key={c}>{c}</option>)}</select></label>
      <label>所属主体<input value={draft.subject} onChange={e=>setDraft({...draft,subject:e.target.value})}/></label>
      <label>适用日期／有效期<input value={draft.applicable_date} onChange={e=>setDraft({...draft,applicable_date:e.target.value})}/></label>
      <label>所属行业<input value={draft.industry} onChange={e=>setDraft({...draft,industry:e.target.value})}/></label>
      <label>核实说明<textarea value={draft.notes} onChange={e=>setDraft({...draft,notes:e.target.value})}/></label>
      <label className="knowledge-checkbox"><input type="checkbox" checked={draft.needs_review} onChange={e=>setDraft({...draft,needs_review:e.target.checked})}/>保留待核实标记</label>
      <div className="knowledge-actions"><button disabled={locked || !draft.name.trim() || !draft.value.trim()} onClick={()=>void onReview([{id:entry.id,status:'accepted',changes:{name:draft.name,value:draft.value,category:draft.category,subject:draft.subject,applicable_date:draft.applicable_date,industry:draft.industry,notes:draft.notes,needs_review:draft.needs_review}}]).then(saved=>{if(saved)setEditing(false)})}>保存并确认</button><button onClick={()=>setEditing(false)}>取消修改</button></div>
    </div> : <div className="knowledge-actions"><button disabled={locked} onClick={()=>void onReview([{id:entry.id,status:'accepted'}])}>确认入库</button><button disabled={locked} onClick={()=>setEditing(true)}>修改</button><button disabled={locked} onClick={()=>void onReview([{id:entry.id,status:'rejected'}])}>剔除</button>{entry.status !== 'pending' ? <button disabled={locked} onClick={()=>void onReview([{id:entry.id,status:'pending'}])}>撤回确认</button> : null}</div>}
    {!reviewable && !readonly ? <small>本条所在批次尚未成功完成，请先重试该批次。</small> : !readonly && !['awaiting_review','reviewed'].includes(entry.run_status) ? <small>本条所在批次已完成，可先确认入库；文档其余批次仍待完成。</small> : null}
  </article>
}

export function KnowledgePage({onBack}: {onBack: () => void}) {
  const [company,setCompany]=useState<KnowledgeCompany|null>(null)
  const [companyName,setCompanyName]=useState('')
  const [documents,setDocuments]=useState<KnowledgeDocument[]>([])
  const [entries,setEntries]=useState<KnowledgeEntries>({items:[],total:0,page:1,has_next:false})
  const [selected,setSelected]=useState('')
  const [status,setStatus]=useState('pending')
  const [entryView,setEntryView]=useState<'review'|'trace'>('review')
  const [linkedSummary,setLinkedSummary]=useState<KnowledgeSummary|null>(null)
  const [linkedEntry,setLinkedEntry]=useState('')
  const [query,setQuery]=useState('')
  const [category,setCategory]=useState('')
  const [page,setPage]=useState(1)
  const [tab,setTab]=useState<'review'|'archive'>('review')
  const [busy,setBusy]=useState(false)
  const [error,setError]=useState('')
  const [notice,setNotice]=useState('')
  const [reload,setReload]=useState(0)
  const [config,setConfig]=useState<AIConfig|null>(null)
  const [confirmation,setConfirmation]=useState<{id:string; action:'start'|'retry';document:KnowledgeDocument;config:AIConfig;ocr:OCRConfig}|null>(null)
  const [checked,setChecked]=useState<string[]>([])
  const fileRef=useRef<HTMLInputElement>(null)
  const nameDirty=useRef(false)
  const requestRef=useRef<AbortController|null>(null)
  useEffect(()=>()=>requestRef.current?.abort(),[])
  useEffect(()=>{setPage(1);setChecked([])},[selected,status,category,query,tab,entryView])
  useEffect(()=>{
    const controller=new AbortController();let timer:ReturnType<typeof setTimeout>
    async function load(){
      try {
        const params=new URLSearchParams({page:String(page),q:query,view:entryView});if(selected)params.set('document_id',selected);if(status&&entryView==='review')params.set('status',status);if(category)params.set('category',category)
        const [c,d,e,ai]=await Promise.all([getKnowledgeCompany(controller.signal),knowledgeRequest<{items:KnowledgeDocument[]}>('/documents',controller.signal),knowledgeRequest<KnowledgeEntries>(`/entries?${params}`,controller.signal),getAIConfig(controller.signal)])
        if(controller.signal.aborted)return
        setConfig(ai);setCompany(c);if(!nameDirty.current)setCompanyName(c.name);setDocuments(d.items);setEntries(e);setChecked(current=>current.filter(id=>e.items.some(entry=>entry.id===id&&canReview(entry))))
        if(d.items.some(doc=>active.has(doc.run.status)||['queued','running','stopping'].includes(doc.summary?.status||'')))timer=setTimeout(()=>void load(),2000)
      }catch(caught){if(!controller.signal.aborted){setError(caught instanceof Error?caught.message:'读取知识库失败');timer=setTimeout(()=>void load(),4000)}}
    }
    void load();return()=>{controller.abort();clearTimeout(timer)}
  },[reload,selected,status,query,category,page,entryView])
  async function showSummary(link:SummaryLink){
    const ok=await operate(async signal=>setLinkedSummary(await knowledgeRequest<KnowledgeSummary>(`/summaries/${link.summary_id}`,signal)))
    if(ok)setLinkedEntry(link.entry_id)
  }
  useEffect(()=>{if(linkedEntry)document.getElementById(`summary-entry-${linkedEntry}`)?.scrollIntoView?.({block:'center'})},[linkedEntry,linkedSummary])
  async function operate(fn:(signal:AbortSignal)=>Promise<unknown>){
    if(busy)return false;const controller=new AbortController();requestRef.current=controller;setBusy(true);setError('');setNotice('')
    try{await fn(controller.signal);if(!controller.signal.aborted){setReload(n=>n+1);setChecked([]);return true}}catch(caught){if(!controller.signal.aborted)setError(caught instanceof Error?caught.message:'操作失败')}finally{if(!controller.signal.aborted)setBusy(false)}
    return false
  }
  async function saveCompany(){await operate(async signal=>{await knowledgeRequest('/company',signal,'PUT',{name:companyName});nameDirty.current=false;setNotice('企业名称已确认')})}
  async function upload(files:File[]){
    if(!files.length)return
    if(files.some(f=>!f.size || f.size>300*1024*1024)||files.reduce((sum,f)=>sum+f.size,0)>1024*1024*1024){setError('请选择非空文件，单文件不超过 300 MB、合计不超过 1 GB');return}
    await operate(async signal=>{
      const body=new FormData();files.forEach(file=>body.append('files',file))
      const data=await knowledgeRequest<{items:{document:KnowledgeDocument;duplicate:boolean}[]}>('/documents',signal,'POST',body)
      setSelected(data.items[0]?.document.id||'');setNotice(`已处理 ${data.items.length} 份上传，其中 ${data.items.filter(i=>i.duplicate).length} 份为重复文件；重复文件直接复用已有结果`)
    })
  }
  async function requestAction(doc:KnowledgeDocument,action:'start'|'retry'|'stop'|'reextract'){
    if((action==='retry'||action==='start')&&doc.run.total_batches===0){
      await operate(async signal=>{await knowledgeRequest(`/documents/${doc.id}/action`,signal,'POST',{action});setNotice('文件重新读取已开始，读取完成后再点击开始拆解')});return
    }
    if(action==='start'||action==='retry'){
      await operate(async signal=>{const [current,ocr]=await Promise.all([getAIConfig(signal),getOCRConfig(signal)]);setConfig(current);if(!current.api_key_configured)throw new Error('请先在右上角配置模型和密钥');if(ocr.enabled&&(doc.run.pending_images??doc.run.diagnostic.images??0)>0&&!ocr.access_token_configured)throw new Error('请先在模型配置的 OCR 识别页签填写 Access Token');setConfirmation({id:doc.id,action,document:doc,config:current,ocr})});return
    }
    if(action==='reextract'&&!window.confirm('重新拆解会生成新候选结果，可能再次产生模型费用；已确认的旧结果保留至新结果确认。继续吗？'))return
    await operate(signal=>knowledgeRequest(`/documents/${doc.id}/action`,signal,'POST',{action}))
  }
  async function runConfirmed(){if(!confirmation)return;await operate(async signal=>{const {config:current,ocr,...request}=confirmation;await knowledgeRequest(`/documents/${request.id}/action`,signal,'POST',{action:request.action,expected_config:{base_url:current.base_url,model:current.model,protocol:current.protocol},expected_ocr:{enabled:ocr.enabled,provider:ocr.provider,model:ocr.model}});setConfirmation(null);setNotice('后台拆解已开始，完成后自动整理材料与能力；刷新可恢复进度')})}
  async function review(items:{id:string;status:string;changes?:unknown}[]){return operate(signal=>knowledgeRequest('/review',signal,'POST',{items}))}
  const summaryDocs=documents.filter(doc=>(!selected||doc.id===selected)&&doc.summary)
  async function summarize(doc:KnowledgeDocument){
    if(!window.confirm('整理已有提取记录及 OCR 文字，不重新读取原图；使用当前模型，可能产生模型费用。继续吗？'))return
    await operate(async signal=>{const ai=await getAIConfig(signal);await knowledgeRequest(`/documents/${doc.id}/summarize`,signal,'POST',{action:'start',expected_config:{base_url:ai.base_url,model:ai.model,protocol:ai.protocol}});setNotice('材料与能力汇总已开始，完成后请确认候选结果')})
  }
  async function summaryAction(id:string,action:'start'|'retry'|'stop'){await operate(async signal=>{const ai=await getAIConfig(signal);await knowledgeRequest(`/summaries/${id}/action`,signal,'POST',{action,expected_config:{base_url:ai.base_url,model:ai.model,protocol:ai.protocol}})})}
  const records=(company?.revision?.records||[]).filter(record=>(!category||record.data.category===category)&&(!query||JSON.stringify(record).toLowerCase().includes(query.toLowerCase())))
  return <section className="knowledge-page">
    <div className="knowledge-title"><div><h1><BookOpen size={24}/>企业知识库</h1><p>历史投标文件拆解一次，确认后反复用于检标</p></div><button onClick={onBack}><ArrowLeft size={15}/>返回找标</button></div>
    {error?<p className="error-banner" role="alert">{error}</p>:null}{notice?<p className="knowledge-notice" role="status">{notice}</p>:null}
    <div className="knowledge-company"><label>本企业完整名称<input aria-label="本企业完整名称" value={companyName} maxLength={200} onChange={e=>{nameDirty.current=true;setCompanyName(e.target.value)}} placeholder="请输入企业全称，用于区分本企业和第三方材料"/></label><button disabled={busy||!companyName.trim()} onClick={()=>void saveCompany()}>确认企业名称</button><small>已确认材料 {company?.revision?.material_count||0} 项 · 企业事实 {company?.revision?.fact_count||0} 项</small><a href="/api/knowledge/backup">备份知识库及原件</a></div>
    <p className="knowledge-help">当前模型：{config?.model || '尚未配置'} · 上传后先定位内容，再由你启动 AI 拆解；完成后自动汇总材料与能力，需人工确认入库。</p>
    <div className="knowledge-upload"><UploadCloud size={23}/><div><strong>上传历史投标文件或补充企业资料</strong><p>支持 PDF、Word、Excel、图片、TXT；单文件 300 MB，合计 1 GB，最多 3,000 页／DOCX 内容块。保留原件，不单独保存证件图片。</p></div><button disabled={busy||!company?.name} onClick={()=>fileRef.current?.click()}>上传文件</button><input className="sr-only" ref={fileRef} type="file" aria-label="上传知识库文件" accept=".pdf,.doc,.docx,.xls,.xlsx,.png,.jpg,.jpeg,.txt" multiple disabled={busy||!company?.name} onChange={e=>{void upload(Array.from(e.target.files||[]));e.target.value=''}}/></div>
    <div className="knowledge-layout"><aside className="knowledge-documents"><h2>原始文档 <span>{documents.length}</span></h2>{documents.length===0?<p>先确认企业名称，再上传文件。</p>:documents.map(doc=><article key={doc.id} className={selected===doc.id?'is-selected':''}><button className="knowledge-doc-select" onClick={()=>setSelected(doc.id)}><FileText size={16}/><strong>{doc.name}</strong></button><small>{(doc.size/1024/1024).toFixed(1)} MB · {doc.run.pages||'—'} 页/内容块 · {names[doc.run.status]}{!doc.active?' · 已停用':''}</small><p>待审核原始条目 {doc.run.pending_entries} 项 · 第 {doc.run.generation} 次拆解 · 已完成 {doc.run.completed_batches}/{doc.run.total_batches} 批 · 失败 {doc.run.failed_batches} 批</p><ReadingStatus doc={doc}/>{doc.run.diagnostic.summary_error?<p className="knowledge-warning">{doc.run.diagnostic.summary_error}</p>:null}<label className="knowledge-checkbox"><input type="checkbox" aria-label={`确认为本企业历史资料 ${doc.name}`} checked={Boolean(doc.ownership?.confirmed&&doc.ownership.company_name===company?.name)} disabled={busy} onChange={e=>void operate(signal=>knowledgeRequest(`/documents/${doc.id}/ownership`,signal,'PUT',{confirmed:e.target.checked}))}/>确认为本企业历史资料</label><small>仅供应商自身资料用于本企业初筛，厂家和客户资料仍分开。</small><button disabled={busy||active.has(doc.run.status)||!doc.run.completed_batches||['queued','running','stopping'].includes(doc.summary?.status||'')} onClick={()=>void summarize(doc)}>整理已有结果</button>{doc.run.error?<p className="knowledge-warning">{doc.run.error}</p>:null}{doc.run.diagnostic?.attempt?<small>尝试 {doc.run.diagnostic.attempt}/3 {doc.run.diagnostic.status_code?`· HTTP ${doc.run.diagnostic.status_code}`:''}{doc.run.diagnostic.retrying?' · 自动重试中':''}</small>:null}<div className="knowledge-actions">{['uploaded','stopped','failed'].includes(doc.run.status)?<button disabled={busy} onClick={()=>void requestAction(doc,doc.run.status==='failed'?'retry':'start')}>{doc.run.total_batches===0?'重新读取文件':doc.run.status==='failed'?'重试未完成批次':doc.run.status==='stopped'?'继续拆解':'开始拆解'}</button>:null}{active.has(doc.run.status)?<button disabled={busy||doc.run.status==='stopping'} onClick={()=>void requestAction(doc,'stop')}>停止</button>:<button disabled={busy} onClick={()=>void requestAction(doc,'reextract')}>重新拆解</button>}<button disabled={busy} onClick={()=>void operate(signal=>knowledgeRequest(`/documents/${doc.id}/active`,signal,'PUT',{active:!doc.active}))}>{doc.active?'停用':'启用'}</button><a href={`/api/knowledge/documents/${doc.id}/original`} target="_blank" rel="noreferrer">原件</a></div>{doc.runs.length>1?<details><summary>历史拆解</summary>{doc.runs.map(run=><p key={run.id}>第 {run.generation} 次 · {names[run.status]} · {run.completed_batches}/{run.total_batches} 批</p>)}</details>:null}</article>)}</aside>
    <div className="knowledge-content">
      <div className="knowledge-tabs"><button className={tab==='review'?'selected':''} onClick={()=>setTab('review')}>拆解结果与审核</button><button className={tab==='archive'?'selected':''} onClick={()=>setTab('archive')}>已确认企业档案</button></div>
      <div className="knowledge-filters"><input aria-label="搜索知识库" placeholder="搜索材料、能力或原文" value={query} onChange={e=>setQuery(e.target.value)}/><select aria-label="知识分类" value={category} onChange={e=>setCategory(e.target.value)}><option value="">全部分类</option>{knowledgeCategories.map(c=><option key={c}>{c}</option>)}</select>{tab==='review'?<><select aria-label="资料确认状态" value={status} onChange={e=>setStatus(e.target.value)}><option value="pending">待确认</option><option value="accepted">已确认</option><option value="rejected">已剔除</option><option value="">全部状态</option></select><button onClick={()=>setSelected('')}>全部文档</button></>:null}</div>
      {confirmation?<div className="bid-confirm" role="region" aria-label="确认文档拆解"><strong>拆解 {confirmation.document.name}</strong><p>预计 {confirmation.document.run.total_batches} 批，已完成 {confirmation.document.run.completed_batches} 批；只处理未完成部分。资料将发送到 {confirmation.config.base_url}，模型 {confirmation.config.model}，可能产生 API 费用。</p><p>拆解完成后自动汇总成功部分，另有少量模型调用；候选结果需人工确认。</p><p>{confirmation.ocr.enabled?`OCR：飞桨官方 AI Studio · PP-OCRv5；待识别 ${confirmation.document.run.pending_images??confirmation.document.run.diagnostic.images??0} 张图片。图片会提交到飞桨官方云端，可能消耗 OCR 配额；AI 只接收识别文字。`:'OCR 未启用，图片沿用当前 AI 识别方式。'}</p><div><button disabled={busy} onClick={()=>setConfirmation(null)}>取消</button><button disabled={busy} onClick={()=>void runConfirmed()}>确认并开始拆解</button></div></div>:null}
      {tab==='review'?<>
        {summaryDocs.map(doc=><KnowledgeSummaryPanel key={doc.summary!.id} summary={doc.summary!} busy={busy} status={status} query={query} category={category} operate={operate} onAction={action=>void summaryAction(doc.summary!.id,action)}/>)}
        {linkedSummary?<section aria-label="关联已确认汇总"><h3>关联材料与能力</h3><button onClick={()=>{setLinkedSummary(null);setLinkedEntry('')}}>关闭关联汇总</button><KnowledgeSummaryPanel key={linkedEntry} initialKind={linkedSummary.items.find(e=>e.id===linkedEntry)?.data.kind||'material'} summary={linkedSummary} busy={busy} status="" query="" category="" readOnly operate={operate} onAction={()=>undefined}/></section>:null}
        <details open={summaryDocs.length===0}><summary>展开原始逐批记录</summary>
        <div className="knowledge-tabs"><button className={entryView==='review'?'selected':''} onClick={()=>setEntryView('review')}>独立审核</button><button className={entryView==='trace'?'selected':''} onClick={()=>setEntryView('trace')}>全部原始记录与线索</button></div>
        <p className="knowledge-help">已通过汇总确认的来源不重复审核。目录与模板只作定位线索；在全部原始记录中可查看原文和历史批次观察。失败批次与 OCR 缺口仍单独保留。</p>
        {entryView==='trace'?<p className="knowledge-help">追溯视图展示全部原始状态；上方确认状态筛选仅影响汇总。</p>:null}
        {selected?<details><summary>查看各批次局部观察（不作为全局缺失结论）</summary>{documents.find(d=>d.id===selected)?.run.limitations.map((lim,i)=><p className="knowledge-warning" key={i}>{lim}</p>)}</details>:null}
        <div className="knowledge-review-bar"><span>{entries.total} 条结果 · 已选择 {checked.length} 条</span><button disabled={busy||!checked.length} onClick={()=>void review(checked.map(id=>({id,status:'accepted'})))}>确认所选</button><button disabled={busy||!checked.length} onClick={()=>void review(checked.map(id=>({id,status:'rejected'})))}>剔除所选</button></div>
        <div className="knowledge-entry-list">{entries.items.filter(entry=>!entry.reference_only).map(entry=><div key={entry.id} className="knowledge-entry-row">{!entry.summary_links?.length?<input aria-label={`选择 ${entry.data.name}`} type="checkbox" disabled={!canReview(entry)||busy} checked={checked.includes(entry.id)} onChange={e=>setChecked(current=>e.target.checked?[...current,entry.id]:current.filter(id=>id!==entry.id))}/>:null}<EntryCard entry={entry} disabled={busy} onReview={review} onShowSummary={link=>void showSummary(link)}/></div>)}</div>
        {entries.items.some(entry=>entry.reference_only)?<section aria-label="目录与模板线索"><h3>目录与模板线索</h3><p className="knowledge-help">只读定位线索，不能单独确认入库。</p>{entries.items.filter(entry=>entry.reference_only).map(entry=><EntryCard key={entry.id} entry={entry} disabled={busy} onReview={review} onShowSummary={link=>void showSummary(link)}/>)}</section>:null}
        {!entries.items.length?<p className="bid-empty">暂无符合条件的拆解结果。上传并开始拆解后，结果会陆续显示。</p>:null}
        <div className="knowledge-pages"><button disabled={page===1||busy} onClick={()=>{setPage(n=>n-1);setChecked([])}}>上一页</button><span>第 {page} 页</span><button disabled={!entries.has_next||busy} onClick={()=>{setPage(n=>n+1);setChecked([])}}>下一页</button></div>
        </details>
      </>:<><p className="knowledge-help">检标使用已确认的版本。已确认归属的实际材料和填写声明支持一般资格初筛；公告额外等级、范围、数量等仍需核对。覆盖缺口和全部出处随版本保存。</p><ul className="knowledge-entry-list">{records.map((record,i)=><li className="knowledge-entry" key={i}><div className="knowledge-entry-heading"><strong>{record.data.name}</strong><span>{record.data.kind==='material'?'材料名称':'企业事实'} · {record.data.category}</span></div><p>{record.data.value}</p><small>{record.data.subject||'主体未知'} · {occurrences[record.data.occurrence]} · {record.data.applicable_date||'时间未注明'} · {record.data.industry||'行业未注明'}</small>{record.conflict?<p className="knowledge-warning">存在不同内容，请在审核区剔除不采用的项，或保留待核实。</p>:null}{(record.current_notes??record.data.notes)?<p className={record.data.needs_review?'knowledge-warning':''}>{record.data.needs_review?'需核实：':''}{record.current_notes??record.data.notes}</p>:null}{record.local_observations?.length?<details><summary>原始批次观察（当时的局部记录）</summary>{record.local_observations.map((n,j)=><p key={j}>{n}</p>)}</details>:null}<details><summary>查看 {record.sources.length} 个来源</summary>{record.sources.map((s,j)=><p key={j}><a href={`/api/knowledge/documents/${s.document_id}/original`} target="_blank" rel="noreferrer">{s.filename}</a> · {s.location}<br/>{s.quote}</p>)}</details></li>)}</ul>{!records.length?<p className="bid-empty">尚无已确认档案，请先审核拆解结果。</p>:null}</>}
    </div></div>
    {config ? <span className="sr-only">当前模型 {config.model}</span>:null}
  </section>
}
