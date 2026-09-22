# Phase 0 / Phase 1A 구현 기록

기준 코드: `66ecf42dce1894868b98ab59b716899dd8cd9aba`. 이번 변경은 실행 경계와 LLM 호출 계약을 추출한다. 기존 구조 검토 문서의 Phase 1B 제안(새 SDK, retry/deadline, 동시성 제어)은 구현하지 않았다.

원래 요청은 Gemini 2.5 Flash-Lite 유지였으나, 작업 도중 사용자가 `MODEL = "gemini-3.5-flash-lite"` 변경을 **이번 커밋에 포함**하도록 명시했다. 최종 정적 라우팅 모델은 이 값이다. 실제 Gemini API를 호출하지 않았으므로 해당 모델 ID의 제공 여부, 계정별 접근 가능성, legacy SDK와의 실제 응답 호환성은 확인하지 않았다. 새로운 공급자는 추가하지 않았다.

## 실행 경계

`python -u choi_bot.py` → `main()` → `load_settings()` → `initialize_runtime()` → `client.run(token)`.

- `bot/settings.py`: 기존 `ini.env`와 환경 변수 우선순위 유지, key1~6의 빈 슬롯 제외. 로딩과 검증 분리. 빈 키 목록/누락·빈 토큰을 Client 생성 전에 진단한다. Settings repr에는 비밀값을 표시하지 않는다.
- import 시 dotenv 로딩, Gemini SDK import/configure/model 생성, Discord Client/CommandTree 생성, 로그 디렉터리 생성, task 시작, 봇 연결을 하지 않는다.
- import 시 명령/Loop의 **선언 객체**와 기존 상수·전역 deque는 생성한다. 실제 이벤트·20개 명령·오류 처리기의 등록은 `initialize_runtime()`에서 수행한다. autocomplete와 administrator check는 기존 선언에 남아 있다.
- `initialize_runtime(settings, router=..., client_factory=..., tree_factory=...)`로 테스트 의존성을 주입할 수 있다. 연결은 하지 않지만 실제 실행 시 Gemini 객체 구성과 로그 디렉터리 생성은 수행한다.
- 전역 대화 상태를 그대로 사용하는 단일 프로세스 초기화 함수다. 여러 독립 봇을 동시에 만드는 factory나 운영 중 재초기화 API가 아니다.
- Dockerfile, requirements, 배포 스크립트는 변경하지 않았다.

## 모듈과 호출 흐름

```text
Discord 이벤트 / Slash / 번역 View
  → generate_content_timeout(prompt, task_type=..., timeout=20)
  → LLMRouter.generate(LLMRequest)
  → TaskPolicy(provider="gemini", model=MODEL)
  → GeminiAdapter.generate(request, policy)
  → legacy SDK model.generate_content(기존 prompt 문자열)
  → LLMResponse 또는 LLMError
  → 기존 Handler의 send/edit/log/context 처리
```

| 모듈 | 책임 |
| --- | --- |
| `bot/llm/contracts.py` | Message, LLMRequest, LLMResponse, LLMError, TaskPolicy, 최소 Provider Protocol |
| `bot/llm/router.py` | task별 정적 policy와 provider 선택. Discord·침묵·종료·출력·retry 책임 없음 |
| `bot/llm/gemini.py` | legacy SDK 요청/응답 변환, 기존 RR/executor/timeout/retry 이식, 오류 정규화 |
| `choi_bot.py` | 기존 이벤트·명령·프롬프트·로그·컨텍스트, 명시적 초기화와 진입점 |
| `tests/fakes.py` | Fake Provider/Discord client/tree/interaction/message/channel |

Request는 request_id, task_type, messages, generation_options, metadata, timeout을 가진다. Adapter는 현재 **user 메시지 한 개의 문자열**만 받아 기존 SDK 입력을 그대로 재현한다. system/history 변환은 아직 지원하지 않으며 잘못된 입력은 명시적으로 거절한다. generation_options가 비어 있으면 generation_config 인수 자체를 전달하지 않아 기존 기본값을 유지한다.

Response는 text/provider/model/usage/latency/finish_reason을 가진다. usage는 SDK가 제공하는 prompt/candidates/total token count만 채우며 quota 잔액으로 해석하지 않는다. latency는 adapter 호출 전체 경과 시간(기존 내부 retry 포함)이다. 알 수 없는 usage/finish reason은 비워 둔다. text 속성 부재는 None, 실제 빈 문자열은 빈 문자열로 구분한다. SDK text accessor가 던지는 예외는 LLMError로 전달한다.

LLMError는 원래 예외의 문자열을 유지하고 error_type/retryable/provider/status_code를 제공한다. timeout, ResourceExhausted(quota), 문자열 기반 rate_limit, authentication, invalid_request, provider_error를 구분한다. retryable은 분류 정보이며 Router가 추가 재시도를 수행하지 않는다. CancelledError는 변환하지 않고 전파한다. error cause는 보존한다.

## 모든 호출 지점과 RR 호환성

| 기존 기능 | task_type | 기존 conf_next 동작 |
| --- | --- | --- |
| 일반 대화 시작/연속 (2곳) | chat | 적용 |
| /질문 | question | 미적용 |
| /알려줘 | info | 미적용 |
| /자세히 | detail | 미적용 |
| /번역 callback | translation | 적용 |
| /요약 부분/최종 | summary_map / summary_reduce | 적용 |
| /찾기 부분/최종 | search_map / search_reduce | 적용 |
| /점메추·/저메추 후보/선택 | menu_candidates / menu_select | 미적용 |

