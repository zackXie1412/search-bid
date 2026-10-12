export type OCRConfig = {enabled: boolean; provider: 'paddle_aistudio'; model: 'PP-OCRv5'; access_token_configured: boolean}
export type OCRDraft = Omit<OCRConfig,'access_token_configured'> & {access_token?: string; clear_access_token: boolean}
export type OCRResult = {ok: boolean; text: string; elapsed_ms: number; message: string}
async function request<T>(path:string,signal:AbortSignal,method='GET',body?:OCRDraft):Promise<T>{
  let response:Response
  try{response=await fetch(`/api/ocr/${path}`,{method,signal,cache:'no-store',...(body?{headers:{'Content-Type':'application/json'},body:JSON.stringify(body)}:{})})}
  catch(error){if(signal.aborted)throw error;throw new Error('无法连接本地 OCR 配置服务')}
  const data=await response.json().catch(()=>null)
  if(!response.ok||!data){const detail=data?.detail;throw new Error(typeof detail==='string'?detail:Array.isArray(detail)?detail.map(e=>e.msg).join('；'):'OCR 配置请求失败')}
  return data as T
}
export const getOCRConfig=(signal:AbortSignal)=>request<OCRConfig>('config',signal)
export const saveOCRConfig=(draft:OCRDraft,signal:AbortSignal)=>request<OCRConfig>('config',signal,'PUT',draft)
export const testOCRConfig=(draft:OCRDraft,signal:AbortSignal)=>request<OCRResult>('test',signal,'POST',draft)
