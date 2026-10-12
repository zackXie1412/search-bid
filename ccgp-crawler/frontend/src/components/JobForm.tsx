import { CalendarDays, Play } from 'lucide-react'
import { FormEvent, useState } from 'react'
import { beijingDate } from '../utils'

export function JobForm({ onCreate, busy }: { onCreate: (start: string, end: string) => Promise<void>; busy: boolean }) {
  const [start, setStart] = useState(beijingDate(-1))
  const [end, setEnd] = useState(beijingDate())
  const [error, setError] = useState('')
  const [submitting, setSubmitting] = useState(false)

  async function submit(event: FormEvent) {
    event.preventDefault()
    const today = beijingDate()
    if (!start || !end) return setError('请选择完整日期范围')
    if (start > end) return setError('开始日期不能晚于结束日期')
    if (end > today) return setError('结束日期不能晚于北京时间今天')
    setError('')
    setSubmitting(true)
    try {
      await onCreate(start, end)
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : '创建任务失败')
    } finally {
      setSubmitting(false)
    }
  }

  return (
    <section className="panel start-panel" aria-labelledby="start-heading">
      <div className="section-heading">
        <div>
          <p className="eyebrow">手动采集</p>
          <h2 id="start-heading">选择日期范围</h2>
          <p>按北京时间逐日切片，每个日期从第 1 页开始采集。</p>
        </div>
        <span className="fixed-type">仅公开招标</span>
      </div>
      <form className="date-form" onSubmit={submit}>
        <label>
          <span>开始日期</span>
          <div className="date-input"><CalendarDays size={17} /><input aria-label="开始日期" type="date" value={start} max={beijingDate()} onChange={(e) => setStart(e.target.value)} /></div>
        </label>
        <span className="date-arrow" aria-hidden="true">至</span>
        <label>
          <span>结束日期</span>
          <div className="date-input"><CalendarDays size={17} /><input aria-label="结束日期" type="date" value={end} max={beijingDate()} onChange={(e) => setEnd(e.target.value)} /></div>
        </label>
        <button className="primary-button" type="submit" disabled={submitting}>
          <Play size={17} fill="currentColor" /> {submitting ? '正在创建…' : busy ? '加入采集队列' : '开始抓取'}
        </button>
      </form>
      {error && <p className="form-error" role="alert">{error}</p>}
      {busy && <p className="queue-note">当前已有任务运行，新任务会进入队列，不会并发打开第二个浏览器。</p>}
    </section>
  )
}

