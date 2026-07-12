# Lightsail 자동 배포 (항상 최신)

`main` 에 앱 변경이 푸시되면 GitHub Actions가 자동으로 Lightsail에 배포한다.
워크플로: [`.github/workflows/deploy-lightsail.yml`](../../.github/workflows/deploy-lightsail.yml)

## 왜 CI 배포인가
로컬 `deploy.sh` 는 Docker 29 + colima의 **containerd 이미지 스토어**와 `lightsailctl` 이
호환되지 않아, 새 이미지를 못 올리고 옛 이미지로 폴백한다(2026-07-08 확인).
GitHub 러너(ubuntu, amd64, 클래식 docker 스토어)에서 빌드·푸시하면 이 문제가 없다.
→ **로컬에서 배포하지 말고 push 하면 CI가 배포**하도록 일원화.

## 한 번만 설정 — GitHub Secrets 등록
GitHub 저장소 → **Settings → Secrets and variables → Actions → New repository secret**

| Secret | 값 | 필수 |
|---|---|---|
| `AWS_ACCESS_KEY_ID` | Lightsail 권한 IAM 액세스 키 | ✅ |
| `AWS_SECRET_ACCESS_KEY` | 위 키의 시크릿 | ✅ |
| `NAVER_CLIENT_ID` | 네이버 검색 API 키 | 선택(없으면 네이버 수집 skip) |
| `NAVER_CLIENT_SECRET` | 네이버 검색 API 시크릿 | 선택 |
| `GIT_RAW_BASE` | git 미러 모드 raw URL (예: `https://raw.githubusercontent.com/Babamba-CU/online-price-compare/main`) | 선택 — 설정 시 **데이터·화면은 커밋만으로 배포 없이 최신화** |
| `GIT_SYNC_TOKEN` | 위 저장소 읽기 토큰(비공개 repo 시 — GitHub fine-grained PAT, contents:read) | 선택 |
| `ANTHROPIC_API_KEY` | Claude API 키 (console.anthropic.com) — **daily-vision.yml**이 카카오 시세표 이미지를 무인 판독하는 데 사용 (Sonnet 5 기본 — 사용자 확정, 월 ~$25 수준) | 선택 — 없으면 이미지 판독 skip, 텍스트 수집만 |

### git 미러 모드 (배포 빈도 최소화)
`GIT_RAW_BASE` 를 설정하면 컨테이너가 60분(기본)마다 저장소의 `index.html`/`*_data.js` 를
직접 받아 `/tmp` 에서 우선 서빙한다 → **데이터/화면 변경은 커밋만으로 반영, 재배포는
파이썬 코드 변경 때만** 필요. Lightsail 은 사내 GitLab 에 접근 불가하므로 GitHub raw 를 쓴다.

- IAM 사용자에 최소 권한: `lightsail:GetContainerServices`, `lightsail:CreateContainerService`,
  `lightsail:RegisterContainerImage`, `lightsail:CreateContainerServiceDeployment`
  (간단히는 `AmazonLightsailFullAccess` 관리형 정책).
- NAVER 키를 넣지 않으면 배포는 되고 네이버 수집만 건너뛴다(경고만 표시).

## 동작
- **트리거**: `main` 에 `*.py`/`*.js`/`index.html`/`Dockerfile`/`vendor/**`/`deploy/lightsail/**` 등
  앱 파일이 푸시될 때 + 수동 실행(`workflow_dispatch`).
- **단계**: 자격증명 확인 → lightsailctl 설치 → 이미지 빌드(amd64) → 푸시 → 배포 명세 생성(이미지 참조 +
  NAVER 키 주입, 커밋된 `containers.json` 은 `environment: {}` 유지) → 배포 → RUNNING 대기 + `/health` 200 확인.
- **동시성**: 뒤 커밋이 진행 중 배포를 취소(`cancel-in-progress`)해 항상 최신 커밋만 반영.

## 수동 배포
GitHub → **Actions → Deploy to Lightsail → Run workflow**.

## 확인
Actions 로그 마지막 단계에 `배포 완료: https://…amazonlightsail.com` 과 `/health → 200` 출력.
라이브: https://seongji-price-monitor.62h5ewkf735c4.ap-northeast-2.cs.amazonlightsail.com/
