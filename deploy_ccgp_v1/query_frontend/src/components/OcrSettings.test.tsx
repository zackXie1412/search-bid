import {act,fireEvent,render,screen,waitFor,within} from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import {afterEach,beforeEach,expect,it,vi} from 'vitest'
import {Header} from './Header'

const ai={base_url:'https://model.example/v1',model:'test-model',protocol:'responses',api_key_configured:true}
const ocr={enabled:false,provider:'paddle_aistudio',model:'PP-OCRv5',access_token_configured:false}
const response=(data:unknown,status=200)=>new Response(JSON.stringify(data),{status})
let calls:{url:string;init?:RequestInit}[]
beforeEach(()=>{
  calls=[]
  Object.defineProperty(HTMLDialogElement.prototype,'showModal',{configurable:true,value:function(){this.setAttribute('open','');this.querySelector('[autofocus]')?.focus()}})
  Object.defineProperty(HTMLDialogElement.prototype,'close',{configurable:true,value:function(){this.removeAttribute('open')}})
  vi.stubGlobal('fetch',vi.fn(async(url:string,init?:RequestInit)=>{calls.push({url,init});return response(url==='/api/ai/config'?ai:url==='/api/ocr/test'?{ok:true,text:'PADDLE OCR TEST 2026',elapsed_ms:201,message:'连接成功，OCR 已返回识别文字'}:{...ocr,access_token_configured:init?.method==='PUT'})}))
})
afterEach(()=>{vi.unstubAllGlobals();vi.restoreAllMocks()})
async function open(){const user=userEvent.setup();render(<Header/>);await user.click(screen.getByRole('button',{name:'模型配置'}));await screen.findByLabelText('模型名称');await user.click(screen.getByRole('tab',{name:'OCR 识别'}));await waitFor(()=>expect(screen.getByLabelText('Access Token')).toBeEnabled());return user}

it('saves OCR independently and clears the token input, with a cloud notice',async()=>{
  const user=await open()
  expect(screen.getByLabelText('Access Token')).toHaveAttribute('type','password')
  expect(screen.getByText(/图片会发送到官方云端/)).toBeInTheDocument()
  expect(screen.getByRole('link',{name:'获取 AI Studio 令牌'})).toHaveAttribute('href','https://aistudio.baidu.com/account/accessToken')
  await user.click(screen.getByLabelText('拆解图片时启用 OCR'))
  await user.type(screen.getByLabelText('Access Token'),'ocr-secret')
  await user.click(screen.getByRole('button',{name:'保存'}))
  expect(await screen.findByRole('status')).toHaveTextContent('OCR 配置已保存')
  const save=calls.find(c=>c.init?.method==='PUT')!
  expect(save.url).toBe('/api/ocr/config');expect(JSON.parse(String(save.init?.body))).toEqual({...ocr,enabled:true,access_token_configured:undefined,access_token:'ocr-secret',clear_access_token:false})
  expect(screen.getByLabelText('Access Token')).toHaveValue('')
  await user.click(screen.getByRole('tab',{name:'AI 模型'}));expect(screen.getByLabelText('模型名称')).toHaveValue('test-model')
  expect(calls.filter(c=>c.url==='/api/ai/config').length).toBe(1)
})

it('tests unsaved OCR token and displays recognized text, then closes and clears memory',async()=>{
  const user=await open();await user.type(screen.getByLabelText('Access Token'),'draft-secret')
  await user.click(screen.getByRole('button',{name:'测试连接'}))
  expect(await screen.findByText('PADDLE OCR TEST 2026')).toBeInTheDocument()
  expect(screen.getByText('PP-OCRv5 · 201 ms')).toBeInTheDocument()
  expect(calls.some(c=>c.init?.method==='PUT')).toBe(false)
  expect(JSON.parse(String(calls.find(c=>c.url==='/api/ocr/test')?.init?.body)).access_token).toBe('draft-secret')
  await user.click(screen.getByRole('button',{name:'关闭模型配置'}));expect(screen.getByRole('button',{name:'模型配置'})).toHaveFocus()
  await user.click(screen.getByRole('button',{name:'模型配置'}));await user.click(screen.getByRole('tab',{name:'OCR 识别'}))
  await waitFor(()=>expect(screen.getByLabelText('Access Token')).toBeEnabled());expect(screen.getByLabelText('Access Token')).toHaveValue('')
})

it('preserves and explicitly clears a configured token',async()=>{
  const base=globalThis.fetch
  vi.stubGlobal('fetch',vi.fn(async(url:string,init?:RequestInit)=>url==='/api/ocr/config'&&!init?.method?.includes('PUT')?response({...ocr,access_token_configured:true}):base(url,init)))
  const user=await open();await user.click(screen.getByRole('button',{name:'保存'}));await screen.findByText('OCR 配置已保存')
  expect(JSON.parse(String(calls.find(c=>c.init?.method==='PUT')?.init?.body))).not.toHaveProperty('access_token')
  await user.click(screen.getByRole('button',{name:'清除令牌'}));await user.click(screen.getByRole('button',{name:'保存'}))
  await waitFor(()=>expect(calls.filter(c=>c.init?.method==='PUT').length).toBe(2))
  expect(JSON.parse(String(calls.filter(c=>c.init?.method==='PUT')[1].init?.body)).clear_access_token).toBe(true)
})

it('supports arrow key tabs, reports errors, and aborts testing on close',async()=>{
  const user=await open()
  screen.getByRole('tab',{name:'OCR 识别'}).focus();await user.keyboard('{ArrowLeft}')
  expect(screen.getByRole('tab',{name:'AI 模型'})).toHaveFocus()
  await user.keyboard('{End}');expect(screen.getByRole('tab',{name:'OCR 识别'})).toHaveFocus()
  await user.click(screen.getByRole('button',{name:'测试连接'}));expect(await screen.findByRole('alert')).toHaveTextContent('请填写 AI Studio Access Token')
  let finish!:(value:Response)=>void;const base=globalThis.fetch
  vi.stubGlobal('fetch',vi.fn(async(url:string,init?:RequestInit)=>{if(url==='/api/ocr/test'){calls.push({url,init});return new Promise<Response>(resolve=>{finish=resolve})}return base(url,init)}))
  await user.type(screen.getByLabelText('Access Token'),'test-secret');await user.click(screen.getByRole('button',{name:'测试连接'}))
  expect(screen.getByRole('button',{name:'保存'})).toBeDisabled()
  const signal=calls.find(c=>c.url==='/api/ocr/test')!.init!.signal!
  fireEvent(screen.getByRole('dialog'),new Event('cancel',{cancelable:true}))
  expect(signal.aborted).toBe(true)
  await act(async()=>finish(response({ok:true,text:'late result',elapsed_ms:50,message:'success'})))
  expect(screen.queryByText('late result')).not.toBeInTheDocument()
})
