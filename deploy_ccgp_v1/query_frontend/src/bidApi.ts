import type { QueryFilters } from './api'
import type { AIConfig } from './aiApi'

export type ScreeningRules = { skill_id: string; skill_name: string; skill_sha256: string; protocol_version: number }
export type BidDecision = {
  protocol_version?: number
  reading?: { scope: string; read_at: string; sha256: string; attachments: { name: string; url: string | null; read: boolean }[] }
  coverage?: { identified: number; checked: number; scope: string }
  coverage_gaps?: { clause_id: string; text: string }[]
  groups?: { id: string; operator: 'and' | 'or' | 'unknown'; members: string[]; context: string; assessment: string; validated: boolean }[]
  verdict: 'retain' | 'exclude' | 'needs_review'
  summary: string
  pending_checks: string[]
  requirements: { reference_only?: boolean; not_applicable?: boolean; condition_id?: string; clause_ids?: string[]; category?: 'general' | 'specific' | 'scoring'; conflict_type?: string; evidence_validated?: boolean; conflict_validated?: boolean; evidence?: { fact_id: string; source_id: string; quote: string; filename?: string; location?: string }[]; requirement: string; mandatory: boolean; announcement_quote: string; assessment: 'met' | 'conflict' | 'unknown'; company_fact_ids: string[]; company_quote: string; reason: string; review_required?: boolean; review_issues?: {kind: 'subject_identity' | 'qualification'; text: string}[] }[]
}
export type BidTask = {
  id: string; status: string; knowledge_revision_id?: string | null; filters: { keyword: string | null; region_code: string | null; start_date: string | null; end_date: string | null }
  total: number; completed: number; retained: number; needs_review: number; excluded: number; failed: number
  estimated_calls: number; has_profile: boolean; current_title: string; error: string
  can_retry_preparation?: boolean
  screening_rules?: ScreeningRules
  legacy_screening?: boolean
  profile_warnings?: string[]
  has_legacy_decisions?: boolean
  diagnostic?: { phase?: string; attempt?: number; max_attempts?: number; retrying?: boolean; retry_in_seconds?: number; status_code?: number | null; category?: string; elapsed_ms?: number }
  model_config: { base_url: string; model: string; protocol: string }; file_names: string[]; created_at: string
  failures: { message: string; count: number }[]
  items: { notice: { id: number; title: string; canonical_url: string; region_raw: string | null; published_at: string }; decision: BidDecision }[]
  page: number; has_next: boolean
}

async function request<T>(path: string, signal: AbortSignal, method = 'GET', body?: FormData): Promise<T> {
  let response: Response
  try { response = await fetch(`/api/bid-check/tasks${path}`, { method, body, signal, cache: 'no-store' }) }
  catch (caught) { if (signal.aborted) throw caught; throw new Error('无法连接本地检标服务，请确认服务已启动') }
  const data = await response.json().catch(() => null)
  if (!response.ok || !data) throw new Error(typeof data?.detail === 'string' ? data.detail : '检标请求失败，请重试')
  return data as T
}

export function createBidTask(filters: QueryFilters, files: File[], signal: AbortSignal, config: AIConfig, knowledgeRevisionId?: string) {
  const body = new FormData()
  body.set('expected_config', JSON.stringify({ base_url: config.base_url, model: config.model, protocol: config.protocol }))
  if (filters.keyword.trim()) body.set('keyword', filters.keyword.trim())
  if (filters.regionCode) body.set('region_code', filters.regionCode)
  if (filters.startDate) body.set('start_date', filters.startDate)
  if (filters.endDate) body.set('end_date', filters.endDate)
  if (knowledgeRevisionId) body.set('knowledge_revision_id', knowledgeRevisionId)
  files.forEach((file) => body.append('files', file))
  return request<{ id: string; total: number; estimated_calls: number }>('', signal, 'POST', body)
}
export const listBidTasks = (signal: AbortSignal) => request<{ items: BidTask[] }>('?limit=20', signal)
export const getBidTask = (id: string, page: number, signal: AbortSignal) => request<BidTask>(`/${id}?page=${page}`, signal)
export const actOnBidTask = (id: string, action: 'start' | 'stop' | 'retry', signal: AbortSignal) => request<BidTask>(`/${id}/${action}`, signal, 'POST')

export async function getBidRules(signal: AbortSignal): Promise<ScreeningRules> {
  const response = await fetch('/api/bid-check/rules', { signal, cache: 'no-store' })
  const data = await response.json().catch(() => null)
  if (!response.ok || !data?.skill_id || !data?.skill_sha256) throw new Error(data?.detail || '资格初筛规则不可用，请检查本地服务')
  return data as ScreeningRules
}
