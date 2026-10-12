import type { CrawlJob, Notice, ScheduleStatus, SyncStatus, SystemStatus } from './types'

async function request<T>(path: string, init?: RequestInit): Promise<T> {
  const response = await fetch(path, {
    ...init,
    headers: { 'Content-Type': 'application/json', ...init?.headers },
  })
  if (!response.ok) {
    const body = await response.json().catch(() => null)
    throw new Error(body?.detail ?? `请求失败（${response.status}）`)
  }
  if (response.status === 204) return undefined as T
  return response.json() as Promise<T>
}

export const api = {
  system: () => request<SystemStatus>('/api/system'),
  jobs: () => request<{ items: CrawlJob[] }>('/api/jobs'),
  job: (id: string) => request<CrawlJob>(`/api/jobs/${id}`),
  notices: (id: string) => request<{ items: Notice[] }>(`/api/jobs/${id}/notices`),
  createJob: (start_date: string, end_date: string) =>
    request<CrawlJob>('/api/jobs', {
      method: 'POST',
      body: JSON.stringify({ start_date, end_date }),
    }),
  stopJob: (id: string) => request<CrawlJob>(`/api/jobs/${id}/stop`, { method: 'POST' }),
  resumeJob: (id: string) => request<CrawlJob>(`/api/jobs/${id}/resume`, { method: 'POST' }),
  deleteJob: (id: string) => request<void>(`/api/jobs/${id}`, { method: 'DELETE' }),
  schedule: () => request<ScheduleStatus>('/api/schedule'),
  updateSchedule: (enabled: boolean) =>
    request<ScheduleStatus>('/api/schedule', {
      method: 'PUT',
      body: JSON.stringify({ enabled }),
    }),
  syncStatus: () => request<SyncStatus>('/api/sync/status'),
  startSync: () => request<SyncStatus>('/api/sync/start', { method: 'POST' }),
  retryFailedSync: () => request<{ reset: number; status: SyncStatus }>('/api/sync/retry-failed', { method: 'POST' }),
}
