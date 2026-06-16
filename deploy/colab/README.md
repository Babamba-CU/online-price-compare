# Polaris Colab 자동 배포 — 매일 정기 1회

GitLab(`MAMF/online-price`)에 올라간 코드를 Polaris Colab에 **매일 1회 자동 배포**하기 위한 설정.
파이프라인 정의는 리포지토리 루트의 [`.gitlab-ci.yml`](../../.gitlab-ci.yml).

---

## 0) 먼저 알아둘 것 — "매일 배포"가 꼭 필요한가?

데이터와 코드는 분리되어 있습니다.

| 구분 | 갱신 방식 | 재배포 필요? |
|---|---|---|
| **데이터**(`*_data.js`) | 화면 컨테이너 스케줄러가 **매일 05:00 KST** 사내 PG를 다시 읽어 재생성 | ❌ 불필요(이미 자동) |
| **코드**(화면/로직) | GitLab에 새 커밋이 올라갈 때만 변경 | ✅ 그때만 의미 있음 |

→ **데이터 신선도 목적이면 매일 재배포는 불필요**합니다(컨테이너가 알아서 갱신).
매일 정기 배포가 의미 있는 경우는: ① 코드가 매일 바뀌거나 ② 컨테이너를 매일 새로 띄워
강제로 최신 코드+즉시 PG 재빌드를 보장하고 싶을 때입니다.

---

## 1) 매일 스케줄 등록 (GitLab UI)

GitLab 프로젝트 → **Build → Pipeline schedules → New schedule**

- **Interval pattern (cron):** `0 5 * * *`  (매일 05:00. 컨테이너 데이터 갱신과 맞추려면 `10 5 * * *` 권장)
- **Cron timezone:** `Asia/Seoul`
- **Target branch:** `main`
- **Activated:** 체크

저장하면 매일 그 시각에 파이프라인이 돌고, `.gitlab-ci.yml`의 `deploy_colab` 잡이 실행됩니다.
(일반 push로는 실행되지 않음 — `rules: $CI_PIPELINE_SOURCE == "schedule"`)

> 사내 GitLab에 **Runner**가 등록되어 있어야 잡이 실제로 돕니다. (Settings → CI/CD → Runners 에서 확인)

---

## 2) Colab 트리거 방식 확인 → 배포 명령 채우기

Colab 콘솔/문서에서 아래 중 무엇을 지원하는지 확인한 뒤 `.gitlab-ci.yml`의 해당 블록을 채웁니다.

### [A] Colab ↔ GitLab 자동연동(GitOps)
Colab 앱 설정에 "GitLab 저장소 연결 + auto-deploy on push" 옵션이 있는 경우.
- 이 방식이면 **CI 파일 없이도 푸시마다 자동 배포**됩니다 → `.gitlab-ci.yml`은 선택사항.
- "매일 강제 재배포"만 원하면 Colab의 재배포 API를 [B]처럼 호출하도록 둡니다.

### [B] Colab 배포 API / CLI  *(가장 일반적, 권장 확인 순위 1위)*
Colab이 배포 트리거용 토큰·엔드포인트나 CLI를 제공하는 경우.
1. GitLab → **Settings → CI/CD → Variables** 에 등록(Masked, Protected 권장):
   - `COLAB_DEPLOY_URL` — 재배포 트리거 엔드포인트
   - `COLAB_TOKEN` — 배포 토큰
2. `.gitlab-ci.yml`의 `[B]` curl 블록 주석 해제.

### [C] 사내 컨테이너 레지스트리 경유
CI가 이미지를 빌드해 사내 레지스트리에 push하고 Colab이 pull하는 경우.
1. CI/CD Variables: `REGISTRY`, `REGISTRY_USER`, `REGISTRY_PASSWORD`
2. `.gitlab-ci.yml`의 `[C]` docker 블록 주석 해제(+ 필요 시 Colab pull/redeploy 설정).

---

## 3) 가드 해제

위 [A]/[B]/[C] 중 하나를 채운 뒤, CI/CD Variable 에 **`COLAB_DEPLOY_CONFIGURED = 1`** 을 추가하세요.
이 값이 없으면 잡이 일부러 실패하며 "아직 미설정" 안내를 출력합니다(빈 배포 방지).

---

## 빌드 정합성 (Colab 빌드 시)
- Colab은 컨테이너를 `0.0.0.0:8080`에서 띄우고 `GET /health`가 200이어야 함(루트 `CLAUDE.md` 규칙).
- DB 접속은 환경변수(`DB_HOST/DB_PORT/DB_USER/DB_PASSWORD/DB_NAME`) — Colab 앱 환경변수로 주입.
- 사내망 빌드 경로는 [`deploy/onprem/`](../onprem/README.md)의 Dockerfile/README 참고(`DATA_SOURCE=postgres`).
