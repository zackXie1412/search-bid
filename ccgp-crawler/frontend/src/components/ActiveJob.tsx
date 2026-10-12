import { Clock3, Pause, Play, RotateCcw } from 'lucide-react'
import { useEffect, useState } from 'react'
import type { CrawlJob, ThrottleStatus } from '../types'
import { elapsed, formatDateTime, percentage } from '../utils'

const labels: Record<CrawlJob['status'], string> = {
  queued: '排队中', running: '采集中', stopping: '正在安全停止', stopped: '已停止',
  blocked: '访问受限', failed: '失败', completed: '已完成',
}

function Progress({ label, detail, value }: { label: string; detail: string; value: number }) {
  return (
    <div className="progress-block">
      <div className="progress-label"><span>{label}</span><strong>{detail}</strong></div>
      <div className="progress-track" role="progressbar" aria-label={label} aria-valuenow={Math.round(value)} aria-valuemin={0} aria-valuemax={100}>
        <span style={{ width: `${value}%` }} />
      </div>
    </div>
  )
}

export function ActiveJob({ job, throttle = null, onStop, onResume }: { job: CrawlJob | null; throttle?: ThrottleStatus | null; onStop: () => void; onResume: () => void }) {
  const [now, setNow] = useState(Date.now())
  useEffect(() => {
    const timer = window.setInterval(() => setNow(Date.now()), 1000)
    return () => window.clearInterval(timer)
  }, [])

  if (!job) {
    return (
      <section className="panel empty-run">
        <div className="empty-icon"><RotateCcw size={22} /></div>
        <div><h2>等待采集任务</h2><p>选择日期并开始后，这里会逐页显示真实进度。</p></div>
      </section>
    )
  }

  const page = job.current_page ?? 0
  const totalPages = job.current_total_pages ?? 0
  const canStop = job.status === 'running' || job.status === 'queued'
  const cooling = throttle?.state === 'cooldown'
  const canResume = ['stopped', 'blocked', 'failed'].includes(job.status)
  const samePausedJob = job.status === 'stopped' && throttle?.pause_resume_job_id === job.id
  const queuedText = throttle?.state === 'daily_limit'
    ? '今日请求额度已用尽，次日从断点继续'
    : throttle?.state === 'waiting' || throttle?.state === 'cooldown'
      ? '等待全局节流'
      : '正在等待前序任务完成'
  const activityText = samePausedJob
    ? `已暂停，可立即从第 ${(job.current_page ?? 0) + 1} 页继续`
    : job.current_title ?? (job.status === 'queued' ? queuedText : '准备读取搜索结果')
  return (
    <section className="panel run-panel" aria-labelledby="run-heading">
      <div className="run-header">
        <div>
          <div className="run-title-line">
            <span className={`status-badge status-${job.status}`}>{labels[job.status]}</span>
            <h2 id="run-heading">{job.start_date} — {job.end_date}</h2>
          </div>
          <p className="current-title">{activityText}</p>
        </div>
        <div className="run-actions">
          <span className="elapsed"><Clock3 size={16} /> {elapsed(job.started_at, job.finished_at, now)}</span>
          {canStop && <button className="secondary-button danger" onClick={onStop}><Pause size={16} />停止</button>}
          {canResume && <button className="secondary-button" onClick={onResume} disabled={cooling}><Play size={16} />{cooling ? `冷却至 ${formatDateTime(throttle?.cooldown_until ?? null)}` : samePausedJob ? '立即从断点继续' : '从断点继续'}</button>}
        </div>
      </div>
      <div className="progress-grid">
        <Progress label="日期总进度" detail={`${job.dates_completed} / ${job.dates_total} 天`} value={percentage(job.dates_completed, job.dates_total)} />
        <Progress label={job.current_date ? `当前日期 · ${job.current_date}` : '当前日期'} detail={totalPages ? `${page} / ${totalPages} 页` : '等待页数'} value={percentage(page, totalPages)} />
      </div>
      <div className="stats-grid">
        <div><span>新增</span><strong>{job.new_count}</strong></div>
        <div><span>重复</span><strong>{job.duplicate_count}</strong></div>
        <div><span>解析失败</span><strong className={job.parse_error_count ? 'text-warn' : ''}>{job.parse_error_count}</strong></div>
        <div><span>已完成页</span><strong>{job.pages_completed}</strong></div>
        <div><span>剩余日期</span><strong>{Math.max(0, job.dates_total - job.dates_completed)}</strong></div>
      </div>
      {job.error_message && <div className="job-error" role="alert">{job.error_message}</div>}
    </section>
  )
}
