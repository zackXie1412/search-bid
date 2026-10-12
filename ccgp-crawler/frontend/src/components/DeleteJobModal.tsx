import { useState } from 'react'
import { LoaderCircle, Trash2, X } from 'lucide-react'
import type { CrawlJob } from '../types'

interface DeleteJobModalProps {
  job: CrawlJob | null
  onClose: () => void
  onConfirm: (job: CrawlJob) => Promise<void>
}

export function DeleteJobModal({ job, onClose, onConfirm }: DeleteJobModalProps) {
  const [deleting, setDeleting] = useState(false)
  const [error, setError] = useState<string | null>(null)
  if (!job) return null

  async function handleConfirm() {
    if (!job || deleting) return
    setDeleting(true)
    setError(null)
    try {
      await onConfirm(job)
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : '删除失败，请稍后重试')
      setDeleting(false)
    }
  }

  return (
    <div className="modal-backdrop" role="presentation" onMouseDown={(event) => event.target === event.currentTarget && !deleting && onClose()}>
      <div className="modal delete-modal" role="dialog" aria-modal="true" aria-labelledby="delete-job-title">
        <button className="modal-close" aria-label="关闭" onClick={onClose} disabled={deleting}><X size={19} /></button>
        <div className="modal-icon failure"><Trash2 size={26} /></div>
        <p className="eyebrow">任务历史</p>
        <h2 id="delete-job-title">删除失败任务？</h2>
        <p className="modal-range">{job.start_date} — {job.end_date}</p>
        <p className="delete-explanation">任务将从历史列表中移除，对应的断点和错误记录也会删除。已采集的公告数据会保留。</p>
        {error && <p className="modal-error" role="alert">{error}</p>}
        <div className="modal-actions">
          <button className="secondary-button modal-button" onClick={onClose} disabled={deleting}>取消</button>
          <button className="primary-button danger modal-button" onClick={handleConfirm} disabled={deleting}>
            {deleting ? <LoaderCircle className="spin" size={17} /> : <Trash2 size={17} />}
            {deleting ? '正在删除' : '删除任务'}
          </button>
        </div>
      </div>
    </div>
  )
}
