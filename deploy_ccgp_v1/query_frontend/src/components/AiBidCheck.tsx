import { useCallback, useEffect, useRef, useState, type DragEvent } from 'react'
import { Building2, Check, FileText, Info, Sparkles, UploadCloud, X } from 'lucide-react'
import type { QueryFilters } from '../api'
import { getAIConfig, type AIConfig } from '../aiApi'
import { actOnBidTask, createBidTask, getBidRules, type ScreeningRules } from '../bidApi'
import { getKnowledgeCompany, type KnowledgeCompany } from '../knowledgeApi'
import { BidTaskPanel } from './BidTaskPanel'

const allowedExtensions = new Set(['pdf', 'doc', 'docx', 'xls', 'xlsx', 'png', 'jpg', 'jpeg', 'txt'])
const maxFileSize = 20 * 1024 * 1024

function fileKey(file: File) {
  return `${file.name}:${file.size}:${file.lastModified}`
}

function formatSize(bytes: number) {
  return bytes < 1024 * 1024 ? `${Math.max(1, Math.ceil(bytes / 1024))} KB` : `${(bytes / (1024 * 1024)).toFixed(1)} MB`
}

export function AiBidCheck({ filters = null, totalCount = 0, queryReady = false, keywordDirty = false, onManageKnowledge }: { filters?: QueryFilters | null; totalCount?: number; queryReady?: boolean; keywordDirty?: boolean; onManageKnowledge?: () => void }) {
  const [files, setFiles] = useState<File[]>([])
  const [dragging, setDragging] = useState(false)
  const [error, setError] = useState('')
  const [notice, setNotice] = useState('')
  const inputRef = useRef<HTMLInputElement>(null)
  const [confirm, setConfirm] = useState<AIConfig | null>(null)
  const [rules, setRules] = useState<ScreeningRules | null>(null)
  const [busy, setBusy] = useState(false)
  const [taskId, setTaskId] = useState('')
  const [taskActive, setTaskActive] = useState(false)
  const [knowledgeLoading, setKnowledgeLoading] = useState(true)
  const [knowledge, setKnowledge] = useState<KnowledgeCompany | null>(null)
  const activeRequest = useRef<AbortController | null>(null)
  const onSelect = useCallback((id: string) => setTaskId(id), [])
  useEffect(() => () => activeRequest.current?.abort(), [])
  useEffect(() => { setConfirm(null) }, [filters, totalCount, queryReady, keywordDirty])
  useEffect(() => {
    const controller = new AbortController()
    void getKnowledgeCompany(controller.signal).then(result => { if (!controller.signal.aborted) setKnowledge(result) }).catch(caught => { if (!controller.signal.aborted && onManageKnowledge) setError(caught instanceof Error ? caught.message : '读取知识库失败') }).finally(() => { if (!controller.signal.aborted) setKnowledgeLoading(false) })
    return () => controller.abort()
  }, [])

  const hasKnowledge = Boolean(knowledge?.revision && knowledge.revision.material_count + knowledge.revision.fact_count > 0)

  async function prepare() {
    if (!filters || !queryReady || !totalCount || keywordDirty || busy) return
    const controller = new AbortController()
    activeRequest.current = controller
    setBusy(true)
    setError('')
    try {
      const [config, currentKnowledge, currentRules] = await Promise.all([getAIConfig(controller.signal), onManageKnowledge ? getKnowledgeCompany(controller.signal) : Promise.resolve(null), getBidRules(controller.signal)])
      if (onManageKnowledge && currentKnowledge) {
        setKnowledge(currentKnowledge)
        if (!currentKnowledge.revision || currentKnowledge.revision.material_count + currentKnowledge.revision.fact_count === 0) throw new Error('企业档案已变化，请先确认可用的知识库资料')
      }
      if (controller.signal.aborted) return
      if (!config.api_key_configured || !config.model) throw new Error('请先通过右上角“模型配置”设置模型和 API Key')
      setRules(currentRules)
      setConfirm(config)
    } catch (caught) { if (!controller.signal.aborted) setError(caught instanceof Error ? caught.message : '读取模型配置失败') }
    finally { if (!controller.signal.aborted) setBusy(false) }
  }

  async function submit() {
    if (!filters || !confirm || busy || !queryReady || keywordDirty) return
    const controller = new AbortController()
    activeRequest.current = controller
    setBusy(true)
    setError('')
    let createdId = ''
    try {
      const created = await createBidTask(filters, hasKnowledge ? [] : files, controller.signal, confirm, hasKnowledge ? knowledge?.revision?.id : undefined)
      createdId = created.id
      // A live crawler may have changed the count between query and snapshot creation.
      if (created.total !== totalCount) {
        await actOnBidTask(created.id, 'stop', controller.signal)
        throw new Error(`筛选结果数量已变为 ${created.total} 条，请重新搜索并确认检标范围`)
      }
      await actOnBidTask(created.id, 'start', controller.signal)
      if (controller.signal.aborted) return
      setTaskId(created.id)
      setConfirm(null)
      setFiles([])
      setNotice('检标任务已提交，关闭页面后后台仍继续处理；刷新可恢复进度')
    } catch (caught) {
      if (!controller.signal.aborted) {
        if (createdId) setTaskId(createdId)
        setError(caught instanceof Error ? caught.message : '提交检标失败')
      }
    } finally { if (!controller.signal.aborted) setBusy(false) }
  }

  function addFiles(incoming: File[]) {
    if (busy) return
    const accepted: File[] = []
    const rejected: string[] = []
    for (const file of incoming) {
      const extension = file.name.split('.').pop()?.toLowerCase() ?? ''
      if (!allowedExtensions.has(extension)) rejected.push(`${file.name}：暂不支持此格式`)
      else if (file.size > maxFileSize) rejected.push(`${file.name}：文件超过 20 MB`)
      else if (file.size === 0) rejected.push(`${file.name}：文件为空`)
      else accepted.push(file)
    }
    setFiles((current) => {
      const keys = new Set(current.map(fileKey))
      let totalBytes = current.reduce((sum, file) => sum + file.size, 0)
      return [...current, ...accepted.filter((file) => {
        const key = fileKey(file)
        if (keys.has(key)) return false
        if (totalBytes + file.size > 50 * 1024 * 1024) return false
        totalBytes += file.size
        keys.add(key)
        return true
      })]
    })
    setError(files.reduce((sum, file) => sum + file.size, 0) + accepted.reduce((sum, file) => sum + file.size, 0) > 50 * 1024 * 1024 ? '公司资料总大小不得超过 50 MB，超出部分未添加' : rejected.join('；'))
    setNotice('')
    setConfirm(null)
  }

  function dropFiles(event: DragEvent<HTMLDivElement>) {
    event.preventDefault()
    setDragging(false)
    addFiles(Array.from(event.dataTransfer.files))
  }

  function removeFile(key: string) {
    setFiles((current) => current.filter((file) => fileKey(file) !== key))
    setNotice('')
    setError('')
    setConfirm(null)
  }

  return (
    <section className="ai-check-section" aria-labelledby="ai-check-title">
      <div className="ai-check-header">
        <div className="ai-check-heading">
          <span className="ai-check-icon"><Sparkles size={21} /></span>
          <div>
            <h2 id="ai-check-title">AI 检标</h2>
            <p>逐条检查全部筛选结果，优先筛掉有明确硬性资格冲突的项目</p>
          </div>
        </div>
        <span className="ai-check-badge">企业资料</span>
      </div>
      <div className="ai-check-body">
        {hasKnowledge && knowledge?.revision ? <div className="company-upload knowledge-summary">
          <h3>使用已确认企业知识库</h3>
          <p className="upload-description">{knowledge.name || '本企业'} · 已确认材料 {knowledge.revision.material_count} 项 · 企业事实 {knowledge.revision.fact_count} 项</p>
          <p className="knowledge-help">本次检标直接使用知识库版本 {knowledge.revision.id.slice(0, 8)}，不会重新解析原始资料。</p>
          <button type="button" className="upload-files-button" onClick={onManageKnowledge}>管理企业知识库</button>
        </div> : onManageKnowledge ? <div className="company-upload knowledge-summary"><h3>企业知识库</h3><p className="knowledge-help">{knowledgeLoading ? '正在读取企业档案…' : '还没有已确认资料，请先上传、拆解并确认企业资料。'}</p><button type="button" className="upload-files-button" onClick={onManageKnowledge}>管理企业资料</button></div> : <div className="company-upload">
          <h3>上传公司信息</h3>
          <p className="upload-description">可上传公司介绍、资质证书、业务范围或项目业绩等资料</p>
          <div
            className={`company-dropzone${dragging ? ' is-dragging' : ''}`}
            onDragOver={(event) => { event.preventDefault(); setDragging(true) }}
            onDragLeave={(event) => { if (!event.currentTarget.contains(event.relatedTarget as Node | null)) setDragging(false) }}
            onDrop={dropFiles}
          >
            <UploadCloud size={31} strokeWidth={1.6} />
            <strong>拖拽公司资料到这里，或点击上传</strong>
            <span>支持 PDF、Word、Excel、图片和 TXT，单个文件不超过 20 MB，总计不超过 50 MB</span>
            <button type="button" className="upload-files-button" disabled={busy} onClick={() => inputRef.current?.click()}>选择文件</button>
            <input
              ref={inputRef}
              className="sr-only"
              type="file"
              tabIndex={-1}
              aria-label="上传公司信息"
              accept=".pdf,.doc,.docx,.xls,.xlsx,.png,.jpg,.jpeg,.txt"
              multiple
              disabled={busy}
              onChange={(event) => {
                addFiles(Array.from(event.target.files ?? []))
                event.target.value = ''
              }}
            />
          </div>
          {error ? <p className="upload-error" role="alert">{error}</p> : null}
          {files.length > 0 ? (
            <div className="company-file-list">
              <p className="file-list-title">已添加 {files.length} 份资料</p>
              <ul>
                {files.map((file) => (
                  <li key={fileKey(file)}>
                    <span className="company-file-icon"><FileText size={19} /></span>
                    <div className="company-file-detail"><strong title={file.name}>{file.name}</strong><span>{formatSize(file.size)} · 已就绪</span></div>
                    <Check size={15} className="file-ready-icon" aria-hidden="true" />
                    <button type="button" disabled={busy} aria-label={`移除 ${file.name}`} title="移除文件" onClick={() => removeFile(fileKey(file))}><X size={16} /></button>
                  </li>
                ))}
              </ul>
            </div>
          ) : null}
        </div>}
        <aside className="company-info-guide">
          <Building2 size={23} strokeWidth={1.6} />
          <h3>让检标更了解你的公司</h3>
          <p>建议提供以下信息，便于后续匹配项目要求：</p>
          <ul>
            <li><Check size={14} />公司简介与主营业务</li>
            <li><Check size={14} />企业资质与相关认证</li>
            <li><Check size={14} />过往项目与履约业绩</li>
            <li><Check size={14} />业务覆盖地区</li>
          </ul>
        </aside>
      </div>
      {onManageKnowledge && error ? <p className="upload-error" role="alert">{error}</p> : null}
      <div className="ai-check-actions">
        <p><Info size={14} />{keywordDirty ? '关键词尚未提交，请先搜索' : !queryReady || !totalCount ? '请先筛选出标讯结果，再提交公司资料' : `将检查全部 ${totalCount} 条公告，包含未翻到的页面`}</p>
        <button
          type="button"
          className="ai-check-button"
          disabled={(!hasKnowledge && (Boolean(onManageKnowledge) || files.length === 0)) || !queryReady || !totalCount || keywordDirty || busy || taskActive}
          onClick={() => void prepare()}
        ><Sparkles size={17} />{busy ? '提交中…' : 'AI 检标'}</button>
      </div>
      {confirm ? <div className="bid-confirm" role="region" aria-label="确认检标范围"><strong>确认检查全部 {totalCount} 条公告</strong><p>关键词：{filters?.keyword || '全部'} · 地区代码：{filters?.regionCode || '全部'} · 日期：{filters?.startDate || '不限'} 至 {filters?.endDate || '不限'}</p><p>预计调用约 {totalCount + (hasKnowledge ? 0 : 1)} 次{hasKnowledge ? '：逐条读取公告并判断，不再整理公司资料' : '：公司资料整理 1 次，之后每条公告独立判断'}。失败重试或兼容模式可能增加调用。</p><p>资格初筛规则：{rules?.skill_name} · 版本 {rules?.skill_sha256.slice(0, 8)} · 判断协议 {rules?.protocol_version}。本轮读取公告正文，附件未读取；阅读缺口保留为待核实。</p><p>将使用模型服务商：{confirm.base_url}，模型 {confirm.model}。可能产生 API 费用，金额取决于资料长度和服务商价格。</p><div><button type="button" disabled={busy} onClick={() => setConfirm(null)}>取消</button><button type="button" disabled={busy} onClick={() => void submit()}>确认并开始检标</button></div></div> : null}
      {notice ? <div className="ai-check-notice" role="status"><Info size={16} />{notice}</div> : null}
      <BidTaskPanel selectedId={taskId} onSelect={onSelect} onActive={setTaskActive} onManageKnowledge={onManageKnowledge} />
    </section>
  )
}
