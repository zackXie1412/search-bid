import { act, render, screen, waitFor } from '@testing-library/react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import App from './App'
import type { CrawlJob, SyncStatus } from './types'

const baseJob: CrawlJob = {
  id: 'job-1', job_type: 'manual', start_date: '2026-09-19', end_date: '2026-09-20',
  status: 'running', current_date: '2026-09-19', current_page: 1, current_total_pages: 2,
  current_title: '正在抓取测试公告', dates_total: 2, dates_completed: 0, pages_total: 2,
  pages_completed: 1, new_count: 20, duplicate_count: 0, parse_error_count: 0,
  filtered_count: 0, error_message: null, created_at: '2026-09-20T00:00:00Z',
  started_at: '2026-09-20T00:00:00Z', finished_at: null,
}

const baseSync: SyncStatus = {
  connection: { state: 'unavailable', message: 'SSH 隧道未连接或云端服务不可达' },
  counts: {
    pending: 12, syncing: 0, synced: 8, failed: 0, total: 20,
    synced_batches: 1, failed_batches: 0, last_success_at: null, last_error: null,
  },
  run: {
    status: 'idle', current_batch_key: null, current_batch_size: 0,
    current_batch_completed: 0, batches_completed: 0, synced: 0,
    inserted: 0, updated: 0, replayed_batches: 0, started_at: null,
    finished_at: null, last_error: null,
  },
}

class FakeEventSource {
  static instances: FakeEventSource[] = []
  listeners = new Map<string, Array<() => void>>()
  onerror: (() => void) | null = null
  constructor(public url: string) { FakeEventSource.instances.push(this) }
  addEventListener(name: string, callback: () => void) {
    this.listeners.set(name, [...(this.listeners.get(name) ?? []), callback])
  }
  close() {}
  emit(name: string) { this.listeners.get(name)?.forEach((callback) => callback()) }
}

function response(body: unknown) {
  return Promise.resolve({ ok: true, json: () => Promise.resolve(body) } as Response)
}

describe('App SSE recovery', () => {
  let currentJob: CrawlJob
  let fetchMock: ReturnType<typeof vi.fn>

  beforeEach(() => {
    currentJob = { ...baseJob }
    FakeEventSource.instances = []
    vi.stubGlobal('EventSource', FakeEventSource)
    fetchMock = vi.fn((input: RequestInfo | URL) => {
      const path = String(input)
      if (path === '/api/system') return response({ service: 'online', worker: 'running', sqlite: 'connected', chrome: 'running', source: 'fake', database_path: '/tmp/test.sqlite3' })
      if (path === '/api/jobs') return response({ items: [currentJob] })
      if (path === '/api/schedule') return response({ enabled: true, run_time: '02:00', timezone: 'Asia/Shanghai', last_success_date: null, next_run_at: null })
      if (path === '/api/sync/status') return response(baseSync)
      if (path.endsWith('/notices')) return response({ items: [] })
      if (path === '/api/jobs/job-1') return response(currentJob)
      throw new Error(`unexpected request: ${path}`)
    })
    vi.stubGlobal('fetch', fetchMock)
  })

  afterEach(() => vi.unstubAllGlobals())

  it('refreshes after disconnect and shows the completion summary from SSE', async () => {
    render(<App />)
    expect((await screen.findAllByText('采集中')).length).toBeGreaterThan(0)
    await waitFor(() => expect(FakeEventSource.instances).toHaveLength(1))
    const source = FakeEventSource.instances[0]
    const beforeReconnect = fetchMock.mock.calls.filter(([url]) => String(url) === '/api/jobs/job-1').length
    await act(async () => source.onerror?.())
    await waitFor(() => {
      const after = fetchMock.mock.calls.filter(([url]) => String(url) === '/api/jobs/job-1').length
      expect(after).toBeGreaterThan(beforeReconnect)
    })

    currentJob = { ...currentJob, status: 'completed', dates_completed: 2, pages_completed: 4, finished_at: '2026-09-20T00:01:00Z' }
    await act(async () => source.emit('completed'))
    expect(await screen.findByRole('dialog')).toHaveTextContent('采集已完成')
  })

  it('refreshes the persisted retry message from a page_retrying event', async () => {
    render(<App />)
    await waitFor(() => expect(FakeEventSource.instances).toHaveLength(1))
    const source = FakeEventSource.instances[0]
    currentJob = {
      ...currentJob,
      current_title: '第 29 页未收到响应，10 秒后进行第 1/2 次自动重试',
    }

    await act(async () => source.emit('page_retrying'))

    expect(await screen.findByText('第 29 页未收到响应，10 秒后进行第 1/2 次自动重试')).toBeInTheDocument()
    expect(screen.getAllByText('采集中').length).toBeGreaterThan(0)
  })
})
