import asyncio
import json
import traceback
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, Mock

import httpx
from google import genai
from google.genai import errors, types

from bot.llm.contracts import LLMError, LLMRequest, Message, TaskPolicy
from bot.llm.gemini import GeminiAdapter, normalize_error
from bot.llm.router import LLMRouter, task_policies
from tests.fakes import FakeProvider

MODEL = 'gemini-3.5-flash-lite'


def response(text='응답', reason='STOP', **kwargs):
    return types.GenerateContentResponse(candidates=[types.Candidate(
        content=types.Content(parts=[types.Part(text=text)]), finish_reason=reason)], **kwargs)


def api_error(code, *, reason=None, retry=None, quota=None, headers=None):
    details = []
    if reason:
        details.append({'reason': reason})
    if retry is not None:
        details.append({'@type':'type.googleapis.com/google.rpc.RetryInfo', 'retryDelay':f'{retry}s'})
    if quota:
        details.append({'violations':[{'quotaId':quota}]})
    return errors.APIError(code, {'error':{'message':'secret-A secret-B', 'details':details}},
                           httpx.Response(code, headers=headers or {}))


class Factory:
    def __init__(self, *results):
        self.results = list(results)
        self.clients = []
        self.options = []

    def __call__(self, **kwargs):
        self.options.append(kwargs)
        async def generate(**call):
            result = self.results.pop(0) if self.results else response()
            if isinstance(result, Exception):
                raise result
            return result
        client = SimpleNamespace(aio=SimpleNamespace(models=SimpleNamespace(
            generate_content=AsyncMock(side_effect=generate)), aclose=AsyncMock()), close=Mock())
        self.clients.append(client)
        return client


