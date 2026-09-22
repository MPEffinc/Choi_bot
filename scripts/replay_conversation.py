#!/usr/bin/env python3
"""Replay one conversation through two prompt versions and print both transcripts.

Each side is replayed the way choi_bot actually runs it: the context deque is
carried turn to turn, the user utterance is recorded before generation, the
latest utterance goes to the model once, and reply() semantics are applied —
00100 suppresses the message but is still recorded, and (마이크 끄는 소리)
sends and then clears the context.

Offline (default) it prints the prompts that would be sent. With --live it calls
the real model for both sides so the tone can be compared directly. It never
touches Discord and never starts the bot.

  python3 scripts/replay_conversation.py                # offline: prompt sizes
  python3 scripts/replay_conversation.py --live         # real responses, both sides
  python3 scripts/replay_conversation.py --live --old ce6acb8
"""
import argparse
import asyncio
from collections import deque
import json
from pathlib import Path
import subprocess
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

MAX_DIALOGS = 20
DEFAULT_OLD = 'ce6acb8'  # Phase 1C, the version the reported conversation ran on.


def load_persona(revision):
    """Load a persona module from git, or the working tree when revision is None.

    bot/persona.py is pure data plus one function, so executing an older copy in
    its own namespace is enough; nothing is imported from the rest of the bot.
    """
    if revision is None:
        from bot import persona
        return persona
    source = subprocess.run(['git', 'show', f'{revision}:bot/persona.py'], cwd=ROOT,
                            capture_output=True, text=True, check=True).stdout
    namespace = {'__name__': f'persona_{revision}'}
    exec(compile(source, f'{revision}:bot/persona.py', 'exec'), namespace)
    return type('Persona', (), namespace)


async def replay(persona, turns, speaker, generate):
    """Yield (turn, prompt, reply) applying the bot's own context rules.

    Mirrors choi_bot: snapshot the context, record the utterance, send it once,
    then let the reply decide whether the context survives.
    """
    context = deque(maxlen=MAX_DIALOGS)
    results = []
    for turn in turns:
        history = list(context)
        context.append(f"{speaker}: {turn['message']}")
        prompt = persona.build_conversation_prompt(
            history, speaker, turn['message'], new_conversation=not history)
        reply = await generate(prompt)
        if reply is not None:
            if '마이크 끄는 소리' in reply:
                context.clear()          # Existing termination path.
            else:
                context.append(f'최씨 봇: {reply}')  # 00100 is recorded but not sent.
        results.append((turn, prompt, reply))
    return results


async def offline(old, new, turns, speaker):
    async def placeholder(_):
        return '(응답)'
    for label, persona in (('OLD', old), ('NEW', new)):
        sizes = [len(prompt) for _, prompt, _ in await replay(persona, turns, speaker, placeholder)]
        print(f'{label}: 캐릭터 프롬프트 {len(persona.CHARACTER_PROMPT)}자, '
              f'요청 입력 {min(sizes)}~{max(sizes)}자')
    print('\n실제 말투 비교는 --live 가 필요하다. 오프라인에서는 크기만 확인된다.')


async def live(old, new, turns, speaker):
    from bot.settings import load_settings, validate_settings
    from bot.llm.contracts import LLMRequest, Message
    from bot.llm.gemini import GeminiAdapter
    from bot.llm.router import LLMRouter, task_policies
    import choi_bot

    settings = load_settings()
    validate_settings(settings)
    router = LLMRouter({'gemini': GeminiAdapter(settings.api_keys, choi_bot.MODEL,
                                                key_ids=settings.key_ids or None)},
                       task_policies(choi_bot.MODEL))

    async def generate(prompt):
        try:
            response = await router.generate(LLMRequest(
                task_type='chat', messages=(Message('user', prompt),)))
        except Exception as error:      # One failed turn must not hide the rest.
            return f'(호출 실패: {error})'
        return response.text if response.text is not None else '(빈 응답)'

    try:
        results = {label: await replay(persona, turns, speaker, generate)
                   for label, persona in (('OLD', old), ('NEW', new))}
    finally:
        await router.aclose()

    print(f'{"=" * 78}\n대화 재생 비교 (실제 모델 호출)\n{"=" * 78}')
    for index, turn in enumerate(turns):
        print(f"\n[{index + 1}] {speaker}: {turn['message']}")
        if turn.get('observed_1c'):
            print(f"    운영 1C 관찰 : {turn['observed_1c']}")
        print(f"    OLD 재생     : {results['OLD'][index][2]}")
        print(f"    NEW 재생     : {results['NEW'][index][2]}")
        print(f"    확인할 점    : {turn['problem']}")


def main():
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument('--old', default=DEFAULT_OLD, help='git revision for the old persona')
    parser.add_argument('--conversation', default='tests/fixtures/phase_1d_conversation.json')
    parser.add_argument('--live', action='store_true', help='call the real model for both sides')
    args = parser.parse_args()

    data = json.loads((ROOT / args.conversation).read_text(encoding='utf-8'))
    old, new = load_persona(args.old), load_persona(None)
    runner = live if args.live else offline
    asyncio.run(runner(old, new, data['turns'], data['speaker']))


if __name__ == '__main__':
    main()
