import { render, screen } from '@testing-library/react'
import { describe, expect, it, vi } from 'vitest'
import { ActiveJob } from './ActiveJob'
import type { CrawlJob } from '../types'

const job: CrawlJob = {
  id: '1', job_type: 'manual', start_date: '2026-09-19', end_date: '2026-09-20',
  status: 'running', current_date: '2026-09-19', current_page: 2, current_total_pages: 4,
  current_title: '测试公告', dates_total: 2, dates_completed: 1, pages_total: 4,
  pages_completed: 2, new_count: 21, duplicate_count: 3, parse_error_count: 1,
  filtered_count: 0, error_message: null, created_at: new Date().toISOString(),
  started_at: new Date().toISOString(), finished_at: null,
}

describe('ActiveJob', () => {
  it('renders honest date and page progress', () => {
    render(<ActiveJob job={job} onStop={vi.fn()} onResume={vi.fn()} />)
    expect(screen.getByText('1 / 2 天')).toBeInTheDocument()
    expect(screen.getByText('2 / 4 页')).toBeInTheDocument()
    expect(screen.getByText('测试公告')).toBeInTheDocument()
    expect(screen.getByRole('button', { name: /停止/ })).toBeEnabled()
  })

  it('disables resume while the site is cooling down', () => {
    render(
      <ActiveJob
        job={{ ...job, status: 'blocked' }}
        throttle={{
          state: 'cooldown', day_key: '2026-09-22', day_request_count: 10,
          daily_limit: 240, batch_request_count: 10, next_request_at: null,
          next_job_at: null, cooldown_until: '2026-09-22T08:00:00+00:00',
          wait_until: '2026-09-22T08:00:00+00:00', active_job_id: null,
          resume_required: 1, confirmed_job_id: null, pause_resume_job_id: null,
        }}
        onStop={vi.fn()}
        onResume={vi.fn()}
      />,
    )
    expect(screen.getByRole('button', { name: /冷却至/ })).toBeDisabled()
  })

  it('explains queued global throttling', () => {
    render(
      <ActiveJob
        job={{ ...job, status: 'queued', current_title: null }}
        throttle={{
          state: 'waiting', day_key: '2026-09-22', day_request_count: 20,
          daily_limit: 240, batch_request_count: 0,
          next_request_at: '2026-09-22T02:04:00+00:00', next_job_at: null,
          cooldown_until: null, wait_until: '2026-09-22T02:04:00+00:00',
          active_job_id: null, resume_required: 0, confirmed_job_id: null,
          pause_resume_job_id: null,
        }}
        onStop={vi.fn()}
        onResume={vi.fn()}
      />,
    )
    expect(screen.getByText('等待全局节流')).toBeInTheDocument()
  })

  it('offers immediate resume for the same manually paused job', () => {
    render(
      <ActiveJob
        job={{ ...job, status: 'stopped', current_title: '等待全局节流' }}
        throttle={{
          state: 'waiting', day_key: '2026-09-24', day_request_count: 28,
          daily_limit: 240, batch_request_count: 8,
          next_request_at: null, next_job_at: '2026-09-24T12:22:44+00:00',
          cooldown_until: null, wait_until: '2026-09-24T12:22:44+00:00',
          active_job_id: null, resume_required: 0, confirmed_job_id: null,
          pause_resume_job_id: job.id,
        }}
        onStop={vi.fn()}
        onResume={vi.fn()}
      />,
    )
    expect(screen.getByText('已暂停，可立即从第 3 页继续')).toBeInTheDocument()
    expect(screen.getByRole('button', { name: '立即从断点继续' })).toBeEnabled()
  })
})
