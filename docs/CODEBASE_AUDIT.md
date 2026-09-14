# Choi_bot 코드베이스 감사

분석 기준: 2026-09-14 로컬 작업본. Priority는 즉시 노출/손실인 P0, 실제 운영 오동작·개인정보·심각한 동시성인 P1, 성능·비용·정확도·운영성인 P2, 정리·작은 UX인 P3로 분류했다. 실제 사용자 로그 내용과 secret 값은 열람하거나 문서에 복사하지 않았다.

| ID | Priority | Area | Problem | Impact |
| --- | --- | --- | --- | --- |
| CB-001 | P1 | Conversation context | 모든 guild/channel/user가 단일 context 공유 | 타 채널·타 사용자 대화 혼합 및 오답 |
| CB-002 | P1 | Logging/privacy | 허용 채널 검사 전에 모든 비-self 메시지 저장 | 비허용 채널 개인정보 과수집 |
| CB-003 | P1 | Logging/privacy | 로그·요약·검색 명령에 권한/채널 제한 없음 | 누구나 타인 대화 열람·외부 전송 가능 |
| CB-004 | P1 | Security | `.dockerignore` 부재로 env와 로그가 이미지에 복사됨 | 이미지 접근자에게 credential/대화 노출 가능 |
| CB-005 | P1 | Security | 역할 whitelist가 autocomplete에만 적용됨 | 임의 role ID로 self-assign 시도 가능 |
| CB-006 | P1 | Discord behavior | `/공지` 무권한이며 everyone mention 허용 | 공지 사칭·대량 알림 가능 |
| CB-007 | P1 | Discord behavior | 활성 context 중 모든 메시지와 다른 bot을 처리 | 단체 채팅 방해, bot loop 및 비용 폭증 |
| CB-008 | P1 | Prompt | 채팅/로그/검색어가 instruction 경계 없이 삽입됨 | prompt injection 및 거짓 검색 결과 |
| CB-009 | P1 | Concurrency | 전역 context와 API model/key 상태에 lock 없음 | 동시 사용자 요청 간 상태 오염 |
| CB-010 | P2 | LLM/backend | timeout이 executor의 실제 API 호출을 취소하지 못함 | 유령 요청, quota·thread 소비 |
| CB-011 | P2 | Search | 모든 문자 chunk를 2단계 LLM 요약으로 검색 | 높은 비용·지연·정보 손실·환각 |
| CB-012 | P2 | Summary | character chunk, cache/incremental/session 없음 | 반복 비용과 경계 손실 |
| CB-013 | P2 | Security | `/요약`·`/찾기` date 형식/path 검증 없음 | `logs` 밖의 `.txt` 접근 가능성 |
| CB-014 | P2 | Discord behavior | deferred 응답 뒤 channel send를 followup 대신 사용 | ephemeral 상실과 interaction UX 혼선 |
| CB-015 | P2 | Discord behavior | 일반 LLM 출력 길이를 실제로 제한/분할하지 않음 | Discord 2,000자 전송 실패 |
| CB-016 | P2 | Error handling | summary 외부 retry와 wrapper retry가 중첩됨 | 최악의 호출 수와 rate limit 악화 |
| CB-017 | P2 | Logging/privacy | username을 장기 식별 key로 사용 | 이름 변경·동명이인·감사 추적 실패 |
| CB-018 | P2 | Logging/privacy | 평문 무기한 로그, 구조/lock/newline 처리 없음 | 보관 위험과 parser 손실/파일 경쟁 |
| CB-019 | P2 | Concurrency | `on_ready()`마다 task를 다시 start | reconnect 후 task start 오류 가능 |
| CB-020 | P2 | Error handling | background loop별 error recovery 없음 | 일회성 Discord 오류로 loop 중단 가능 |
| CB-021 | P2 | Security | provider/Discord 예외 전문을 채널·console에 출력 | 내부 정보와 사용자 내용 노출 |
| CB-022 | P2 | LLM/backend | 빈 API key 검증 전에 index 0 접근 | 잘못된 시작 오류와 진단 실패 |
| CB-023 | P2 | Maintainability | 의존성 미고정, legacy Gemini SDK, 누락 dependency | 재현 불가 build 및 이행 위험 |
| CB-024 | P2 | Maintainability | 자동 테스트가 전혀 없음 | 회귀를 배포 전에 검출하기 어려움 |
| CB-025 | P3 | Maintainability | 1,347줄 단일 파일과 mutable global 집중 | 변경 영향 범위 확대 |
| CB-026 | P3 | Maintainability | naming·dead import·stale variable/분기 존재 | 이해·정적 분석 품질 저하 |
| CB-027 | P3 | Discord behavior | `/정보`의 시각이 import 시점에 고정 | 표시 정보가 오래됨 |
| CB-028 | P3 | Maintainability | 실행 스크립트와 README가 불완전/불일치 | 운영자 실수 가능 |