원래 소스의 생성 호출 10곳(요약과 찾기, 두 메뉴 명령은 함수를 공유)을 모두 Router로 연결했다. 선택적 conf_next는 policy의 `advance_legacy_key`로 Adapter에서 실행한다. 첫 키 3회 이후 각 키 4회가 되는 기존 카운터 순서도 유지한다. 일반 질문/메뉴도 timeout/quota에 의한 내부 키 회전은 기존과 동일하게 가능하다.

기존 20초 wait_for, 키 개수만큼의 내부 시도, 회전 후 0.5초 대기, 검색/요약의 키 개수만큼 외부 재시도와 2초 대기를 유지한다. 과거 요약 UI의 ResourceExhausted와 문자열 매칭 오류 구분도 유지한다. summary의 timeout 안내 문구에 적힌 10초는 기존 문구 그대로이며 실제 기본 timeout은 20초다.

## 의도한 동작 차이와 유지 범위

의도한 변경:

1. import로 자동 실행되지 않는다. 실제 실행 명령은 동일하다.
2. 설정이 없으면 키 배열 IndexError 대신 명확한 ValueError로 종료한다. 빈 Discord token도 초기 검증에서 거절한다.
3. SDK 응답/예외가 공통 DTO/LLMError로 변환된다. 메뉴의 최종 응답에 text 속성이 없는 비정상 경로는 SDK 객체 문자열을 노출하는 대신 기존 공통 실패 문구를 쓴다.
4. 사용자가 승인한 모델 상수 3.5 변경.
5. conf_next는 Handler의 prompt 구성 직전에서 Adapter 호출 시작으로 이동한다. 정상 요청의 회전 횟수/순서는 동일하다. configure/model 생성 자체가 실패하면 이제 공통 LLMError로 처리된다.

프롬프트·캐릭터 설정·관계·명령 이름/인수/설명/권한/자동완성, 전역 공유 컨텍스트·120초 만료·20항목 deque, 종료 문구 우선/00100 숨김 및 로그/context 저장, TXT 형식, 날짜 검색·4000자 청킹·map/reduce·출력 분할은 유지했다. 보안/권한 정책을 변경하지 않았다.

모델 변경의 실제 생성 품질 차이는 오프라인 테스트로 평가할 수 없다. 테스트가 통과했다는 것은 제어 흐름과 입력 계약의 호환성이지 생성 문장이 같다는 뜻이 아니다.

## 검증

```sh
PYTHONDONTWRITEBYTECODE=1 python3 -m unittest discover -v
```

테스트는 기준 코드에서 추출한 `tests/fixtures/legacy_contract.json`과 AST를 비교한다. 20개 명령의 인수·기본값·keyword-only·decorator, 모든 호출의 입력 표현식 및 persona/검색/요약/번역 template을 고정한다. fixture에는 실제 대화 로그와 API key가 없다.

Fake Provider/Discord로 새 대화·다중 사용자/채널 전역 공유·만료·침묵·종료·오류·전체 명령 callback의 주요 경로·검색/요약 map-reduce와 외부 retry·긴 출력·번역 View·공지 thread/mentions·역할·config·로그·on_ready task 등록을 검증한다. import 검사는 무키/빈 작업 디렉터리의 별도 프로세스에서 Client 생성과 socket 연결을 차단한다.

Adapter 테스트는 Fake SDK와 결정적인 executor future를 사용해 payload, RR, quota/timeout retry, 공통 오류, 취소, 사용량/finish reason, 빈 응답을 검증한다. 실제 worker thread 중단이나 SDK transport 동시성 해결을 검증하는 테스트가 아니다. 초기 실제 executor를 사용한 테스트는 이 실행 환경에서 asyncio executor teardown이 대기하여 중단했고, 테스트 목적에 맞게 fake executor로 바꿨다. 최종 suite에는 실제 SDK 요청이나 실행 중 worker가 없다.

Python 구문 검사는 compile()로 수행하며 저장소에 bytecode를 만들지 않는다. 기존 CRLF를 보존했으므로 diff whitespace 검사는 `git -c core.whitespace=cr-at-eol diff --check`를 사용한다. 별도 Git 설정을 변경하지 않는다.

## Phase 1B에 남은 사항

- legacy SDK의 전역 configure, 지연된 model/client 결정, 공유 model/key 경쟁은 그대로다. 여러 Adapter 인스턴스도 process 전역 SDK 설정을 공유할 수 있다.
- executor wait_for timeout은 실제 SDK worker/원격 요청을 중단하지 않는다. SDK 내부 retry와 요약 외부 retry도 중첩되어 있다.
- key별 명시적 client와 native async를 가진 지원 SDK로 이전하고, 요청 고정·worker lifecycle·단일 retry/deadline·quota group과 전체 동시 실행 수를 별도로 설계/검증해야 한다.
- epoch/stale 응답, 동일 대화의 순서, /stop 이후 늦은 응답, reconnect 시 task 재시작, 번역 View의 mutable 상태, Discord 응답 길이/수명 문제는 미해결이다.
- 채널별 ConversationManager, 프롬프트 최적화, DB/검색/요약 개편은 이번 범위에 없다.
- 다음 실제 연결 검증에서 사용자가 선택한 모델 ID의 접근 가능성과 legacy/신규 SDK 응답 차이도 확인한다. 이번에는 배포, 컨테이너 조작, 실제 Gemini/Discord 호출을 하지 않았다.

작업 시작 전에 존재한 미추적 `docs/STRUCTURAL_REVIEW.md`는 수정하거나 이번 구현 커밋에 포함하지 않는다. 신규 구현 문서가 해당 분석 문서를 대체하지는 않는다.
