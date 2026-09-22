#!/usr/bin/env python3
"""Compare the Phase 1B and Phase 1C chat prompts on the same evaluation inputs.

Offline (default) it only reports how each prompt is built: sizes, whether the
latest utterance is duplicated, and which rules each side carries. It makes no
claim about response quality.

With --live it sends both prompts to the configured Gemini model and prints the
two answers next to each other so a human can judge tone. --live needs API keys
in the environment or ini.env and performs real API calls. It never touches
Discord and never starts the bot.

  python3 scripts/compare_prompts.py                      # offline report
  python3 scripts/compare_prompts.py --case C09 --show    # full prompt text
  python3 scripts/compare_prompts.py --live --case C09 C10
"""
import argparse
import asyncio
import json
from pathlib import Path
import re
import subprocess
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from bot import persona  # noqa: E402

CASES = json.loads((ROOT / 'tests/fixtures/style_eval_cases.json').read_text(encoding='utf-8'))['cases']


def legacy_templates(revision):
    """Pull the previous prompt text out of git instead of keeping a stale copy."""
    source = subprocess.run(['git', 'show', f'{revision}:choi_bot.py'], cwd=ROOT,
                            capture_output=True, text=True, check=True).stdout.replace('\r\n', '\n')
    start = source.index('CHARACTER_PROMPT = """') + len('CHARACTER_PROMPT = """')
    character = source[start:source.index('"""', start)]
    body = source[source.index('async def process_conversation_message'):source.index('#Commands')]
    chats = re.findall(r'generate_content_timeout\(f"""(.*?)""", task_type="chat"\)', body, re.S)
    if len(chats) != 2:
        raise SystemExit(f'{revision} does not have the two legacy chat prompts')
    return character, chats[0], chats[1]


def legacy_prompt(templates, case):
    character, new_template, continued = templates
    # The legacy code appended the utterance to the context BEFORE rendering, so the
    # rendered log already contains it; that is the duplication Phase 1C removes.
    log = case['history'] + [f"{case['speaker']}: {case['message']}"]
    template = new_template if not case['history'] else continued
    return (template.replace('{CHARACTER_PROMPT}', character)
                    .replace('{get_context()}', '\n'.join(log))
                    .replace('{real_name}', case['speaker'])
                    .replace('{msg}', case['message']))


def new_prompt(case):
    return persona.build_conversation_prompt(case['history'], case['speaker'], case['message'],
                                             new_conversation=not case['history'])


SENTINEL = '\u27ea\uc774\ubc88\ubc1c\uc5b8\u27eb'


def utterance_count(render, templates, case):
    """How many times the current utterance reaches the model.

    Rendered with a sentinel in place of the message text, so fixed persona wording
    that happens to contain the same characters is not miscounted.
    """
    probe = dict(case, message=SENTINEL)
    return render(templates, probe).count(SENTINEL)


RULE_MARKERS = {
    '3:7 성향': '평소 말투 3 : 장난식 말투 7',
    '음슴체 고정': '음슴체(~했음/~임), 짧은 단답 위주',
    '1~2줄 고정': '기본: 1~2줄',
    '힝/뀨 유지': '힝과 뀨는 실제 로그에서의',
    '종료 표현 예시': '나 자러 갈게',
    '종료 대상 구분': '다른 사람에게 작별 인사를 함',
    '00100 신호': '00100, 관계성 부족',
    '응답 대상 구분': '누가 누구에게 말했는지',
}


def report(revision, selected, show):
    templates = legacy_templates(revision)
    print(f'legacy revision: {revision}   cases: {len(selected)}\n')
    header = f"{'case':5} {'goal':16} {'old chars':>9} {'new chars':>9} {'old dup':>8} {'new dup':>8}"
    print(header)
    print('-' * len(header))
    duplicated_before = duplicated_after = 0
    for case in selected:
        old, new = legacy_prompt(templates, case), new_prompt(case)
        old_n = utterance_count(legacy_prompt, templates, case)
        new_n = utterance_count(lambda _, c: new_prompt(c), templates, case)
        duplicated_before += old_n > 1
        duplicated_after += new_n > 1
        print(f"{case['id']:5} {case['goal'][:16]:16} {len(old):>9} {len(new):>9} {old_n:>8} {new_n:>8}")
    print(f"\n최신 발언이 두 번 이상 들어간 입력: 이전 {duplicated_before}건 -> 현재 {duplicated_after}건")

    sample = legacy_prompt(templates, selected[0]), new_prompt(selected[0])
    print(f"\n{'규칙':16} {'이전':>6} {'현재':>6}")
    for label, marker in RULE_MARKERS.items():
        print(f'{label:16} {"있음" if marker in sample[0] else "없음":>6} '
              f'{"있음" if marker in sample[1] else "없음":>6}')

    if show:
        for case in selected:
            for label, prompt in (('OLD', legacy_prompt(templates, case)), ('NEW', new_prompt(case))):
                print(f"\n{'=' * 70}\n{case['id']} [{label}] {case['goal']}\n{'=' * 70}\n{prompt}")
    print('\n출력 문장의 자연스러움과 장난 비중은 이 보고서로 판단할 수 없다. --live 비교가 필요하다.')


async def live(revision, selected):
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
    templates = legacy_templates(revision)
    try:
        for case in selected:
            print(f"\n{'=' * 70}\n{case['id']} {case['goal']}\n입력: {case['speaker']}: {case['message']}")
            print(f"기대 관찰점: {case['expect']}\n{'-' * 70}")
            for label, prompt in (('OLD', legacy_prompt(templates, case)), ('NEW', new_prompt(case))):
                try:
                    response = await router.generate(LLMRequest(
                        task_type='chat', messages=(Message('user', prompt),)))
                    print(f'[{label}] {response.text}')
                except Exception as error:  # A failed call must not hide the other side.
                    print(f'[{label}] 호출 실패: {error}')
    finally:
        await router.aclose()


def main():
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument('--revision', default='HEAD', help='git revision holding the legacy prompt')
    parser.add_argument('--case', nargs='*', help='case ids to compare (default: all)')
    parser.add_argument('--show', action='store_true', help='print the full prompt text')
    parser.add_argument('--live', action='store_true', help='call the real model for both prompts')
    args = parser.parse_args()

    selected = [c for c in CASES if not args.case or c['id'] in args.case]
    if not selected:
        raise SystemExit('no matching case id')
    if args.live:
        asyncio.run(live(args.revision, selected))
    else:
        report(args.revision, selected, args.show)


if __name__ == '__main__':
    main()