요약: P0 0건, P1 9건, P2 15건, P3 4건. 현재 추적 파일에서는 literal API key나 Discord token 패턴을 찾지 못했고 `ini.env`, `logs/`, `logs_bak/`는 Git에서 제외된다. 다만 CB-004 때문에 Docker artifact 차원의 노출 위험은 남아 있다.

## Discord behavior

### CB-006

ID: CB-006  
Severity: P1 - High  
Category: Discord behavior / authorization  
Location: `choi_bot.py:1273-1322`, 특히 `AllowedMentions(... everyone=True)`  
Current behavior: `/공지` 호출자 권한을 확인하지 않고 고정 공지 채널에 글을 쓰며 user, role, everyone mention을 모두 허용한다.  
Why this is a problem: 서버 구성원 누구나 공식 공지를 사칭하거나 `@everyone`을 포함한 대량 알림을 발생시킬 수 있다.  
Real Discord scenario: 일반 사용자가 `/공지 title:x content:@everyone ...`을 실행해 전체 서버에 공지하고 공식 thread까지 만든다.  
Recommended direction: administrator 또는 명시 role/permission 검사, mention 기본 차단, 허용 채널·guild 검증, 감사 로그를 추가한다.  
Requires behavior change: Yes

### CB-007

ID: CB-007  
Severity: P1 - High  
Category: Discord behavior / message targeting  
Location: `choi_bot.py:543-615`  
Current behavior: 자기 자신만 제외한다. context가 활성화되면 mention/reply/대화 상대를 보지 않고 허용 채널의 모든 메시지를 LLM에 보낸다. 다른 bot도 처리한다.  
Why this is a problem: 단체 대화를 봇 자신에 대한 말로 오판하고 끼어들며, bot 간 응답 연쇄와 불필요한 API 비용을 만들 수 있다.  
Real Discord scenario: A가 최씨를 한 번 부른 뒤 B와 C가 서로 이야기하면 매 발화마다 봇이 답한다. 다른 bot의 안내 메시지에도 반응할 수 있다.  
Recommended direction: self와 모든 bot/webhook 제외, mention/direct reply/명시 호출을 우선 신호로 삼고 session 참가자와 inactivity 규칙을 분리한다.  
Requires behavior change: Yes

### CB-014

ID: CB-014  
Severity: P2 - Medium  
Category: Discord behavior / interaction lifecycle  
Location: `choi_bot.py:348-374`  
Current behavior: initial response가 끝난 뒤 공통 `send()`는 `interaction.followup.send()` 대신 `interaction.channel.send()`를 사용하며 `ephemeral` 값도 버린다.  
Why this is a problem: interaction 응답 연결이 끊기고, 비공개로 의도한 메시지가 공개될 수 있으며, 원 응답은 별도 edit가 필요해진다.  
Real Discord scenario: defer 후 오류를 `send(..., ephemeral=True)`로 보내도 일반 채널 메시지가 되어 서버 구성원에게 보인다.  
Recommended direction: 최초 응답, deferred original edit, followup을 명확히 구분하는 response helper로 교체한다.  
Requires behavior change: Yes

