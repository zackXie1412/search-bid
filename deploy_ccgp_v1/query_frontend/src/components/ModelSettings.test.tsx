import { act, fireEvent, render, screen, waitFor, within } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { Header } from './Header'

const config = { base_url: 'https://api.openai.com/v1', model: 'gpt-test', protocol: 'responses', api_key_configured: false }
const success = { ok: true, model: 'gpt-test', protocol: 'responses', elapsed_ms: 321, message: '连接成功，模型已返回有效文本' }
let fetchMock: ReturnType<typeof vi.fn>
const response = (data: unknown, status = 200) => new Response(JSON.stringify(data), { status })

beforeEach(() => {
  // jsdom does not implement native dialog behavior; browser QA covers focus trapping.
  Object.defineProperty(HTMLDialogElement.prototype, 'showModal', { configurable: true, value: function (this: HTMLDialogElement) { this.setAttribute('open', ''); this.querySelector<HTMLButtonElement>('[autofocus]')?.focus() } })
  Object.defineProperty(HTMLDialogElement.prototype, 'close', { configurable: true, value: function (this: HTMLDialogElement) { this.removeAttribute('open') } })
  fetchMock = vi.fn().mockImplementation(async () => response(config))
  vi.stubGlobal('fetch', fetchMock)
})
afterEach(() => { vi.unstubAllGlobals(); vi.restoreAllMocks() })

async function open() {
  const user = userEvent.setup()
  render(<Header />)
  expect(fetchMock).not.toHaveBeenCalled()
  await user.click(screen.getByRole('button', { name: '模型配置' }))
  await waitFor(() => expect(screen.getByLabelText('模型名称')).toBeEnabled())
  return user
}