class AdapterTests(unittest.IsolatedAsyncioTestCase):
    def adapter(self, factory=None, keys=('secret-A', 'secret-B'), **kwargs):
        self.factory = factory or Factory()
        adapter = GeminiAdapter(keys, MODEL, client_factory=self.factory, **kwargs)
        self.addAsyncCleanup(adapter.aclose)
        return adapter

    def request(self, **kwargs):
        return LLMRequest('chat', (Message('user','원문\n그대로'),), **kwargs)

    async def test_bound_client_model_and_defaults(self):
        adapter = self.adapter()
        policy = task_policies(MODEL)['chat']
        first = adapter.bind(self.request(), policy)
        second = adapter.bind(self.request(), policy)
        self.assertNotEqual(first.key_id, second.key_id)
        for bound in (second, first, first):
            await bound.generate(self.request(), policy, 2)
        self.assertEqual([k.attempts for k in adapter.keys], [2,1])
        self.assertEqual([c.aio.models.generate_content.await_count for c in self.factory.clients], [2,1])
        for factory_options in self.factory.options:
            self.assertFalse(factory_options['vertexai'])
            self.assertEqual(factory_options['http_options'].retry_options.attempts, 1)
        call = self.factory.clients[0].aio.models.generate_content.call_args.kwargs
        self.assertEqual(call['model'], MODEL)
        self.assertEqual(call['contents'], '원문\n그대로')
        self.assertIsNone(call['config'].temperature)
        self.assertIsNone(call['config'].thinking_config)
        self.assertIsNone(call['config'].system_instruction)
        self.assertEqual(call['config'].http_options.timeout, 2000)
        self.assertEqual(call['config'].http_options.retry_options.attempts, 1)
        self.assertTrue(call['config'].automatic_function_calling.disable)
        first.release(); second.release()
        self.assertEqual([k.reservations for k in adapter.keys], [0,0])

    async def test_usage_visible_text_and_finish_reason(self):
        raw = response('부분', 'MAX_TOKENS', model_version='actual-model', usage_metadata=types.GenerateContentResponseUsageMetadata(
            prompt_token_count=5,candidates_token_count=3,total_token_count=8))
        raw.candidates[0].content.parts.insert(0, types.Part(text='hidden thought', thought=True))
        adapter = self.adapter(Factory(raw))
        result = await LLMRouter({'gemini':adapter},task_policies(MODEL)).generate(self.request())
        self.assertEqual(result.text,'부분')
        self.assertEqual(result.finish_reason,'MAX_TOKENS')
        self.assertEqual(result.usage['total_token_count'],8)
        self.assertEqual(result.model_version,'actual-model')
        self.assertEqual(adapter.keys[0].usage['total_token_count'],8)
        self.assertEqual(result.attempt_count,1)

    async def test_blocked_empty_invalid_response_do_not_retry(self):
        cases = [(types.GenerateContentResponse(prompt_feedback=types.GenerateContentResponsePromptFeedback(block_reason='SAFETY')),'blocked'),
                 (response('text','SAFETY'),'blocked'), (types.GenerateContentResponse(),'empty_response'),
                 (response(''),'empty_response'), (response('text','MALFORMED_FUNCTION_CALL'),'invalid_response')]
        for raw, kind in cases:
            with self.subTest(kind=kind):
                adapter = self.adapter(Factory(raw))
                with self.assertRaises(LLMError) as error:
                    await LLMRouter({'gemini':adapter},task_policies(MODEL)).generate(self.request())
                self.assertEqual(error.exception.error_type,kind)
                self.assertEqual(error.exception.attempt_count,1)

    async def test_error_redaction_and_disabled_auth_key(self):
        adapter = self.adapter(Factory(api_error(400,reason='API_KEY_INVALID'), response()))
        router = LLMRouter({'gemini':adapter},task_policies(MODEL))
        try:
            await router.generate(self.request())
        except LLMError as error:
            self.assertEqual(error.error_type,'authentication')
            output = ''.join(traceback.format_exception(type(error),error,error.__traceback__))
            self.assertNotIn('secret-A',output)
            self.assertNotIn('secret-B',output)
        else:
            self.fail('Authentication error expected')
        self.assertTrue(adapter.keys[0].disabled)
        await router.generate(self.request())
        self.assertEqual([k.attempts for k in adapter.keys],[1,1])
        self.assertNotIn('secret-A',repr(adapter.keys))
        self.assertNotIn('secret-A',repr(router.metrics))

    async def test_unknown_quota_group_stops_key_churn(self):
        adapter = self.adapter(Factory(api_error(429)))
        router = LLMRouter({'gemini':adapter},task_policies(MODEL))
        with self.assertRaises(LLMError) as error:
            await router.generate(self.request())
        self.assertEqual(error.exception.error_type,'quota_unknown')
        with self.assertRaises(LLMError) as error:
            await router.generate(self.request())
        self.assertEqual(error.exception.error_type,'deadline')
        self.assertEqual(sum(k.attempts for k in adapter.keys),1)
        self.assertEqual(len(adapter.groups),1)

    async def test_long_quota_and_known_independent_groups(self):
        adapter = self.adapter(Factory(api_error(429,quota='GenerateRequestsPerDayPerProject'),response()),
                               quota_groups={'key1':'project-a','key2':'project-b'})
        router = LLMRouter({'gemini':adapter},task_policies(MODEL))
        with self.assertRaises(LLMError) as error:
            await router.generate(self.request())
        self.assertEqual(error.exception.error_type,'quota_long')
        await router.generate(self.request())
        self.assertTrue(adapter.groups[('project-a',MODEL)].blocked)
        self.assertFalse(adapter.groups[('project-b',MODEL)].blocked)

    async def test_partial_keys_and_lifecycle(self):
        adapter = self.adapter(keys=('', 'secret-B'),key_ids=('key1','key5'))
        self.assertEqual([k.key_id for k in adapter.keys],['key5'])
        await adapter.aclose()
        await adapter.aclose()
        self.factory.clients[0].aio.aclose.assert_awaited_once()
        self.factory.clients[0].close.assert_called_once()
        with self.assertRaises(LLMError):
            adapter.bind(self.request(),task_policies(MODEL)['chat'])

    async def test_error_classification_and_retry_after(self):
        cases = [(api_error(429,retry=2,quota='RequestsPerMinute'),'rate_limit',True),
                 (api_error(429,quota='TokensPerDay'),'quota_long',False),
                 (api_error(503,headers={'Retry-After':'3'}),'unavailable',True),
                 (api_error(403),'permission',False),(api_error(400),'invalid_request',False),
                 (httpx.ConnectError('secret-A'),'network',True)]
        for raw,kind,retry in cases:
            error=normalize_error(raw)
            self.assertEqual((error.error_type,error.retryable),(kind,retry))
            self.assertNotIn('secret-A',str(error))
        self.assertEqual(normalize_error(cases[0][0]).retry_after,2)
        self.assertEqual(normalize_error(cases[2][0]).retry_after,3)

    async def test_real_sdk_mock_transport_one_http_attempt_and_payload(self):
        requests=[]
        async def handle(request):
            requests.append(request)
            return httpx.Response(503,json={'error':{'code':503,'message':'test'}})
        http = httpx.AsyncClient(transport=httpx.MockTransport(handle),trust_env=False)
        self.addAsyncCleanup(http.aclose)
        def factory(**kwargs):
            kwargs['http_options'].httpx_async_client=http
            return genai.Client(**kwargs)
        adapter=GeminiAdapter(('secret-A',),MODEL,client_factory=factory)
        self.addAsyncCleanup(adapter.aclose)
        request=self.request()
        policy=task_policies(MODEL)['chat']
        bound=adapter.bind(request,policy)
        with self.assertRaises(LLMError):
            await bound.generate(request,policy,2)
        bound.release()
        self.assertEqual(len(requests),1)  # Proves SDK retry disabled on actual HTTP path.
        self.assertEqual(requests[0].headers['x-goog-api-key'],'secret-A')
        payload=json.loads(requests[0].content)
        self.assertEqual(payload['contents'],[{'parts':[{'text':'원문\n그대로'}],'role':'user'}])
        self.assertNotIn('systemInstruction',payload)
        self.assertIn(MODEL,str(requests[0].url))


