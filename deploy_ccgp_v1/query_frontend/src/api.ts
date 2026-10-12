import { regions, type Notice, type timeRanges } from './data'

export const NOTICE_PAGE_SIZE = 6

export type QueryFilters = {
  keyword: string
  regionCode: string
  timeRange: typeof timeRanges[number] | '自定义'
  startDate: string
  endDate: string
}

export type QueryResult = {
  items: Notice[]
  nextCursor: string | null
  totalCount: number
  elapsedMs: number
}

type ApiNotice = {
  id: number
  title: string
  canonical_url: string
  published_at: string
  region_code: string | null
  region_raw: string | null
}

const rangeDays: Partial<Record<QueryFilters['timeRange'], number>> = {
  '近一周': 7, '近一个月': 30,
  '近三个月': 90, '近六个月': 180, '近一年': 365,
}

export function resolveDateRange(filters: QueryFilters, now = new Date()) {
  if (filters.timeRange === '全部' || filters.timeRange === '自定义') {
    if (filters.startDate && filters.endDate && filters.startDate > filters.endDate) {
      throw new Error('开始日期不得晚于结束日期')
    }
    return { startDate: filters.startDate, endDate: filters.endDate }
  }

  const parts = new Intl.DateTimeFormat('en', {
    timeZone: 'Asia/Shanghai', year: 'numeric', month: '2-digit', day: '2-digit',
  }).formatToParts(now)
  const part = (name: string) => parts.find((value) => value.type === name)!.value
  // Use UTC solely for calendar arithmetic; today's date comes from Beijing time.
  const today = new Date(`${part('year')}-${part('month')}-${part('day')}T00:00:00Z`)
  const end = new Date(today)
  if (filters.timeRange !== '今天') end.setUTCDate(end.getUTCDate() - 1)
  const start = new Date(today)
  // Rolling ranges contain the N complete days before today, not the current month.
  if (filters.timeRange !== '今天') {
    start.setUTCDate(start.getUTCDate() - (rangeDays[filters.timeRange] ?? 1))
  }
  return { startDate: start.toISOString().slice(0, 10), endDate: end.toISOString().slice(0, 10) }
}

export async function queryNotices(
  filters: QueryFilters,
  options: { cursor?: string | null; signal?: AbortSignal } = {},
): Promise<QueryResult> {
  const { startDate, endDate } = resolveDateRange(filters)
  const params = new URLSearchParams({ limit: String(NOTICE_PAGE_SIZE) })
  if (filters.regionCode) params.set('region_code', filters.regionCode)
  if (filters.keyword.trim()) params.set('keyword', filters.keyword.trim())
  if (startDate) params.set('start_date', startDate)
  if (endDate) params.set('end_date', endDate)
  if (options.cursor) params.set('cursor', options.cursor)

  const started = performance.now()
  let response: Response
  try {
    response = await fetch(`/api/v1/notices?${params}`, { signal: options.signal })
  } catch (error) {
    if (options.signal?.aborted) throw error
    throw new Error('无法连接本地采集服务，请确认服务已启动')
  }
  if (!response.ok) {
    const error = await response.json().catch(() => null)
    const message = typeof error?.detail === 'string' ? error.detail : null
    throw new Error(message || (response.status === 422
      ? '查询参数无效，请检查地区、日期和关键词'
      : '本地查询失败，请稍后重试'))
  }
  const data: { items: ApiNotice[]; next_cursor: string | null; total_count: number } = await response.json()
  return {
    items: data.items.map((notice) => ({
      id: notice.id,
      title: notice.title,
      canonicalUrl: notice.canonical_url,
      publishedAt: notice.published_at,
      regionCode: notice.region_code,
      region: notice.region_raw || regions.find(([, code]) => code === notice.region_code)?.[0] || '未知',
    })),
    nextCursor: data.next_cursor,
    totalCount: data.total_count,
    elapsedMs: Math.round(performance.now() - started),
  }
}
