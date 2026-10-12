import { useCallback, useEffect, useRef, useState } from 'react'
import { api } from './api'
import { ActiveJob } from './components/ActiveJob'
import { DeleteJobModal } from './components/DeleteJobModal'
import { JobForm } from './components/JobForm'
import { NoticeTable } from './components/NoticeTable'
import { SidePanel } from './components/SidePanel'
import { StatusHeader } from './components/StatusHeader'
import { SummaryModal } from './components/SummaryModal'
import { SyncPanel } from './components/SyncPanel'
import type { CrawlJob, Notice, ScheduleStatus, SyncStatus, SystemStatus } from './types'

const liveStatuses = new Set(['queued', 'running', 'stopping'])
const terminalEvents = new Set(['completed', 'failed', 'blocked'])

export default function App() {
  const [system, setSystem] = useState<SystemStatus | null>(null)
  const [schedule, setSchedule] = useState<ScheduleStatus | null>(null)
  const [sync, setSync] = useState<SyncStatus | null>(null)
  const [jobs, setJobs] = useState<CrawlJob[]>([])
  const [active, setActive] = useState<CrawlJob | null>(null)
  const [notices, setNotices] = useState<Notice[]>([])
  const [summary, setSummary] = useState<CrawlJob | null>(null)
  const [deleteTarget, setDeleteTarget] = useState<CrawlJob | null>(null)
  const eventSourceRef = useRef<EventSource | null>(null)

  const refreshJob = useCallback(async (id: string) => {
    const [job, result] = await Promise.all([api.job(id), api.notices(id)])
    setActive(job)
    setNotices(result.items)
    return job
  }, [])

  const refreshAll = useCallback(async () => {
    const [health, taskList, scheduleInfo] = await Promise.all([api.system(), api.jobs(), api.schedule()])
    setSystem(health)
    setJobs(taskList.items)
    setSchedule(scheduleInfo)
    setActive((current) => {
      const preferred = current ? taskList.items.find((job) => job.id === current.id) : null
      return preferred ?? taskList.items.find((job) => liveStatuses.has(job.status)) ?? taskList.items[0] ?? null
    })
  }, [])

  useEffect(() => {
    refreshAll().catch(() => undefined)
    const timer = window.setInterval(() => refreshAll().catch(() => undefined), 5000)
    return () => window.clearInterval(timer)
  }, [refreshAll])

  const refreshSync = useCallback(async () => setSync(await api.syncStatus()), [])

  useEffect(() => {
    refreshSync().catch(() => undefined)
    const interval = sync?.run.status === 'running' ? 1000 : 5000
    const timer = window.setInterval(() => refreshSync().catch(() => undefined), interval)
    return () => window.clearInterval(timer)
  }, [refreshSync, sync?.run.status])

  useEffect(() => {
    if (!active?.id) return
    refreshJob(active.id).catch(() => undefined)
  }, [active?.id, refreshJob])

  useEffect(() => {
    eventSourceRef.current?.close()
    if (!active || !liveStatuses.has(active.status)) return
    const source = new EventSource(`/api/jobs/${active.id}/events`)
    eventSourceRef.current = source
    const update = () => refreshJob(active.id).then((job) => {
      if (terminalEvents.has(job.status)) setSummary(job)
      refreshAll().catch(() => undefined)
    }).catch(() => undefined)
    ;['queued', 'running', 'throttle_waiting', 'page_retrying', 'page_completed', 'stopping', 'stopped', 'completed', 'failed', 'blocked'].forEach((name) => source.addEventListener(name, update))
    source.onerror = () => {
      refreshJob(active.id).catch(() => undefined)
    }
    return () => source.close()
  }, [active?.id, active?.status, refreshAll, refreshJob])

  async function createJob(start: string, end: string) {
    const job = await api.createJob(start, end)
    setActive(job)
    setNotices([])
    await refreshAll()
  }

  async function stop() {
    if (!active) return
    setActive(await api.stopJob(active.id))
  }

  async function resumeJob(jobId: string) {
    const resumed = await api.resumeJob(jobId)
    setActive(resumed)
    setSummary(null)
    await refreshAll()
  }

  async function resume() {
    if (!active) return
    await resumeJob(active.id)
  }

  async function toggleSchedule(enabled: boolean) {
    setSchedule(await api.updateSchedule(enabled))
    await refreshAll()
  }

  async function startSync() {
    setSync(await api.startSync())
  }

  async function retryFailedSync() {
    const result = await api.retryFailedSync()
    setSync(result.status)
  }

  async function deleteFailedJob(job: CrawlJob) {
    await api.deleteJob(job.id)
    if (active?.id === job.id) {
      setActive(null)
      setNotices([])
    }
    if (summary?.id === job.id) setSummary(null)
    setDeleteTarget(null)
    await refreshAll()
  }

  const busy = jobs.some((job) => liveStatuses.has(job.status))
  return (
    <div className="app-shell">
      <StatusHeader status={system} />
      <main>
        <JobForm onCreate={createJob} busy={busy} />
        <div className="workspace-grid">
          <div className="main-stack">
            <ActiveJob job={active} throttle={system?.throttle ?? null} onStop={stop} onResume={resume} />
            <NoticeTable notices={notices} />
          </div>
          <div className="side-stack">
            <SyncPanel status={sync} onRefresh={refreshSync} onStart={startSync} onRetry={retryFailedSync} />
            <SidePanel schedule={schedule} jobs={jobs} onToggle={toggleSchedule} onSelect={setActive} onDelete={setDeleteTarget} />
          </div>
        </div>
      </main>
      <footer>CCGP 搜索页采集 · bidType=1 · 按 canonical_url 唯一去重</footer>
      <SummaryModal
        key={summary?.id ?? 'summary-closed'}
        job={summary}
        throttle={system?.throttle ?? null}
        onClose={() => setSummary(null)}
        onResume={(job) => resumeJob(job.id)}
      />
      <DeleteJobModal
        key={deleteTarget?.id ?? 'delete-closed'}
        job={deleteTarget}
        onClose={() => setDeleteTarget(null)}
        onConfirm={deleteFailedJob}
      />
    </div>
  )
}
