# 일일 데이터 갱신 — 클로드 앱 루틴 (API 판독 대체)

> 2026-09-07부터 카카오 시세표 판독은 Anthropic API(크레딧 소모)가 아니라 **클로드 앱 예약 작업**이
> 이 맥에서 매일 직접 이미지를 읽어 수행한다. 추가 비용 없음. CI(`daily-vision.yml`)의 정기 실행은 끈 상태
> (수동 `workflow_dispatch` 폴백만 유지).

## 한눈에

```
[매일 05:00 KST — 클로드 앱 예약 작업 "daily-seongji-routine" (이 맥, 앱 실행 중일 때)]
 1) bash routine_prepare.sh 40      git pull → 신규 시세표 이미지 ≤40장 다운로드 → 긴 이미지 분할·RULES.md·results/ 준비
 2) (Claude 세션이 직접 판독)        /tmp/sise_batch/session_manifest.json 순회 → 조각 이미지 Read → results/<idx>.json 저장
 3) bash routine_finalize.sh        results 병합(reader=claude-app-routine) → 재빌드 → [data-only] 커밋 → push
                                     └→ deploy-lightsail.yml 자동 배포(~4분) → 라이브 반영
```

- **부분 완료 허용**: 상한/시간 때문에 못 읽은 이미지는 results 가 없어 스킵리스트에 반영되지 않고, 다음 날 다시 받는다.
- **판독 규칙은 단일 소스**: `vision_api_reader.PROMPT`/`SCHEMA` 를 `vision_session_prep.py` 가 `RULES.md` 로 그대로 내보낸다. 규칙을 바꾸려면 `vision_api_reader.py` 의 PROMPT 만 고친다.
- **저장 스키마 동일**: `vision_session_merge.py` 가 API 판독기의 `to_items`/`merge` 를 재사용. `reader` 태그만 `claude-app-routine`.
- 기종명은 적재 시 `model_normalize.py` 로 정규화되므로 **새로 출시된 기종도 자동으로** 목록에 뜬다.

## 수동 실행 (검수·재실행)

```bash
cd "/Users/1108526/Documents/대시보드/온라인 단가비교"
bash routine_prepare.sh 40            # 1단계
# 2단계: Claude Code 세션에서 "ROUTINE.md 2단계대로 /tmp/sise_batch 판독해줘"
bash routine_finalize.sh              # 3단계
```

## 2단계 판독 절차 (세션이 따르는 순서)

1. `/tmp/sise_batch/RULES.md` 를 읽는다(규칙·출력 스키마).
2. `/tmp/sise_batch/session_manifest.json` 을 읽는다. 항목마다 `idx`, 매장 `name`/`region`, 게시글 `context`(시세표 보는 법), `pieces`(이미지 파일 경로 목록).
3. 항목을 `idx` 순으로: `pieces` 의 파일을 모두 Read 도구로 본 뒤(조각은 위→아래 순, 겹침 중복 제거) 규칙대로 행을 추출해
   `/tmp/sise_batch/results/<idx>.json` 에 스키마 JSON **한 객체**로 저장한다. 시세표가 아니면 `is_price_table:false, rows:[]`.
4. 전부 끝나거나 상한에 도달하면 3단계로 넘어간다. 확신 없는 셀은 confidence 를 0.5 미만으로(적재에서 자동 제외).

## 전제 조건 (이 맥)

| 항목 | 상태 |
|---|---|
| venv | `~/venvs/online-price` (pillow·requests·anthropic·flask). activate 가 `~/certs/env.sh`(사내 CA) 자동 로드 |
| git push 인증 | `gh` 로그인(Babamba-CU) + `gh auth setup-git`. 토큰은 `GH_CONFIG_DIR=~/.ghconfig`(`~/.config` 가 root 소유라 우회) — 스크립트가 export |
| 네트워크 | 사내 프록시 경유. `SSL_CERT_FILE` 등은 venv activate 가 설정 |
| 앱 | 클로드 앱이 켜져 있어야 예약 작업이 돈다. 꺼져 있으면 다음 실행 시 밀린 작업 실행 |

## 문제 해결

