"""Phase 1C: prompt composition, preserved settings and conversation input shape.

These are offline contract checks. Nothing here asserts that generated wording is
natural or that the joking ratio improved; that needs a real model comparison.
"""
import json
from pathlib import Path
from types import SimpleNamespace
import unittest
from unittest.mock import patch

import choi_bot as bot
from bot import persona
from bot.conversation import ConversationQueue
from bot.llm.router import LLMRouter, task_policies
from tests.fakes import FakeProvider, FakeClient, FakeChannel, FakeInteraction

ROOT = Path(__file__).resolve().parents[1]
LEGACY_REVISION = json.loads((ROOT / 'tests/fixtures/legacy_contract.json')
                             .read_text(encoding='utf-8'))['legacy_prompt_revision']

# Verbatim excerpts of the operational role-play setting that must survive Phase 1C.
PRESERVED_BACKGROUND = [
    '- 남편: 김두멍(본명: 김주영, 특징: 파파존스 칵테일 메이커임)',
    '- 아들: 박주녁(본명: 박준혁, 특징: 똥을 못 싸서 변기에 오래 앉아있음, 삼도류를 사용함, 키가 많이 작음 그러나 최씨보단 큼)',
    '- 전우애: 박태민(특징: 전우애를 실시하는 무적해병임.)',
    '- 유기: 성탄종(본명: 성탄종, 특징: 디제이맥스의 장인이자 대전 성심당 카이스트의 수호자, 최씨가 유기해버렸음.)',
    '취미는 운동, 게임.',
    '최씨의 살아생전 별명: 뉴트리아, 게이, 할아버지, 할아브.',
    '로스트아크(줄여서 로아)에서 백전노장할아브라는 이름의 버서커를 육성했었어. 롤, 발로란트, 오버워치도 했었어.',
    '여기까지가 최씨의 주변인들이야.',
]


class PersonaCompositionTests(unittest.TestCase):
    def test_background_setting_is_preserved_verbatim(self):
        legacy = _legacy_character_prompt()
        for line in PRESERVED_BACKGROUND:
            self.assertIn(line, legacy, 'excerpt no longer matches the baseline prompt')
            self.assertIn(line, persona.BACKGROUND)
            self.assertIn(line, persona.CHARACTER_PROMPT)
            self.assertIn(line, persona.COMMAND_PERSONA)
        # Every 배경지식 line of the old prompt is either kept or deliberately relocated.
        section = legacy[legacy.index('[배경지식]'):legacy.index('[대화 종료 트리거]')]
        moved = [l for l in section.splitlines() if l.strip() and l not in persona.BACKGROUND]
        self.assertEqual(len(moved), 9, moved)  # Only the old tone sentences moved out.
        for line in moved:
            self.assertNotIn('본명:', line)

    def test_sections_are_separate_and_composable(self):
        names = ('IDENTITY', 'PRIORITY', 'SPEECH', 'INTERACTION', 'LENGTH',
                 'CONTROL', 'OUTPUT', 'COMMAND_OUTPUT', 'BACKGROUND')
        for name in names:
            self.assertTrue(getattr(persona, name).strip(), name)
        for section in (persona.IDENTITY, persona.SPEECH, persona.INTERACTION,
                        persona.LENGTH, persona.CONTROL, persona.BACKGROUND):
            self.assertIn(section, persona.CHARACTER_PROMPT)
        # Commands compose a different set; they never carry the control signals.
        self.assertNotIn(persona.CONTROL, persona.COMMAND_PERSONA)
        self.assertNotIn(persona.INTERACTION, persona.VOICE_ONLY)

    def test_tone_rules_replace_the_old_contradictory_ones(self):
        legacy = _legacy_character_prompt()
        for stale in ('음슴체(~했음/~임), 짧은 단답 위주', '무덤덤 + 유머 + 약간 회의적',
                      '감탄사(잉/뀨/힝 등)는 가끔만', '기본: 1~2줄'):
            self.assertIn(stale, legacy)
            self.assertNotIn(stale, persona.CHARACTER_PROMPT)
            self.assertNotIn(stale, persona.COMMAND_PERSONA)
        self.assertIn('평소 말투 3 : 장난식 말투 7', persona.SPEECH)
        self.assertIn('음슴체(~임, ~음, ~했음)만 반복하지 않는다', persona.SPEECH)

    def test_intended_verbal_habits_are_kept(self):
        for habit in ('잉', '힝', '뀨', '헤에', '야다', '아뇨아뇨', '~데숑', '~구만', 'ㅋㅋㅋ'):
            self.assertIn(habit, persona.CHARACTER_PROMPT, habit)
        # 힝/뀨 are absent from the source logs but are a deliberate character trait.
        self.assertIn('힝과 뀨는 실제 로그에서의 등장 여부와 무관하게', persona.SPEECH)

    def test_control_signals_match_the_runtime_exactly(self):
        for signal in ('00100, 관계성 부족', '00100, 다음 답변과 연계', '00100, 의미 없음'):
            self.assertIn(signal, persona.CONTROL)
        self.assertIn('(마이크 끄는 소리)', persona.CONTROL)
        # reply() matches on these substrings; spelling drift would break output control.
        source = (ROOT / 'choi_bot.py').read_text(encoding='utf-8')
        self.assertIn('"마이크 끄는 소리" in reply_text', source)
        self.assertIn('"00100" not in reply_text', source)

    def test_termination_recognition_and_non_termination_cases(self):
        for phrase in ('됐어', '나 갈게', '나 잘가', '잘자', '자야겠다', '끊어', '이따 봐'):
            self.assertIn(phrase, persona.CONTROL, phrase)
        self.assertIn('사용자가 다른 사람에게 작별 인사를 함 → 종료 아님', persona.CONTROL)
        self.assertIn('대화 중 짧게 맞장구를 침 → 종료 아님', persona.CONTROL)
        self.assertIn('맥락이 부족하다는 이유만으로 무조건 침묵하지 않는다', persona.CONTROL)

    def test_command_persona_forbids_conversation_control_signals(self):
        self.assertIn('00100이나 (마이크 끄는 소리) 같은 대화 제어 신호를 절대 출력하지 않는다',
                      persona.COMMAND_PERSONA)
        for signal in ('00100, 관계성 부족', '종료 표현의 예'):
            self.assertNotIn(signal, persona.COMMAND_PERSONA)


