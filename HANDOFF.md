# HANDOFF — 온라인 단가비교 (성지폰/공시지원금 대시보드)

> 다른 컴퓨터에서 이 문서를 붙여넣거나 `git pull` 후 읽고 이어서 작업하세요.
> 작성 기준: 2026-07-19 · 최신 커밋 `3afeb7c` (GitHub·GitLab·로컬 3곳 동기화, working tree 클린)

---

## 0. 30초 요약

3사 휴대폰 단가(공시지원금 + 성지폰 사이트 + **카카오 성지 시세표 이미지**)를 모으는 BI 대시보드.
카카오 시세표는 **이미지**라서 Claude API(Vision, **Sonnet 5**)로 판독한다.
**매일 04:40 KST GitHub Actions가 완전 무인으로** 수집→판독→빌드→커밋→배포하는 폐루프가 가동 중.
가장 최근 작업은 **판독 정확도 개선**(실이미지 감사 → 원인 진단 → 프롬프트/코드 수정). 내일 정기 실행 결과 재검증만 남음.

- **로컬 경로**: `/Users/taeholee/Documents/대시보드/온라인 단가비교/`
- **라이브**: https://seongji-price-monitor.62h5ewkf735c4.ap-northeast-2.cs.amazonlightsail.com/
- **원격**: GitHub `Babamba-CU/online-price-compare` + GitLab `MAMF/online-price`(사내, oauth2 토큰 URL)
- **라이브 현황**: 카카오 1,345행 / 17매장 (전부 당일 자동판독분)

---

## 1. 데이터 폐루프 (전체 아키텍처)

```
[매일 04:40 KST — GitHub Actions: daily-vision.yml, ubuntu 러너]
 1) seongji_vision_batch.py  카카오 342채널 → 신규 시세표 이미지 40장 다운로드
                             (vision_skiplist 2회실패 채널 제외, /tmp/sise_batch/manifest.json)
 2) vision_api_reader.py     이미지 → Claude API(Sonnet 5) 판독 → seongji_vision_data.json 병합
                             (초대형 이미지는 원본해상도 겹침분할 전송)
 3) daily_collect.py finalize  seed + 카카오 텍스트 재수집 + vision 적재 + seongji_build → *_data.js
 4) git commit "[data-only]" + push
 5) gh workflow run deploy-lightsail.yml  ← 봇 커밋은 워크플로 미트리거라 직접 dispatch

[deploy-lightsail.yml]  Docker 빌드(amd64) → Lightsail push → 배포 → /health 200 검증 (~4분)

[Lightsail 컨테이너]  app.py: 0.0.0.0:8080 정적 서빙 + APScheduler 매일 05:00 자체 데이터 재빌드
```

**중요 우회 이유**: 로컬 Mac의 Docker(colima+containerd)는 `lightsailctl`과 호환이 깨져
**로컬 `deploy/lightsail/deploy.sh`는 고장남**. 그래서 배포는 GitHub Actions(clean amd64)에서만.
→ 코드 변경 후엔 그냥 `git push` 하면 CI가 배포. 로컬에서 deploy.sh 직접 실행 금지.

---

## 2. 자동화 & 시크릿 (GitHub → Settings → Secrets and variables → Actions)

| Secret | 용도 | 상태 |
|---|---|---|
| `AWS_ACCESS_KEY_ID` / `AWS_SECRET_ACCESS_KEY` | Lightsail 배포 | ✅ 등록됨 |
| `ANTHROPIC_API_KEY` | 시세표 Vision 판독 (크레딧 필요) | ✅ 등록·충전됨 |
| `NAVER_CLIENT_ID` / `NAVER_CLIENT_SECRET` | 네이버 카페/블로그 수집 | ❌ 미등록(네이버 수집만 skip). 값은 로컬 `deploy/lightsail/.env`에 있음 |
| `GIT_RAW_BASE` / `GIT_SYNC_TOKEN` | git 미러 모드(배포없이 데이터 갱신) | ❌ 미등록 |

워크플로 3개: `daily-vision.yml`(신규·핵심), `deploy-lightsail.yml`(배포), `daily-crawl.yml`(구 공시크롤·별개).

---

## 3. 핵심 파일 지도

