import asyncio
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, patch

from google.api_core.exceptions import ResourceExhausted, InvalidArgument
from bot.llm.contracts import LLMError, LLMRequest, Message, TaskPolicy
from bot.llm.gemini import GeminiAdapter
from bot.llm.router import LLMRouter, legacy_policies
from tests.fakes import FakeProvider

MODEL = 'gemini-3.5-flash-lite'


class FakeSDK:
    def __init__(self, *results):
        self.results = list(results)
        self.keys = []
        self.calls = []
        self.models = []

    def configure(self, *, api_key):
        self.keys.append(api_key)

    def GenerativeModel(self, model):
        self.models.append(model)
        return self

    def generate_content(self, prompt, **kwargs):
        self.calls.append((prompt, kwargs))
        result = self.results.pop(0) if self.results else SimpleNamespace(text='응답')
        if isinstance(result, Exception):
            raise result
        return result


class LLMTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        # Deterministic executor boundary: tests never leave real workers running.
        loop = asyncio.get_running_loop()
        def execute(executor, function):
            future = loop.create_future()
            try:
                future.set_result(function())
            except Exception as error:
                future.set_exception(error)
            return future
        patcher = patch.object(loop, 'run_in_executor', side_effect=execute)
        patcher.start()
        self.addCleanup(patcher.stop)

    def request(self, task='chat', **kwargs):
        return LLMRequest(task, (Message('user', '원문\n그대로'),), **kwargs)

    async def test_router_selects_task_policy_and_propagates_common_error(self):
        provider = FakeProvider('text', LLMError('failure', error_type='quota', retryable=True, provider='gemini'))
        router = LLMRouter({'gemini': provider}, legacy_policies(MODEL))
        request = self.request('detail')
        response = await router.generate(request)
        self.assertEqual(response.text, 'text')
        self.assertIs(provider.requests[0][0], request)
        self.assertEqual(provider.requests[0][1], TaskPolicy('gemini', MODEL, False))
        with self.assertRaises(LLMError) as error:
            await router.generate(self.request())
        self.assertTrue(error.exception.retryable)
        with self.assertRaises(KeyError):
            await router.generate(self.request('unknown'))
        self.assertEqual(len(provider.requests), 2)

    async def test_sdk_conversion_preserves_payload_and_default_options(self):
        sdk = FakeSDK(SimpleNamespace(text='내용', usage_metadata=SimpleNamespace(prompt_token_count=3, candidates_token_count=2, total_token_count=5), candidates=[SimpleNamespace(finish_reason=SimpleNamespace(name='STOP'))]))
        adapter = GeminiAdapter(('fake',), MODEL, sdk=sdk)
        response = await adapter.generate(self.request(), legacy_policies(MODEL)['chat'])
        self.assertEqual(sdk.calls, [('원문\n그대로', {})])
        self.assertEqual(response.text, '내용')
        self.assertEqual(response.usage['total_token_count'], 5)
        self.assertEqual(response.finish_reason, 'STOP')
        self.assertGreaterEqual(response.latency, 0)
        self.assertEqual(response.model, MODEL)
        await adapter.generate(self.request(generation_options={'temperature': 0.3}), legacy_policies(MODEL)['chat'])
        self.assertEqual(sdk.calls[-1][1], {'generation_config': {'temperature': 0.3}})

    async def test_original_rr_sequence_and_nonadvancing_tasks(self):
        sdk = FakeSDK()
        adapter = GeminiAdapter(('first', 'second', 'third'), MODEL, sdk=sdk)
        selected = []
        for _ in range(9):
            await adapter.generate(self.request(), legacy_policies(MODEL)['chat'])
            selected.append(adapter.current_api_index)
        self.assertEqual(selected, [0,0,0,1,1,1,1,2,2])
        count = adapter.call_count
        await adapter.generate(self.request('question'), legacy_policies(MODEL)['question'])
        self.assertEqual(adapter.call_count, count)
        self.assertEqual(sdk.models, [MODEL]*3)

    async def test_quota_rotation_retry_budget(self):
        sdk = FakeSDK(ResourceExhausted('quota'), ResourceExhausted('quota'))
        adapter = GeminiAdapter(('first','second'), MODEL, sdk=sdk)
        with patch('bot.llm.gemini.asyncio.sleep', new_callable=AsyncMock) as sleep:
            with self.assertRaises(LLMError) as error:
                await adapter.generate(self.request(), legacy_policies(MODEL)['chat'])
        self.assertEqual(error.exception.error_type, 'quota')
        self.assertEqual(error.exception.status_code, 429)
        self.assertEqual(sdk.keys, ['first','second'])
        self.assertEqual(len(sdk.calls), 2)
        sleep.assert_awaited_once_with(0.5)

    async def test_timeout_retry_and_nonretryable_failure(self):
        sdk = FakeSDK(asyncio.TimeoutError(), SimpleNamespace(text='recovered'))
        adapter = GeminiAdapter(('first','second'), MODEL, sdk=sdk)
        with patch('bot.llm.gemini.asyncio.sleep', new_callable=AsyncMock):
            response = await adapter.generate(self.request(), legacy_policies(MODEL)['chat'])
        self.assertEqual(response.text, 'recovered')
        sdk.results = [InvalidArgument('bad input')]
        with self.assertRaises(LLMError) as error:
            await adapter.generate(self.request(), legacy_policies(MODEL)['chat'])
        self.assertEqual(error.exception.error_type, 'invalid_request')
        self.assertFalse(error.exception.retryable)
        self.assertEqual(len(sdk.calls), 3)

    async def test_actual_wait_for_timeout_and_cancellation(self):
        adapter = GeminiAdapter(('fake',), MODEL, sdk=FakeSDK())
        pending = asyncio.get_running_loop().create_future()
        with patch.object(asyncio.get_running_loop(), 'run_in_executor', return_value=pending):
            with self.assertRaises(LLMError) as error:
                await adapter.generate(self.request(timeout=0.001), legacy_policies(MODEL)['chat'])
        self.assertEqual(error.exception.error_type, 'timeout')
        pending = asyncio.get_running_loop().create_future()
        with patch.object(asyncio.get_running_loop(), 'run_in_executor', return_value=pending):
            task = asyncio.create_task(adapter.generate(self.request(), legacy_policies(MODEL)['chat']))
            await asyncio.sleep(0)
            task.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await task

    async def test_missing_empty_and_text_accessor_failure(self):
        class BrokenText:
            @property
            def text(self):
                raise ValueError('empty candidates')
        sdk = FakeSDK(SimpleNamespace(), SimpleNamespace(text=''), BrokenText())
        adapter = GeminiAdapter(('fake',), MODEL, sdk=sdk)
        policy = legacy_policies(MODEL)['chat']
        self.assertIsNone((await adapter.generate(self.request(), policy)).text)
        self.assertEqual((await adapter.generate(self.request(), policy)).text, '')
        with self.assertRaises(LLMError) as error:
            await adapter.generate(self.request(), policy)
        self.assertEqual(str(error.exception), 'empty candidates')

    async def test_unsupported_input_never_calls_sdk(self):
        sdk = FakeSDK()
        adapter = GeminiAdapter(('fake',), MODEL, sdk=sdk)
        with self.assertRaises(LLMError):
            await adapter.generate(LLMRequest('chat', (Message('system', 'unsupported'),)), legacy_policies(MODEL)['chat'])
        with self.assertRaises(LLMError):
            await adapter.generate(self.request(), TaskPolicy('gemini', 'other'))
        self.assertFalse(sdk.calls)