- **다운로드 0장**: 채널의 최신 이미지가 이미 판독된 URL이거나(정상), 프록시/네트워크 문제. `python seongji_vision_batch.py --channels 20 --max-images 5` 로 수동 확인.
- **push 거부**: `GH_CONFIG_DIR=~/.ghconfig ~/.local/gh/bin/gh auth status` 로 로그인 확인 후 `git pull --rebase origin main && git push`.
- **라이브 미반영**: `GH_CONFIG_DIR=~/.ghconfig ~/.local/gh/bin/gh run list --workflow=deploy-lightsail.yml --limit 3` 로 배포 성공 확인.
- **CI 폴백(API 판독)**: 크레딧이 있으면 `gh workflow run daily-vision.yml` 로 수동 실행 가능(정기 스케줄은 꺼둠).

## 클라우드 루틴 (2026-09-07 추가 — 맥이 꺼져 있어도 실행)

Anthropic 클라우드 세션(claude.ai/code → Routines, `성지폰 시세표 일일 루틴 (cloud)`)이 같은 3단계를 수행한다.
**단, 클라우드 샌드박스는 `pf.kakao.com` egress 가 차단(403)** 되므로 다운로드는 GitHub Actions 가 대신한다:

```
[04:10 KST  GitHub Actions sise-fetch.yml]   카카오 채널에서 신규 시세표 ≤40장 다운로드 + 분할·RULES.md
                                             → 고아 브랜치 `sise-batch` 에 단일 커밋 force-push (main 오염 없음)
[05:05 KST  클라우드 루틴]                    routine_prepare.sh 가 `sise-batch` 를 받아 /tmp/sise_batch 복원
                                             → Claude 판독(results/) → routine_finalize.sh → push origin HEAD:main
                                             → deploy-lightsail 자동 배포
```

- 스케줄: fetch `10 19 * * *` UTC(04:10 KST), 루틴 `0 20 * * *` UTC(05:05 KST). 관리: https://claude.ai/code/routines
- 스크립트는 환경을 감지한다(venv 없음 → pip 설치 + 배치 브랜치 수신). 배치 브랜치가 없으면 직접 다운로드를 시도.
- 커밋 신원이 없으면 `seongji-routine`으로 설정하고 `git push origin HEAD:main`.
- 로컬 예약작업(`daily-seongji-routine`)은 **중복 수집·push 경합 방지를 위해 비활성화**, 클라우드 실패 시 수동 "Run now" 폴백.
- 클라우드 실행 로그: 세션에서 `/schedule` → 해당 루틴 `list_runs` → `get_run_log`. fetch 워크플로: `gh run list --workflow=sise-fetch.yml`.

## 데이터 규칙 (2026-09-08 점검 후 확정)

점검에서 "누적 판독 전량을 매일 오늘 날짜로 재스탬프"하던 구조가 2023년 표까지 '오늘 단가'로 보여주던 것이 확인돼 아래처럼 바꿨다.

| 항목 | 규칙 | 코드 |
|---|---|---|
| 행의 날짜 | 시세표에 적힌 기준일(`board_date`), 없으면 판독일(KST). 재스탬프 없음 | `vision_api_reader.to_items`, `seongji_vision_load` |
| 현재 시세 | 최근 30일(`SEONGJI_CURRENT_DAYS`) 관측 중 매장×(기종·통신사·가입유형·용량)별 **최신 기준일 행만**. 옛 표는 새 표에 밀려남 | `seongji_build.current_set` |
| 화면 필드 | `board_date` / `age_days` / `store_asof` / `is_conditional` 가 각 행에 붙음. `snapshot_date` 는 빌드일 | `seongji_build`, `build_from_pg` |
| 시계열 | `daily` 는 실제 기준일별 통계(하루치가 아니라 날짜별로 쌓임) | `seongji_db.aggregate_daily` |
| 가격 범위 | `PRICE_SANITY = (-1,500,000, 3,000,000)` — 50만 초과 페이백을 버리던 하한 완화 | `vision_api_reader` (적재도 같은 상수) |
| 차비·0원 | 실제 거래가이므로 일별 통계·박스플롯에 포함 | `seongji_db`, `seongji_build.box_stats_from` |
| 조건부 가격 | 결합·제휴카드·온누리·적용가·이벤트가 → `is_conditional` 표시, 통계·전일비교 제외, 화면 토글 | `price_conditions.py` |
| 표 전체 조건 | "인터넷+TV 동시가입시" 같은 표 전체 구매조건은 모든 행 add_condition 에 기록(판독 규칙) | `vision_api_reader.PROMPT` |
| 신뢰도 | 판독 보존·적재 공통 0.5. 병합 로그에 사유별 제외 행 수 출력 | `vision_session_merge` |
| 날짜 | 모든 '오늘'은 `kst.today_kst()` (클라우드 UTC 라벨 오류 방지) | `kst.py` |
| 기종명 | A175→Galaxy A17, 키즈폰류 별칭 통합, 용량 꼬리표 제거, iPhone Air 오병합 수정 | `model_normalize.py` |
| 링크 게시글 | 교차링크로 받은 시세표는 게시한 채널의 매장으로 귀속 | `seongji_vision_batch`, `seongji_kakao` |
| 전일 대비 | 히스토리 `version=2`(현재 시세 집합 기준). 구버전 기록은 비교 대상 아님 | `kakao_history.py` |