### CB-015

ID: CB-015  
Severity: P2 - Medium  
Category: Discord behavior / response limit  
Location: `choi_bot.py:517-539`, `933-1027`, `1064-1118`, `1192-1219`  
Current behavior: 대부분의 LLM 응답은 실제 길이를 검사하지 않고 한 메시지로 전송한다. prompt의 “2,000자 이내” 지시에 의존한다.  
Why this is a problem: 모델은 길이 지시를 보장하지 않으며 Discord content 한도를 넘으면 전송 자체가 실패한다.  
Real Discord scenario: `/자세히`가 2,000자를 넘으면 결과 대신 예외 문자열만 보이거나 global error handler가 추가 메시지를 보낸다.  
Recommended direction: 안전한 Markdown-aware chunking 또는 attachment/embed 정책을 공통 출력 계층에 둔다.  
Requires behavior change: No

### CB-027

ID: CB-027  
Severity: P3 - Low  
Category: Discord behavior / UX  
Location: `choi_bot.py:26`, `82-90`, `911-915`  
Current behavior: `INFORMATION`이 import 때 만들어져 `/정보` 함수에서 새로 계산한 지역 변수 `now`가 출력에 반영되지 않는다.  
Why this is a problem: 명령 실행 시각으로 오해할 수 있고 코드 의도와 출력이 다르다.  
Real Discord scenario: 수개월 실행한 봇에서 `/정보`를 호출해도 최초 프로세스 시작 시각만 계속 보인다.  
Recommended direction: 시작 시각이라는 label을 명확히 하거나 호출 시점에 문자열을 생성한다.  
Requires behavior change: No

## Conversation context

### CB-001

ID: CB-001  
Severity: P1 - High  
Category: Conversation context / isolation  
Location: `choi_bot.py:339-410`, `543-615`  
Current behavior: `conversation_context`, `active_users`, `last_conversation_time`, `reset_flag`가 프로세스 전체에 하나뿐이다.  
Why this is a problem: guild, channel, thread, user별 대화가 격리되지 않아 개인정보와 의미가 서로 섞인다.  
Real Discord scenario: A가 채널 X에서 최씨를 호출한 직후 D가 채널 Y에서 말하면 D의 말이 A의 history와 함께 외부 LLM에 전송된다.  
Recommended direction: `(guild_id, channel_id/thread_id)`를 최소 session key로 하고, 필요하면 참여자/explicit reply chain을 추가한다.  
Requires behavior change: Yes

## LLM/backend

### CB-010

ID: CB-010  
Severity: P2 - Medium  
Category: LLM/backend / timeout  
Location: `choi_bot.py:255-287`  
Current behavior: 동기 SDK 호출을 executor로 옮겨 event loop block은 피하지만 `wait_for()` timeout 후 worker thread의 호출은 계속될 수 있다.  
Why this is a problem: 이미 실패로 처리한 요청이 quota와 thread를 소비하는 동안 다음 key 재시도가 겹칠 수 있다.  
Real Discord scenario: 느린 공급자 요청이 20초에 timeout되고 새 key로 재시도되지만 원 요청도 서버에서 완료되어 중복 사용량이 발생한다.  
Recommended direction: native async/cancellable client와 provider-level timeout을 사용하고 executor 수·in-flight 요청을 제한한다.  
Requires behavior change: No

### CB-016

ID: CB-016  
Severity: P2 - Medium  
Category: LLM/backend / retry  
Location: `choi_bot.py:255-287`, `768-816`, `850-870`  
Current behavior: wrapper가 key 수만큼 재시도한 뒤 summary 함수도 다시 key 수만큼 wrapper를 호출한다.  
Why this is a problem: 6개 key 환경에서 한 chunk가 최악 36회 수준의 attempt를 유발하고 장애를 증폭한다.  
Real Discord scenario: provider timeout이 지속되면 한 `/요약`이 장시간 상태 메시지를 edit하며 여러 key quota를 소모한다.  
Recommended direction: retry 책임을 단일 계층으로 합치고 오류별 retryability, 총 deadline, exponential backoff/jitter를 둔다.  
Requires behavior change: No

