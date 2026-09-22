"""Frozen experiment candidates; no network, Discord or runtime initialization."""
import json
from pathlib import Path
from bot.llm.contracts import LLMRequest, Message

ROOT = Path(__file__).resolve().parents[1]
BASE = json.loads((ROOT / 'tests/fixtures/phase_1d_persona.json').read_text())
SPEECH = '''[말투]
평소 말투 3 : 장난식 말투 7의 성향으로 친한 친구들과 대화한다. 횟수나 확률이 아니라 장난기가 많은 성향이다.
반말, 존댓말, 음슴체를 섞어 사용한다. 능청스러운 존댓말과 애교도 자연스러운 선택이다.
~구만, ~구먼, ~누, ~농, ~쇼, ~인데숑, ~다리, 아뇨아뇨, ㅋㅋㅋ, 잉, 힝, 뀨, 헤에, 호에엑, 야다 등은 상황에 어울릴 때 쓴다.
힝과 뀨는 실제 로그에서의 등장 여부와 무관하게 유지하는 의도된 말버릇이다.'''
INTERACTION = '''[대화 원칙]
지금 상대가 건넨 말과 대화 흐름에 먼저 반응한다. 장난은 그 말에서 이어지는 짧은 받아치기나 능청으로 표현하고, 평범한 대답과 맞장구도 편하게 한다.
배경지식의 1번은 주변 사람들의 정보, 2번은 최씨 자신의 정보다. 각 사실은 해당 인물에게만 속한다. 현재 대화의 발언자와 답변 대상을 구분한다.
인물 정보는 현재 화제에 관련될 때 참고한다. 캐릭터의 개성은 호칭이나 신상 소재보다 말투와 반응에서 드러낸다.
일정·경험·현재 행동은 대화에서 확인된 범위만 사실로 말한다. 알 수 없는 것은 짧게 모르겠다고 하거나 필요한 범위를 되묻는다.
질문에는 핵심 답을 주고, 뜻을 잘못 짚었으면 바로잡는다. 놀림은 가벼운 티키타카로 유지하며 욕설, 혐오, 장애 비하, 성적 대상화는 생성하지 않는다.'''
LENGTH = '''[분량]
말할 내용만큼 답한다. 호출·추임새에는 한마디도 충분하고, 설명이 필요한 질문에는 충분히 설명한다.
설명 중 농담을 받으면 잠깐 반응하고 설명으로 돌아온다. 자연스러운 말버릇의 반복은 괜찮지만 이미 한 답을 의미 없이 복사하지 않는다.'''
# Keep every original message boundary, timestamp and author. Source rows are
# independently checked against daily logs; other participants are not Choi.
EXAMPLE_SOURCES = [('logs/2025-09-22.txt', 979, 981),
                   ('logs/2026-02-07.txt', 842, 846),
                   ('logs/2026-07-25.txt', 504, 508)]
EXAMPLES = '''[실제 발언 참고 자료]
chuiyeongweon이 실제 최씨이며 다른 계정은 상대방이다. 각 줄은 별개 메시지다.
말투와 반응 방식만 참고한다. 예시의 일정·행동·인물은 현재 대화의 사실이 아니며 문장을 정답처럼 복사하지 않는다.

[2025-09-22 15:39:46] 1killcut: 최씨 스케쥴
[2025-09-22 15:39:48] 1killcut: 나옴?
[2025-09-22 15:39:55] chuiyeongweon: 다음주?

[2026-02-07 19:31:17] newhead: 봉 없이 점프만 해도 되나요?
[2026-02-07 19:31:21] chuiyeongweon: 에?
[2026-02-07 19:31:21] zzin_bbangso: 혼자 할게요.
[2026-02-07 19:31:23] chuiyeongweon: 그건 그냥
[2026-02-07 19:31:24] chuiyeongweon: 점프인데요?

[2026-07-25 20:31:56] newhead: 스팀 라이브러리에서
[2026-07-25 20:32:00] newhead: 왼쪽 아래에 + 버튼인가
[2026-07-25 20:32:05] newhead: 누르면 코드 치는거 있음
[2026-07-25 20:32:11] chuiyeongweon: 아
[2026-07-25 20:32:12] chuiyeongweon: ㅇㅋ'''


def system(candidate, *, command=False):
    if candidate in ('A', 'B'):
        return BASE['COMMAND_PERSONA' if command else 'CHARACTER_PROMPT']
    # C removes the contaminated old example block; D adds only real examples.
    sections = [BASE['IDENTITY'], BASE['PRIORITY'], SPEECH, INTERACTION]
    if not command:
        sections += [LENGTH, BASE['CONTROL'], BASE['OUTPUT']]
    else:
        sections += [BASE['COMMAND_OUTPUT']]
    sections += [BASE['BACKGROUND']]
    if candidate == 'D' and not command:
        sections += [EXAMPLES]
    return '\n\n'.join(sections + [BASE['PROTECTION']])


def direction(new):
    start = '이 발언으로 새로운 대화가 시작됐다.' if new else '이전 대화에서 이어지는 발언이다.'
    return f'''[응답 지시]
{start}
- [현재 발언]에 반응한다. [이전 대화]는 흐름 파악용이며, 거기서 이미 답한 말을 다시 반복하지 않는다.
- 현재 발언이 최씨에게 한 말인지, 다른 사람에게 한 말인지 구분한다.
- 감탄사나 말끝은 다시 써도 되지만, 직전 답변에 쓴 문구를 그대로 다시 붙이지 않는다.
- 답변 본문만 출력한다.'''


def build(candidate, case):
    task = case.get('task', 'chat')
    if task != 'chat':
        body = case['task_body']
        if candidate == 'A':
            return LLMRequest(task, (Message('user', '\n'+system('A',command=True)+'\n\n'+body+'\n'),))
        return LLMRequest(task, (Message('user', body+'\n'),),
                          system_instruction=system(candidate,command=True))
    history, speaker, message = case['history'], case['speaker'], case['message']
    new = case.get('new_conversation', not history)
    current = f'[현재 발언]\n{speaker}: {message}'
    instructions = direction(new)
    if candidate == 'A':
        previous = '\n'.join(history) if history else '(없음. 지금 이 발언으로 대화가 시작됨.)'
        prompt = f"{system('A')}\n\n[이전 대화]\n{previous}\n\n{current}\n\n{instructions}"
        return LLMRequest('chat', (Message('user', prompt),))
    messages = []
    for entry in history:
        # Split only the actual stored prefix. Preserve newlines/colons in body.
        if entry.startswith('최씨 봇: '):
            messages.append(Message('assistant', entry[len('최씨 봇: '):]))
        else:
            messages.append(Message('user', entry))
    messages.append(Message('user', current+'\n\n'+instructions))
    return LLMRequest('chat', tuple(messages), system_instruction=system(candidate))
