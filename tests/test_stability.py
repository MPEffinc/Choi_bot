import asyncio
from pathlib import Path
import os
import tempfile
import unittest
from unittest.mock import patch, AsyncMock

import choi_bot as bot
from bot.llm.contracts import LLMResponse
from bot.discord_output import send, edit, split_text
from tests import test_handlers
from tests.fakes import FakeInteraction, FakeMessage


class StabilityTests(unittest.IsolatedAsyncioTestCase):
    asyncSetUp = test_handlers.HandlerTests.asyncSetUp
    message = test_handlers.HandlerTests.message
    tasks = test_handlers.HandlerTests.tasks
    async def test_fifo_and_stop_invalidates_pending_and_late_result(self):
        entered=asyncio.Event(); release=asyncio.Event()
        async def generate(*args, **kwargs):
            entered.set()
            try: await release.wait()
            except asyncio.CancelledError: await release.wait()
            return LLMResponse('늦은 응답','gemini',bot.MODEL)
        a=self.message('최씨 첫 질문'); b=self.message('후속 질문','B')
        with patch.object(bot,'generate_content_timeout',side_effect=generate) as gen:
            first=asyncio.create_task(bot.on_message(a))
            await entered.wait()
            second=asyncio.create_task(bot.on_message(b))
            await asyncio.sleep(0)
            await bot.stop.callback(FakeInteraction())
            release.set()
            await asyncio.gather(first,second)
            self.assertEqual(gen.call_count,1)
        a.channel.send.assert_not_called();b.channel.send.assert_not_called()
        self.assertFalse(bot.conversation_context)
        await bot.on_message(self.message('최씨 새 질문'))
        self.assertEqual(len(bot.conversation_context),2)

    async def test_fifo_preserves_multi_user_order(self):
        entered=asyncio.Event(); release=asyncio.Event();prompts=[]
        async def generate(prompt,**kw):
            prompts.append(prompt)
            if len(prompts)==1:
                entered.set();await release.wait()
            return LLMResponse(str(len(prompts)),'gemini',bot.MODEL)
        with patch.object(bot,'generate_content_timeout',side_effect=generate):
            first=asyncio.create_task(bot.on_message(self.message('최씨 A','A')))
            await entered.wait()
            second=asyncio.create_task(bot.on_message(self.message('B 질문','B')))
            await asyncio.sleep(0)
            self.assertEqual(len(prompts),1)
            release.set();await asyncio.gather(first,second)
        self.assertEqual(list(bot.conversation_context),['A: 최씨 A','최씨 봇: 1','B: B 질문','최씨 봇: 2'])
        self.assertIn('최씨 봇: 1',prompts[1])

    async def test_natural_end_pending_explicit_call_starts_new_epoch(self):
        entered=asyncio.Event(); release=asyncio.Event();calls=[]
        async def generate(prompt,**kw):
            calls.append(prompt)
            if len(calls)==1:
                entered.set();await release.wait()
                return LLMResponse('(마이크 끄는 소리)','gemini',bot.MODEL)
            return LLMResponse('새 대화','gemini',bot.MODEL)
        with patch.object(bot,'generate_content_timeout',side_effect=generate):
            a=asyncio.create_task(bot.on_message(self.message('최씨 종료')))
            await entered.wait()
            b=asyncio.create_task(bot.on_message(self.message('최씨 새 질문','B')))
            await asyncio.sleep(0);release.set();await asyncio.gather(a,b)
        self.assertIn('[새로운 대화 시작됨.]',calls[1])
        self.assertEqual(list(bot.conversation_context),['B: 최씨 새 질문','최씨 봇: 새 대화'])

    async def test_translation_snapshot(self):
        view=bot.TranslateView();view.message='처음 문장';view.target_lang='영어'
        async def generate(prompt,**kw):
            self.assertIn('처음 문장',prompt)
            view.message='변경 문장';view.target_lang='일본어'
            return LLMResponse('original','gemini',bot.MODEL)
        interaction=FakeInteraction()
        with patch.object(bot,'generate_content_timeout',side_effect=generate):
            await view.translate_callback(interaction)
        result=interaction.edit_original_response.call_args.kwargs['content']
        self.assertIn('처음 문장',result);self.assertIn('영어',result);self.assertNotIn('일본어',result)
        view.stop()

    async def test_progress_failure_never_regenerates(self):
        old=os.getcwd()
        with tempfile.TemporaryDirectory() as directory:
            try:
                os.chdir(directory);Path('logs').mkdir()
                Path('logs/2026-01-01.txt').write_text('[2026-01-01 12:00:00] A: 내용\n')
                message=FakeMessage();message.edit.side_effect=RuntimeError('deleted')
                with patch.object(bot,'progress_send',AsyncMock(return_value=message)):
                    await bot.summary(FakeInteraction(),'2026-01-01',0)
                self.assertEqual(self.tasks(),['summary_map','summary_reduce'])
            finally:os.chdir(old)

    async def test_expiry_boundary(self):
        bot.update_context('A','hello')
        bot.last_conversation_time=100
        with patch.object(bot.time,'time',return_value=220): self.assertTrue(bot.is_alive())
        with patch.object(bot.time,'time',return_value=220.001): self.assertFalse(bot.is_alive())


class OutputTests(unittest.IsolatedAsyncioTestCase):
    async def test_followup_and_long_output_policy(self):
        interaction=FakeInteraction()
        await interaction.response.defer(ephemeral=True)
        await send(interaction,'가'*4100,ephemeral=True)
        self.assertEqual(interaction.followup.send.await_count,3)
        self.assertTrue(all(c.kwargs['ephemeral'] for c in interaction.followup.send.call_args_list))
        self.assertTrue(all(len(c.args[0])<=2000 for c in interaction.followup.send.call_args_list))
        self.assertEqual(''.join(split_text('😀'*1500)),'😀'*1500)
        self.assertTrue(all(len(c.encode('utf-16-le'))//2<=2000 for c in split_text('😀'*1500)))

    async def test_original_edit_keeps_ephemeral_for_overflow(self):
        interaction=FakeInteraction();await interaction.response.defer(ephemeral=True)
        original=FakeMessage();original.flags.ephemeral=True
        interaction.edit_original_response.return_value=original
        await edit(interaction,'x'*2200)
        self.assertTrue(interaction.followup.send.call_args.kwargs['ephemeral'])


class LifecycleTests(unittest.IsolatedAsyncioTestCase):
    async def test_client_shutdown_closes_services(self):
        client = bot.ChoiClient(intents=bot.discord.Intents.default())
        conversation = AsyncMock()
        router = AsyncMock()
        with patch.object(bot, 'conversation', conversation), patch.object(bot, 'llm_router', router):
            await client.close()
        conversation.aclose.assert_awaited_once()
        router.aclose.assert_awaited_once()
        self.assertTrue(client.is_closed())
