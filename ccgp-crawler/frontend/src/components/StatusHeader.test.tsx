import { render, screen } from '@testing-library/react'
import { describe, expect, it } from 'vitest'
import { StatusHeader } from './StatusHeader'
import type { SystemStatus } from '../types'

const status: SystemStatus = {
  service: 'online',
  worker: 'stopped',
  sqlite: 'connected',
  chrome: 'ready',
  source: 'fake',
  database_path: '/tmp/test.sqlite3',
  throttle: {
    state: 'ready', day_key: '2026-09-24', day_request_count: 0,
    daily_limit: 240, batch_request_count: 0, next_request_at: null,
    next_job_at: null, cooldown_until: null, wait_until: null,
    active_job_id: null, resume_required: 0, confirmed_job_id: null,
    pause_resume_job_id: null,
  },
}

describe('StatusHeader', () => {
  it('shows when the crawl worker has stopped', () => {
    render(<StatusHeader status={status} />)
    expect(screen.getByText('采集线程')).toBeInTheDocument()
    expect(screen.getByText('已停止')).toBeInTheDocument()
  })
})
