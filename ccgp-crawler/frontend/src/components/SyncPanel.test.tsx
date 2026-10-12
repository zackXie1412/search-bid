import { cleanup, render, screen } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { afterEach, describe, expect, it, vi } from 'vitest'
import type { SyncStatus } from '../types'
import { SyncPanel } from './SyncPanel'

function status(overrides: Partial<SyncStatus> = {}): SyncStatus {
  return {
    connection: { state: 'connected', message: '云端写入 API 已连接' },
    counts: {
      pending: 16822,
      syncing: 0,
      synced: 20,
      failed: 0,
      total: 16842,
      synced_batches: 1,
      failed_batches: 0,
      last_success_at: '2026-09-23T06:00:00+00:00',
      last_error: null,
    },
    run: {
      status: 'idle',
      current_batch_key: null,
      current_batch_size: 0,
      current_batch_completed: 0,
      batches_completed: 0,
      synced: 0,
      inserted: 0,
      updated: 0,
      replayed_batches: 0,
      started_at: null,
      finished_at: null,
      last_error: null,
    },
    ...overrides,
  }
}

const noOp = () => Promise.resolve()

afterEach(cleanup)

describe('SyncPanel', () => {
  it('显示连接状态和四类本地数量', () => {
    render(<SyncPanel status={status()} onRefresh={noOp} onStart={noOp} onRetry={noOp} />)

    expect(screen.getByText('已连接')).toBeInTheDocument()
    for (const label of ['待上传', '上传中', '本地已上传', '失败']) {
      expect(screen.getByText(label)).toBeInTheDocument()
    }
    expect(screen.getByRole('button', { name: '上传待同步数据' })).toBeEnabled()
  })

  it('点击上传只触发一次开始请求', async () => {
    const user = userEvent.setup()
    const onStart = vi.fn(() => Promise.resolve())
    render(<SyncPanel status={status()} onRefresh={noOp} onStart={onStart} onRetry={noOp} />)

    await user.click(screen.getByRole('button', { name: '上传待同步数据' }))

    expect(onStart).toHaveBeenCalledTimes(1)
  })

  it('上传中禁用重复启动并显示批次进度', () => {
    const running = status({
      counts: { ...status().counts, pending: 16622, syncing: 200 },
      run: {
        ...status().run,
        status: 'running',
        current_batch_key: 'ccgp-batch',
        current_batch_size: 200,
        current_batch_completed: 0,
        batches_completed: 1,
        synced: 200,
      },
    })
    render(<SyncPanel status={running} onRefresh={noOp} onStart={noOp} onRetry={noOp} />)

    expect(screen.getByRole('button', { name: '正在上传…' })).toBeDisabled()
    expect(screen.getByText('当前批次 0 / 200 · 已完成 1 批')).toBeInTheDocument()
  })

  it('完成后显示本次新增、更新、重放和剩余数量', () => {
    const completed = status({
      counts: { ...status().counts, pending: 15822, synced: 1020 },
      run: {
        ...status().run,
        status: 'completed',
        synced: 1000,
        inserted: 980,
        updated: 20,
        replayed_batches: 1,
      },
    })
    render(<SyncPanel status={completed} onRefresh={noOp} onStart={noOp} onRetry={noOp} />)

    expect(screen.getByText('本次成功上传 1000 条：新增 980，更新 20，重放 1 批，剩余 15822 条')).toBeInTheDocument()
  })

  it('云端断开时保留本地数量并禁用上传', () => {
    const unavailable = status({
      connection: { state: 'unavailable', message: 'SSH 隧道未连接或云端服务不可达' },
    })
    render(<SyncPanel status={unavailable} onRefresh={noOp} onStart={noOp} onRetry={noOp} />)

    expect(screen.getByText('隧道未连接')).toBeInTheDocument()
    expect(screen.getByText('16822')).toBeInTheDocument()
    expect(screen.getByRole('button', { name: '上传待同步数据' })).toBeDisabled()
  })

  it('有终止失败数据时允许用户明确重试', async () => {
    const user = userEvent.setup()
    const onRetry = vi.fn(() => Promise.resolve())
    const failed = status({ counts: { ...status().counts, pending: 16820, failed: 2 } })
    render(<SyncPanel status={failed} onRefresh={noOp} onStart={noOp} onRetry={onRetry} />)

    await user.click(screen.getByRole('button', { name: '重试失败数据' }))

    expect(onRetry).toHaveBeenCalledTimes(1)
  })
})
