import { History, Power, TimerReset, Trash2 } from 'lucide-react'
import type { CrawlJob, ScheduleStatus } from '../types'
import { formatDateTime } from '../utils'

const statusText: Record<CrawlJob['status'], string> = {
  queued: '排队中', running: '采集中', stopping: '停止中', stopped: '已停止', blocked: '受限', failed: '失败', completed: '已完成',
}

interface SidePanelProps {
  schedule: ScheduleStatus | null
  jobs: CrawlJob[]
  onToggle: (enabled: boolean) => void
  onSelect: (job: CrawlJob) => void
  onDelete: (job: CrawlJob) => void
}

export function SidePanel({ schedule, jobs, onToggle, onSelect, onDelete }: SidePanelProps) {
  return (
    <aside className="side-stack">
      <section className="panel schedule-panel">
        <div className="side-title"><div className="side-icon"><TimerReset size={18} /></div><div><p className="eyebrow">后台运行</p><h2>每日自动增量</h2></div></div>
        <div className="schedule-time"><strong>02:00</strong><span>北京时间 · 今天与昨天</span></div>
        <label className="switch-row">
          <span><b>{schedule?.enabled ? '已启用' : '已停用'}</b><small>网页关闭后仍由本机服务运行</small></span>
          <input aria-label="每日自动增量" type="checkbox" checked={schedule?.enabled ?? false} onChange={(e) => onToggle(e.target.checked)} />
        </label>
        <dl className="schedule-meta">
          <div><dt>下次运行</dt><dd>{schedule?.next_run_at ? formatDateTime(schedule.next_run_at) : '—'}</dd></div>
          <div><dt>最近成功</dt><dd>{schedule?.last_success_date ?? '尚无记录'}</dd></div>
        </dl>
      </section>
      <section className="panel history-panel">
        <div className="side-title"><div className="side-icon"><History size={18} /></div><div><p className="eyebrow">最近 30 次</p><h2>任务历史</h2></div></div>
        <div className="history-list">
          {jobs.length === 0 && <p className="history-empty">暂无任务</p>}
          {jobs.map((job) => (
            <div key={job.id} className="history-row">
              <button className="history-item" onClick={() => onSelect(job)}>
                <span><b>{job.start_date} → {job.end_date}</b><small>{job.job_type === 'scheduled' ? '自动增量' : '手动任务'} · {job.new_count} 条新增</small></span>
                <span className={`history-status status-${job.status}`}>{statusText[job.status]}</span>
              </button>
              {job.status === 'failed' && (
                <button
                  className="history-delete"
                  aria-label={`删除 ${job.start_date} 至 ${job.end_date} 的失败任务`}
                  title="删除失败任务"
                  onClick={() => onDelete(job)}
                >
                  <Trash2 size={15} />
                </button>
              )}
            </div>
          ))}
        </div>
      </section>
      <div className="local-note"><Power size={15} /><span>仅监听 127.0.0.1，数据不会暴露到局域网。</span></div>
    </aside>
  )
}
