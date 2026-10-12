import {useEffect,useRef,useState} from 'react'
import {getOCRConfig,saveOCRConfig,testOCRConfig,type OCRConfig,type OCRDraft,type OCRResult} from '../ocrApi'

const defaults:OCRConfig={enabled:false,provider:'paddle_aistudio',model:'PP-OCRv5',access_token_configured:false}
export function OcrSettings(){
  const [saved,setSaved]=useState(defaults)
  const [draft,setDraft]=useState<OCRDraft>({...defaults,access_token:'',clear_access_token:false})
  const [ready,setReady]=useState(false)
  const [busy,setBusy]=useState<'load'|'save'|'test'|''>('load')
  const [error,setError]=useState('')
  const [notice,setNotice]=useState('')
  const [result,setResult]=useState<OCRResult|null>(null)
  const requestRef=useRef<AbortController|null>(null)
  async function load(){
    requestRef.current?.abort();const controller=new AbortController();requestRef.current=controller;setBusy('load');setError('')
    try{const config=await getOCRConfig(controller.signal);if(controller.signal.aborted)return;setSaved(config);setDraft({enabled:config.enabled,provider:config.provider,model:config.model,access_token:'',clear_access_token:false});setReady(true)}
    catch(e){if(!controller.signal.aborted)setError(e instanceof Error?e.message:'读取 OCR 配置失败')}
    finally{if(!controller.signal.aborted)setBusy('')}
  }
  useEffect(()=>{void load();return()=>requestRef.current?.abort()},[])
  function update(changes:Partial<OCRDraft>){setDraft(current=>({...current,...changes}));setError('');setNotice('');setResult(null)}
  async function submit(action:'save'|'test'){
    if(!ready||busy)return
    const token=draft.access_token?.trim()||''
    setError('');setNotice('');setResult(null)
    if(action==='test'&&!token&&(!saved.access_token_configured||draft.clear_access_token)){setError('请填写 AI Studio Access Token 后测试');return}
    const payload:OCRDraft={enabled:draft.enabled,provider:'paddle_aistudio',model:'PP-OCRv5',clear_access_token:draft.clear_access_token,...(token?{access_token:token}:{})}
    const controller=new AbortController();requestRef.current=controller;setBusy(action)
    try{
      if(action==='save'){const config=await saveOCRConfig(payload,controller.signal);if(controller.signal.aborted)return;setSaved(config);setDraft({...payload,access_token:'',clear_access_token:false});setNotice('OCR 配置已保存')}
      else{const tested=await testOCRConfig(payload,controller.signal);if(!controller.signal.aborted)setResult(tested)}
    }catch(e){if(!controller.signal.aborted)setError(e instanceof Error?e.message:'OCR 请求失败')}
    finally{if(!controller.signal.aborted)setBusy('')}
  }
  return <form onSubmit={e=>{e.preventDefault();void submit('save')}}>
    <fieldset className="model-fields" disabled={!ready||Boolean(busy)}>
      <label className="ocr-enable"><input type="checkbox" checked={draft.enabled} onChange={e=>update({enabled:e.target.checked})}/>拆解图片时启用 OCR</label>
      <p className="ocr-provider">飞桨官方 AI Studio · PP-OCRv5</p>
      <small>启用后，图片先提交给飞桨识别，AI 只接收识别文字与文件出处。图片会发送到官方云端，可能消耗 OCR 配额。</small>
      <div className="model-key-label"><label htmlFor="ocr-access-token">Access Token</label><span>{draft.clear_access_token?'待清除':saved.access_token_configured?'已配置':'未配置'}</span></div>
      <input id="ocr-access-token" type="password" value={draft.access_token||''} maxLength={8192} autoComplete="new-password" spellCheck={false} placeholder={saved.access_token_configured?'已配置，留空保持不变':'请输入 AI Studio Access Token'} onChange={e=>update({access_token:e.target.value,...(e.target.value?{clear_access_token:false}:{})})}/>
      <div className="model-key-help"><a href="https://aistudio.baidu.com/account/accessToken" target="_blank" rel="noreferrer">获取 AI Studio 令牌</a>{saved.access_token_configured?<button type="button" className="model-clear-key" onClick={()=>update({clear_access_token:!draft.clear_access_token,access_token:''})}>{draft.clear_access_token?'取消清除':'清除令牌'}</button>:null}</div>
      <small>令牌只保存在本机后端，页面不会读取已保存的值。OCR 只在开始拆解后运行，不会因上传自动调用。</small>
    </fieldset>
    <div className="model-feedback" aria-live="polite" aria-busy={Boolean(busy)}>
      {busy==='load'?<p>正在读取 OCR 配置…</p>:null}
      {error?<p className="model-error" role="alert">{error}</p>:null}
      {!ready&&!busy?<button type="button" onClick={()=>void load()}>重新读取</button>:null}
      {notice?<p className="model-success" role="status">{notice}</p>:null}
      {result?<div className={result.ok?'model-test-success':'model-test-error'} role={result.ok?'status':'alert'}><strong>{result.message}</strong><span>PP-OCRv5 · {result.elapsed_ms} ms</span>{result.text?<blockquote className="ocr-test-text">{result.text}</blockquote>:null}</div>:null}
    </div>
    <div className="model-dialog-footer"><p>测试仅提交程序生成的测试图片，不使用企业资料，可能消耗少量 OCR 配额。</p><div><button className="model-test-button" type="button" disabled={!ready||Boolean(busy)} onClick={()=>void submit('test')}>{busy==='test'?'测试中…':'测试连接'}</button><button className="model-save-button" type="submit" disabled={!ready||Boolean(busy)}>{busy==='save'?'保存中…':'保存'}</button></div></div>
  </form>
}
