import { Cloud, LoaderCircle, RefreshCw, RotateCcw, UploadCloud } from 'lucide-react'
import { useState } from 'react'
import type { SyncStatus } from '../types'
import { formatDateTime, percentage } from '../utils'

const connectionLabels: Record<SyncStatus['connection']['state'], string> = {
  connected: '已连接',
  unavailable: '隧道未连接',
  unauthorized: 'API 鉴权失败',
  error: '云端服务异常',
  not_configured: '未配置云端',
}

interface SyncPanelProps {
  status: SyncStatus | null
  onRefresh: () => Promise<void>
  onStart: () => Promise<void>
  onRetry: () => Promise<void>
}

export function SyncPanel({ status, onRefresh, onStart, onRetry }: SyncPanelProps) {
  const [action, setAction] = useState<'start' | 'retry' | 'refresh' | null>(null)
  const [actionError, setActionError] = useState<string | null>(null)
  const run = status?.run
  const counts = status?.counts
  const running = run?.status === 'running'
  const totalToUpload = (counts?.synced ?? 0) + (counts?.pending ?? 0) + (counts?.syncing ?? 0) + (counts?.failed ?? 0)
  const uploaded = counts?.synced ?? 0
  const progress = totalToUpload ? percentage(uploaded, totalToUpload) : 0

  async function perform(kind: 'start' | 'retry' | 'refresh') {
    if (action) return
    setAction(kind)
    setActionError(null)
    try {
      if (kind === 'start') await onStart()
      else if (kind === 'retry') await onRetry()
      else await onRefresh()
    } catch (error) {
      setActionError(error instanceof Error ? error.message : '操作失败，请稍后再试')
    } finally {
      setAction(null)
    }
  }

  return (
    <section className="panel sync-panel" aria-labelledby="sync-heading">
      <div className="sync-panel-header">
        <div className="side-title"><div className="side-icon sync-icon"><Cloud size={18} /></div><div><p className="eyebrow">SQLite → PostgreSQL</p><h2 id="sync-heading">云端同步</h2></div></div>
        <span className={`sync-connection sync-${status?.connection.state ?? 'error'}`}><span className="sync-connection-dot" />{connectionLabels[status?.connection.state ?? 'error']}</span>
      </div>
      <p className="sync-message">{status?.connection.message ?? '正在检查 SSH 隧道与云端写入 API…'}</p>
      <div className="sync-counts">
        <div><span>待上传</span><strong>{counts?.pending ?? '—'}</strong></div>
        <div><span>上传中</span><strong>{counts?.syncing ?? '—'}</strong></div>
        <div><span>本地已上传</span><strong className="sync-good">{counts?.synced ?? '—'}</strong></div>
        <div><span>失败</span><strong className={counts?.failed ? 'sync-bad' : ''}>{counts?.failed ?? '—'}</strong></div>
      </div>
      <div className="sync-progress-block">
        <div className="progress-label"><span>{running ? '正在上传本地数据' : '本地上传进度'}</span><strong>{counts ? `${uploaded} / ${totalToUpload}` : '—'}</strong></div>
        <div className="progress-track" role="progressbar" aria-label="本地上传进度" aria-valuenow={Math.round(progress)} aria-valuemin={0} aria-valuemax={100}><span style={{ width: `${progress}%` }} /></div>
        {running && <small className="sync-running-detail">当前批次 {run.current_batch_completed} / {run.current_batch_size} · 已完成 {run.batches_completed} 批</small>}
      </div>
      {(run?.status === 'completed' || run?.status === 'failed') && (
        <div className={`sync-result ${run.status === 'failed' ? 'sync-result-error' : ''}`} role={run.status === 'failed' ? 'alert' : undefined}>
          {run.status === 'failed' ? run.last_error : `本次成功上传 ${run.synced} 条：新增 ${run.inserted}，更新 ${run.updated}，重放 ${run.replayed_batches} 批，剩余 ${counts?.pending ?? 0} 条`}
        </div>
      )}
      {actionError && <div className="sync-result sync-result-error" role="alert">{actionError}</div>}
      {!actionError && run?.status !== 'failed' && counts?.last_error && (
        <div className="sync-last-error">最近错误：{counts.last_error}</div>
      )}
      <div className="sync-meta">
        <span>最近成功 {counts?.last_success_at ? formatDateTime(counts.last_success_at) : '尚无记录'}</span>
        <span>本地成功批次 {counts?.synced_batches ?? '—'}</span>
      </div>
      <div className="sync-actions">
        <button className="primary-button sync-upload-button" onClick={() => perform('start')} disabled={running || action !== null || !counts?.pending || status?.connection.state !== 'connected'}>
          {action === 'start' || running ? <LoaderCircle className="spin" size={17} /> : <UploadCloud size={17} />}
          {running ? '正在上传…' : '上传待同步数据'}
        </button>
        {Boolean(counts?.failed) && <button className="secondary-button" onClick={() => perform('retry')} disabled={running || action !== null}><RotateCcw size={16} />重试失败数据</button>}
        <button className="sync-refresh-button" aria-label="刷新同步状态" title="刷新同步状态" onClick={() => perform('refresh')} disabled={action !== null}><RefreshCw className={action === 'refresh' ? 'spin' : ''} size={16} /></button>
      </div>
    </section>
  )
}
