# Phase 1B — Gemini SDK 및 LLM 실행 안정화

기준 HEAD: `997e89fa5dd2b550cbc8a3c4b4e3075153ba7531`. 모델은 사용자가 실호출을 검증한 `gemini-3.5-flash-lite`를 유지한다. 이번 개발에서는 실제 Gemini/Discord 호출 및 Docker 변경을 하지 않았다.

## 구성과 SDK

`Discord Handler → LLMRouter → 요청에 고정된 GeminiRequest → key별 Client.aio.models.generate_content`.

- `requirements.txt`: `google-generativeai` 대신 **google-genai==2.24.0** 고정. Python >=3.10 필요.
- `bot/llm/gemini.py`: process-global configure/executor 제거. 키마다 명시적 `Client(api_key=..., vertexai=False)`, API v1beta, native async 사용.
- 기존 완성된 프롬프트 문자열을 contents에 그대로 전달한다. system/history로 재구성하지 않는다. temperature, thinking, token limit 등 모델 기본값은 임의 설정하지 않는다.
- SDK `HttpRetryOptions(attempts=1)`은 최초 요청 포함 1회다. Client 및 요청 config 양쪽에 적용하고 자동 function calling도 비활성화한다. 실제 SDK+httpx.MockTransport의 503 응답에 HTTP 호출이 한 번임을 검증했다.
- `ChoiClient.close()`는 background loop 취소, 대화 queue 종료, Router 종료, 각 Client의 aio.aclose와 sync close, Discord close를 수행한다. on_ready 재접속 시 이미 실행 중인 loop는 재시작하지 않는다.
- SDK 예외 본문/키/URL을 공통 예외나 콘솔로 전달하지 않는다. 오류는 정해진 종류와 HTTP 상태로 정규화한다. SDK 예외 chaining 출력도 억제한다.