| 파일 | 역할 |
|---|---|
| `vision_api_reader.py` ⭐ | 시세표 이미지 → Claude API 판독. **판독 프롬프트·스키마가 여기 있음(정확도 튜닝 지점)**. MODEL=`claude-sonnet-5`, max_tokens=32000+stream, thinking off, `_image_blocks`(2300px 겹침150 분할) |
| `seongji_vision_load.py` | vision_data.json → SQLite 적재. **신선도 필터: `reader` 태그(자동판독분)만 적재**, 비휴대폰(워치/태블릿) 제외 NON_PHONE_RE, 조건부(결합/제휴/온누리) add_condition 표기 |
| `seongji_vision_batch.py` | 이미지 다운로드기(로컬/CI). vision_skiplist 연동 |
| `seongji_vision_data.json` | 판독 누적본(커밋). 항목에 `reader:"claude-sonnet-5"` 태그 있으면 자동판독분 |
| `daily_collect.py` | `prepare`(다운로드)/`finalize`(병합+빌드) |
| `seongji_build.py` | SQLite → seongji_data.js. kakaoStores/kakaoChanges(전일대비 리베이트) 산출 |
| `kakao_history.py` | 전일 대비 리베이트 변화 감지(롤링 히스토리). 미표기 가입유형=MNP 정규화 |
| `app.py` | Flask 서빙 + 스케줄러 + git 미러 모드(`git_data_sync.py`, GIT_RAW_BASE 설정시) |
| `index.html` | 대시보드(단일 HTML). 3탭(공시/성지폰/카카오) + AI 요약 리포트. COND_RE=조건부 필터 |
| `deploy/onprem/` | 사내망 배포(build_from_pg.py, PostgreSQL) |
| `.github/workflows/daily-vision.yml` | ⭐ 일일 무인 파이프라인 |

---

## 4. 지금까지 한 최근 작업 (판독 정확도 개선) — 상세

사용자 피드백: "성지폰 단가가 실제 이미지와 안 맞는다."
→ **실이미지 5장 다운로드해 직접 Read로 판독 → 저장값과 셀 단위 대조(감사)** 수행.

**진단된 원인 2가지 + 수정:**
1. **초대형 이미지 자동축소** — 세로 6,137px 시세표를 API가 2,576px로 축소 → "현금가/적용가" 행 라벨 뭉개져 값 혼입.
   → `_image_blocks()`가 2,300px 초과 시 겹침 150px로 **원본 해상도 분할 전송**.
2. **낡은 데이터가 오늘 시세로 표시** — 6월 판독분 ~6,900행이 매일 재스탬프. posted_at도 신선도 기준 안 됨(매장이 옛 게시글 이미지만 교체, 2025년 게시글에 2026 시세).
   → vision_load가 **`reader` 태그된 자동판독분만 적재**(레거시 제외). 결과: 라이브 7,817→1,345행(전부 당일).

**사용자 확정 정책(질문으로 정리):**
- 신선도: 최근분(자동판독분)만 표시
- 조건부 적용가(페스티벌 등): **수집하되 add_condition 표기**(기본뷰 제외, '조건부' 필터로 조회)
- 수집범위: 휴대폰만(저가·키즈폰 포함, 워치/태블릿/유심 제외)
- 재판독: 기존 판독분 새 프롬프트로 정정 완료

**감사 결과(셀 정확도):** 표준 그리드 ≈99~100%(굳폰 42/42, 따르릉 102/103).
2줄(현금가/적용가) 레이아웃이 최난관 — 값은 정확하나 일부 매장(일타폰)이 **전 행을 조건부로 오태그**(안전실패: 틀린 값 대신 기본뷰에서 빠짐). 대응으로 프롬프트에 few-shot 예시 추가함(`7cdd245`).

---

## 5. 알려진 함정 / 실측 교훈 (반복 실수 방지)