class ConversationPromptTests(unittest.TestCase):
    def test_latest_utterance_appears_exactly_once(self):
        history = ['박준혁: 최씨 뭐해', '최씨 봇: 그냥 있음']
        prompt = persona.build_conversation_prompt(
            history, '박준혁', '독특한신규발언xyz', new_conversation=False)
        self.assertEqual(prompt.count('독특한신규발언xyz'), 1)
        self.assertIn('[현재 발언]\n박준혁: 독특한신규발언xyz', prompt)
        previous = prompt.split('[이전 대화]\n')[1].split('\n\n[현재 발언]')[0]
        self.assertEqual(previous, '\n'.join(history))
        self.assertNotIn('독특한신규발언xyz', previous)

    def test_new_conversation_marks_empty_history(self):
        prompt = persona.build_conversation_prompt([], '문도', '최씨 뭐하냐', new_conversation=True)
        self.assertIn('[이전 대화]\n(없음. 지금 이 발언으로 대화가 시작됨.)', prompt)
        self.assertIn('이 발언으로 새로운 대화가 시작됐다.', prompt)
        self.assertEqual(prompt.count('최씨 뭐하냐'), 1)

    def test_prompt_carries_the_full_character_and_addressing_rule(self):
        prompt = persona.build_conversation_prompt([], 'A', 'x', new_conversation=True)
        self.assertIn(persona.CHARACTER_PROMPT, prompt)
        self.assertIn('현재 발언이 최씨에게 한 말인지, 다른 사람에게 한 말인지 구분한다.', prompt)
        self.assertIn('답변 본문만 출력한다.', prompt)


class ConversationHandlerTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.provider = FakeProvider()
        self.patches = [
            patch.object(bot, 'conversation', ConversationQueue(bot.process_conversation_message)),
            patch.object(bot, 'llm_router', LLMRouter({'gemini': self.provider}, task_policies(bot.MODEL))),
            patch.object(bot, 'client', FakeClient()), patch.object(bot, 'save__logs'),
        ]
        for p in self.patches:
            p.start()
            self.addCleanup(p.stop)
        self.addAsyncCleanup(bot.conversation.aclose)
        bot.conversation_context.clear()
        bot.active_users.clear()
        bot.last_conversation_time = 0

    def message(self, text, user='A'):
        return SimpleNamespace(content=text, author=SimpleNamespace(name=user), channel=FakeChannel(0))

    def prompts(self):
        return [request.messages[0].content for request, _ in self.provider.requests]

    async def test_both_branches_use_one_builder_without_duplicating_input(self):
        await bot.on_message(self.message('최씨 오늘 뭐함'))
        await bot.on_message(self.message('그럼 저녁에 게임 ㄱ?', 'B'))
        first, second = self.prompts()
        self.assertIn('이 발언으로 새로운 대화가 시작됐다.', first)
        self.assertEqual(first.count('최씨 오늘 뭐함'), 1)
        self.assertIn('이전 대화에서 이어지는 발언이다.', second)
        self.assertEqual(second.count('그럼 저녁에 게임 ㄱ?'), 1)
        self.assertIn('[이전 대화]\nA: 최씨 오늘 뭐함\n최씨 봇: 응답', second)
        # The user utterance is still recorded before generation, as before.
        self.assertEqual(list(bot.conversation_context),
                         ['A: 최씨 오늘 뭐함', '최씨 봇: 응답', 'B: 그럼 저녁에 게임 ㄱ?', '최씨 봇: 응답'])

    async def test_usermap_display_name_is_used_once(self):
        with patch.dict(bot.USER_MAP, {'jun_xx_': '박준혁'}):
            await bot.on_message(self.message('최씨 뭐함', 'jun_xx_'))
        prompt = self.prompts()[0]
        self.assertIn('[현재 발언]\n박준혁: 최씨 뭐함', prompt)
        self.assertNotIn('jun_xx_', prompt)

    async def test_silence_and_termination_handling_is_unchanged(self):
        from bot.llm.contracts import LLMResponse
        msg = self.message('최씨')
        await bot.reply(msg, LLMResponse('00100, 관계성 부족', 'gemini', bot.MODEL))
        msg.channel.send.assert_not_called()
        self.assertIn('최씨 봇: 00100, 관계성 부족', bot.conversation_context)
        await bot.reply(msg, LLMResponse('잘자라~ (마이크 끄는 소리)', 'gemini', bot.MODEL))
        msg.channel.send.assert_called_once_with('잘자라~ (마이크 끄는 소리)')
        self.assertFalse(bot.conversation_context)


class CommandPromptTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.provider = FakeProvider()
        self.patches = [
            patch.object(bot, 'llm_router', LLMRouter({'gemini': self.provider}, task_policies(bot.MODEL))),
            patch.object(bot, 'save__logs'), patch.object(bot, 'stopflag', 0),
        ]
        for p in self.patches:
            p.start()
            self.addCleanup(p.stop)

    async def last_prompt(self, command, **kwargs):
        await command.callback(FakeInteraction(), **kwargs)
        return self.provider.requests[-1][0].messages[0].content

    async def test_character_commands_share_the_command_persona(self):
        for command in (bot.질문, bot.알려줘, bot.자세히):
            prompt = await self.last_prompt(command, prompt='합성 질문')
            self.assertIn(persona.COMMAND_PERSONA, prompt, command.name)
            self.assertIn('[이번 작업]', prompt)
            self.assertIn('합성 질문', prompt)

    async def test_each_command_keeps_its_own_task_instruction(self):
        question = await self.last_prompt(bot.질문, prompt='q')
        self.assertIn('매번 장문의 해설을 붙이지 않는다', question)
        info = await self.last_prompt(bot.알려줘, prompt='q')
        self.assertIn('정확한 정보 제공이 목적이다', info)
        self.assertIn('장난스러운 수식어를 실제 정보와 섞어서 사실처럼 말하지 않는다', info)
        detail = await self.last_prompt(bot.자세히, prompt='q')
        self.assertIn('여기서는 1~2줄 제한을 적용하지 않는다', detail)
        self.assertIn('출력 제한: 2000자 이내로 답변한다', detail)
        # /자세히 no longer forces 음슴체 / Z세대 wording.
        for stale in ('음슴체로 답변해', 'Z세대의 말투를 사용해'):
            self.assertNotIn(stale, detail)
        self.assertNotEqual(question, info)
        self.assertNotEqual(info, detail)

    async def test_menu_keeps_format_and_gains_only_the_voice(self):
        await bot.점메추.callback(FakeInteraction(), message='매운거')
        candidates, final = [r.messages[0].content for r, _ in self.provider.requests[-2:]]
        self.assertNotIn(persona.VOICE_ONLY, candidates)  # Candidate pool stays mechanical.
        self.assertIn(persona.VOICE_ONLY, final)
        self.assertNotIn(persona.BACKGROUND, final)
        for line in ('**점심메뉴 추천**', '1. 메뉴명: 설명', '5. 메뉴명: 설명'):
            self.assertIn(line, final)
        self.assertIn('메뉴명은 그대로 두고, 각 설명만 최씨 말투로 짧게 쓴다', final)

    async def test_translation_and_summary_prompts_stay_free_of_persona(self):
        view = bot.TranslateView()
        view.message, view.target_lang = '안녕', '영어'
        await view.translate_callback(FakeInteraction())
        view.stop()
        translation = self.provider.requests[-1][0].messages[0].content
        for section in (persona.CHARACTER_PROMPT, persona.COMMAND_PERSONA, persona.VOICE_ONLY):
            self.assertNotIn(section, translation)
        self.assertIn('너는 번역기고', translation)
        source = (ROOT / 'choi_bot.py').read_text(encoding='utf-8')
        summary_block = source[source.index('async def summary('):source.index('@app_commands.command(name="요약"')]
        for name in ('CHARACTER_PROMPT', 'COMMAND_PERSONA', 'VOICE_ONLY'):
            self.assertNotIn(name, summary_block)  # Quoted logs must not be restyled.


class EvaluationCaseTests(unittest.TestCase):
    def setUp(self):
        self.data = json.loads((ROOT / 'tests/fixtures/style_eval_cases.json').read_text(encoding='utf-8'))

    def test_cases_are_well_formed_and_buildable(self):
        ids = [c['id'] for c in self.data['cases']]
        self.assertEqual(len(ids), len(set(ids)))
        self.assertGreaterEqual(len(ids), 15)
        for case in self.data['cases']:
            for key in ('id', 'basis', 'goal', 'history', 'speaker', 'message', 'expect'):
                self.assertIn(key, case, case['id'])
            prompt = persona.build_conversation_prompt(
                case['history'], case['speaker'], case['message'],
                new_conversation=not case['history'])
            # Count inside the conversation input only; the persona and the
            # instruction block are fixed text and may legitimately contain '최씨'.
            body = prompt.split('[이전 대화]\n', 1)[1].split('\n\n[응답 지시]')[0]
            self.assertEqual(body.count(case['message']), 1, case['id'])

    def test_case_coverage_includes_termination_and_silence(self):
        goals = ' '.join(c['goal'] for c in self.data['cases'])
        for goal in ('종료 인식', '종료 아님', '침묵', '맞장구', '오해 정정'):
            self.assertIn(goal, goals)
        self.assertEqual(sum(c['goal'] == '종료 인식' for c in self.data['cases']), 3)


def _legacy_character_prompt():
    """The pre-Phase-1C CHARACTER_PROMPT, read from git so no stale copy lives here.

    Pinned to the Phase 1B revision, not HEAD: once Phase 1C is committed HEAD no
    longer holds the prompt this comparison is against.
    """
    import subprocess
    source = subprocess.run(['git', 'show', f'{LEGACY_REVISION}:choi_bot.py'], cwd=ROOT,
                            capture_output=True, text=True, check=True).stdout
    start = source.index('CHARACTER_PROMPT = """') + len('CHARACTER_PROMPT = """')
    return source[start:source.index('"""', start)].replace('\r\n', '\n')


if __name__ == '__main__':
    unittest.main()
