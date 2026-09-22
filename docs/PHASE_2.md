# Phase 2 — 전체 백업, SQLite 로그 저장소 구축, 기존 TXT 로그 이관

Phase 1E(`dd84c7d`) 이후 작업이다. 캐릭터 프롬프트는 건드리지 않았다. `bot/persona.py`는 한 글자도 바뀌지 않았고, Phase 1E의 후보 B/C/D도 운영 경로에 연결하지 않았다. 모델, API key 순환, Router, ConversationQueue, Discord 응답 구조는 저장소 작업과 무관한 범위에서 그대로다.

## 1. 백업

소스를 수정하기 전에 가장 먼저 수행했다.

| 항목 | 값 |
|---|---|
| 백업 위치 | `/home/ubuntu/Choi_bot_backups/pre_phase2_20260922-173524/` |
| 생성 시각 | 2026-09-22 17:35 (KST 가정) |
| 원본 HEAD / origin/main | `dd84c7d` / `dd84c7d` (동일) |
| 백업 HEAD | `dd84c7d` |
| 복사 방식 | `cp -a` (mode·mtime·symlink 보존) |
| 총 파일 수 | 원본 671 / 백업 671 (`.git` 제외) |
| 총 크기 | 원본 56,916,315 / 백업 56,916,315 bytes (동일) |
| `logs/` snapshot | 548 파일, 52,796,280 bytes |
| `.git` 포함 | 예 |
| git bundle | `Choi_bot.bundle` — `bundle verify` 통과, "records a complete history" |

백업의 `git status --short`와 `git rev-parse HEAD`가 원본과 일치함을 확인했다. 미커밋 `choi_bot.py` 변경과 미추적 4개 파일이 백업에도 그대로 들어 있다. `ini.env`, `logs/`, `logs_bak/`, 테스트 fixture, 문서 모두 포함된다. 백업은 repository 바깥에 있고 Git에 추가하지 않았다. API key·token 값은 어떤 산출물에도 출력하지 않았다.

**운영 봇은 중지하지 않았다.** 컨테이너 `choi-bot`이 계속 실행 중이며 백업 도중에도 `logs/2026-09-22.txt`에 append했다. 따라서 이것을 "모든 파일이 정확히 같은 한 시각의 filesystem snapshot"이라고 주장하지 않는다. 대신 복사 완료된 각 파일의 size·mtime·sha256을 `logs_manifest.json`으로 고정했고, **그 manifest가 migration 검증의 기준 입력**이다. 백업 이후 원본 로그의 hash가 달라지는 것은 정상이며 실패로 보지 않는다.

## 2. 로그 사용처 inventory

repository 전체를 검색해 확인한 지점이다.

| 지점 | 현재 동작 | DB 전환 후 | 외부 동작 변화 |
|---|---|---|---|
| `LOG_FOLDER` | `logs` 상수 | 유지 (TXT mirror 경로) | 없음 |
| `save__logs()` | TXT append | **유지.** mirror 전용으로 역할 축소 | 없음. 형식 동결 |
| `record_event()` | 신규 | TXT mirror + DB 기록 | 없음 |
| `get_latest_log_lines()` | 최신 파일 마지막 N **line** | fallback 전용으로 잔존 | 없음 |
| `get_recent_log_view()` | 신규 | DB에서 N **logical message** | `/로그` 의미 변경 (§6) |
| `get_day_summary_lines()` | 신규 | DB에서 날짜별 렌더링 | `/요약`·`/찾기` 입력 변화 (§6) |
| `record_bot_reply()` | `save__logs("최씨 봇", …)` | + `delivery_state` 기록 | 없음 |
| `on_message()` | allowed channel 판정 **전에** TXT 기록 | 동일 위치에서 record_event | **수집 범위 불변** |
| `/로그` | TXT line | DB logical message | §6 |
| `summary()` / `/요약` / `/찾기` | TXT regex line parser | DB 조회 → 동일 형식 렌더 | §6 |
| `/질문`·`/알려줘`·`/자세히` | `save__logs("USER", …)` | DB에 실제 실행자, TXT는 `USER` 유지 | 없음 |
| `/점메추`·`/저메추` | 최종 답변만 TXT | 동일 범위로 DB 저장 | 없음 |
| `/번역`, 요약 결과 | 저장 안 함 | **그대로 저장 안 함** | 없음 |
| `Console` 기록 | 전부 주석 처리 상태 | 그대로 둠 | 없음 |

