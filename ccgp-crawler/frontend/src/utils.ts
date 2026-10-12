export function beijingDate(offsetDays = 0): string {
  const now = new Date(Date.now() + offsetDays * 86_400_000)
  const parts = new Intl.DateTimeFormat('en-US', {
    timeZone: 'Asia/Shanghai',
    year: 'numeric',
    month: '2-digit',
    day: '2-digit',
  }).formatToParts(now)
  const part = (type: string) => parts.find((item) => item.type === type)?.value ?? ''
  return `${part('year')}-${part('month')}-${part('day')}`
}

export function percentage(value: number, total: number): number {
  if (total <= 0) return 0
  return Math.min(100, Math.max(0, (value / total) * 100))
}

export function formatDateTime(value: string | null): string {
  if (!value) return '—'
  return new Intl.DateTimeFormat('zh-CN', {
    timeZone: 'Asia/Shanghai',
    month: '2-digit',
    day: '2-digit',
    hour: '2-digit',
    minute: '2-digit',
    hour12: false,
  }).format(new Date(value))
}

export function elapsed(startedAt: string | null, finishedAt: string | null, now: number): string {
  if (!startedAt) return '00:00'
  const end = finishedAt ? new Date(finishedAt).getTime() : now
  const seconds = Math.max(0, Math.floor((end - new Date(startedAt).getTime()) / 1000))
  const hours = Math.floor(seconds / 3600)
  const minutes = Math.floor((seconds % 3600) / 60)
  const rest = seconds % 60
  return [hours, minutes, rest].map((unit) => String(unit).padStart(2, '0')).join(':')
}

