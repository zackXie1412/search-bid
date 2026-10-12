"""PaddleOCR official job API, using in-memory images and resumable job IDs."""
from __future__ import annotations
import asyncio
import io
import json
import time
from urllib.parse import urlsplit, quote
import httpx
from PIL import Image, ImageDraw
from pydantic import BaseModel
from .ocr_config import OCR_BASE_URL, OCRConfig


class OCRError(Exception):
    def __init__(self, message, *, category='format', retryable=False, fatal=False):
        super().__init__(message)
        self.category=category;self.retryable=retryable;self.fatal=fatal


class OCRTestResult(BaseModel):
    ok: bool
    text: str
    elapsed_ms: int
    message: str


def _error(status, body):
    # Provider bodies can echo tokens or company content. Never expose them.
    code=str(body.get('code','')) if isinstance(body,dict) else ''
    message=json.dumps(body,ensure_ascii=False).lower() if isinstance(body,dict) else ''
    if status==402 or any(k in message for k in ('quota','balance','配额','额度','欠费','余额','用量已')):
        return OCRError('OCR 配额或余额不足，请在 AI Studio 检查额度后重试',category='quota',fatal=True)
    if status in {401,403} or code in {'401','403'} or any(k in message for k in ('invalid token','token expired','authentication','无效token','令牌无效')):
        return OCRError('OCR 认证失败，请检查 AI Studio Access Token',category='authentication',fatal=True)
    if status==429 or code=='429':
        return OCRError('OCR 请求过于频繁，请稍后重试',category='rate_limit',retryable=True)
    if status>=500:
        return OCRError('飞桨 OCR 服务暂时不可用，请稍后重试',category='service',retryable=True)
    return OCRError('OCR 请求未被接受，请检查令牌权限或图片格式',category='request')


def _data(response):
    try:body=response.json()
    except ValueError:body=None
    if not response.is_success or isinstance(body,dict) and body.get('code',0) not in (0,None):
        raise _error(response.status_code,body)
    if not isinstance(body,dict) or not isinstance(body.get('data'),dict):
        raise OCRError('OCR 服务返回格式不兼容')
    return body['data']


def parse_result(content):
    try:
        texts=[];scores=[]
        for line in content.splitlines():
            if not line.strip():continue
            data=json.loads(line)
            for page in data['result']['ocrResults']:
                result=page['prunedResult']
                if isinstance(result,str):result=json.loads(result)
                values=result['rec_texts']
                if not isinstance(values,list) or any(not isinstance(t,str) for t in values):raise ValueError()
                texts.extend(values)
                raw_scores=result.get('rec_scores',[])
                if isinstance(raw_scores,list):
                    scores.extend(float(v) for v in raw_scores if isinstance(v,(float,int)) and 0<=v<=1)
        return {'text':'\n'.join(t for t in texts if t.strip()),'scores':scores}
    except (KeyError,TypeError,ValueError):
        raise OCRError('OCR 识别结果无法解析') from None


class PaddleOCRClient:
    def __init__(self, *, transport=None, timeout=120, poll_interval=3, retry_delays=(3,8)):
        self.transport=transport;self.timeout=timeout;self.poll_interval=poll_interval;self.retry_delays=retry_delays

    async def recognize(self,config:OCRConfig,image:bytes|None,*,job_id='',save_job=None,check=None,progress=None):
        check=check or (lambda:None);save_job=save_job or (lambda value:None);progress=progress or (lambda **kw:None)
        token=config.access_token.get_secret_value()
        if not token:raise OCRError('尚未配置 OCR Access Token',category='authentication',fatal=True)
        started=time.monotonic();deadline=started+self.timeout
        async def interruptible(awaitable):
            task=asyncio.ensure_future(awaitable)
            try:
                while True:
                    check()
                    remaining=deadline-time.monotonic()
                    if remaining<=0:raise OCRError(f'OCR 识别超时（{self.timeout:g} 秒）',category='timeout',retryable=True)
                    done,_=await asyncio.wait({task},timeout=min(.2,remaining))
                    if done:
                        return task.result()
            finally:
                if not task.done():
                    task.cancel()
                    await asyncio.gather(task,return_exceptions=True)
        async with httpx.AsyncClient(transport=self.transport,timeout=self.timeout,follow_redirects=False) as client:
            attempts=0
            while True:
                progress(attempt=attempts+1,retrying=attempts>0,elapsed_ms=round((time.monotonic()-started)*1000))
                try:
                    if not job_id:
                        response=await interruptible(client.post(OCR_BASE_URL+'/api/v2/ocr/jobs',
                            headers={'Authorization':'Bearer '+token},
                            data={'model':config.model,'optionalPayload':json.dumps({'useDocOrientationClassify':True,'useDocUnwarping':False,'visualize':False})},
                            files={'file':('evidence.jpg',image,'image/jpeg')}))
                        data=_data(response);job_id=data.get('jobId','')
                        if not isinstance(job_id,str) or not job_id:raise OCRError('OCR 未返回有效任务编号')
                        save_job(job_id)
                    while True:
                        response=await interruptible(client.get(OCR_BASE_URL+'/api/v2/ocr/jobs/'+quote(job_id,safe=''),headers={'Authorization':'Bearer '+token}))
                        data=_data(response);state=data.get('state')
                        if state=='done':break
                        if state=='failed':raise _error(400,{'message':data.get('errorMsg','')})
                        if state not in {'pending','running'}:raise OCRError('OCR 任务状态无法识别')
                        await interruptible(asyncio.sleep(self.poll_interval))
                    url=data.get('resultUrl',{}).get('jsonUrl','') if isinstance(data.get('resultUrl'),dict) else ''
                    try:parts=urlsplit(url)
                    except ValueError:parts=None
                    if not parts or parts.scheme!='https' or not parts.hostname or parts.username or parts.password:
                        raise OCRError('OCR 结果下载地址无效')
                    # Signed result links must never receive the service token.
                    response=await interruptible(client.get(url))
                    if not response.is_success:raise _error(response.status_code,{})
                    result=parse_result(response.text)
                    return {**result,'job_id':job_id,'elapsed_ms':round((time.monotonic()-started)*1000)}
                except httpx.RequestError:
                    error=OCRError('OCR 网络连接失败或超时，请稍后重试',category='network',retryable=True)
                except OCRError as exc:error=exc
                if not error.retryable or attempts>=len(self.retry_delays) or time.monotonic()>=deadline:raise error
                delay=self.retry_delays[attempts];attempts+=1
                progress(attempt=attempts+1,retrying=True,elapsed_ms=round((time.monotonic()-started)*1000))
                await interruptible(asyncio.sleep(delay))

    async def test(self,config):
        started=time.monotonic()
        stream=io.BytesIO();image=Image.new('RGB',(760,130),'white')
        ImageDraw.Draw(image).text((30,40),'PADDLE OCR TEST 2026',fill='black',font_size=42)
        image.save(stream,'JPEG',quality=90)
        try:
            result=await self.recognize(config,stream.getvalue())
            text=result['text'];ok=bool(text.strip())
            message='连接成功，OCR 已返回识别文字' if ok else '连接成功，但测试图片未识别出文字'
        except OCRError as exc:ok=False;text='';message=str(exc)
        return OCRTestResult(ok=ok,text=text,elapsed_ms=round((time.monotonic()-started)*1000),message=message)