검토 근거: [공식 SDK](https://googleapis.github.io/python-genai/), [SDK 이전](https://ai.google.dev/gemini-api/docs/migrate), [고정 버전 소스](https://github.com/googleapis/python-genai/tree/v2.24.0), [PyPI](https://pypi.org/project/google-genai/2.24.0/).

## 키와 quota

Settings의 GOOGLE_API_KEY1~6 로딩과 기존 dotenv 우선순위를 유지한다. 빈 슬롯은 제외하며 key_ids에는 원래 슬롯 번호를 보존한다. 키 자체는 변경하지 않는다.

`KeyState`는 client, disabled, actual attempts, inflight, reservations, 관측 token usage를 가진다. 키 선택은 cooldown과 attempts+예약 수를 비교하고 동률은 순환한다. 논리 요청 시작 때 client/model을 고정하고 재시도에도 같은 client를 쓴다. 기존 conf_next/3~4회 카운터는 제거했다. 사용량은 API에서 관측한 token 수이며 프로젝트 잔여 quota가 아니다.

`QuotaGroup`은 `(group, model)` 단위다. 프로젝트 관계를 모르므로 기본값은 모든 키가 **unknown 한 그룹**이다. 별도 group 매핑은 Adapter 생성 인수로만 지원하며 운영 값은 추측하지 않았다. 키와 quota group을 분리하는 이유는 [공식 rate limits](https://ai.google.dev/gemini-api/docs/rate-limits)의 프로젝트 기준 제한 때문이다.

- 인증 실패: 해당 key 비활성화, 그 요청은 즉시 실패. 다음 논리 요청은 다른 정상 key 선택 가능.
- permission/잘못된 요청/차단/빈 응답: 자동 재시도 없음.
- 명확한 일일 quota: 재시도 없음. 제공된 reset 대기 시간이 있으면 cooldown, 없으면 해당 그룹은 프로세스 재초기화까지 block.
- RetryInfo/Retry-After 또는 분당 quota 식별자가 있는 429: group cooldown 후 같은 client로 제한된 재시도.
- 분류할 근거가 없는 429: 자동 재시도 없이 실패하고 unknown group에 60초 cooldown. **60초는 운영상 재호출 억제 값이며 실제 API 한도가 아니다.**
- backoff와 cooldown 대기는 전체 deadline 안에 들어갈 때만 수행한다. 키 교체로 quota를 우회하지 않는다.

## Router 정책

| task | 시도 timeout | 논리 deadline | 최대 시도 |
| --- | --- | --- | --- |
| chat, question, info, detail, translation, menu 두 단계 | 20초 | 45초 | 2 |
| search/summary map 및 reduce 각각 | 30초 | 90초 | 3 |

전체 동시 API 실행은 기본 **2**. 이것은 서비스 실행 예산이며 Google의 실제 한도를 주장하지 않는다. 지수 backoff는 0.5초 시작, 최대 8초이고 서버 Retry-After/RetryInfo가 더 길면 이를 따른다. HTTP 날짜 Retry-After도 해석한다. 네트워크, timeout, 일시 장애와 확인된 단기 제한만 재시도한다.

논리 deadline은 monotonic 기준으로 Router 슬롯 대기, quota 대기, 모든 시도와 backoff를 포함한다. 대화 FIFO에서 기다리는 시간은 별도 120초 입력 유효기간으로 제한한다. 요약 전체 job deadline은 아직 없으며 개별 청크/reduce에 정책을 적용한다.

시도 timeout은 남은 deadline 이하로 줄이고 SDK에는 밀리초로 전달한다. timeout/취소 시 결과는 폐기하지만 원격 서버 연산이 취소됐다고 가정하지 않는다. 취소를 무시하는 transport가 있다면 실제 종료까지 semaphore 슬롯을 반환하지 않고 해당 요청을 재시도하지 않는다. 종료 때 outstanding task를 취소하고 1초 정리 유예 후 Client를 닫는다. 임의로 취소를 영원히 거부하는 타 Provider까지 강제 종료하는 구조는 아니다.

Response에 request_id, actual attempt_count, total latency, model_version, usage, finish_reason을 기록한다. 최근 200개 요청의 비밀값 없는 metrics를 메모리에 보관하고 logger.info로 전달한다. 후보 없는 응답은 empty_response, safety 등 차단은 blocked, 비텍스트/비정상 finish는 invalid_response다. STOP과 MAX_TOKENS의 visible text는 반환하며 thoughts는 출력하지 않는다. MAX_TOKENS는 finish_reason으로 보존하며 자동 이어쓰기는 하지 않는다.

## 공유 대화 순서 및 epoch

`bot/conversation.py`의 단일 ConversationQueue는 **현재 전역 공유 대화 하나만** 순차 처리한다. 모든 Slash Command를 이 queue에 넣지 않는다. pending 최대 32개, 대기 입력 최대 120초. 큐가 꽉 차면 안내하고 입력 로그는 유지한다. 너무 오래된 pending은 폐기한다.

- 수신 content/작성자 이름을 snapshot으로 넣고, worker에서 사용자 발언→prompt→생성→출력→봇 context 순서로 처리한다.
- /stop·자동 만료·자연 종료 때 epoch 증가. /stop은 generation 완료를 기다리지 않는다. 취소와 별개로 reply 직전 및 분할 전송 각 조각 전후에 epoch 검증을 수행한다.
- 이미 Discord 전송이 시작된 한 조각까지 철회할 수 있다고 보장하지 않는다. reset 이후 추가 조각/context 적용은 중단한다.
- 자연 종료 시 이미 대기하던 **명시 호출**은 새 대화로 재평가한다. 단순 후속 발언은 폐기한다. /stop의 이전 pending은 호출 여부와 관계없이 폐기한다.
- 만료된 deque가 남아 있어도 worker에서 만료를 정리한 후 명시 호출로 재시작할 수 있다. 기존 120초 경계(<=120 활성), 20항목, 다중 사용자/다중 허용 채널 공유, 침묵 갱신은 유지한다.
- 새로운 입력마다 기존 생성을 취소하지 않는다. 순차 실행으로 동일 대화의 응답 완료 순서 역전을 예방한다. 다른 Slash 요청은 Router 슬롯 범위에서 독립 실행된다.

## Discord 및 검색·요약 호환성

`bot/discord_output.py`는 initial response/defer/original edit/followup을 분리한다. defer 이후 일반 channel.send를 쓰지 않고 interaction followup(wait=True)을 사용한다. 공개/ephemeral 인수를 유지하고 original edit의 추가 조각은 원 응답 flags의 공개 범위를 따른다. /질문도 생성 전에 defer한다.

출력은 UTF-16 기준 최대 2000 단위로 나누고 원문 문자는 보존한다. Markdown fence를 재구성하지 않으므로 분할 경계의 서식은 완벽하지 않을 수 있다. 일반 대화는 chunk마다 epoch를 확인한다. 역할·공지 명령의 기존 대상과 mention 정책은 변경하지 않는다.

진행 메시지 생성/edit/delete 오류는 경고로 분리하여 성공한 LLM 결과를 재생성하지 않는다. 일반 대화 로그 쓰기 실패도 재생성을 하지 않는다. 검색/요약의 외부 key-count retry는 제거했고 Router만 retry한다. TXT parser, 4000자 청킹, map/reduce prompt, 날짜/검색 제목은 유지한다. 실패 청크가 있으면 부분 결과를 완성 요약처럼 반환하지 않고 실패 안내 후 종료한다. 긴 검색도 검색 제목을 유지하고 공통 출력기가 분할한다.

번역 callback은 source_text/target_lang을 첫 await 전 고정하여 prompt와 결과 라벨에 같은 snapshot을 쓴다. 언어 옵션/500자 입력/번역 지시문은 유지한다. 병렬 번역 클릭의 완료 순서까지 UI revision으로 통제하는 것은 이번 구현에 포함하지 않았다.

## 검증 및 작업 보존

테스트 명령:

```bash
PYTHONDONTWRITEBYTECODE=1 python3 -m unittest discover -v
```

이번 환경은 venv의 ensurepip가 없어 운영 Python을 변경하는 대신 `/tmp/choi-phase1b-deps`에 google-genai 2.24.0과 의존성을 설치하고 `PYTHONPATH=/tmp/choi-phase1b-deps`로 검증했다. 배포 환경에서는 requirements.txt 설치가 필요하다.

기존 20명령 인수/설명/check 및 prompt AST fixture 유지, 번역 변수명만 snapshot으로 정규화하여 비교한다. Fake Provider/Discord로 명령 경로와 오류를 검증하고 실제 google-genai 타입과 MockTransport로 요청 payload/SDK retry를 검증한다. quota/client 고정/timeout/deadline/Retry-After/취소/동시성/late 결과/대화 순서/stop/자연 종료/만료/번역 snapshot/진행 UI 실패/긴 응답을 테스트한다. 생성 문장 품질과 실제 서버 transport 취소는 검증했다고 주장하지 않는다.

최종 전체 오프라인 테스트 **50개 통과**(기존 미커밋 키 검사 테스트 6개 포함).

실제 Gemini 호출은 0회, Discord 테스트 전송 0회. Docker·운영 서비스는 변경하지 않았다. 문서와 소스 구문/diff 검사를 수행한다.

시작 시 있던 사용자 `BUILD_VERSION=1.9.0` 변경은 작업 트리에 보존하고 이번 commit에서는 제외한다. 키 검사 shell/Python/문서/test 및 ini.env도 변경하거나 포함하지 않는다. 요청에 따라 기존 미추적 STRUCTURAL_REVIEW.md만 원문 그대로 문서 commit에 포함한다. 그 문서의 과거 모델/버전 표기는 당시 분석 기록이며 현재 구현은 본 문서가 우선한다.

## 남은 일과 Phase 2A

실제 키별 프로젝트 관계 및 quota 회복 시간 설정, SDK 실환경 canary, 장시간 부하와 shutdown 검증은 남아 있다. queue 대기 안내/공정 우선순위, Markdown-aware split, 여러 번역 작업의 UI revision, 전체 요약 job 취소/deadline은 후속 개선 가능하다.

Phase 2A는 TXT 원본 inventory·백업·출처/identity/time/type 계약, multiline parser, 중복/파싱 실패 보고, 재실행 가능한 migration dry-run부터 진행한다. 실제 DB 전환 전에 SQLite 런타임/영속 디렉터리/backup-restore 검증을 수행한다. DB 구축이나 로그 변환은 이번에 실행하지 않았다.