describe('model configuration', () => {
  it('fills the official DeepSeek compatible endpoint preset', async () => {
    const user = await open()
    await user.click(screen.getByRole('button', { name: 'DeepSeek' }))
    expect(screen.getByLabelText('Base URL')).toHaveValue('https://api.deepseek.com')
    expect(screen.getByLabelText('模型名称')).toHaveValue('deepseek-flash')
    expect(screen.getByLabelText('调用协议')).toHaveValue('chat_completions')
  })

  it('requires a new service key when switching from a configured GPT endpoint to DeepSeek', async () => {
    fetchMock.mockResolvedValueOnce(response({ ...config, api_key_configured: true })).mockResolvedValueOnce(response({ ...success, model: 'deepseek-flash', protocol: 'chat_completions' }))
    const user = await open()
    await user.type(screen.getByLabelText('API Key'), 'unsaved-gpt-key')
    await user.click(screen.getByRole('button', { name: 'DeepSeek' }))
    expect(screen.getByLabelText('API Key')).toHaveValue('')
    await user.click(screen.getByRole('button', { name: '测试连接' }))
    expect(await screen.findByRole('alert')).toHaveTextContent('Base URL 已改变')
    expect(fetchMock).toHaveBeenCalledTimes(1)
    await user.type(screen.getByLabelText('API Key'), 'deepseek-test-key')
    await user.click(screen.getByRole('button', { name: '测试连接' }))
    await screen.findByText('deepseek-flash · Chat Completions · 321 ms')
    expect(JSON.parse(fetchMock.mock.calls[1][1].body)).toMatchObject({ base_url: 'https://api.deepseek.com', model: 'deepseek-flash', protocol: 'chat_completions', api_key: 'deepseek-test-key' })
  })
  it('loads on demand, uses a password field, closes and restores focus without storing the key', async () => {
    const user = await open()
    expect(fetchMock.mock.calls[0][0]).toBe('/api/ai/config')
    expect(screen.getByRole('dialog', { name: '模型配置' })).toBeInTheDocument()
    expect(screen.getByLabelText('API Key')).toHaveAttribute('type', 'password')
    await user.type(screen.getByLabelText('API Key'), 'secret-in-memory')
    const signal = fetchMock.mock.calls[0][1].signal
    fireEvent(screen.getByRole('dialog'), new Event('cancel', { cancelable: true }))
    expect(screen.queryByRole('dialog')).not.toBeInTheDocument()
    expect(screen.getByRole('button', { name: '模型配置' })).toHaveFocus()
    expect(signal.aborted).toBe(true)
    expect(document.body.style.overflow).toBe('')
    await user.click(screen.getByRole('button', { name: '模型配置' }))
    await waitFor(() => expect(screen.getByLabelText('API Key')).toBeEnabled())
    expect(screen.getByLabelText('API Key')).toHaveValue('')
    expect(localStorage.length).toBe(0)
  })

  it('saves with a new key, clears its input and updates configured status', async () => {
    fetchMock.mockResolvedValueOnce(response(config)).mockResolvedValueOnce(response({ ...config, api_key_configured: true }))
    const user = await open()
    await user.type(screen.getByLabelText('API Key'), 'new-secret')
    await user.click(screen.getByRole('button', { name: '保存' }))
    expect(await screen.findByRole('status')).toHaveTextContent('配置已保存')
    expect(fetchMock.mock.calls[1][0]).toBe('/api/ai/config')
    expect(fetchMock.mock.calls[1][1].method).toBe('PUT')
    expect(JSON.parse(fetchMock.mock.calls[1][1].body)).toEqual({ ...config, api_key_configured: undefined, api_key: 'new-secret', clear_api_key: false })
    expect(screen.getByLabelText('API Key')).toHaveValue('')
    expect(screen.getByPlaceholderText('已配置，留空保持不变')).toBeInTheDocument()
    expect(screen.getByText('已配置', { exact: true })).toBeInTheDocument()
  })

  it('keeps an existing key when blank and explicitly clears it', async () => {
    const configured = { ...config, api_key_configured: true }
    fetchMock.mockResolvedValueOnce(response(configured)).mockResolvedValueOnce(response(configured)).mockResolvedValueOnce(response(config))
    const user = await open()
    await user.click(screen.getByRole('button', { name: '保存' }))
    await screen.findByText('配置已保存')
    expect(JSON.parse(fetchMock.mock.calls[1][1].body)).not.toHaveProperty('api_key')
    await user.click(screen.getByRole('button', { name: '清除密钥' }))
    expect(screen.getByText('待清除')).toBeInTheDocument()
    await user.click(screen.getByRole('button', { name: '保存' }))
    await waitFor(() => expect(screen.getByText('未配置', { exact: true })).toBeInTheDocument())
    expect(JSON.parse(fetchMock.mock.calls[2][1].body).clear_api_key).toBe(true)
  })

  it('tests draft settings without saving and shows timing and protocol', async () => {
    fetchMock.mockResolvedValueOnce(response(config)).mockResolvedValueOnce(response({ ...success, model: 'draft-model', protocol: 'chat_completions' }))
    const user = await open()
    await user.clear(screen.getByLabelText('模型名称'))
    await user.type(screen.getByLabelText('模型名称'), 'draft-model')
    await user.selectOptions(screen.getByLabelText('调用协议'), 'chat_completions')
    await user.type(screen.getByLabelText('API Key'), 'draft-secret')
    await user.click(screen.getByRole('button', { name: '测试连接' }))
    expect(await screen.findByRole('status')).toHaveTextContent('连接成功')
    expect(screen.getByText('draft-model · Chat Completions · 321 ms')).toBeInTheDocument()
    expect(fetchMock.mock.calls[1][0]).toBe('/api/ai/test')
    expect(fetchMock.mock.calls[1][1].method).toBe('POST')
    expect(JSON.parse(fetchMock.mock.calls[1][1].body)).toMatchObject({ model: 'draft-model', protocol: 'chat_completions', api_key: 'draft-secret' })
    expect(screen.getByText('未配置', { exact: true })).toBeInTheDocument()
    await user.type(screen.getByLabelText('模型名称'), '-changed')
    expect(screen.queryByText('连接成功，模型已返回有效文本')).not.toBeInTheDocument()
  })

  it('requires a fresh key or explicit clear for a changed URL', async () => {
    fetchMock.mockResolvedValueOnce(response({ ...config, api_key_configured: true })).mockResolvedValueOnce(response({ ...config, base_url: 'https://other.example/v1', api_key_configured: true }))
    const user = await open()
    await user.clear(screen.getByLabelText('Base URL'))
    await user.type(screen.getByLabelText('Base URL'), 'https://other.example/v1')
    await user.click(screen.getByRole('button', { name: '测试连接' }))
    expect(await screen.findByRole('alert')).toHaveTextContent('Base URL 已改变')
    expect(fetchMock).toHaveBeenCalledTimes(1)
    await user.click(screen.getByRole('button', { name: '保存' }))
    expect(fetchMock).toHaveBeenCalledTimes(1)
    await user.type(screen.getByLabelText('API Key'), 'new-endpoint-key')
    await user.click(screen.getByRole('button', { name: '保存' }))
    await screen.findByText('配置已保存')
    expect(JSON.parse(fetchMock.mock.calls[1][1].body).api_key).toBe('new-endpoint-key')
  })

  it('disables controls during requests and aborts on close, ignoring late results', async () => {
    let finish!: (value: Response) => void
    fetchMock.mockResolvedValueOnce(response(config)).mockImplementationOnce(() => new Promise((resolve) => { finish = resolve }))
    const user = await open()
    await user.click(screen.getByRole('button', { name: '测试连接' }))
    expect(screen.getByLabelText('模型名称')).toBeDisabled()
    expect(screen.getByRole('button', { name: '保存' })).toBeDisabled()
    const signal = fetchMock.mock.calls[1][1].signal
    await user.click(screen.getByRole('button', { name: '关闭模型配置' }))
    expect(signal.aborted).toBe(true)
    await act(async () => finish(response(success)))
    expect(screen.queryByText('连接成功，模型已返回有效文本')).not.toBeInTheDocument()
  })

  it('reports connection and saving failures and permits retry', async () => {
    fetchMock.mockResolvedValueOnce(response(config)).mockResolvedValueOnce(response({ ...success, ok: false, message: '认证失败，请检查 API Key' }))
      .mockResolvedValueOnce(response({ detail: '模型配置暂时无法读写' }, 503)).mockResolvedValueOnce(response(config))
    const user = await open()
    await user.click(screen.getByRole('button', { name: '测试连接' }))
    expect(await screen.findByRole('alert')).toHaveTextContent('认证失败')
    await user.click(screen.getByRole('button', { name: '保存' }))
    expect(await screen.findByRole('alert')).toHaveTextContent('暂时无法读写')
    await user.click(screen.getByRole('button', { name: '保存' }))
    await screen.findByText('配置已保存')
    expect(screen.queryByRole('alert')).not.toBeInTheDocument()
  })

  it('fails closed on unreadable configuration, and supports loading retry', async () => {
    fetchMock.mockRejectedValueOnce(new TypeError('Failed to fetch')).mockResolvedValueOnce(response(config))
    const user = userEvent.setup()
    render(<Header />)
    await user.click(screen.getByRole('button', { name: '模型配置' }))
    expect(await screen.findByRole('alert')).toHaveTextContent('无法连接本地服务')
    expect(within(screen.getByRole('dialog')).getByRole('button', { name: '保存' })).toBeDisabled()
    await user.click(screen.getByRole('button', { name: '重新读取' }))
    await waitFor(() => expect(screen.getByLabelText('模型名称')).toBeEnabled())
    expect(screen.queryByRole('alert')).not.toBeInTheDocument()
  })
})