DB 도입을 이유로 수집 범위를 넓히지 않았다.

## 3. 원본 로그 구조

frozen backup snapshot 기준 (`scripts/inventory_logs.py`).

| source_kind | 파일 | bytes | header record |
|---|---:|---:|---:|
| `legacy_daily` (canonical) | 537 | 28,996,185 | 507,879 |
| `legacy_merged` (`*_all.txt`) | 11 | 23,800,095 | 426,713 |
| `legacy_backup` (`logs_bak/`) | 70 | 3,508,873 | 54,381 |
| **합계** | **618** | **56,305,153** | **988,973** |

- **byte coverage 56,305,153 / 56,305,153 = 100.0000%**
- multiline 6,425 / 빈 본문 33,501 / actor 34명
- 기간 2025-03-10 22:45:04 ~ 2026-09-22 17:25:12
- UTF-8 decode 실패 파일 0
- parse issue 40건 (전부 `ambiguous_header_timestamp_regression`)

`logs_bak/`은 **pristine backup이 아니다.** `logs_bak/clear.py`가 `[DEBUG] 이미 초기화됨` 줄을 실제로 제거하며 파일을 덮어쓴 흔적이 있다. 따라서 canonical로 쓰지 않는다. `logs_bak/2025-03-14`(확장자 없음)는 0 bytes이며 대상이 아니다.

## 4. Migration 결과

설계는 [PHASE_2_DB_DESIGN.md](PHASE_2_DB_DESIGN.md)에 있다.

### frozen snapshot (검증 기준)

```
run 1: inserted 507,879 | matched 0       | 62s
run 2: inserted 0       | matched 987,731 | 11s
```

| 항목 | 값 |
|---|---|
| canonical message | 507,879 |
| message_origins | 987,731 |
| log_sources | 618 |
| multiline | 4,062 |
| 빈 본문 | 16,966 |
| secondary exact match | 479,852 |
| secondary ambiguous | 1,242 |
| parse issue | 1,282 |

**전수 정합:** 507,879 + 479,852 + 1,242 = 988,973 = inventory의 전체 header record 수. 모든 레코드가 canonical 메시지이거나, 기존 메시지에 연결된 provenance이거나, 기록된 issue다. **버려진 것은 없다.**

### 재실행 (idempotency)

2회차 `inserted = 0`, `matched = 987,731`. messages·message_origins·log_sources·parse_issues **모두 증가 0**. issue 목록은 실행마다 쌓이지 않고 outstanding worklist로 유지된다.

### 중복 처리

content 문자열로 dedupe하지 않는다. occurrence identity는 `(relative_path, byte_start, raw_hash)`다.

- 같은 `(timestamp, actor)` 그룹 2,463개가 실재한다.
- 그중 **내용까지 완전히 같은 200개 그룹도 각각 별도 row로 보존**했다. 같은 사람이 같은 초에 같은 말을 두 번 한 것이지 중복 기록이 아니다.

### secondary 미일치 1,242건 분석

전부 `*_all.txt` 사용자별 export에서 나왔다. `logs_bak/`은 **미일치 0건**으로 canonical에 완전히 포함된다.

1,242건을 구조적으로 확인한 결과:

| 확인 | 결과 |
|---|---|
| canonical에 해당 날짜가 아예 없음 | 0 |
| 같은 `(timestamp, actor)`가 canonical에 없음 | 0 |
| 같은 `(timestamp, actor)`는 있으나 content가 다름 | 1,242 |
| **secondary content가 canonical content의 substring** | **1,242 (전부)** |

즉 export본이 **canonical에 더 온전한 형태로 존재하는 메시지의 잘린 변형**이다. export에만 있는 고유한 역사는 발견되지 않았다. 그럼에도 자동으로 병합하거나 새 메시지를 만들지 않고 issue로 남겼다. 원본 파일과 byte 범위가 보존되므로 언제든 재검토할 수 있다.

## 5. 무손실 검증

`scripts/verify_log_migration.py`가 canonical source를 다시 읽어 DB와 대조한다.

```
canonical sources verified : 537
messages compared          : 507,879
content mismatches         : 0
missing origins            : 0
raw hash mismatches        : 0
uncovered bytes            : 0
integrity_check ok         : True
RESULT: LOSSLESS
```

