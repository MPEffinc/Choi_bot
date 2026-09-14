# Choi_bot 개선 로드맵

현재 기능을 한 번에 재작성하지 않고, 보안 경계와 관측 가능성을 먼저 세운 뒤 대화·저장·검색을 순차적으로 바꾸는 계획이다. 각 phase는 독립적으로 검증하고 배포 가능한 크기로 나눈다.

## 우선순위 원칙

1. secret·개인정보와 권한 경계를 먼저 닫는다.
2. 현재 동작을 characterization test로 고정한 뒤 구조를 바꾼다.
3. LLM 호출을 한 계층에 모은 다음 session을 격리한다.
4. 원문 저장 형식을 안정화한 후 검색·요약을 개선한다.
5. 비용/latency/정확도는 측정 가능한 지표로 비교한다.

## Phase 0 - Critical security and authorization guardrails

목적: 현재 배포 artifact와 Discord command에서 즉시 악용 가능한 노출 면을 최소화한다.

변경 대상:

- `.dockerignore`를 추가해 `ini.env`, `.env*`, `logs/`, `logs_bak/`, `.git/`, cache, DB를 build context에서 제외
- 기존 image/build cache/registry의 secret·로그 포함 여부 조사 및 필요 시 Discord/Gemini credential rotation
- `/공지`에 permission/role 검사를 추가하고 everyone/role/user mention을 기본 차단
- `/알림`, `/해제` command 본문에서 `ROLE_WHITELIST`, guild, role hierarchy를 강제
- `/로그`, `/요약`, `/찾기`, `/stop`에 guild/channel/role 권한 정책 적용
- `date`를 엄격한 YYYY-MM-DD로 검증하고 logs root 밖 경로를 거부
- `on_message`에서 허용 guild/channel 검사 후 logging하며 bot/webhook을 제외

Dependency: 서버 관리자가 허용 role/channel, 로그 조회 정책, retention과 credential rotation 범위를 결정해야 한다.

예상 효과: image를 통한 secret·대화 노출, 공지 사칭, role 우회, 무권한 로그 조회를 우선 차단한다.

Breaking change 가능성: 높음. 기존에 누구나 쓰던 명령이 제한되고 bot 메시지/비허용 채널 로그가 더 이상 수집되지 않는다.

완료 조건:

- Docker image/history에서 env와 실제 로그가 존재하지 않음
- command 본문 authorization test 통과
- path traversal test 통과
- 비허용 채널 및 다른 bot 메시지가 저장/LLM 호출되지 않음

## Phase 1 - Tests, configuration, and LLM abstraction

목적: provider 및 Discord 연결 없이 핵심 정책을 검증하고 LLM 장애를 일관되게 처리한다.

변경 대상:

- import-time side effect를 제거한 typed settings와 startup validation
- exact dependency lock과 runtime/dev dependency 분리; 지원되는 Gemini SDK로 이행 계획 확정
- `LLMClient` interface: model, timeout, total deadline, retryable status, usage metadata, redaction
- native async 호출 또는 bounded executor/semaphore
- retry를 단일 계층으로 통합하고 429/timeout/5xx/401/403를 구분
- key별 독립 상태 및 동시성 보호; request 수가 아닌 공급자 quota 정책에 맞춘 rotation
- fake provider와 unit test, `ruff`/type check/CI 기반 마련

Dependency: Phase 0, 공급자 quota·SDK 정책 확인.

예상 효과: 중복 retry, 유령 요청, 전역 model race를 줄이고 이후 변경의 회귀를 자동 검출한다.

Breaking change 가능성: 낮음~중간. 사용자 명령 인터페이스는 유지할 수 있으나 오류 메시지와 retry timing은 달라진다.

완료 조건:

- provider error matrix와 total deadline test 통과
- 빈/부분 env 설정에서 명확한 startup error
- 동시 요청 시 key/client 상태가 서로 침범하지 않음

## Phase 2 - Discord conversation/session redesign

목적: 단체 채팅에서 누구에게 답해야 하는지 명확히 하고 context를 격리한다.