### CB-022

ID: CB-022  
Severity: P2 - Medium  
Category: LLM/backend / startup validation  
Location: `choi_bot.py:201-222`  
Current behavior: 빈 key 목록을 확인하기 전에 `API_KEYS[current_api_index]`로 SDK를 설정한다.  
Why this is a problem: 설정 누락 시 의도한 설명형 오류가 아닌 `IndexError`로 시작이 실패한다.  
Real Discord scenario: 새 서버에서 env 이름을 오타 내면 운영자가 “list index out of range”만 보고 원인을 바로 찾기 어렵다.  
Recommended direction: 모든 필수 환경 변수를 먼저 검증하고 나서 client/model 객체를 만든다.  
Requires behavior change: No

## Prompt

### CB-008

ID: CB-008  
Severity: P1 - High  
Category: Prompt / injection  
Location: `choi_bot.py:569-607`, `772-849`, `936-1014`, `1075-1112`, `1200-1210`  
Current behavior: 사용자 메시지, 검색어, 원문 로그를 같은 문자열 prompt에 delimiter/role isolation 없이 삽입한다. 프롬프트 보호 문구는 모델에 대한 요청일 뿐 강제 경계가 아니다.  
Why this is a problem: 저장된 채팅의 “이전 지시를 무시하라” 같은 문장이 요약/검색 instruction처럼 취급되어 결과를 조작할 수 있다.  
Real Discord scenario: 공격자가 로그에 결과 조작 문장을 남긴 뒤 다른 사용자의 `/찾기` 결과에 허위 사실이나 내부 prompt 비슷한 내용을 생성하게 한다.  
Recommended direction: system/developer와 user data를 SDK role로 분리하고, 로그는 명시적 untrusted data delimiter와 구조화 형식으로 전달하며 결과를 근거 원문과 연결한다.  
Requires behavior change: Yes

현재 character prompt는 Core Persona, style, relationship facts, runtime protocol(`00100`), 대화 종료 신호가 한 큰 상수에 섞여 매 캐릭터 요청마다 반복된다. few-shot은 로컬 변경에서 대부분 제거됐지만 관계 정보가 길고 개인에 관한 모욕적·민감할 수 있는 서술이 포함된다. `00100`은 모델 출력 문자열 검색에 의존해 정상 문장에 우연히 포함되거나 변형되면 제어가 실패한다. 장기적으로 Core Persona / Style / Relationship Facts / Conversation / Runtime Control / Retrieved Data를 구분하고 control은 구조화 output으로 바꾸는 편이 안전하다.

## Logging/privacy

### CB-002

ID: CB-002  
Severity: P1 - High  
Category: Logging/privacy / scope  
Location: `choi_bot.py:543-552`  
Current behavior: `save__logs()`가 `ALLOWED_CH` 검사보다 먼저 실행되고 self 외 bot 여부도 보지 않는다.  
Why this is a problem: 기능상 허용하지 않은 채널의 사람·bot 대화까지 동의/목적 범위를 넘어 수집한다.  
Real Discord scenario: 봇이 열람 권한만 가진 운영진 채널의 대화도 응답은 하지 않지만 날짜 로그에는 저장한다.  
Recommended direction: 명시적으로 허용된 guild/channel 및 non-bot 조건을 통과한 뒤 필요한 이벤트만 최소 수집한다.  
Requires behavior change: Yes

### CB-003