대표 표본을 **저장된 byte offset으로 원본에서 다시 읽어** 재파싱 결과가 DB 내용과 일치하는지 확인했다: 최초 기록, 최종 기록, 빈 본문, multiline, 코드블록, 매우 긴 본문, 한글 — 전부 통과.

## 6. 영향받은 명령어

### `/로그` — 의미가 바뀐다 (문서화된 유일한 사용자 관찰 변화)

기존은 최신 파일의 마지막 N **물리 line**이었다. multiline 메시지는 여러 line으로 쪼개져 셈해졌다.

DB 전환 후에는 N **논리 메시지**다. 출력 형식 `[YYYY-MM-DD HH:MM:SS] actor: content`와 chunking·헤더는 그대로다.

이 차이를 숨기지 않는다. 명령어 설명이 이미 "n개의 채팅 로그"이므로 논리 메시지가 설명에 더 부합한다고 판단했다. 실제 archive에서 multiline은 canonical 507,879건 중 4,062건(0.8%)이므로 대부분의 호출에서 체감 차이는 없다.

### `/요약`·`/찾기` — source만 전환

map/reduce 프롬프트, 4000자 chunking, LLM task_type, 결과 제목은 **하나도 바꾸지 않았다.** 요약 알고리즘 개선과 저장소 전환을 섞지 않았다.

렌더링 형식 `[HH:MM] display_name: content`도 동일하다. 두 가지가 달라진다.

1. **multiline 본문이 온전히 들어간다.** 기존 line regex는 continuation line을 통째로 버렸다. DB는 실제 메시지를 갖고 있으므로 복원된다.
2. 표시 이름은 저장이 아니라 **render 시점**에 `USER_MAP`으로 해석된다.

빈 본문은 기존 regex가 `(.+)`로 배제했으므로 **동일하게 계속 배제**한다.

해당 날짜가 DB에 없으면 기존 TXT regex 경로로 fallback한다(§9).

### 그 외

- `on_message()`: allowed channel 판정 **전에** 기록하는 현재 수집 범위를 그대로 유지했다.
- bot reply: 억제된 `00100`은 `delivery_state='suppressed'`, `message_type='suspected_control'`로 기록해 실제 전송된 답변과 구분한다. 과거 데이터는 확정 불가이므로 `suspected_control`로 둔다.
- `/질문`·`/알려줘`·`/자세히`: DB에는 `interaction.user`의 실제 id/name과 `command_name`을 저장하고, TXT mirror는 legacy `USER` 라벨을 유지한다. 과거 `USER` 기록의 실행자는 **추측해서 backfill하지 않았다.**
- `/점메추`·`/저메추`: 최종 답변만 저장하는 기존 범위 유지. 후보 15개 내부 응답은 저장하지 않는다.
- 20개 command의 이름·인수·권한·설명은 무변경(contract 테스트 유지).

## 7. 성능

production candidate DB 실측.

| 항목 | 값 |
|---|---|
| DB 파일 크기 | 347 MB (원본 TXT 56 MB) |
| 전체 migration | 72초 / 508,044 메시지 |
| 재실행(전량 match) | 11초 |
| messages / origins | 508,044 / 987,896 |
| 가장 바쁜 날 | 2026-05-13 (3,810건) |

| 조회 | cold | warm median |
|---|---:|---:|
| 최신 100건 (`/로그`) | 1.2 ms | 0.8 ms |
| 하루치 3,810건 (`/요약`) | 37.6 ms | 34.5 ms |
| actor+date 811건 | 7.7 ms | 7.3 ms |
| 전체 count | 9.8 ms | 9.4 ms |
| `MIN+MAX(local_date)` | 55.9 ms | 55.6 ms |

`/로그`와 `/요약`의 hot path는 covering index를 탄다(`EXPLAIN QUERY PLAN`으로 확인). `MIN+MAX` 동시 집계만 index 한 번으로 처리되지 않는데, backup 검증에서만 쓰이므로 이번에는 두지 않았다.

DB가 원본의 6배인 이유는 provenance다. `message_origins` 98만 row와 그 UNIQUE index가 대부분을 차지한다. hash를 hex TEXT에서 32byte BLOB으로 바꿔 435MB → 347MB로 줄였다. 현재 규모에서 SQLite로 충분하다고 판단하며, 이 수치를 과장하지 않는다.

