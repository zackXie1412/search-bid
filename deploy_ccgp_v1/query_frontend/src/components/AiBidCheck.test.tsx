import { fireEvent, render, screen } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { AiBidCheck } from './AiBidCheck'

beforeEach(() => vi.stubGlobal('fetch', vi.fn().mockImplementation(async () => new Response(JSON.stringify({ items: [] })))))
afterEach(() => vi.unstubAllGlobals())
const ready = { filters: { keyword: '', regionCode: '370000', timeRange: '自定义' as const, startDate: '2026-10-07', endDate: '2026-10-07' }, totalCount: 8, queryReady: true }

describe('AI bid check frontend', () => {
  it('enables checking only with files and successful search, and resets after removal', async () => {
    const fetchMock = vi.fn().mockImplementation(async () => new Response(JSON.stringify({ base_url: 'https://model.example/v1', model: 'gpt-test', protocol: 'responses', api_key_configured: true })))
    vi.stubGlobal('fetch', (input: string) => input === '/api/bid-check/rules' ? Promise.resolve(new Response(JSON.stringify({skill_id:'qualification-screening',skill_name:'资格初筛',skill_sha256:'a'.repeat(64),protocol_version:2}))) : input.startsWith('/api/knowledge/') ? Promise.resolve(new Response(JSON.stringify({name:'',revision:null}))) : input.startsWith('/api/bid-check/tasks') ? Promise.resolve(new Response(JSON.stringify({ items: [] }))) : fetchMock(input))
    const user = userEvent.setup()
    render(<AiBidCheck {...ready} />)
    const button = screen.getByRole('button', { name: 'AI 检标' })
    expect(button).toBeDisabled()
    await user.upload(screen.getByLabelText('上传公司信息'), new File(['公司资料'], '公司介绍.pdf', { type: 'application/pdf' }))
    expect(screen.getByText('公司介绍.pdf')).toBeInTheDocument()
    expect(screen.getByText('已添加 1 份资料')).toBeInTheDocument()
    expect(button).toBeEnabled()
    await user.click(button)
    expect(await screen.findByRole('region', { name: '确认检标范围' })).toHaveTextContent('全部 8 条公告')
    expect(fetchMock).toHaveBeenCalledTimes(1)
    await user.click(screen.getByRole('button', { name: '移除 公司介绍.pdf' }))
    expect(button).toBeDisabled()
    expect(screen.queryByRole('status')).not.toBeInTheDocument()
  })

  it('accepts multiple files and deduplicates repeat uploads', async () => {
    const user = userEvent.setup()
    render(<AiBidCheck {...ready} />)
    const files = [new File(['介绍'], '介绍.docx'), new File(['资质'], '资质.PNG')]
    await user.upload(screen.getByLabelText('上传公司信息'), files)
    await user.upload(screen.getByLabelText('上传公司信息'), files)
    expect(screen.getByText('已添加 2 份资料')).toBeInTheDocument()
    expect(screen.getAllByText('介绍.docx')).toHaveLength(1)
    await user.click(screen.getByRole('button', { name: '移除 介绍.docx' }))
    expect(screen.getByText('已添加 1 份资料')).toBeInTheDocument()
    expect(screen.getByRole('button', { name: 'AI 检标' })).toBeEnabled()
  })

  it('validates dropped files while keeping valid company information', () => {
    const { container } = render(<AiBidCheck {...ready} />)
    const oversized = new File(['x'], '超大文件.pdf')
    Object.defineProperty(oversized, 'size', { value: 21 * 1024 * 1024 })
    fireEvent.drop(container.querySelector('.company-dropzone')!, { dataTransfer: { files: [
      new File(['资料'], '公司简介.txt'), new File(['x'], '程序.exe'), new File([], '空文件.pdf'), oversized,
    ] } })
    expect(screen.getByText('公司简介.txt')).toBeInTheDocument()
    expect(screen.getByText('已添加 1 份资料')).toBeInTheDocument()
    expect(screen.getByRole('alert')).toHaveTextContent('暂不支持此格式')
    expect(screen.getByRole('alert')).toHaveTextContent('文件为空')
    expect(screen.getByRole('alert')).toHaveTextContent('文件超过 20 MB')
  })

  it('opens the file selector and permits the same file to be selected after removal', async () => {
    const user = userEvent.setup()
    render(<AiBidCheck {...ready} />)
    const input = screen.getByLabelText('上传公司信息')
    const clickSpy = vi.spyOn(input, 'click')
    await user.click(screen.getByRole('button', { name: '选择文件' }))
    expect(clickSpy).toHaveBeenCalled()
    const file = new File(['test'], '公司.pdf')
    await user.upload(input, file)
    await user.click(screen.getByRole('button', { name: '移除 公司.pdf' }))
    await user.upload(input, file)
    expect(screen.getByText('已添加 1 份资料')).toBeInTheDocument()
  })
})

it('blocks confirmation when the default qualification Skill is unavailable',async()=>{
  vi.stubGlobal('fetch',vi.fn(async(url:string)=>new Response(JSON.stringify(url==='/api/bid-check/rules'?{detail:'默认资格初筛 Skill 缺失或为空'}:url==='/api/ai/config'?{base_url:'https://model.example/v1',model:'gpt-test',protocol:'responses',api_key_configured:true}:{items:[]}),{status:url==='/api/bid-check/rules'?503:200})))
  const user=userEvent.setup()
  render(<AiBidCheck {...ready}/>)
  await user.upload(screen.getByLabelText('上传公司信息'),new File(['资料'],'公司.txt'))
  await user.click(screen.getByRole('button',{name:'AI 检标'}))
  expect(await screen.findByRole('alert')).toHaveTextContent('默认资格初筛 Skill 缺失或为空')
  expect(screen.queryByRole('region',{name:'确认检标范围'})).not.toBeInTheDocument()
})
