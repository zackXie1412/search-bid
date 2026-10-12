import { useState } from 'react'
import { CheckCircle2, CircleAlert, LoaderCircle, RotateCcw, X } from 'lucide-react'
import type { CrawlJob, ThrottleStatus } from '../types'
import { elapsed, formatDateTime } from '../utils'

interface SummaryModalProps {
  job: CrawlJob | null
  onClose: () => void
  onResume: (job: CrawlJob) => Promise<void>
  throttle?: ThrottleStatus | null
}

export function SummaryModal({ job, throttle = null, onClose, onResume }: SummaryModalProps) {
  const [resuming, setResuming] = useState(false)
  const [resumeError, setResumeError] = useState<string | null>(null)
  if (!job) return null
  const success = job.status === 'completed'
  const canResume = ['stopped', 'blocked', 'failed'].includes(job.status)
  const cooling = throttle?.state === 'cooldown'

  async function handleResume() {
    if (!job || resuming) return
    setResuming(true)
    setResumeError(null)
    try {
      await onResume(job)
    } catch (error) {
      setResumeError(error instanceof Error ? error.message : '继续任务失败，请稍后重试')
      setResuming(false)
    }
  }

  return (
    <div className="modal-backdrop" role="presentation" onMouseDown={(e) => e.target === e.currentTarget && onClose()}>
      <div className="modal" role="dialog" aria-modal="true" aria-labelledby="summary-title">
        <button className="modal-close" aria-label="关闭" onClick={onClose}><X size={19} /></button>
        <div className={`modal-icon ${success ? 'success' : 'failure'}`}>{success ? <CheckCircle2 size={28} /> : <CircleAlert size={28} />}</div>
        <p className="eyebrow">采集任务通知</p>
        <h2 id="summary-title">{success ? '采集已完成' : job.status === 'blocked' ? '访问受限，任务已暂停' : '任务未完成'}</h2>
        <p className="modal-range">{job.start_date} — {job.end_date}</p>
        <div className="modal-stats">
          <div><span>总页数</span><strong>{job.pages_completed}</strong></div>
          <div><span>新增</span><strong>{job.new_count}</strong></div>
          <div><span>重复</span><strong>{job.duplicate_count}</strong></div>
          <div><span>失败</span><strong>{job.parse_error_count}</strong></div>
          <div><span>耗时</span><strong>{elapsed(job.started_at, job.finished_at, Date.now())}</strong></div>
        </div>
        {job.error_message && <p className="modal-error">{job.error_message}</p>}
        {resumeError && <p className="modal-error" role="alert">{resumeError}</p>}
        {canResume ? (
          <div className="modal-actions">
            <button className="secondary-button modal-button" onClick={onClose} disabled={resuming}>稍后处理</button>
            <button className="primary-button modal-button" onClick={handleResume} disabled={resuming || cooling}>
              {resuming ? <LoaderCircle className="spin" size={17} /> : <RotateCcw size={17} />}
              {resuming ? '正在重新打开' : cooling ? `冷却至 ${formatDateTime(throttle?.cooldown_until ?? null)}` : '重新打开并继续'}
            </button>
          </div>
        ) : (
          <button className="primary-button modal-button" onClick={onClose}>知道了</button>
        )}
      </div>
    </div>
  )
}
