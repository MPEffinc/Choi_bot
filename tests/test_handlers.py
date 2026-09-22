import os
from pathlib import Path
from types import SimpleNamespace
import tempfile
import time
import unittest
from unittest.mock import patch, AsyncMock

import choi_bot as bot
from bot.conversation import ConversationQueue
from bot.llm.contracts import LLMError, LLMResponse
from bot.llm.router import LLMRouter, task_policies
from tests.fakes import temp_log_store, FakeProvider, FakeClient, FakeChannel, FakeInteraction


class HandlerTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.provider = FakeProvider()
        self.patches = [patch.object(bot, 'conversation', ConversationQueue(bot.process_conversation_message)), patch.object(bot, 'llm_router', LLMRouter({'gemini': self.provider}, task_policies(bot.MODEL))),
                        patch.object(bot, 'client', FakeClient()), patch.object(bot, 'API_KEYS', ('fake',)),
                        patch.object(bot, 'save__logs'), patch.object(bot, 'stopflag', 0),
                        patch.object(bot, 'log_store', temp_log_store(self))]
        for p in self.patches:
            p.start()
            self.addCleanup(p.stop)
        self.addAsyncCleanup(bot.conversation.aclose)
        bot.conversation_context.clear()
        bot.active_users.clear()
        bot.last_conversation_time = 0

    def message(self, text, user='A', channel=0):
        return SimpleNamespace(content=text, author=SimpleNamespace(name=user), channel=FakeChannel(channel))

    def tasks(self):
        return [request.task_type for request, _ in self.provider.requests]

    async def test_shared_group_context_and_prompts(self):
        first = self.message('최씨 뭐해?')
        await bot.on_message(first)
        other_channel = next(c for c in bot.ALLOWED_CH if c != 0)
        second = self.message('무슨 게임?', 'B', other_channel)
        await bot.on_message(second)
        self.assertEqual(self.tasks(), ['chat', 'chat'])
        a, b = [req.messages[0].content for req, _ in self.provider.requests]
        self.assertIn(bot.CHARACTER_PROMPT, a)
        self.assertIn('이 발언으로 새로운 대화가 시작됐다.', a)
        self.assertIn('[현재 발언]\nA: 최씨 뭐해?', a)
        self.assertIn('(없음. 지금 이 발언으로 대화가 시작됨.)', a)
        self.assertIn('이전 대화에서 이어지는 발언이다.', b)
        self.assertIn('A: 최씨 뭐해?', b)
        self.assertIn('최씨 봇: 응답', b)
        # The new utterance belongs to [현재 발언] only, never also to [이전 대화].
        self.assertEqual(b.count('B: 무슨 게임?'), 1)
        self.assertIn('[현재 발언]\nB: 무슨 게임?', b)
        self.assertEqual(len(bot.conversation_context), 4)
        self.assertTrue(all(policy.model == bot.MODEL for _, policy in self.provider.requests))

    async def test_ignore_and_expiration_behavior(self):
        msg = self.message('최씨')
        msg.author = bot.client.user
        await bot.on_message(msg)
        bot.save__logs.assert_not_called()
        await bot.on_message(self.message('최씨', channel=-1))
        bot.save__logs.assert_called_once()
        for text in ('영원', '<@123>', '일반 대화'):
            await bot.on_message(self.message(text))
        self.assertEqual(self.tasks(), [])
        bot.update_context('A', 'expired')
        bot.last_conversation_time = time.time() - 121
        await bot.on_message(self.message('최씨'))
        self.assertEqual(self.tasks(), ['chat'])  # Expiry is now cleared before an explicit call.

    async def test_silence_end_and_empty_text_controls(self):
        msg = self.message('최씨')
        await bot.reply(msg, LLMResponse('00100', 'gemini', bot.MODEL))
        msg.channel.send.assert_not_called()
        self.assertIn('최씨 봇: 00100', bot.conversation_context)
        bot.save__logs.assert_called_with('최씨 봇', '00100')
        await bot.reply(msg, LLMResponse('00100 (마이크 끄는 소리)', 'gemini', bot.MODEL))
        msg.channel.send.assert_called_once_with('00100 (마이크 끄는 소리)')
        self.assertFalse(bot.conversation_context)
        self.assertFalse(bot.active_users)
        await bot.reply(msg, LLMResponse(None, 'gemini', bot.MODEL))
        msg.channel.send.assert_called_with('응애! 대답할 수 없음!')

    async def test_information_commands(self):
        for command, task in [(bot.질문, 'question'), (bot.알려줘, 'info'), (bot.자세히, 'detail')]:
            interaction = FakeInteraction()
            await command.callback(interaction, prompt='합성 질문')
            self.assertEqual(self.tasks()[-1], task)
            self.assertTrue(any(c == 'Q. 합성 질문\nA. 응답' for c, _, _ in interaction.channel.sent))
            self.assertEqual(self.provider.requests[-1][1].model, bot.MODEL)

    async def test_summary_and_search_map_reduce(self):
        old = os.getcwd()
        with tempfile.TemporaryDirectory() as directory:
            try:
                os.chdir(directory)
                Path('logs').mkdir()
                Path('logs/2026-01-01.txt').write_text('[2026-01-01 12:34:56] A: 첫 줄\n이어지는 줄\n', encoding='utf-8')
                for flag, prefix in [(0, 'summary'), (1, 'search')]:
                    interaction = FakeInteraction()
                    await bot.summary(interaction, '2026-01-01', flag, '주제')
                    self.assertEqual(self.tasks()[-2:], [prefix+'_map', prefix+'_reduce'])
                    prompt = self.provider.requests[-2][0].messages[0].content
                    self.assertIn('[12:34] A: 첫 줄', prompt)
                    self.assertNotIn('이어지는 줄', prompt)  # Existing parser unchanged.
                    self.assertIn('응답', interaction.edit_original_response.call_args.kwargs['content'])
            finally:
                os.chdir(old)

    async def test_summary_router_retry_and_long_output(self):
        self.provider.results = [LLMError('overload', error_type='unavailable', retryable=True, provider='gemini'), '부분', '가'*2001]
        old = os.getcwd()
        with tempfile.TemporaryDirectory() as directory, patch.object(bot, 'API_KEYS', ('one','two')), patch('choi_bot.asyncio.sleep', new_callable=AsyncMock):
            try:
                os.chdir(directory)
                Path('logs').mkdir()
                Path('logs/2026-01-01.txt').write_text('[2026-01-01 12:00:00] A: 내용\n')
                interaction = FakeInteraction()
                await bot.summary(interaction, '2026-01-01', 0)
                self.assertEqual(self.tasks(), ['summary_map', 'summary_map', 'summary_reduce'])
                notation = interaction.channel.sent[0][2]
                self.assertEqual(len(self.provider.requests), 3)
                self.assertTrue(all(len(c) <= 2000 for c, _, _ in interaction.channel.sent))
            finally:
                os.chdir(old)

    async def test_menu_both_commands_and_translation(self):
        for command, meal in [(bot.점메추, '점심'), (bot.저메추, '저녁')]:
            await command.callback(FakeInteraction(), message='매운 음식')
            self.assertEqual(self.tasks()[-2:], ['menu_candidates', 'menu_select'])
            self.assertIn(meal, self.provider.requests[-2][0].messages[0].content)
        view = bot.TranslateView()
        interaction = FakeInteraction()
        await view.translate_callback(interaction)
        self.assertEqual(len(self.tasks()), 4)
        view.message = '안녕'
        view.target_lang = '영어'
        interaction = FakeInteraction()
        await view.translate_callback(interaction)
        self.assertEqual(self.tasks()[-1], 'translation')
        self.assertIn('영어', self.provider.requests[-1][0].messages[0].content)
        self.assertIn('응답', interaction.edit_original_response.call_args.kwargs['content'])
        view.stop()

    async def test_common_error_preserves_user_error_output(self):
        self.provider.results = [LLMError('fake failure', error_type='provider_error', retryable=False, provider='gemini')]
        msg = self.message('최씨')
        await bot.on_message(msg)
        msg.channel.send.assert_called_with('잉! 잘못된 명령 발생! fake failure')

    async def test_roles_and_stop(self):
        interaction = FakeInteraction()
        await bot.알림.callback(interaction, role='123')
        interaction.user.add_roles.assert_awaited_once()
        interaction = FakeInteraction()
        await bot.해제.callback(interaction, role='123')
        interaction.user.remove_roles.assert_awaited_once()
        bot.update_context('A', '내용')
        await bot.stop.callback(FakeInteraction())
        self.assertFalse(bot.conversation_context)
        self.assertEqual(self.tasks(), [])

    async def test_announcement_preserves_thread_and_mentions(self):
        interaction = FakeInteraction()
        with patch.object(bot.discord, 'TextChannel', FakeChannel):
            await bot.공지.callback(interaction, title='제목', content='내용')
        channel = bot.client.announcement
        content, options, message = channel.sent[0]
        self.assertEqual(content, '# **제목**\nby: @tester\n내용')
        self.assertTrue(options['allowed_mentions'].everyone)
        message.create_thread.assert_awaited_once_with(
            name='제목', auto_archive_duration=1440, reason='공지 스레드 자동 생성')
        interaction.followup.send.assert_awaited_once()
        self.assertTrue(interaction.followup.send.call_args.kwargs['ephemeral'])

    async def test_ready_registers_background_loops(self):
        tree = SimpleNamespace(clear_commands=lambda **kw: None, sync=AsyncMock(return_value=[]))
        bot.client.change_presence = AsyncMock()
        with patch.object(bot, 'tree', tree), \
             patch.object(bot.send_announcement, 'start') as announcement, \
             patch.object(bot.check_context, 'start') as context, \
             patch.object(bot.send_waist, 'start') as waist:
            await bot.on_ready()
        self.assertEqual(tree.sync.await_count, 2)
        for start in (announcement, context, waist):
            start.assert_called_once()

    async def test_static_commands_and_summary_wrappers(self):
        for command in (bot.test, bot.정보, bot.후앰아이, bot.패치노트, bot.언제와, bot.유저):
            interaction = FakeInteraction()
            await command.callback(interaction)
            self.assertTrue(interaction.channel.sent, command.name)
        with patch.object(bot, 'summary', new_callable=AsyncMock) as summary:
            interaction = FakeInteraction()
            await bot.요약.callback(interaction, date='2026-01-01')
            summary.assert_awaited_with(interaction, '2026-01-01', 0)
            await bot.찾기.callback(interaction, date='2026-01-01', find='게임')
            summary.assert_awaited_with(interaction, '2026-01-01', 1, '게임')

    async def test_translation_command_view_and_menu_missing_text(self):
        interaction = FakeInteraction()
        await bot.번역.callback(interaction)
        view = interaction.channel.sent[0][1]['view']
        self.assertEqual(view.timeout, 300)
        self.assertIsNotNone(view.original_message)
        view.stop()
        self.provider.results = ['후보', None]
        interaction = FakeInteraction()
        await bot.점메추.callback(interaction)
        self.assertEqual(interaction.edit_original_response.call_args.kwargs['content'], '응애! 대답할 수 없음!')

    async def test_config_and_log_command(self):
        await bot.config.callback(FakeInteraction(), command='summary', value='False')
        interaction = FakeInteraction()
        await bot.summary(interaction, 'not-read', 0)
        self.assertIn('서비스를 중지', interaction.channel.sent[0][0])
        await bot.config.callback(FakeInteraction(), command='summary', value='True')
        self.assertEqual(bot.stopflag, 0)
        with patch.object(bot, 'USER_MAP', {}):
            await bot.config.callback(FakeInteraction(), command='user', value='fake', args='합성 사용자')
            self.assertEqual(bot.USER_MAP, {'fake': '합성 사용자'})
        with tempfile.TemporaryDirectory() as directory, patch.object(bot, 'LOG_FOLDER', directory):
            Path(directory, '2026-01-01.txt').write_text('first\nlast\n')
            interaction = FakeInteraction()
            await bot.로그.callback(interaction, n=1)
            self.assertIn('last', interaction.channel.sent[-1][0])
            self.assertNotIn('first', interaction.channel.sent[-1][0])
