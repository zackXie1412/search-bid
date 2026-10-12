import { render, screen } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { describe, expect, it, vi } from 'vitest'
import type { CrawlJob } from '../types'
import { DeleteJobModal } from './DeleteJobModal'

const failedJob = {
  id: 'failed', job_type: 'manual', start_date: '2026-06-04', end_date: '2026-06-04',
  status: 'failed', current_date: null, current_page: null, current_total_pages: null,
  current_title: null, dates_total: 1, dates_completed: 0, pages_total: 0,
  pages_completed: 0, new_count: 0, duplicate_count: 0, parse_error_count: 0,
  filtered_count: 0, error_message: '失败', created_at: '2026-09-20T00:00:00Z',
  started_at: null, finished_at: null,
} satisfies CrawlJob

describe('DeleteJobModal', () => {
  it('explains retained data and confirms the selected job', async () => {
    const onConfirm = vi.fn().mockResolvedValue(undefined)
    render(<DeleteJobModal job={failedJob} onClose={vi.fn()} onConfirm={onConfirm} />)

    expect(screen.getByRole('dialog')).toHaveTextContent('已采集的公告数据会保留')
    await userEvent.click(screen.getByRole('button', { name: '删除任务' }))
    expect(onConfirm).toHaveBeenCalledWith(failedJob)
  })
})