ID: CB-003  
Severity: P1 - High  
Category: Logging/privacy / access control  
Location: `choi_bot.py:624-668`, `726-906`  
Current behavior: `/로그`, `/요약`, `/찾기`에 permission, guild, channel 검사가 없고 결과를 공개 채널로 보낸다. 원문은 Gemini에도 전송된다.  
Why this is a problem: command 접근자는 다른 사용자들의 저장 대화를 읽거나 검색하고 외부 처리시킬 수 있다.  
Real Discord scenario: 신규 멤버가 `/로그 100`을 실행해 가장 최근의 사적 대화 내용을 그대로 공개한다.  
Recommended direction: 관리자/감사 role, 전용 채널, ephemeral 응답, 요청/조회 감사 로그, 데이터 subject와 retention 정책을 적용한다.  
Requires behavior change: Yes

### CB-017

ID: CB-017  
Severity: P2 - Medium  
Category: Logging/privacy / identity  
Location: `choi_bot.py:61-80`, `548-549`, `564-565`, `591-592`, `741-754`  
Current behavior: `message.author.name` 문자열을 저장하고 `USER_MAP`도 username을 key로 사용한다. `/config` 변경은 메모리에만 남는다.  
Why this is a problem: username 변경과 동명이인에 취약하고 immutable Discord user ID로 감사·삭제 요청을 연결할 수 없다.  
Real Discord scenario: 사용자가 이름을 바꾸면 과거 로그와 현재 매핑이 분리되고, 같은 이름 사용자의 발화가 한 사람처럼 보인다.  
Recommended direction: immutable user ID를 canonical key로 저장하고 표시명은 event 당시 snapshot으로 별도 보관한다.  
Requires behavior change: Yes

### CB-018

ID: CB-018  
Severity: P2 - Medium  
Category: Logging/privacy / storage  
Location: `choi_bot.py:302-336`, `741-754`  
Current behavior: UTF-8 text 파일에 동기 append하며 보관 기한, 크기 rotation, file lock, message newline escaping이 없다. parser는 정규식에 맞는 첫 줄만 취한다.  
Why this is a problem: 로그가 무기한 늘고 동시 write/비정상 종료에 취약하며 여러 줄 메시지의 뒷부분과 metadata가 사라진다.  
Real Discord scenario: 긴 code block이 여러 줄로 저장되지만 `/요약`에는 첫 줄만 포함되어 전혀 다른 의미로 요약된다.  
Recommended direction: retention을 먼저 정의하고 JSONL/DB 등 구조화 record, ID/timestamp timezone, atomic writer queue를 도입한다.  
Requires behavior change: Yes

## Search

### CB-011

ID: CB-011  
Severity: P2 - Medium  
Category: Search / accuracy and cost  
Location: `choi_bot.py:726-906`  
Current behavior: 날짜 한 파일 전체를 4,000자씩 잘라 모든 chunk를 LLM에 보내고, 부분 요약을 다시 LLM으로 합친다. exact/기간/index 검색은 없다.  
Why this is a problem: 무관한 내용도 모두 과금되고 timestamp와 직접 근거가 두 번의 생성 요약에서 소실되며 “없음” 판정도 생성 모델에 맡긴다.  
Real Discord scenario: 한 단어가 chunk 경계에 걸리거나 첫 요약에서 빠지면 실제 기록이 있어도 최종 결과는 없다고 답한다. 반대의 환각도 가능하다.  
Recommended direction: 날짜 입력을 검증하고 deterministic substring/metadata filtering을 먼저 제공한 뒤, 후보 원문만 선택적으로 LLM에 전달하고 citation을 보존한다.  
Requires behavior change: Yes

## Summary

### CB-012

ID: CB-012  
Severity: P2 - Medium  
Category: Summary / repeated processing  
Location: `choi_bot.py:741-885`  
Current behavior: token이 아닌 문자 4,000개 단위이며 message/session 경계를 무시한다. 같은 날짜도 매 요청 전체를 다시 처리하고 cache나 incremental summary가 없다.  
Why this is a problem: 한국어와 모델 token budget의 대응이 불명확하고, 대화가 잘리며 반복 호출 비용과 latency가 그대로 재발한다.  
Real Discord scenario: 여러 사용자가 오늘 요약을 연달아 요청하면 동일한 모든 chunk와 최종 요약을 매번 새로 호출한다.  
Recommended direction: message/session 기반 chunking, token budget, source hash 기반 cache와 incremental daily summary를 단계적으로 도입한다.  
Requires behavior change: No