변경 대상:

- 최소 `(guild_id, channel_id 또는 thread_id)` session key
- mention, bot message reply, 명시 keyword, slash command를 직접 호출 신호로 분류
- 사용자 간 대화, 대화 종료, 참가자 전환, 동시 질문 정책 정의
- session별 deque/last activity/lock와 bounded request queue
- `00100` 문자열 대신 구조화된 `should_reply`, `reason`, `text` control 결과
- `/stop`은 호출 session만 종료하고 권한/ownership 적용
- `on_ready` task 시작을 `setup_hook` 또는 guarded lifecycle로 이동
- interaction response/defer/followup 공통 계층과 2,000자 안전 출력

Dependency: Phase 1의 test harness와 LLM interface.

예상 효과: 채널·사용자 간 context 오염과 불필요한 끼어들기/API 호출을 크게 줄인다.

Breaking change 가능성: 높음. 호출 없이 이어지던 일부 대화에서 봇이 더 이상 응답하지 않을 수 있다.

완료 조건:

- A 호출 후 B/C 대화, 타 채널 D 대화, bot 메시지, reply/mention, 동시 호출 시나리오 test
- session timeout/reset이 다른 session을 지우지 않음
- 모든 long response와 ephemeral error가 올바른 interaction 경로로 전송됨

## Phase 3 - Structured persistence and privacy lifecycle

목적: 검색 가능한 원문을 손실 없이 저장하면서 수집 최소화·보관·삭제 정책을 구현한다.

변경 대상:

- 우선 JSONL 또는 경량 DB schema: guild/channel/thread/message/user immutable ID, display snapshot, UTC timestamp, content, message type
- 단일 async writer queue와 transaction/atomicity
- multiline 및 edit/delete event 정책
- configurable retention, archive, deletion/anonymization job
- 로그 조회 audit trail과 최소 권한
- `USER_MAP`을 immutable Discord ID 기반 persistent mapping으로 전환
- 기존 평문 로그 migration은 원본 backup과 dry-run 검증 후 별도 수행

Dependency: Phase 0의 privacy 정책, Phase 2의 session/identity key. 저장 기술 선택은 데이터 규모와 운영 환경을 측정한 뒤 결정한다.

예상 효과: username 충돌과 여러 줄 손실을 없애고 보관·삭제 및 검색의 신뢰 가능한 기반을 만든다.

Breaking change 가능성: 중간~높음. 저장 format과 관리 명령의 key가 달라지며 migration이 필요하다.

완료 조건:

- concurrent write/crash recovery/multiline/timezone test
- retention과 사용자 삭제 요청을 재현 가능한 절차로 수행
- 기존 파일과 새 저장소의 표본 record 수/내용 검증

## Phase 4 - Global hybrid search

목적: 전 기간에서 빠르고 근거가 보존되는 검색을 제공하고 LLM 전송량을 줄인다.

변경 대상:

- exact/substring 검색과 날짜·사용자·channel filter를 1차 기능으로 제공
- FTS 기반 lexical index 우선 도입
- 필요성과 데이터 정책이 확인된 경우에만 embedding/semantic index 추가
- lexical + semantic 후보 병합/rerank
- 원문 message ID, timestamp, channel을 citation으로 반환
- LLM은 top-k 후보의 설명/요약에만 사용하고 “없음” 판정은 retrieval score/근거와 연결
- query/result 권한을 원문 channel access와 함께 검사

Dependency: Phase 3 structured storage. Vector DB는 필수 선행 조건이 아니다.

예상 효과: 모든 일별 chunk를 보내는 비용과 latency를 낮추고 exact search와 전체 기간 검색, 검증 가능한 결과를 제공한다.

Breaking change 가능성: 중간. `/찾기` 입력 옵션과 결과 형식에 filter/citation이 추가될 수 있다.

완료 조건:

- 알려진 정답 query set의 precision/recall 및 “없음” 정확도 측정
- 권한 없는 channel의 결과가 검색·citation에 섞이지 않음
- 현재 full-scan 대비 latency와 LLM input 사용량 감소

