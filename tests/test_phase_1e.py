"""Offline transport contracts and actual Discord message ownership semantics."""
import json
from pathlib import Path
from types import SimpleNamespace
import unittest
import tempfile
from unittest.mock import AsyncMock, patch
import discord
import choi_bot as bot
from bot import discord_output as output
from bot.llm.contracts import LLMRequest, Message, LLMError
from bot.llm.gemini import GeminiAdapter
from bot.llm.router import LLMRouter, task_policies
from scripts.phase_1e_candidates import BASE, build, EXAMPLES, EXAMPLE_SOURCES, ROOT
from scripts.evaluate_phase_1e import plan, run
from tests.test_llm import Factory, api_error
from tests.fakes import temp_log_store, FakeProvider


class InputTests(unittest.IsolatedAsyncioTestCase):
    async def test_structured_contents_system_and_same_key_retry(self):
        factory=Factory(api_error(503),)
        adapter=GeminiAdapter(('fake-a','fake-b'),bot.MODEL,client_factory=factory)
        router=LLMRouter({'gemini':adapter},task_policies(bot.MODEL),sleep=AsyncMock())
        self.addAsyncCleanup(router.aclose)
        request=LLMRequest('chat',(Message('user','A: hello\nB: quoted'),
            Message('assistant','hi'),Message('user','B: next')),system_instruction='fixed')
        response=await router.generate(request)
        self.assertEqual(response.attempt_count,2)
        self.assertEqual([c.aio.models.generate_content.await_count for c in factory.clients],[2,0])
        for call in factory.clients[0].aio.models.generate_content.call_args_list:
            values=call.kwargs
            self.assertEqual([c.role for c in values['contents']],['user','model','user'])
            self.assertEqual(values['contents'][0].parts[0].text,'A: hello\nB: quoted')
            config=values['config']
            self.assertEqual(config.system_instruction,'fixed')
            self.assertIsNone(config.temperature);self.assertIsNone(config.top_p)
            self.assertIsNone(config.top_k);self.assertIsNone(config.thinking_config)
        await router.generate(LLMRequest('translation',(Message('user','translate'),)))
        call=factory.clients[1].aio.models.generate_content.call_args.kwargs
        self.assertEqual(call['contents'],'translate');self.assertIsNone(call['config'].system_instruction)

    async def test_invalid_contract_does_not_reserve_key(self):
        adapter=GeminiAdapter(('fake',),bot.MODEL,client_factory=Factory())
        self.addAsyncCleanup(adapter.aclose)
        requests=[LLMRequest('chat',()),LLMRequest('chat',(Message('system','x'),)),
                  LLMRequest('chat',(Message('assistant','x'),)),
                  LLMRequest('chat',(Message('user','x'),),system_instruction=42)]
        requests += [LLMRequest('chat',(Message('user','x'),),generation_options={k:'x'})
                     for k in ('system_instruction','systemInstruction','httpOptions')]
        for req in requests:
            with self.assertRaises(LLMError):adapter.bind(req,task_policies(bot.MODEL)['chat'])
        self.assertEqual(adapter.keys[0].reservations,0)

    def test_frozen_baseline_and_candidate_boundary(self):
        import subprocess
        source=subprocess.check_output(['git','show',BASE['revision']+':bot/persona.py'],text=True)
        namespace={};exec(source,namespace)
        for history in ([],['A: one','최씨 봇: hi','B: two\nA: quoted']):
            case=dict(history=history,speaker='C',message='unique-current')
            a=build('A',case)
            self.assertEqual(a.messages[0].content,namespace['build_conversation_prompt'](history,'C','unique-current',new_conversation=not history))
            for candidate in 'BCD':
                req=build(candidate,case)
                self.assertEqual(len(req.messages),len(history)+1)
                self.assertEqual(sum(m.content.count('unique-current') for m in req.messages),1)
                self.assertIn(BASE['BACKGROUND'],req.system_instruction)
                self.assertIn(BASE['CONTROL'],req.system_instruction)
        self.assertEqual(len(plan('screen','B'))+len(plan('confirm','B')),40)

    def test_local_log_provenance(self):
        if not all((ROOT/path).exists() for path, _, _ in EXAMPLE_SOURCES):
            self.skipTest('Private source logs are intentionally not versioned')
        for path,start,end in EXAMPLE_SOURCES:
            for line in (ROOT/path).read_text().splitlines()[start-1:end]:
                self.assertIn(line,EXAMPLES)

    def test_real_examples_and_holdout_are_separate(self):
        cases=json.loads((ROOT/'tests/fixtures/phase_1e_cases.json').read_text())['cases']
        for case in cases:
            example_messages = [line.split('] ', 1)[1].split(': ', 1)[1]
                                for line in EXAMPLES.splitlines() if line.startswith('[20')]
            self.assertNotIn(case['message'],example_messages)
            self.assertTrue(case['source'])

    async def test_experiment_guard_never_loads_keys_for_unsafe_resume(self):
        with tempfile.TemporaryDirectory() as directory:
            ledger=Path(directory)/'ledger.jsonl'
            args=SimpleNamespace(stage='confirm',chosen='B',live=True,ledger=ledger)
            for rows in ([], [{'event':'start','stage':'screen'}],
                         [{'event':'start','stage':'screen'},
                          {'event':'result','error':'authentication'}],
                         [r for _ in range(29) for r in ({'event':'start','stage':'screen'},
                                                        {'event':'result'})]):
                ledger.write_text(''.join(json.dumps(row)+'\n' for row in rows))
                with patch('scripts.evaluate_phase_1e.load_settings') as load:
                    with self.assertRaises(SystemExit):await run(args)
                    load.assert_not_called()