## Concurrency

### CB-009

ID: CB-009  
Severity: P1 - High  
Category: Concurrency / shared mutable state  
Location: `choi_bot.py:212-213`, `226-283`, `339-410`, `543-615`  
Current behavior: 여러 async handler가 lock 없이 동일 deque/set/timestamp/reset flag와 전역 `model`, key index, call count를 읽고 쓴다.  
Why this is a problem: await 사이에 다른 handler가 상태를 바꿔 context 순서, reset, key attribution이 비결정적으로 변한다.  
Real Discord scenario: A와 B의 동시 질문 중 A가 timeout으로 key를 회전시키면 B의 요청 및 count가 예상하지 않은 model/key 상태를 사용한다.  
Recommended direction: session별 lock/queue, immutable request snapshot, bounded LLM semaphore, provider client/key별 독립 상태를 둔다.  
Requires behavior change: Yes

### CB-019

ID: CB-019  
Severity: P2 - Medium  
Category: Concurrency / lifecycle  
Location: `choi_bot.py:423-433`  
Current behavior: `on_ready()`가 호출될 때마다 세 task에 `.start()`를 호출하고 실행 여부를 확인하지 않는다.  
Why this is a problem: Gateway reconnect로 ready event가 반복되면 이미 실행 중인 loop 시작 예외가 날 수 있다.  
Real Discord scenario: 네트워크 단절 후 봇은 재접속했지만 `on_ready` task start 오류 때문에 이후 초기화가 완료되지 않는다.  
Recommended direction: `setup_hook()` 또는 최초 1회 시작 지점으로 옮기고 `is_running()` guard를 둔다.  
Requires behavior change: No

## Error handling

### CB-020

ID: CB-020  
Severity: P2 - Medium  
Category: Error handling / background tasks  
Location: `choi_bot.py:435-494`  
Current behavior: loop body의 일부 조회만 try/except하고 task별 error callback과 send failure recovery가 없다.  
Why this is a problem: 일시적인 permission, rate limit, network 오류가 주기 작업을 영구 중단시킬 수 있다.  
Real Discord scenario: 공지 채널 send가 한 번 실패한 뒤 12시간 공지 loop가 더 이상 실행되지 않지만 운영자는 console을 보지 않으면 모른다.  
Recommended direction: 각 loop에 bounded retry/error hook, structured log, health metric을 추가한다.  
Requires behavior change: No

### CB-021

ID: CB-021  
Severity: P2 - Medium  
Category: Error handling / disclosure  
Location: `choi_bot.py:268-279`, `502-514`, `583-584`, `611-612`, 각 command의 `except Exception`  
Current behavior: 모델 응답과 사용자 질의를 console에 출력하고 provider/Discord 예외 `str(e)`를 일부 공개 채널에 보낸다. command 내부와 global handler가 겹쳐 두 오류 메시지가 날 가능성도 있다.  
Why this is a problem: prompt/대화/공급자 세부정보가 운영 로그 또는 공개 채널에 노출되고 중복 UX가 발생한다.  
Real Discord scenario: API 오류 전문에 요청 세부가 포함되면 일반 채널의 오류 응답과 console 양쪽에 남는다.  
Recommended direction: 사용자용 안정된 error code와 내부 structured/redacted log를 분리하고 command/global handler 책임을 하나로 정한다.  
Requires behavior change: No

## Security

### CB-004

