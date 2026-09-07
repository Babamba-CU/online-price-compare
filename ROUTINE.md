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
