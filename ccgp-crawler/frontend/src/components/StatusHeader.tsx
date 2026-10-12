import { CircleDot, Globe2 } from 'lucide-react'
import type { SystemStatus } from '../types'
import { formatDateTime } from '../utils'

function HealthItem({ label, value, ok = true }: { label: string; value: string; ok?: boolean }) {
  return (
    <div className="health-item">
      <span className={`health-dot ${ok ? 'is-ok' : 'is-warn'}`} aria-hidden="true" />
      <span>{label}</span>
      <strong>{value}</strong>
    </div>
  )
}

export function StatusHeader({ status }: { status: SystemStatus | null }) {
  const throttle = status?.throttle
  const throttleText = !throttle ? '检查中' : {
    ready: '可请求',
    waiting: throttle.resume_required ? '等待用户确认' : `等待至 ${formatDateTime(throttle.wait_until)}`,
    cooldown: `冷却至 ${formatDateTime(throttle.cooldown_until)}`,
    daily_limit: '今日额度已用尽',
  }[throttle.state]
  return (
    <header className="topbar">
      <div className="brand">
        <div className="brand-mark"><Globe2 size={20} /></div>
        <div>
          <p className="eyebrow">中国政府采购网</p>
          <h1>公开招标采集控制台</h1>
        </div>
      </div>
      <div className="health-strip" aria-label="系统状态">
        <HealthItem label="本地服务" value={status?.service === 'online' ? '在线' : '连接中'} ok={status?.service === 'online'} />
        <HealthItem
          label="采集线程"
          value={status?.worker === 'running' ? '运行中' : '已停止'}
          ok={status?.worker === 'running'}
        />
        <HealthItem label="SQLite" value={status?.sqlite === 'connected' ? '已连接' : '检查中'} ok={status?.sqlite === 'connected'} />
        <HealthItem label="Chrome" value={status?.chrome === 'running' ? '运行中' : '就绪'} />
        <HealthItem
          label="站点节流"
          value={throttleText}
          ok={!throttle || throttle.state === 'ready'}
        />
        <HealthItem
          label="今日请求"
          value={throttle ? `${throttle.day_request_count} / ${throttle.daily_limit}` : '—'}
          ok={!throttle || throttle.day_request_count < throttle.daily_limit}
        />
        <span className="source-pill"><CircleDot size={14} /> {status?.source ?? 'search.ccgp.gov.cn'}</span>
      </div>
    </header>
  )
}