ID: CB-004  
Severity: P1 - High  
Category: Security / secret and privacy leakage  
Location: `Dockerfile:15`, 프로젝트에 `.dockerignore` 없음  
Current behavior: `COPY . .`가 로컬 `ini.env`, `logs/`, `logs_bak/`를 build context와 image layer에 포함한다. Git ignore는 Docker에 적용되지 않는다.  
Why this is a problem: image, registry, build cache 또는 Docker daemon 접근자가 Discord token, Gemini keys, 실제 사용자 대화를 추출할 수 있다.  
Real Discord scenario: 이미지를 다른 host/registry로 전달하면 `docker save`나 container shell로 secret과 과거 로그를 읽을 수 있다.  
Recommended direction: 즉시 `.dockerignore`로 env/log/VCS/cache를 제외하고, secret은 runtime injection만 사용하며 기존 image/cache/registry 노출 범위를 조사하고 필요시 key를 rotate한다.  
Requires behavior change: No

### CB-005

ID: CB-005  
Severity: P1 - High  
Category: Security / authorization  
Location: `choi_bot.py:1239-1270`, `1326-1341`  
Current behavior: autocomplete 후보만 `ROLE_WHITELIST`로 거르고 command 본문은 입력 role ID가 whitelist인지 확인하지 않는다.  
Why this is a problem: 클라이언트 요청을 직접 만들거나 알려진 ID를 입력하면 봇보다 낮은 임의 역할을 자신에게 부여할 수 있다.  
Real Discord scenario: 사용자가 관리성 권한이 포함된 role ID를 직접 제출해 self-assignment를 시도한다.  
Recommended direction: command 본문에서 whitelist, guild, caller eligibility, role hierarchy를 모두 강제하고 변경을 감사한다.  
Requires behavior change: Yes

### CB-013

ID: CB-013  
Severity: P2 - Medium  
Category: Security / path handling  
Location: `choi_bot.py:726-737`  
Current behavior: `date`를 설명만으로 YYYY-MM-DD라고 안내하고 `os.path.join('logs', f'{date}.txt')`에 직접 삽입한다.  
Why this is a problem: `../`를 포함한 값으로 logs 밖의 접근 가능한 `.txt` 파일을 선택할 수 있고 절대/상대 경로 경계가 강제되지 않는다.  
Real Discord scenario: 공격자가 `../private/report` 같은 date를 넣어 프로젝트 주변의 `report.txt` 존재와 내용을 LLM 요약으로 탐색한다.  
Recommended direction: `datetime.strptime(date, '%Y-%m-%d')`, basename 고정, resolve 후 logs root containment를 검증한다.  
Requires behavior change: No

현재 Git index에는 `.env`, 로그, DB, cache가 추적되지 않으며 추적 파일에서 literal Google/Discord credential 패턴은 검출되지 않았다. `rotate_api_key()`는 key 값이 아니라 index만 출력한다. 과거 commit 전체에 대한 전문 secret scanner는 이번 환경에서 수행하지 않았으므로 저장소 이력 무결성을 보증하는 결과는 아니다.

## Maintainability

### CB-023

ID: CB-023  
Severity: P2 - Medium  
Category: Maintainability / dependencies  
Location: `requirements.txt:1-3`, `auto_restart.py:1`  
Current behavior: 모든 dependency가 exact pin/lock 없이 설치되고 `auto_restart.py`가 쓰는 `watchdog`는 requirements에 없다. Gemini 호출은 `google-generativeai` 패키지에 결합돼 있다.  
Why this is a problem: 같은 commit도 build 시점에 따라 다른 버전이 설치되며 개발 도구는 새 환경에서 실행되지 않는다. SDK 이행 시 호출부 전체 영향이 크다.  
Real Discord scenario: Docker rebuild에서 새 discord.py 버전이 설치되어 interaction 동작이 바뀌지만 코드 변경 없이 장애가 난다.  
Recommended direction: 검증된 버전 lock, runtime/dev dependency 분리, LLM adapter 뒤에서 지원 SDK로 이행한다.  
Requires behavior change: No

### CB-024

