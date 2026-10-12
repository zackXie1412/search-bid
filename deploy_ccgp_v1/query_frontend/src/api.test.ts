import { afterEach, describe, expect, it, vi } from 'vitest'
import { queryNotices, resolveDateRange, type QueryFilters } from './api'

const filters: QueryFilters = { keyword: '', regionCode: '', timeRange: '全部', startDate: '', endDate: '' }
const now = new Date('2026-10-07T16:30:00Z') // Beijing October 8, even on a UTC or US computer.
afterEach(() => vi.unstubAllGlobals())

describe('Beijing date ranges', () => {
  it.each([
    ['今天', '2026-10-08', '2026-10-08'], ['昨天', '2026-10-07', '2026-10-07'],
    ['近一周', '2026-10-01', '2026-10-07'], ['近一个月', '2026-09-08', '2026-10-07'],
    ['近三个月', '2026-07-10', '2026-10-07'], ['近六个月', '2026-04-11', '2026-10-07'],
    ['近一年', '2025-10-08', '2026-10-07'],
  ] as const)('%s uses the intended complete days in Beijing', (timeRange, startDate, endDate) => {
    expect(resolveDateRange({ ...filters, timeRange }, now)).toEqual({ startDate, endDate })
    if (timeRange !== '今天' && timeRange !== '昨天') {
      const days = { '近一周': 7, '近一个月': 30, '近三个月': 90, '近六个月': 180, '近一年': 365 }[timeRange]
      expect((Date.parse(endDate) - Date.parse(startDate)) / 86_400_000 + 1).toBe(days)
    }
  })

  it('handles a leap-year calendar boundary and open-ended custom dates', () => {
    expect(resolveDateRange({ ...filters, timeRange: '近一周' }, new Date('2024-03-01T02:00:00Z')))
      .toEqual({ startDate: '2024-02-23', endDate: '2024-02-29' })
    expect(resolveDateRange({ ...filters, timeRange: '自定义', endDate: '2026-10-07' }, now))
      .toEqual({ startDate: '', endDate: '2026-10-07' })
  })

  it.each([
    ['2026-01-05T02:00:00Z', '2025-12-06', '2026-01-04'],
    ['2026-03-01T02:00:00Z', '2026-01-30', '2026-02-28'],
    ['2024-03-01T02:00:00Z', '2024-01-31', '2024-02-29'],
  ])('the previous 30 days cross month/year boundaries at %s', (date, startDate, endDate) => {
    expect(resolveDateRange({ ...filters, timeRange: '近一个月' }, new Date(date)))
      .toEqual({ startDate, endDate })
  })
})

it('maps missing regions to unknown and reports network failures', async () => {
  const fetchMock = vi.fn().mockResolvedValueOnce(new Response(JSON.stringify({
    items: [{ id: 1, title: '真实公告', canonical_url: 'https://www.ccgp.gov.cn/cggg/zygg/gkzb/test.htm',
      published_at: '2026-10-07T00:00:00+08:00', region_code: null, region_raw: null }], next_cursor: null,
  }))).mockRejectedValueOnce(new TypeError('Failed to fetch'))
  vi.stubGlobal('fetch', fetchMock)
  expect((await queryNotices(filters)).items[0].region).toBe('未知')
  await expect(queryNotices(filters)).rejects.toThrow('无法连接本地采集服务')
})
