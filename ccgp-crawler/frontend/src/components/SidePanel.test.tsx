import { render, screen } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { describe, expect, it, vi } from 'vitest'
import type { CrawlJob } from '../types'
import { SidePanel } from './SidePanel'

const baseJob: CrawlJob = {
  id: 'job', job_type: 'manual', start_date: '2026-06-04', end_date: '2026-06-04',
  status: 'failed', current_date: '2026-06-04', current_page: 1, current_total_pages: null,
  current_title: null, dates_total: 1, dates_completed: 0, pages_total: 0,
  pages_completed: 0, new_count: 0, duplicate_count: 0, parse_error_count: 0,
  filtered_count: 0, error_message: '失败', created_at: '2026-09-20T00:00:00Z',
  started_at: null, finished_at: null,
}

describe('SidePanel task history', () => {
  it('shows delete only for failed jobs', async () => {
    const onDelete = vi.fn()
    render(
      <SidePanel
        schedule={null}
        jobs={[baseJob, { ...baseJob, id: 'completed', status: 'completed', start_date: '2026-06-03' }]}
        onToggle={vi.fn()}
        onSelect={vi.fn()}
        onDelete={onDelete}
      />,
    )

    const deleteButton = screen.getByRole('button', { name: '删除 2026-06-04 至 2026-06-04 的失败任务' })
    expect(screen.getAllByTitle('删除失败任务')).toHaveLength(1)
    await userEvent.click(deleteButton)
    expect(onDelete).toHaveBeenCalledWith(baseJob)
  })
})