class OwnedMessage:
    def __init__(self, owner, content=''):
        self.owner=owner;self.content=content;self.deleted=False
        self.flags=SimpleNamespace(ephemeral=False)
        self.id=len(owner.messages)+1
        owner.messages.append(self)
    async def edit(self, *, content):
        if self.deleted or self.owner.cleanup_error:
            raise missing()
        self.content=content
        return self
    async def delete(self):
        if self.deleted:raise missing()
        self.deleted=True


def missing():
    return discord.NotFound(SimpleNamespace(status=404,reason='Not Found'),
                            {'code':10008,'message':'Unknown Message'})


class LifecycleInteraction:
    """First webhook followup after defer edits the original, per Discord API."""
    def __init__(self):
        self.messages=[];self.original=None;self.done=False;self.pending=False
        self.cleanup_error=False;self.fail_final=False;self.expire_after_final=False
        self.response=SimpleNamespace(is_done=lambda:self.done,
                                      defer=self.defer,send_message=self.initial)
        self.followup=SimpleNamespace(send=self.followup_send)
    async def defer(self,**kwargs):
        self.done=True;self.pending=True;self.original=OwnedMessage(self)
    async def initial(self,content,**kwargs):
        self.done=True;self.original=OwnedMessage(self,content)
    async def original_response(self):return self.original
    async def edit_original_response(self, *,content):
        if not self.original:raise missing()
        result=await self.original.edit(content=content)
        self.pending=False
        return result
    async def followup_send(self,content,**kwargs):
        if self.fail_final and content.startswith('Q.'):
            raise missing()
        if self.pending:
            result=await self.edit_original_response(content=content)
        else:
            result=OwnedMessage(self,content)
        if self.expire_after_final and content.startswith('Q.'):
            self.original.deleted=True
        return result