ID: CB-024  
Severity: P2 - Medium  
Category: Maintainability / tests  
Location: repository 전체  
Current behavior: test 디렉터리, test 파일, test runner 설정이 없다.  
Why this is a problem: context routing, permission, path validation, Discord 2,000자 분할, retry 같은 핵심 정책의 회귀를 자동 검출하지 못한다.  
Real Discord scenario: 명령 하나를 수정한 뒤 `send()` lifecycle이 깨져도 실제 서버에서 호출하기 전까지 알 수 없다.  
Recommended direction: 순수 함수 unit test부터 시작하고 fake Discord objects/provider로 handler integration test를 추가한다.  
Requires behavior change: No

### CB-025

ID: CB-025  
Severity: P3 - Low  
Category: Maintainability / structure  
Location: `choi_bot.py` 전체(1,347줄)  
Current behavior: config, persona, provider, persistence, event, UI, command, background task가 한 모듈과 mutable global에 집중돼 있다.  
Why this is a problem: 작은 수정도 import-time side effect와 전역 상태에 얽혀 테스트 및 리뷰 범위가 커진다.  
Real Discord scenario: logging만 바꾸려 해도 client 시작과 실제 env key 요구 때문에 모듈을 안전하게 import해 test하기 어렵다.  
Recommended direction: 다음 단계에서 config/provider/session/log repository/command cog 경계를 작은 단위로 분리한다.  
Requires behavior change: No

### CB-026

ID: CB-026  
Severity: P3 - Low  
Category: Maintainability / code quality  
Location: `choi_bot.py:3,5,26,42,45,349,670,1046-1061`, 다수 위치  
Current behavior: unused `commands`, stale `nowmodel`, `edit()` 등 사용되지 않거나 미완성인 코드, `CONTEXT_EXPERATION`/`save__logs`/`menu_recommand` 오탈자, magic ID가 섞여 있다. `/유저 help`는 문자열을 만들지만 보내지 않는다.  
Why this is a problem: 실제 상태와 의도를 오해하게 하고 변경 시 누락을 만든다.  
Real Discord scenario: 운영자가 help가 구현됐다고 보고 안내하지만 사용자는 오류 메시지만 받고 실제 help는 보지 못한다.  
Recommended direction: lint/type check를 도입하고 dead code 제거, naming/config 상수 정리를 작은 호환성 commit으로 수행한다.  
Requires behavior change: No

### CB-028

ID: CB-028  
Severity: P3 - Low  
Category: Maintainability / operations  
Location: `README.md`, `run.sh`, `restart.sh`, `auto_restart.py`, `Dockerfile`  
Current behavior: README는 한 줄이며 `run.sh`는 build/restart policy가 없고 `restart.sh`만 `--restart always`다. `auto_restart.py`는 추적되지만 `.gitignore`에도 적혀 있고 Docker와 무관하다.  
Why this is a problem: 어떤 경로가 공식 실행법인지, 필요한 intent/env/권한과 장애 복구 방식이 무엇인지 알기 어렵다.  
Real Discord scenario: 운영자가 `run.sh`로 재배포한 뒤 host 재부팅 시 컨테이너가 자동 복구되지 않는다.  
Recommended direction: 단일 운영 runbook을 README에 정의하고 script 역할, prerequisites, rollback/secret rotation 절차를 맞춘다.  
Requires behavior change: No

## 검증 결과와 제한

- `python3 -m compileall -q choi_bot.py auto_restart.py`: 성공 (`PYTHONPYCACHEPREFIX`를 `/tmp`로 지정해 작업본 cache 생성 방지)
- `bash -n run.sh`: 성공
- `bash -n restart.sh`: 성공
- 자동 테스트: 없음
- Discord/Gemini production 연결 및 API 호출: 수행하지 않음
- 원격 확인: SSH fetch는 local SSH key 부재로 실패했으나, 공개 HTTPS `git ls-remote`에서 `main`이 `d69e8ef743a0aec9abbac7fc11393dbdca83c2ae`임을 확인했다. 분석 시작 시 local HEAD 및 cached `origin/main`과 동일했으므로 당시 divergence는 없었다.