## Phase 5 - Summary segmentation and cache

목적: 반복 일별 요약의 비용과 정보 손실을 줄이고 변경된 부분만 갱신한다.

변경 대상:

- message/token 기준 chunking과 시간 간격/topic 기반 session segmentation
- source range/hash를 가진 부분 요약 cache
- 당일에는 새 메시지만 처리하는 incremental update, 종료된 날짜는 immutable cache
- 최종 요약에 source range 및 불확실성 포함
- Discord output renderer와 attachment fallback
- cache invalidation: message edit/delete, prompt/model/version 변경

Dependency: Phase 1 LLM metadata, Phase 3 storage, 가능하면 Phase 4 retrieval.

예상 효과: 동일 날짜 재요청 비용을 대부분 제거하고 대화 경계와 근거를 보존한다.

Breaking change 가능성: 낮음~중간. 결과 문구와 citation 형식이 달라질 수 있다.

완료 조건:

- 같은 source hash 재요청 시 LLM 호출 0회
- append/edit/delete별 invalidation test
- 대표 로그 표본에서 핵심 사실 보존률 평가

## Phase 6 - Prompt decomposition and optimization

목적: persona 품질을 유지하면서 지시/data 경계를 강화하고 반복 token과 제어 실패를 줄인다.

변경 대상:

- Core Persona, Style, Relationship Facts, Few-shot, Conversation, Runtime Control, Retrieved Knowledge 분리
- command별 필요한 구성만 선택
- 개인 관계 정보의 필요성, 정확성, 동의, 비하 표현 검토
- system/user 역할 분리와 untrusted log delimiter
- JSON schema 또는 typed structured output으로 reply control
- prompt/model version과 offline regression set
- 동일 표현 반복, 사실 질문 정확도, injection resistance 평가

Dependency: Phase 1 LLM interface, Phase 2 control contract, Phase 4/5 retrieval metadata.

예상 효과: prompt 비용 감소, `00100` 오검출 제거, injection 저항과 일관성 향상.

Breaking change 가능성: 중간. 캐릭터 말투와 응답 빈도가 체감상 달라질 수 있다.

완료 조건:

- 고정 regression 대화에서 style/정확도/should-reply 기준 충족
- prompt injection suite에서 system rule과 원문 경계 유지
- 평균 input token과 반복률 감소

## Phase 7 - Observability and production hardening

목적: 장애·비용·개인정보 접근을 조기에 탐지하고 안전하게 운영한다.

변경 대상:

- structured/redacted logging과 correlation ID
- command/LLM latency, error class, retry, token/비용, queue depth, rate limit metric
- background task health와 alert
- graceful shutdown: 새 요청 차단, writer flush, in-flight deadline, Discord close
- readiness/liveness 및 배포 smoke test
- run/restart 방식 통합, least-privilege container, backup/restore/runbook
- README에 intents, 권한, env 이름, 설치·배포·rollback·secret rotation·retention 문서화

Dependency: 앞선 phase의 안정된 component 경계.

예상 효과: 사용자 대화 내용을 console에 그대로 남기지 않고도 장애 원인과 비용을 추적하며 안전하게 배포·복구할 수 있다.

Breaking change 가능성: 낮음. 운영 방식과 로그 형식은 바뀌지만 명령 UX는 유지 가능하다.

완료 조건:

- provider 장애, Discord reconnect, background task failure, SIGTERM 시나리오 drill
- dashboard/alert와 redaction test
- 새 운영자가 문서만으로 clean environment 배포 및 rollback 성공

## 권장 첫 구현 묶음

다음 작업에서는 대규모 리팩터링 대신 아래를 하나의 작은 보안 release로 권장한다.

1. `.dockerignore` 추가 및 credential/image 노출 대응
2. `/공지`, 역할, 로그 계열 command authorization
3. message logging 순서 변경과 bot/webhook 제외
4. date path validation
5. 위 네 항목의 unit test와 배포 전 image content 검사

이후 LLM abstraction과 session redesign은 별도 release로 분리해야 원인 추적과 rollback이 쉽다.
