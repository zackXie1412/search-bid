import { act, fireEvent, render, screen, waitFor } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import App from './App'

const notice = (id = 1, title = '山东公开招标测试公告') => ({
  id, title, canonical_url: `https://www.ccgp.gov.cn/cggg/dfgg/gkzb/202610/${id}.htm`,
  published_at: '2026-10-07T23:45:20+08:00', region_code: '370000', region_raw: '山东',
})
const response = (items = [notice()], next_cursor: string | null = null, total_count = items.length) => new Response(JSON.stringify({ items, next_cursor, limit: 6, total_count }), {
  headers: { 'Content-Type': 'application/json' },
})
const fetchMock = vi.fn<typeof fetch>()
const requestParams = (index: number) => new URL(String(fetchMock.mock.calls[index][0]), 'http://localhost').searchParams

beforeEach(() => {
  fetchMock.mockReset()
  fetchMock.mockImplementation(async () => response())
  vi.stubGlobal('fetch', (input: Parameters<typeof fetch>[0], init?: Parameters<typeof fetch>[1]) => String(input).startsWith('/api/knowledge/') ? Promise.resolve(new Response(JSON.stringify({name:'',revision:null}))) : String(input).startsWith('/api/bid-check/tasks') ? Promise.resolve(new Response(JSON.stringify({ items: [] }))) : fetchMock(input, init))
})
afterEach(() => vi.unstubAllGlobals())

