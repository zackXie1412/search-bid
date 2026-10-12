export type AIProtocol = 'responses' | 'chat_completions'

export type AIConfig = {
  base_url: string
  model: string
  protocol: AIProtocol
  api_key_configured: boolean
}

export type AIConfigDraft = Omit<AIConfig, 'api_key_configured'> & {
  api_key?: string
  clear_api_key: boolean
}

export type AIConnectionResult = {
  ok: boolean
  model: string
  protocol: AIProtocol
  elapsed_ms: number
  message: string
}

async function request<T>(path: string, signal: AbortSignal, payload?: AIConfigDraft, method = 'GET'): Promise<T> {
  let response: Response
  try {
    response = await fetch(`/api/ai/${path}`, {
      method, signal, cache: 'no-store',
      ...(payload ? { headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(payload) } : {}),
    })
  } catch (error) {
    if (signal.aborted) throw error
    throw new Error('无法连接本地服务，请确认服务已启动')
  }
  const data = await response.json().catch(() => null)
  if (!response.ok) {
    const detail = data?.detail
    const message = typeof detail === 'string' ? detail : Array.isArray(detail)
      ? detail.map((item) => item.msg).filter((item) => typeof item === 'string').join('；') : ''
    throw new Error(message || '模型配置请求失败，请稍后重试')
  }
  if (!data || typeof data !== 'object') throw new Error('本地服务返回格式无效，请重试')
  return data as T
}

export const getAIConfig = (signal: AbortSignal) => request<AIConfig>('config', signal)
export const saveAIConfig = (draft: AIConfigDraft, signal: AbortSignal) => request<AIConfig>('config', signal, draft, 'PUT')
export const testAIConnection = (draft: AIConfigDraft, signal: AbortSignal) => request<AIConnectionResult>('test', signal, draft, 'POST')
