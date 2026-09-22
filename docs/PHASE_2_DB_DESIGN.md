# Phase 2 — SQLite 로그 저장소 설계

이 문서는 schema 참조본이다. 실행 결과와 이관 통계는 [PHASE_2.md](PHASE_2.md)에 있다.

설계는 추정이 아니라 실제 archive inventory 결과를 근거로 확정했다. 근거가 되는 수치는 `scripts/inventory_logs.py`가 frozen backup snapshot에서 산출한 값이다.

## 1. 기술 선택

Python 표준 `sqlite3`만 사용한다. ORM, 외부 DB, 벡터 DB는 도입하지 않았다. 현재 규모(약 50만 레코드, 원본 56MB)에서 SQLite로 충분함을 §7 성능 측정으로 확인했다.

## 2. 파일 위치

| 위치 | 경로 |
|---|---|
| host | `logs/db/choi_bot.sqlite3` |
| container | `/app/logs/db/choi_bot.sqlite3` |

`run.sh`/`restart.sh`가 이미 `-v $(pwd)/logs:/app/logs`로 bind mount하므로 **Docker volume 구조를 바꾸지 않고** DB가 host에 영속된다. `.gitignore`의 `logs/`가 DB 파일을 덮으므로 commit되지 않는다.

경로는 hard-code하지 않는다. 우선순위는 명시 인자 → `CHOI_DB_PATH` 환경변수 → 기본값이다. 이번 작업에서 `ini.env`에는 아무 값도 추가하지 않았다.

## 3. 연결 설정

`bot/storage/database.py`가 소유한다.

| 설정 | 값 | 근거 |
|---|---|---|
| `foreign_keys` | ON | `message_origins` → `messages`/`log_sources` 무결성 |
| `journal_mode` | WAL | 봇이 append하는 동안 migration/조회가 읽을 수 있어야 함 |
| `synchronous` | NORMAL | WAL과 함께 쓰는 일반 조합 |
| `busy_timeout` | 5000ms | 동시 접근 시 즉시 실패 방지 |

WAL은 파일시스템이 거부할 수 있으므로 설정값을 가정하지 않고 `journal_mode(connection)`으로 **실제 적용된 모드를 되읽어** 확인한다. 실측 결과는 `wal`이다.

schema version은 `schema_meta` 테이블에 저장한다. 코드보다 높은 version의 DB를 열면 조용히 진행하지 않고 `RuntimeError`로 거절한다.

backup은 **열린 파일을 `cp`하지 않는다.** `sqlite3.Connection.backup()`을 사용한다. 쓰기 중인 DB를 파일 복사하면 torn page나 WAL tail 누락이 발생할 수 있다.

## 4. 테이블

### schema_meta
`key`/`value`. 현재 `schema_version = 1`.

### log_sources
원본 TXT snapshot의 provenance.

`id`, `source_kind`, `relative_path`, `original_path`, `filename`, `file_size`, `mtime`, `sha256`, `encoding`, `newline_style`, `has_bom`, `timezone_assumption`, `parser_version`, `imported_at`

`UNIQUE (sha256, relative_path)`.

`source_kind`:

| 값 | 대상 | canonical 여부 |
|---|---|---|
| `legacy_daily` | `logs/YYYY-MM-DD.txt` | **canonical** |
| `legacy_merged` | `logs/*_all.txt` 사용자별 export | secondary |
| `legacy_backup` | `logs_bak/*.txt` | secondary |
| `live_txt` | 신규 런타임 이벤트 | 해당 없음 |

절대 경로는 저장하지 않고 repository 기준 상대 경로만 기록한다.

**append와 rewrite를 구분한다.** `save__logs()`는 append만 하므로, 기존 크기까지의 prefix hash가 일치하면 같은 파일이 자란 것으로 보고 같은 row를 갱신한다. 그래야 catch-up을 반복해도 실행 횟수만큼 snapshot row가 쌓이지 않는다. prefix가 달라졌으면 rewrite이므로 **새 row를 만들고 이전 기록을 남긴다**(`logs_bak/clear.py`가 실제로 파일을 덮어쓴 사례가 있다).

### messages
canonical 논리 메시지.