1. **구조화 출력 스키마**: `"type": ["string","null"]` 유니온 배열 **불가**(400). nullable은 `anyOf: [{...},{"type":"null"}]`.
2. **대형 표 출력**: 빽빽한 시세표는 8K 토큰 초과 → `messages.stream()` + max_tokens 32000 필수(비스트리밍 8K는 잘림).
3. **이미지 해상도**: API가 2,576px로 자동축소 → 큰 이미지는 `_image_blocks` 분할 필수.
4. **import 순서**: 모듈 상단 상수에서 `re`/`os` 쓰기 전에 import 되어 있어야(과거 NameError로 CI 3단계 크래시).
5. **봇 커밋 배포**: `GITHUB_TOKEN`으로 push한 커밋은 다른 워크플로를 트리거 못함(재귀방지) → `gh workflow run`으로 직접 dispatch(actions:write 권한).
6. **로컬 배포 금지**: colima+containerd가 lightsailctl과 비호환. 배포는 CI로만.
7. **프로덕션 배포 auto-mode 차단**: (참고) 과거 로컬 deploy.sh는 "배포 진행해" 명시 필요했음. 지금은 CI라 무관.
8. **API 실패 ≠ 채널 잘못**: vision_api_reader는 호출 실패를 skiplist에 반영 안 함(오염 방지 설계).
9. **git 미러 모드 미설정**: Lightsail엔 GIT_RAW_BASE 없음 → 데이터는 배포로만 반영(그래서 daily-vision이 배포까지 함).

---

## 6. 다음 할 일 / 미해결

- [ ] **(내일 아침) 일타폰 2줄 레이아웃 재검증** — few-shot 규칙 후 04:40 정기 실행 결과에서 "휴대폰성지 일타폰 목동점"(image_url에 `hJe4S`)의 S26 256G 현금가가 SK23/KT8/LG-8(만원)로, 적용가만 add_condition 붙는지 셀 대조.
- [ ] (선택) **네이버 키 시크릿 등록** → 네이버 수집 복원.
- [ ] (선택) **로컬 5am 스케줄 태스크 정리** — CI(04:40)와 중복. 비활성화하거나 주1회 품질검수로 전환 권장.
- [ ] (선택) **비용 모니터링** — console.anthropic.com Usage. Sonnet 실판독이 예상보다 커서 월 $25~40 가능. (오늘 감사에 재판독 여러 번 = 일회성 ~$10)
- [ ] (선택) **git 미러 모드 전환** — GIT_RAW_BASE/GIT_SYNC_TOKEN 등록 시 데이터 갱신을 배포 없이. deploy-lightsail.yml의 `[data-only]` skip 가드 복원 가능(주석에 방법 있음).

---

## 7. 재개 시 첫 명령 (다른 컴퓨터에서)

```bash
cd "<repo>/온라인 단가비교"
git pull origin main                    # 최신 동기화 (3afeb7c 이상)
gh auth status                          # gh CLI 로그인 확인 (워크플로 조작에 필요)
git remote -v                           # gitlab 토큰 URL 유효한지(만료 시 재발급 필요)

# 최근 자동 실행 상태
gh run list --workflow=daily-vision.yml --limit 3
gh run list --workflow=deploy-lightsail.yml --limit 3

# 라이브 데이터 요약
curl -s "https://seongji-price-monitor.62h5ewkf735c4.ap-northeast-2.cs.amazonlightsail.com/seongji_data.js?cb=$(date +%s)" \
 | python3 -c "import json,sys; d=json.loads(sys.stdin.read().split('window.SEONGJI_DATA = ',1)[1].rstrip().rstrip(';')); print('kakao', d['kakaoSummary']['rows'],'행/',d['kakaoSummary']['stores'],'매장, gen', d['generatedAt'])"

# 판독 감사(수동): 이미지 URL은 seongji_vision_data.json의 image_url.
#   curl로 받아 Read 도구로 직접 보고, 저장 행(reader="claude-sonnet-5")과 셀 대조.
# 판독 워크플로 수동 실행: gh workflow run daily-vision.yml
```

**환경 준비물**: Python 3.12, `pip install anthropic requests pillow`(로컬 테스트 시), `gh` CLI 로그인,
`ANTHROPIC_API_KEY`(로컬에서 vision_api_reader 돌리려면), AWS 자격증명(로컬 배포는 어차피 깨졌으니 CI 사용).
로컬 vision 테스트용 venv: `/private/tmp/...scratchpad/venv`(세션별 — 새 머신에선 재생성).

---

## 8. 메모리 참조
사용자 auto-memory에 `online-price-compare.md`가 있음(이 프로젝트 전체 이력·함정 요약). 세션 시작 시 자동 로드됨.