## 8. DB 무결성·backup/restore

production candidate에서:

```
PRAGMA integrity_check    : ok
PRAGMA foreign_key_check  : 위반 0
journal_mode (실측)        : wal
```

`sqlite3.Connection.backup()`으로 백업 후 새 위치에서 열어 대조했다. messages·message_origins·log_sources·parse_issues 수, date range, source hash 개수, 샘플 row 모두 **일치**. restored DB의 `integrity_check`도 ok. `RESTORE VERIFIED`.

## 9. 신규 저장 구조와 TXT mirror

`bot/storage/`

| 파일 | 역할 |
|---|---|
| `legacy_parser.py` | multiline·byte offset parser |
| `schema.py` | DDL, schema version |
| `database.py` | connection·pragma·backup·integrity |
| `repository.py` | 조회/기록 API, 렌더러 |
| `migration.py` | source 등록, import, idempotency |
| `runtime.py` | `LogStore` — lazy 연결, 실패 격리 |

`scripts/`: `inventory_logs.py`, `migrate_logs.py`, `verify_log_migration.py`, `backup_database.py`.

명령어는 SQL을 직접 쓰지 않는다. `latest_messages`, `messages_for_date`, `messages_for_date_and_actor`, `count_messages`, `date_range`, `render_legacy_view`, `render_summary_view`만 사용한다.

**쓰기 정책.** DB가 신규 구조의 primary store, TXT는 compatibility/recovery mirror다. **원자적 dual-write가 아니며 그렇게 표현하지 않는다.** SQLite transaction과 filesystem append를 한 단위로 묶을 수 없다.

순서는 TXT → DB다. DB 실패 시 TXT에는 남으므로 나중에 reconcile할 수 있고, 불일치를 `logger.error`로 명시한다. `LogStore`는 예외를 봇으로 올리지 않으며, **logging 실패로 이미 생성된 LLM 답변을 다시 생성하지 않는다.** 재처리된 이벤트는 `discord_message_id` partial unique index로 중복 저장되지 않는다.

`LogStore`는 **첫 사용 시점에** 연결한다. import만으로는 파일을 만들지 않으므로 기존 `test_import_has_no_runtime_side_effects` 계약이 유지된다.

**TXT는 삭제하지 않았다.** 원본 TXT는 변형되지도 않았다.

## 10. 테스트

```bash
PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=/tmp/choi-phase1b-deps python3 -m unittest discover
```

**127개 전부 통과** (기존 87 + Phase 2 신규 40).

신규 검증: multiline 보존, CRLF/BOM 기록, strict UTF-8 거부, orphan text, 모호 header 보존, byte coverage, 동일 초 동일 내용 분리 보존, 재실행 inserted 0, append 시 tail만 import, rewrite 시 새 source row, 중단 후 재개, raw_actor 불변, secondary 연결/미일치 flag, source metadata, legacy `event_time` NULL, 유형 분류, integrity/foreign key, backup·restore, 재오픈, 상위 schema version 거절, constraint 위반 후 무결성, 날짜·actor·최신 조회, 동일 초 정렬, 렌더 형식, lazy 연결, DB 불가 시 degrade, 재처리 중복 방지, TXT mirror 동시 기록, mirror 실패 전파, DB 실패 시 mirror 유지, Discord/interaction metadata, 억제 신호 기록, `/로그` 논리 메시지, 요약 렌더, fallback 경로.

실제 원본 로그는 fixture로 commit하지 않았다. 전부 synthetic이다. 원본 대조는 로컬에서 `verify_log_migration.py`로 수행하고 통계만 문서에 남긴다.

테스트가 실제 `logs/` 디렉터리에 DB를 만들지 않도록 `temp_log_store()`로 격리했다. (초기 구현에서 실제로 `logs/db/`가 생성되는 것을 발견해 수정했다.)

**실제 Gemini 호출 0회. 실제 Discord 전송 0회. Docker 변경 0회.**

## 11. production candidate DB

`logs/db/choi_bot.sqlite3` (container: `/app/logs/db/choi_bot.sqlite3`).

live `logs/`를 오래 붙잡지 않도록 **0.07초짜리 short-lived snapshot**을 뜬 뒤 그것을 import했다.