describe('local notice queries', () => {
  it('searches without a key and displays real links and the Beijing date', async () => {
    const user = userEvent.setup()
    render(<App />)
    expect(screen.queryByLabelText('API Key')).not.toBeInTheDocument()
    expect(screen.queryByRole('columnheader', { name: '采购单位' })).not.toBeInTheDocument()
    await user.type(screen.getByLabelText('搜索关键词'), '设备 卫星')
    expect(fetchMock).not.toHaveBeenCalled()
    await user.keyboard('{Enter}')
    expect(await screen.findByText('山东公开招标测试公告')).toBeInTheDocument()
    expect(requestParams(0).get('keyword')).toBe('设备 卫星')
    expect(screen.getByRole('link', { name: '山东公开招标测试公告' })).toHaveAttribute('href', notice().canonical_url)
    expect(screen.getByText('2026/10/07 23:45')).toBeInTheDocument()
  })

  it('queries region and time immediately and clears manual dates for presets', async () => {
    const user = userEvent.setup()
    render(<App />)
    await user.click(screen.getByRole('button', { name: '山东' }))
    await screen.findByText('共 1 条记录')
    expect(requestParams(0).get('region_code')).toBe('370000')
    fireEvent.change(screen.getByLabelText('开始日期'), { target: { value: '2026-10-01' } })
    await waitFor(() => expect(fetchMock).toHaveBeenCalledTimes(2))
    expect(requestParams(1).get('start_date')).toBe('2026-10-01')
    await user.click(screen.getByRole('button', { name: '昨天' }))
    await waitFor(() => expect(fetchMock).toHaveBeenCalledTimes(3))
    expect(requestParams(2).get('start_date')).toBe(requestParams(2).get('end_date'))
    expect(screen.getByLabelText('开始日期')).toHaveValue('')
    expect(screen.getByLabelText('结束日期')).toHaveValue('')
  })

  it('uses saved cursors and submitted filters, then resets pagination on region changes', async () => {
    fetchMock.mockResolvedValueOnce(response([notice()], 'page-two', 7))
      .mockResolvedValueOnce(response([notice(2, '第二页真实公告')], null, 7))
      .mockResolvedValueOnce(response([notice()], 'page-two', 7))
      .mockResolvedValueOnce(response([notice(3, '广东公告')]))
    const user = userEvent.setup()
    render(<App />)
    await user.type(screen.getByLabelText('搜索关键词'), '已提交')
    await user.click(screen.getByRole('button', { name: '山东' }))
    await screen.findByText('山东公开招标测试公告')
    expect(screen.getByText('共 7 条记录')).toBeInTheDocument()
    expect(screen.getByText('共 2 页')).toBeInTheDocument()
    expect(screen.getByText('第 1 页')).toBeInTheDocument()
    await user.clear(screen.getByLabelText('搜索关键词'))
    await user.type(screen.getByLabelText('搜索关键词'), '未提交')
    await user.click(screen.getByRole('button', { name: '下一页' }))
    await screen.findByText('第二页真实公告')
    expect(requestParams(1).get('cursor')).toBe('page-two')
    expect(requestParams(1).get('keyword')).toBe('已提交')
    expect(screen.getByText('共 2 页')).toBeInTheDocument()
    expect(screen.getByText('第 2 页')).toBeInTheDocument()
    expect(screen.getByText('共 7 条记录')).toBeInTheDocument()
    expect(screen.getByRole('button', { name: '下一页' })).toBeDisabled()
    await user.click(screen.getByRole('button', { name: '上一页' }))
    await screen.findByText('山东公开招标测试公告')
    expect(requestParams(2).has('cursor')).toBe(false)
    await user.click(screen.getByRole('button', { name: '广东' }))
    await screen.findByText('广东公告')
    expect(requestParams(3).has('cursor')).toBe(false)
    expect(requestParams(3).get('keyword')).toBe('未提交')
    expect(screen.getByText('共 1 页')).toBeInTheDocument()
    expect(screen.getByText('第 1 页')).toBeInTheDocument()
    expect(screen.getByText('共 1 条记录')).toBeInTheDocument()
  })

  it('cancels old requests and ignores late replies when switching quickly', async () => {
    let finishOld!: (value: Response) => void
    fetchMock.mockImplementationOnce(() => new Promise((resolve) => { finishOld = resolve }))
      .mockResolvedValueOnce(response([notice(2, '广东最新结果')]))
    const user = userEvent.setup()
    render(<App />)
    await user.click(screen.getByRole('button', { name: '山东' }))
    await user.click(screen.getByRole('button', { name: '广东' }))
    await screen.findByText('广东最新结果')
    expect(fetchMock.mock.calls[0][1]?.signal?.aborted).toBe(true)
    await act(async () => finishOld(response([notice(1, '山东旧结果')])))
    expect(screen.queryByText('山东旧结果')).not.toBeInTheDocument()
    expect(screen.getByText('广东最新结果')).toBeInTheDocument()
  })

  it('reports reversed manual dates without sending a request', async () => {
    render(<App />)
    fireEvent.change(screen.getByLabelText('开始日期'), { target: { value: '2026-10-08' } })
    await screen.findByText('山东公开招标测试公告')
    fireEvent.change(screen.getByLabelText('结束日期'), { target: { value: '2026-10-07' } })
    expect(await screen.findByRole('alert')).toHaveTextContent('开始日期不得晚于结束日期')
    expect(fetchMock).toHaveBeenCalledTimes(1)
    expect(screen.queryByText('山东公开招标测试公告')).not.toBeInTheDocument()
  })

  it('distinguishes empty results from a backend error and recovers on retry', async () => {
    fetchMock.mockResolvedValueOnce(response([])).mockResolvedValueOnce(new Response(JSON.stringify({ detail: '本地数据库暂时不可用' }), { status: 503 }))
      .mockResolvedValueOnce(response())
    const user = userEvent.setup()
    render(<App />)
    await user.click(screen.getByRole('button', { name: '今天' }))
    await screen.findByText('没有找到匹配的标讯')
    expect(screen.getByText('共 0 条记录')).toBeInTheDocument()
    expect(screen.getByText('共 0 页')).toBeInTheDocument()
    expect(screen.getByRole('button', { name: '上一页' })).toBeDisabled()
    expect(screen.getByRole('button', { name: '下一页' })).toBeDisabled()
    await user.click(screen.getByRole('button', { name: '昨天' }))
    expect(await screen.findByRole('alert')).toHaveTextContent('本地数据库暂时不可用')
    expect(screen.queryByText('没有找到匹配的标讯')).not.toBeInTheDocument()
    expect(screen.queryByText('共 0 条记录')).not.toBeInTheDocument()
    expect(screen.queryByText('共 0 页')).not.toBeInTheDocument()
    await user.click(screen.getByRole('button', { name: '搜索' }))
    await screen.findByText('山东公开招标测试公告')
    expect(screen.queryByRole('alert')).not.toBeInTheDocument()
  })

  it('expands the remaining province options', async () => {
    const user = userEvent.setup()
    render(<App />)
    expect(screen.queryByRole('button', { name: '四川' })).not.toBeInTheDocument()
    await user.click(screen.getByRole('button', { name: '更多' }))
    await user.click(screen.getByRole('button', { name: '四川' }))
    await screen.findByText('共 1 条记录')
    expect(requestParams(0).get('region_code')).toBe('510000')
  })

  it('shows the rolling date bounds sent to the backend', async () => {
    const user = userEvent.setup()
    render(<App />)
    await user.click(screen.getByRole('button', { name: '近一个月' }))
    await screen.findByText('共 1 条记录')
    const params = requestParams(0)
    const start = params.get('start_date')!
    const end = params.get('end_date')!
    expect(screen.getByLabelText('实际查询日期')).toHaveTextContent(`${start} 至 ${end}`)
    expect((Date.parse(end) - Date.parse(start)) / 86_400_000 + 1).toBe(30)
  })
})