- 매장이 30일 넘게 새 표를 안 올리면 화면에서 빠진다(과거 관측은 `daily` 시계열에만 남음). 창을 넓히려면 `SEONGJI_CURRENT_DAYS` 환경변수.
- 최근 창에 관측이 하나도 없으면 마지막 관측일 기준 창으로 대체하고 `kakaoSummary.stale=true` 로 표시한다.

## 공시지원금 (2026-09-08 — 시드 폐기, 스마트초이스 실데이터)

- 출처: 방통위 통신요금정보포털 **스마트초이스** 단말기 기준 조회(`subsidy_smartchoice.py`). 갤럭시 S·Z 폴드/플립·아이폰 전 단말(용량별, 95종)의
  3사 공시지원금(번호이동/기기변경, 010신규는 번호이동값 준용)·출고가·요금제 구간을 받아 성지 시세표 기준 요금제(SKT 109k/KT 110k/LGU+ 115k)에
  가장 가까운 구간을 대표값으로 적재한다. 전 구간은 `raw_payload.tiers`.
- 흐름: CI `sise-fetch.yml`(04:10) 가 `subsidy_snapshot.json` 을 배치에 싣고 → 클라우드 루틴 `routine_prepare.sh` 가 리포로 복사 →
  `daily_collect.py finalize` 가 스냅샷을 DB 에 적재해 `subsidy_data.js` 빌드 → 커밋(DATA 목록에 포함). 로컬 맥은 prepare 에서 직접 수집.
  라이트세일 컨테이너(`app.py refresh_data`)는 매일 직접 수집하고 실패 시 커밋된 스냅샷을 쓴다. 종전 시드(`subsidy_seed.py`)는 `SUBSIDY_SEED=1` 일 때만.
- 수동: `SSL_CERT_FILE=~/certs/ca-bundle.pem python3 subsidy_smartchoice.py` (약 2분) → `python3 subsidy_build.py`.

## 이상치·조건부 (2026-09-08 점검 반영)

- 26장 시세표 이미지 전수 대조 결과 판독 숫자는 전부 일치했으나, 매장별 표 자체가 **결합(인터넷+TV)·제휴카드 포함가**인 곳이 9곳(가산·굳폰/센텀/덕하/싸당 동작·평택·화성/직폰 평택·가산/사직 카드)이라
  해당 표 전체를 `add_condition` 에 표기해 조건부로 뺐다. 판독 규칙(PROMPT)에도 표 전체 구매조건 기록을 추가했다.
- 매장 간 이상치: 같은 오퍼의 매장 중앙값에서 30만원 넘게 벗어나면 `is_outlier`, 비교 가능한 오퍼의 절반 이상이 이탈한 매장은 표 전체를 이상치로(정직폰 본점·전주·화성 실측 — 시장보다 40~60만 낮음).
  KPI·박스플롯·전일 대비·AI 리포트에서 제외하고 표에는 '이상치' 배지로 남긴다(`seongji_build._flag_outliers`).
- 외부 대조: 알고사 김포 시세표(9/6)·정직폰 강서점 등과 비교해 비이상치 매장 중앙값이 ±10만 안에서 일치함을 확인.
