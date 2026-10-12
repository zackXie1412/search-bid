import { render, screen } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { describe, expect, it, vi } from 'vitest'
import { JobForm } from './JobForm'

describe('JobForm', () => {
  it('rejects a reversed date range', async () => {
    const user = userEvent.setup()
    const create = vi.fn()
    render(<JobForm onCreate={create} busy={false} />)
    await user.clear(screen.getByLabelText('开始日期'))
    await user.type(screen.getByLabelText('开始日期'), '2026-09-20')
    await user.clear(screen.getByLabelText('结束日期'))
    await user.type(screen.getByLabelText('结束日期'), '2026-09-19')
    await user.click(screen.getByRole('button', { name: '开始抓取' }))
    expect(screen.getByRole('alert')).toHaveTextContent('开始日期不能晚于结束日期')
    expect(create).not.toHaveBeenCalled()
  })
})

