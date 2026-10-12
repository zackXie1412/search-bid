import { useEffect, useRef, useState, type FormEvent, type KeyboardEvent } from 'react'
import { OcrSettings } from './OcrSettings'
import { createPortal } from 'react-dom'
import { CheckCircle2, FlaskConical, KeyRound, LoaderCircle, Settings2, X } from 'lucide-react'
import { getAIConfig, saveAIConfig, testAIConnection, type AIConfig, type AIConfigDraft, type AIConnectionResult } from '../aiApi'

type Props = { onClose: () => void }
const defaults: AIConfig = { base_url: 'https://api.openai.com/v1', model: '', protocol: 'responses', api_key_configured: false }

function urlIdentity(value: string) {
  try {
    const url = new URL(value.trim())
    return `${url.origin}${url.pathname.replace(/\/+$/, '')}`
  } catch { return value.trim().replace(/\/+$/, '') }
}

export function ModelSettings({ onClose }: Props) {
  const [tab,setTab]=useState<'ai'|'ocr'>('ai')
  const [ocrVisited,setOcrVisited]=useState(false)
  const [saved, setSaved] = useState<AIConfig>(defaults)
  const [draft, setDraft] = useState<AIConfigDraft>({ ...defaults, api_key: '', clear_api_key: false })
  const [ready, setReady] = useState(false)
  const [busy, setBusy] = useState<'load' | 'save' | 'test' | ''>('load')
  const [error, setError] = useState('')
  const [notice, setNotice] = useState('')
  const [result, setResult] = useState<AIConnectionResult | null>(null)
  const dialogRef = useRef<HTMLDialogElement>(null)
  const activeRequest = useRef<AbortController | null>(null)

  async function load() {
    activeRequest.current?.abort()
    const controller = new AbortController()
    activeRequest.current = controller
    setBusy('load')
    setError('')
    try {
      const config = await getAIConfig(controller.signal)
      if (controller.signal.aborted) return
      setSaved(config)
      setDraft({ base_url: config.base_url, model: config.model, protocol: config.protocol, api_key: '', clear_api_key: false })
      setReady(true)
    } catch (caught) {
      if (!controller.signal.aborted) setError(caught instanceof Error ? caught.message : '读取配置失败')
    } finally {
      if (!controller.signal.aborted) setBusy('')
    }
  }

  useEffect(() => {
    const dialog = dialogRef.current!
    const previousFocus = document.activeElement as HTMLElement | null
    const previousOverflow = document.body.style.overflow
    dialog.showModal()
    document.body.style.overflow = 'hidden'
    void load()
    return () => {
      activeRequest.current?.abort()
      dialog.close()
      document.body.style.overflow = previousOverflow
      previousFocus?.focus()
    }
  }, [])

  function update<K extends keyof AIConfigDraft>(key: K, value: AIConfigDraft[K]) {
    setDraft((current) => ({ ...current, [key]: value, ...(key === 'api_key' && value ? { clear_api_key: false } : {}) }))
    setError('')
    setNotice('')
    setResult(null)
  }

  function useDeepSeekPreset() {
    setDraft((current) => ({ ...current, base_url: 'https://api.deepseek.com', model: 'deepseek-flash', protocol: 'chat_completions', api_key: '', clear_api_key: false }))
    setError('')
    setNotice('')
    setResult(null)
  }

  async function submit(action: 'save' | 'test') {
    if (!ready || busy) return
    setError('')
    setNotice('')
    setResult(null)
    const key = draft.api_key?.trim() || ''
    if (saved.api_key_configured && urlIdentity(draft.base_url) !== urlIdentity(saved.base_url) && !key && !draft.clear_api_key) {
      setError('Base URL 已改变，请输入新密钥或明确清除原密钥')
      return
    }
    if (!draft.model.trim()) { setError('请填写模型名称'); return }
    const payload: AIConfigDraft = {
      base_url: draft.base_url.trim(), model: draft.model.trim(), protocol: draft.protocol,
      ...(key ? { api_key: key } : {}), clear_api_key: draft.clear_api_key,
    }
    const controller = new AbortController()
    activeRequest.current = controller
    setBusy(action)
    try {
      if (action === 'save') {
        const config = await saveAIConfig(payload, controller.signal)
        if (controller.signal.aborted) return
        setSaved(config)
        setDraft({ base_url: config.base_url, model: config.model, protocol: config.protocol, api_key: '', clear_api_key: false })
        setNotice('配置已保存')
      } else {
        const tested = await testAIConnection(payload, controller.signal)
        if (!controller.signal.aborted) setResult(tested)
      }
    } catch (caught) {
      if (!controller.signal.aborted) setError(caught instanceof Error ? caught.message : '请求失败，请重试')
    } finally {
      if (!controller.signal.aborted) setBusy('')
    }
  }

  function save(event: FormEvent<HTMLFormElement>) {
    event.preventDefault()
    void submit('save')
  }

  function keepFocus(event: KeyboardEvent<HTMLDialogElement>) {
    if (event.key !== 'Tab') return
    const controls = Array.from(event.currentTarget.querySelectorAll<HTMLElement>('button, input, select, textarea, a[href], [tabindex]'))
      .filter((element) => element.tabIndex >= 0 && !element.matches(':disabled') && !element.closest('[hidden]'))
    const first = controls[0]
    const last = controls[controls.length - 1]
    if (event.shiftKey && document.activeElement === first) {
      event.preventDefault()
      last?.focus()
    } else if (!event.shiftKey && document.activeElement === last) {
      event.preventDefault()
      first?.focus()
    }
  }

  return createPortal(
    <dialog className="model-dialog" ref={dialogRef} aria-labelledby="model-settings-title" aria-describedby="model-settings-description" onKeyDown={keepFocus} onCancel={(event) => { event.preventDefault(); onClose() }}>
      <div className="model-dialog-heading">
        <span className="model-settings-icon"><Settings2 size={21} /></span>
        <div><h2 id="model-settings-title">模型配置</h2><p id="model-settings-description">配置 AI 检标模型和图片 OCR 识别</p></div>
        <button className="model-close" type="button" aria-label="关闭模型配置" onClick={onClose} autoFocus><X size={19} /></button>
      </div>
      <div className="model-tabs" role="tablist" aria-label="配置类别">
        {(['ai','ocr'] as const).map((value,index)=><button key={value} id={`settings-tab-${value}`} role="tab" aria-selected={tab===value} aria-controls={`settings-panel-${value}`} tabIndex={tab===value?0:-1} type="button" onClick={()=>{setTab(value);if(value==='ocr')setOcrVisited(true)}} onKeyDown={e=>{if(['ArrowLeft','ArrowRight','Home','End'].includes(e.key)){e.preventDefault();const next=e.key==='Home'?'ai':e.key==='End'?'ocr':index===0?'ocr':'ai';setTab(next);if(next==='ocr')setOcrVisited(true);document.getElementById(`settings-tab-${next}`)?.focus()}}}>{value==='ai'?'AI 模型':'OCR 识别'}</button>)}
      </div>
      <div role="tabpanel" id="settings-panel-ai" aria-labelledby="settings-tab-ai" hidden={tab!=='ai'}>
      <form onSubmit={save}>
        <fieldset disabled={!ready || Boolean(busy)} className="model-fields">
          <div className="model-provider-row"><span>服务商预设</span><button type="button" className="model-provider-button" onClick={useDeepSeekPreset}>DeepSeek</button><small>官方接口 · deepseek-flash · Chat Completions</small></div>
          <label htmlFor="model-base-url">Base URL</label>
          <input id="model-base-url" type="url" required maxLength={2048} value={draft.base_url} onChange={(event) => update('base_url', event.target.value)} placeholder="https://api.openai.com/v1" autoComplete="off" spellCheck={false} />
          <small>填写 API 根地址，例如以 /v1 结尾的地址</small>

          <div className="model-field-row">
            <div><label htmlFor="model-name">模型名称</label><input id="model-name" required maxLength={200} value={draft.model} onChange={(event) => update('model', event.target.value)} placeholder="填写服务商提供的模型 ID" autoComplete="off" spellCheck={false} /></div>
            <div><label htmlFor="model-protocol">调用协议</label><select id="model-protocol" value={draft.protocol} onChange={(event) => update('protocol', event.target.value as AIConfigDraft['protocol'])}><option value="responses">Responses</option><option value="chat_completions">Chat Completions</option></select></div>
          </div>
          {draft.model.toLowerCase().startsWith('deepseek-') ? <small>建议先用 deepseek-flash，支持文字和图片资料。deepseek-v4-pro 用于纯文字资料。切换服务商需填写对应的 API Key，模型名称也可手工修改。</small> : null}
          <div className="model-key-label"><label htmlFor="model-api-key">API Key</label><span><KeyRound size={12} />{draft.clear_api_key ? '待清除' : saved.api_key_configured ? '已配置' : '未配置'}</span></div>
          <input id="model-api-key" type="password" maxLength={8192} value={draft.api_key || ''} onChange={(event) => update('api_key', event.target.value)} placeholder={saved.api_key_configured ? '已配置，留空保持不变' : '请输入 API Key'} autoComplete="new-password" spellCheck={false} />
          <div className="model-key-help"><small>{draft.clear_api_key ? '保存后清除原密钥；本次测试也不使用原密钥' : '密钥仅保存在本机后端，不会返回到页面'}</small>{saved.api_key_configured ? <button type="button" className="model-clear-key" onClick={() => { update('clear_api_key', !draft.clear_api_key); setDraft((current) => ({ ...current, api_key: '' })) }}>{draft.clear_api_key ? '取消清除' : '清除密钥'}</button> : null}</div>
        </fieldset>
        <div className="model-feedback" aria-live="polite" aria-busy={Boolean(busy)}>
          {busy === 'load' ? <p className="model-loading"><LoaderCircle size={15} />正在读取配置…</p> : null}
          {error ? <p className="model-error" role="alert">{error}</p> : null}
          {!ready && !busy ? <button className="model-retry" type="button" onClick={() => void load()}>重新读取</button> : null}
          {notice ? <p className="model-success" role="status"><CheckCircle2 size={15} />{notice}</p> : null}
          {result ? <div className={result.ok ? 'model-test-success' : 'model-test-error'} role={result.ok ? 'status' : 'alert'}><strong>{result.message}</strong><span>{result.model} · {result.protocol === 'responses' ? 'Responses' : 'Chat Completions'} · {result.elapsed_ms} ms</span></div> : null}
        </div>
        <div className="model-dialog-footer">
          <p>测试只发送固定短文本，可能产生少量 API 费用</p>
          <div><button type="button" className="model-test-button" disabled={!ready || Boolean(busy)} onClick={() => void submit('test')}>{busy === 'test' ? <LoaderCircle size={15} /> : <FlaskConical size={15} />}{busy === 'test' ? '测试中…' : '测试连接'}</button><button type="submit" className="model-save-button" disabled={!ready || Boolean(busy)}>{busy === 'save' ? '保存中…' : '保存'}</button></div>
        </div>
      </form>
      </div>
      <div role="tabpanel" id="settings-panel-ocr" aria-labelledby="settings-tab-ocr" hidden={tab!=='ocr'}>{ocrVisited?<OcrSettings/>:null}</div>
    </dialog>, document.body,
  )
}
