import { render, screen } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { describe, expect, it, vi } from 'vitest'
import type { CrawlJob } from '../types'
import { SummaryModal } from './SummaryModal'

const failedJob: CrawlJob = {
  id: 'failed-job', job_type: 'manual', start_date: '2026-06-04', end_date: '2026-06-04',
  status: 'failed', current_date: '2026-06-04', current_page: null, current_total_pages: null,
  current_title: null, dates_total: 1, dates_completed: 0, pages_total: 0,
  pages_completed: 0, new_count: 0, duplicate_count: 0, parse_error_count: 0,
  filtered_count: 0, error_message: 'Page.goto: Target page, context or browser has been closed',
  created_at: '2026-09-20T00:00:00Z', started_at: '2026-09-20T00:00:00Z',
  finished_at: '2026-09-20T00:00:01Z',
}

describe('SummaryModal', () => {
  it('lets a failed task resume directly from the blocking dialog', async () => {
    const onResume = vi.fn().mockResolvedValue(undefined)
    render(<SummaryModal job={failedJob} onClose={vi.fn()} onResume={onResume} />)

    await userEvent.click(screen.getByRole('button', { name: '重新打开并继续' }))

    expect(onResume).toHaveBeenCalledWith(failedJob)
  })

  it('keeps completed tasks as acknowledgement-only', () => {
    render(
      <SummaryModal
        job={{ ...failedJob, status: 'completed', error_message: null }}
        onClose={vi.fn()}
        onResume={vi.fn()}
      />,
    )

    expect(screen.getByRole('button', { name: '知道了' })).toBeInTheDocument()
    expect(screen.queryByRole('button', { name: '重新打开并继续' })).not.toBeInTheDocument()
  })
})