`id`, `event_uuid`, `source_kind`, `message_type`, `raw_actor`, `display_actor`, `discord_user_id`, `guild_id`, `channel_id`, `thread_id`, `discord_message_id`, `command_name`, `content`, `raw_timestamp`, `event_time`, `recorded_at`, `local_date`, `delivery_state`, `control_kind`, `created_at`

Discord snowflake는 **TEXT**로 저장한다. SQLite INTEGER 범위 문제 때문이 아니라 JSON·API·향후 export 사이에서 int/str이 섞이는 것을 막기 위해서다.

### message_origins
canonical record와 원본 byte 범위의 연결.

`id`, `message_id`, `source_id`, `byte_start`, `byte_end`, `line_start`, `line_end`, `raw_hash`, `match_method`, `confidence`

`UNIQUE (source_id, byte_start, raw_hash)` — **occurrence identity**다. content 문자열이 아니다. 같은 사람이 같은 초에 같은 말을 두 번 했으면 두 record로 남는다(실제 archive에 그런 그룹이 200개 있다).

`raw_hash`는 64자 hex TEXT가 아니라 **32byte BLOB**이다. 약 99만 row에서 테이블과 UNIQUE index에 hex로 저장하면 약 126MB가 낭비되며, BLOB 전환으로 DB가 435MB → 347MB로 줄었다.

`match_method`: `byte_range`(canonical 직접), `exact_secondary_match`(secondary가 canonical에 연결됨).

### import_runs
`id`, `started_at`, `finished_at`, `parser_version`, `mode`, `source_count`, `inserted_count`, `matched_count`, `skipped_count`, `issue_count`, `status`

### parse_issues
파싱 실패·모호 구간을 버리지 않고 남긴다.

`id`, `import_run_id`, `source_id`, `byte_start`, `byte_end`, `line_start`, `line_end`, `issue_type`, `raw_excerpt_hash`, `description`, `resolution_state`

`UNIQUE (source_id, byte_start, issue_type)` — 재실행해도 worklist가 불어나지 않는다. 실행별 집계는 `import_runs.issue_count`가 갖는다.

원문 전문은 저장하지 않고 **hash와 byte 범위만** 남긴다. 원본 파일이 보존되므로 언제든 재확인할 수 있다.

### actor_aliases
`raw_actor` → `display_actor` 확정 매핑과 `mapping_version`. 현재 표시 이름은 render 시점에 `USER_MAP`으로 해석하므로 이 테이블은 향후 버전 관리용으로 비어 있다.

## 5. Index

| index | 용도 |
|---|---|
| `messages(local_date, event_time, id)` | `/요약`·`/찾기` 날짜 조회, `MAX(local_date)` |
| `messages(raw_actor, local_date)` | 사용자·날짜 조회 |
| `messages(message_type, local_date)` | 유형별 조회 |
| `messages(recorded_at, id)` | `/로그` 최신 N건 |
| `messages(discord_message_id)` partial unique | live 이벤트 재처리 중복 방지 |
| `messages(raw_timestamp, raw_actor)` | secondary → canonical 연결. 없으면 export 1건마다 전체 스캔 |
| `message_origins(message_id)` | provenance 역조회 |

FTS5는 도입하지 않았다. 한국어 1~2글자 검색에 대한 tokenizer 검증이 없으므로 Phase 3에서 별도 평가한다.

## 6. 메시지 유형과 확정할 수 없는 것

`human_message`, `bot_message`, `command_input`, `command_output`, `legacy_unknown`, `suspected_control`.

legacy TXT는 actor 문자열만 남겼으므로 과거 봇 줄이 실제로 전송된 답변인지 억제된 `00100` 제어 신호인지 확정할 수 없다. 내용에 `00100`이 있으면 `suspected_control`로 두고 **둘 중 하나로 단정하지 않는다.** `Console` actor는 `legacy_unknown`이다.

신규 이벤트는 다르다. `reply()`가 실제 전송 여부를 알고 있으므로 `delivery_state`를 `sent`/`suppressed`로 정확히 기록한다.

## 7. 시간 모델

legacy header `[YYYY-MM-DD HH:MM:SS]`는 **Discord 이벤트 발생 시각이 아니다.** `save__logs()`가 `datetime.now()`로 찍은 **기록 시각**이다. 따라서:

- legacy row: `recorded_at` = header 값, `event_time` = **NULL**
- live row: `recorded_at` = 기록 시각, `event_time` = `message.created_at`

timezone은 naive 값이다. 컨테이너가 `TZ=Asia/Seoul`로 실행되므로 그 가정을 `log_sources.timezone_assumption`에 문자열로 남기되, **전체 기간이 동일 timezone이었음을 증명할 수 없다는 한계를 함께 기록한다.**

## 8. Actor 정책

`raw_actor`는 원본 문자열 그대로 저장하며 **절대 덮어쓰지 않는다.** 표시 이름은 조회 시점에 `USER_MAP`으로 해석한다. 매핑이 바뀌어도 저장된 역사는 변하지 않는다.

문자열 유사성으로 계정을 병합하지 않는다. `USER_MAP`에 명시된 매핑만 표시에 사용하고, 과거 `USER` 기록의 실제 실행자를 현재 정보로 추측해 backfill하지 않는다.

## 9. 순서

같은 초에 여러 메시지가 존재한다(실제 archive에 2,463개 그룹). timestamp만으로 순서를 정하지 않고 `(timestamp, id)`를 사용하며, legacy에서 `id`는 **source file + byte_start 순서**로 부여된다. 이것을 Discord 실제 전달 순서라고 주장하지 않는다.

## 10. canonical source 정책

`logs/YYYY-MM-DD.txt`만 canonical이다. secondary는 새 canonical record를 만들지 않는다.

secondary record는 `(raw_timestamp, raw_actor, content)`가 **정확히** 일치하는 canonical row에만 연결한다. 일치하는 것이 없으면 새 메시지를 만들지 않고 `secondary_without_canonical_match` issue로 남긴다. 원본 파일과 byte 범위가 보존되므로 나중에 언제든 재검토할 수 있다.

## 11. Parser

`bot/storage/legacy_parser.py`, `PARSER_VERSION = 2.0.0`.

- header를 만나면 새 record를 시작하고, 다음 header까지의 모든 줄은 본문 continuation으로 **줄바꿈까지 보존**한다.
- byte offset과 line offset을 모두 추적한다.
- UTF-8을 **strict**하게 검사한다. replacement character로 조용히 바꾸지 않고 예외를 발생시켜 source 단위 issue로 기록한다.
- BOM/CRLF/LF를 감지해 `log_sources`에 기록하고 본문에서는 정규화한다.
- 첫 header 이전 텍스트는 `orphan_text` issue다.

**모호성을 숨기지 않는다.** TXT에 escape 규칙이 없으므로 본문 한 줄이 우연히 header 형식일 수 있고 이를 완벽히 판별할 수 없다. `save__logs()`가 시간순으로만 append하므로, header timestamp가 직전보다 과거로 돌아가면 인용된 본문일 가능성이 있다고 보고 `ambiguous_header_timestamp_regression`으로 **기록하되 메시지는 그대로 보존한다**(버리면 실제 내용이 사라진다). 자동 parser가 모든 경계를 100% 복원했다고 주장하지 않으며, byte 범위가 있으므로 원본과 언제든 재검증할 수 있다.

## 12. 재실행 안전성

occurrence identity가 `(relative_path, byte_start, raw_hash)`이므로 같은 snapshot을 다시 import하면 `inserted = 0`이다. parser version이 바뀌었다는 이유만으로 동일 occurrence를 새 메시지로 넣지 않는다. 파일 단위 transaction이므로 중단된 실행은 다음 실행에서 이어서 진행된다.

## 13. 쓰기 정책

DB가 신규 구조의 primary store이고 TXT는 compatibility/recovery mirror다. **원자적 dual-write가 아니다.** SQLite transaction과 filesystem append를 하나의 원자 단위로 묶을 수 없다.

순서는 TXT → DB다. DB 쓰기가 실패하면 TXT에는 남아 있으므로 나중에 reconcile할 수 있고, 그 불일치를 `logger.error`로 명시한다. 어느 경우에도 logging 실패 때문에 이미 생성된 LLM 답변을 다시 생성하지 않는다.