class LifecycleTests(unittest.IsolatedAsyncioTestCase):
    async def test_old_order_reproduces_unknown_message(self):
        i=LifecycleInteraction()
        await output.loading(i)
        start=await output.send(i,'progress')
        final=await output.send(i,'answer')
        self.assertIs(start,i.original);self.assertIsNot(start,final)
        await output.progress_delete(start)
        with self.assertRaises(discord.NotFound) as e:await output.loading(i,'done')
        self.assertEqual(e.exception.code,10008)
        self.assertFalse(final.deleted)

    async def command(self,name,i):
        provider=FakeProvider('generated answer')
        router=LLMRouter({'gemini':provider},task_policies(bot.MODEL))
        with patch.object(bot,'llm_router',router),patch.object(bot,'save__logs'),\
             patch.object(bot,'log_store',temp_log_store(self)):
            await getattr(bot,name).callback(i,prompt='synthetic question')
        await router.aclose()
        self.assertEqual(len(provider.requests),1)
        return [m.content for m in i.messages if not m.deleted]

    async def test_character_commands_success(self):
        for name in ('질문','알려줘','자세히'):
            i=LifecycleInteraction();texts=await self.command(name,i)
            self.assertTrue(any('A. generated answer' in x for x in texts))
            self.assertFalse(any('잘못된 명령' in x for x in texts))

    async def test_lost_progress_after_final_does_not_report_failure(self):
        for name in ('알려줘','자세히'):
            i=LifecycleInteraction();i.expire_after_final=True
            with self.assertLogs('bot.discord_output',level='WARNING') as captured:
                texts=await self.command(name,i)
            self.assertTrue(any('code=10008' in x for x in captured.output))
            self.assertTrue(any('A. generated answer' in x for x in texts))
            self.assertFalse(any('잘못된 명령' in x for x in texts))

    async def test_real_final_failure_remains_failure_without_regeneration(self):
        for name in ('질문','알려줘','자세히'):
            i=LifecycleInteraction();i.fail_final=True
            texts=await self.command(name,i)
            self.assertFalse(any('A. generated answer' in x for x in texts))
            self.assertTrue(any('잘못된 명령' in x for x in texts))

    async def test_menu_original_final_is_not_deleted(self):
        i=LifecycleInteraction();provider=FakeProvider('candidates','final menu')
        router=LLMRouter({'gemini':provider},task_policies(bot.MODEL))
        with patch.object(bot,'llm_router',router),patch.object(bot,'save__logs'),\
             patch.object(bot,'log_store',temp_log_store(self)):
            await bot.점메추.callback(i,message='rice')
        await router.aclose()
        self.assertEqual(i.original.content,'final menu')
        self.assertFalse(i.original.deleted)
        self.assertEqual(len(provider.requests),2)

    async def test_optional_cleanup_reports_status(self):
        i=LifecycleInteraction();await i.defer();await i.original.delete()
        with self.assertLogs('bot.discord_output',level='WARNING') as captured:
            await output.progress_delete(i.original)
        self.assertIn('code=10008',captured.output[0])
        # Mandatory final edit still raises; helper does not silently succeed.
        with self.assertRaises(discord.NotFound):await output.edit(i,'final')

    async def test_progress_creation_failure_is_logged_and_final_can_send(self):
        i=LifecycleInteraction();await i.defer();i.original.deleted=True
        with self.assertLogs('bot.discord_output',level='WARNING'):
            self.assertIsNone(await output.progress_send(i,'progress'))
        # Once no deferred response is pending, followups have their own IDs.
        i.pending=False
        final=await output.send(i,'final answer')
        self.assertFalse(final.deleted)

    async def test_partial_final_send_failure_is_not_swallowed(self):
        i=LifecycleInteraction();await i.defer()
        await output.progress_send(i,'progress')
        original=i.followup.send
        calls=0
        async def failing(content,**kwargs):
            nonlocal calls
            calls+=1
            if calls==2:raise missing()
            return await original(content,**kwargs)
        i.followup.send=failing
        with self.assertRaises(discord.NotFound):await output.send(i,'x'*2001)
        self.assertEqual(calls,2)
        self.assertEqual(i.messages[-1].content,'x'*2000)