| 항목 | 값 |
|---|---|
| messages | 508,044 |
| message_origins | 987,896 |
| log_sources | 618 |
| parse_issues | 1,282 |
| 기간 | 2025-03-10 ~ 2026-09-22 |
| 검증 | LOSSLESS, integrity ok, RESTORE VERIFIED |
| import 시점 최신 레코드 | 2026-09-22 17:56:04 |

**이 DB는 생성 시점 이후 메시지를 포함하지 않는다.** 확인 시점에 이미 오늘 파일에 2,377 bytes의 새 tail이 쌓여 있었다. 배포 직전 incremental catch-up이 반드시 필요하다.

## 12. 운영 cutover 절차 (이번에 실행하지 않음)

Docker stop/rm/restart, 봇 재시작, 실제 Discord 전송은 **하지 않았다.**

배포 시 순서:

1. 현재 프로젝트 전체 백업 (§1과 동일 방식) + 기존 DB backup
2. `cp -a logs logs_bak` → fresh short-lived snapshot 생성
3. `python3 scripts/migrate_logs.py --root <snapshot> --db logs/db/choi_bot.sqlite3` — append분만 들어가고 기존은 match된다
4. `python3 scripts/verify_log_migration.py` — LOSSLESS 확인
5. `python3 scripts/backup_database.py --out <backup dir>` — RESTORE VERIFIED 확인
6. `sqlite3` 없이 `integrity_check`/`foreign_key_check` 확인(스크립트가 수행)
7. 신규 코드 배포 (`./restart.sh`)
8. canary: `/로그 5`로 DB 경로 동작 확인, `/요약 <어제>`로 렌더 확인
9. 신규 메시지 1건 후 DB와 TXT 양쪽에 남는지 확인
10. 일정 기간 TXT mirror 유지, `write_failures` 경고 모니터링

컨테이너는 이미 `logs/`를 bind mount하므로 **volume 설정 변경이 필요 없다.**

## 13. Rollback

| 실패 지점 | 복구 |
|---|---|
| 코드 문제 | `git revert` 또는 Phase 1E `dd84c7d` checkout |
| DB 손상 | `backup_database.py` 산출물에서 복원 |
| DB 전체 폐기 | `logs/db/` 삭제 — TXT 원본이 그대로 남아 있음 |
| 프로젝트 전체 | `/home/ubuntu/Choi_bot_backups/pre_phase2_20260922-173524/Choi_bot` |
| Git history | 같은 위치의 `Choi_bot.bundle` |

TXT parser와 TXT 읽기 경로를 삭제하지 않았다. `get_latest_log_lines()`와 summary의 regex 경로는 fallback으로 남아 있어 DB 없이도 동작한다. 이 compatibility code에는 `TODO(phase-3)` 주석을 달아 두었고, 안정화 후 제거 대상이다. 영구적으로 두 구현을 병렬 유지할 의도는 없다.

## 14. 남은 위험과 Phase 3

1. **production DB는 catch-up 없이 최신이 아니다.** 배포 직전 §12의 3단계가 필수다.
2. secondary 미일치 1,242건은 `resolution_state='open'`이다. 전부 canonical의 substring으로 확인됐지만 자동 병합하지 않았다.
3. 모호 header 40건은 메시지로 보존하되 flag되어 있다. 자동 parser가 모든 경계를 100% 복원했다고 주장하지 않는다.
4. legacy timezone은 `Asia/Seoul` 가정이며 전체 기간에 대해 증명되지 않았다.
5. legacy `event_time`은 영구히 NULL이다. 과거 Discord 실제 발생 시각은 복구 불가다.
6. 과거 `USER` command 기록의 실제 실행자는 복구 불가다.
7. 운영 Discord에서의 `/로그`·`/요약` 동작은 금지 조건상 미검증이다. fake와 로컬 DB로만 확인했다.
8. `/찾기`는 여전히 하루치 전체를 LLM에 넘긴다. FTS5·정확 검색·semantic search는 Phase 3다. 한국어 1~2글자 검색에 대한 tokenizer 검증 없이 FTS를 붙이지 않았다.
9. `conversation_context` deque(maxlen=20)는 그대로다. DB로 대체하지 않았고, 매 LLM 요청마다 전체 로그를 읽지 않는다. 채널별 컨텍스트는 후속 Phase다.