class RouterTests(unittest.IsolatedAsyncioTestCase):
    def request(self):
        return LLMRequest('chat',(Message('user','test'),))

    def router(self,provider,**kwargs):
        policies=kwargs.pop('policies',{'chat':TaskPolicy('gemini',MODEL,0.2,1,2,0,0)})
        router=LLMRouter({'gemini':provider},policies,**kwargs)
        self.addAsyncCleanup(router.aclose)
        return router

    async def test_retry_attempt_count_and_nonretryable(self):
        provider=FakeProvider(LLMError('temporary',error_type='unavailable',retryable=True,provider='gemini'),'ok')
        result=await self.router(provider).generate(self.request())
        self.assertEqual(result.attempt_count,2)
        self.assertEqual(len(provider.requests),2)
        provider=FakeProvider(LLMError('bad',error_type='invalid_request',retryable=False,provider='gemini'))
        with self.assertRaises(LLMError) as error:
            await self.router(provider).generate(self.request())
        self.assertEqual(error.exception.attempt_count,1)

    async def test_retry_after_and_deadline_prevents_next_attempt(self):
        now=[0.0]; sleeps=[]
        async def sleep(delay):
            sleeps.append(delay);now[0]+=delay
        provider=FakeProvider(LLMError('429',error_type='rate_limit',retryable=True,provider='gemini',retry_after=3),'ok')
        router=self.router(provider,clock=lambda:now[0],sleep=sleep,
                           policies={'chat':TaskPolicy('gemini',MODEL,5,10,2,0.5)})
        result=await router.generate(self.request())
        self.assertEqual(sleeps,[3]);self.assertEqual(result.attempt_count,2)
        provider=FakeProvider(LLMError('429',error_type='rate_limit',retryable=True,provider='gemini',retry_after=20))
        with self.assertRaises(LLMError) as error:
            await self.router(provider).generate(self.request())
        self.assertEqual(error.exception.error_type,'deadline')
        self.assertEqual(len(provider.requests),1)

    async def test_concurrency_limit_and_independent_requests(self):
        release=asyncio.Event(); entered=asyncio.Event();active=0;peak=0
        class Provider(FakeProvider):
            async def generate(inner,request,policy,timeout):
                nonlocal active,peak
                active+=1;peak=max(peak,active)
                if active==2:entered.set()
                try:
                    await release.wait()
                    return await super().generate(request,policy,timeout)
                finally:active-=1
        provider=Provider();router=self.router(provider,max_concurrency=2)
        tasks=[asyncio.create_task(router.generate(self.request())) for _ in range(3)]
        await asyncio.wait_for(entered.wait(),1)
        self.assertEqual(peak,2)
        release.set()
        await asyncio.gather(*tasks)
        self.assertEqual(peak,2)

    async def test_timeout_cancellation_and_late_result_discard(self):
        started=asyncio.Event();release=asyncio.Event();finished=asyncio.Event()
        class Resistant(FakeProvider):
            async def generate(inner,request,policy,timeout):
                started.set()
                try:await release.wait()
                except asyncio.CancelledError:await release.wait()
                finished.set()
                return await super().generate(request,policy,timeout)
        router=self.router(Resistant(),max_concurrency=1,
                           policies={'chat':TaskPolicy('gemini',MODEL,0.02,0.05,2,0,0)})
        try:
            with self.assertRaises(LLMError) as error:
                await router.generate(self.request())
            self.assertEqual(error.exception.error_type,'timeout')
            self.assertEqual(error.exception.attempt_count,1)
            self.assertEqual(len(router._inflight),1)
            with self.assertRaises(LLMError) as error:
                await router.generate(self.request())
            self.assertEqual(error.exception.error_type,'deadline')
            self.assertEqual(error.exception.attempt_count,0)
        finally:
            release.set()
            await asyncio.wait_for(finished.wait(),1)
        await asyncio.sleep(0)
        self.assertFalse(router._inflight)

    async def test_user_cancel_propagates(self):
        entered=asyncio.Event();cancelled=asyncio.Event()
        class Waiting(FakeProvider):
            async def generate(inner,*args):
                entered.set()
                try:await asyncio.Event().wait()
                finally:cancelled.set()
        router=self.router(Waiting())
        task=asyncio.create_task(router.generate(self.request()))
        await entered.wait();task.cancel()
        with self.assertRaises(asyncio.CancelledError):await task
        await asyncio.wait_for(cancelled.wait(),1)
        self.assertEqual(router.metrics[-1]['outcome'],'cancelled')

    async def test_request_snapshot_and_task_routing(self):
        options={'temperature':0.2};request=LLMRequest('detail',(Message('user','test'),),generation_options=options)
        options['temperature']=1
        self.assertEqual(request.generation_options['temperature'],0.2)
        provider=FakeProvider()
        router=self.router(provider,policies=task_policies(MODEL))
        result=await router.generate(request)
        self.assertEqual(result.model,MODEL)
        self.assertEqual(provider.requests[0][1],task_policies(MODEL)['detail'])
