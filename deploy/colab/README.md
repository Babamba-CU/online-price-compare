# 사내 배포 — Polaris Colab (user-apps 저장소 방식)

이 앱의 사내 화면은 **Polaris Colab** 컨테이너로 띄운다. Colab 은 GitLab
`CDS/orbit/colab/user-apps/<앱이름>` 저장소를 소스로 빌드한다(2026-08 이후 표준 — 포털이 저장소를
자동 생성하며 별도 Git 토큰이 필요 없다. 다른 앱 `storecount`·`voice-report` 가 이 방식).
코드 저장소 `MAMF/online-price` 는 GitHub 의 사내 미러이자 **데이터 원천**(git 미러 모드가 여기서 화면 파일을 받아간다).

```
[외부] GitHub main ──push──▶ GitLab MAMF/online-price (미러·데이터 원천, 매일 05:05 루틴이 갱신)
                                     │  deploy_colab.sh (슬림 트리, author taeholee@sk.com)
                                     ▼
                     GitLab CDS/orbit/colab/user-apps/<앱이름> ──포털 '배포'──▶ Colab 컨테이너
                                                                         https://<앱이름>.colab-mydesk.sktelecom.com
                     컨테이너는 실행 중 GIT_RAW_BASE(MAMF/online-price API) 에서 index.html·seongji_data.js 를
                     60분마다 받아 서빙 → 데이터 갱신에 재배포 불필요
```

## 1) 최초 1회 — 포털에서 앱 생성 (사내 PC)
포털 `https://polaris-colab.sktelecom.com` (이 맥에서는 접속 차단 — 사내 Windows PC 에서)
1. 새 앱 생성. 이름 예: `online-price` → 저장소 `https://gitlab.tde.sktelecom.com/CDS/orbit/colab/user-apps/online-price.git` 자동 생성.
2. 앱 환경변수 등록 (권장: **git 미러 모드** — DB 없이 동작, 데이터는 GitLab 커밋만으로 최신화)

| 변수 | 값 | 비고 |
|---|---|---|
| `GIT_RAW_BASE` | `https://gitlab.tde.sktelecom.com/api/v4/projects/36013/repository/files` | 사내 GitLab 은 SSO 라 웹 raw 경로는 302 → 반드시 API 경로 |
| `GIT_SYNC_TOKEN` | `MAMF/online-price` 프로젝트 액세스 토큰(Settings → Access Tokens, 역할 Reporter, 스코프 `read_api`) | 개인 토큰 대신 프로젝트 토큰 권장(계정 차단 무관) |
| `GIT_SYNC_MINUTES` | `60` | 폴링 주기 |
| `TZ` | `Asia/Seoul` | Dockerfile 기본값과 동일 |

   PG 모드로 쓰려면 대신 `DATA_SOURCE=postgres` + `DB_HOST/DB_PORT/DB_USER/DB_PASSWORD/DB_NAME` (price-pack 반입 필요, `deploy/onprem/README.md`).
3. 포트/헬스: 컨테이너는 `0.0.0.0:8080`, `GET /health` 200 (루트 `Dockerfile`·`app.py` 준수).

## 2) 코드 올리기 — 이 맥에서
```bash
cd "/Users/1108526/Documents/대시보드/온라인 단가비교"
bash deploy_colab.sh --repo https://gitlab.tde.sktelecom.com/CDS/orbit/colab/user-apps/online-price.git   # 최초 1회
bash deploy_colab.sh "메시지"        # 이후 코드가 바뀔 때마다
bash deploy_colab.sh --dry-run       # push 없이 트리·크기 확인
```
스크립트가 하는 일: main 트리에서 컨테이너에 불필요한 `seongji_vision_data.json`(11MiB, 10MiB 규칙 위반)·`.github`·`dist` 를 뺀
배포 커밋을 만들고(author `taeholee@sk.com` 고정 — 이 그룹은 팀 멤버 이메일만 허용), 로컬 브랜치 `colab-deploy` 에 쌓아
원격 `main` 으로 push 한다. 인증은 `MAMF/online-price` 와 같은 GitLab 계정 토큰(git credential 또는 URL 내장)을 쓴다.

## 3) 배포 실행 — 포털에서 '배포' 버튼
push 만으로는 배포되지 않는다. 포털에서 앱의 **배포** 를 누르면 `cicd-builder` 파이프라인(prepare → build → deploy → cleanup, 약 2분)이
돌고 `https://<앱이름>.colab-mydesk.sktelecom.com` 에 반영된다. 파이프라인 상태는 GitLab `CDS/orbit/colab/cicd-builder` 에서 확인.

## 4) 이후 운영
- **데이터**: 매일 05:05 KST 클라우드 루틴이 GitHub → GitLab(MAMF/online-price) 에 커밋 → 컨테이너가 60분 내 자동 반영. 재배포 불필요.
- **코드**(index.html 외 파이썬·화면 로직): `deploy_colab.sh` + 포털 '배포'. index.html 만 바뀐 경우는 git 미러가 받아가므로 재배포 없이도 반영된다.
- 확인: `GET /health` 200, 화면 하단 "최종 빌드" 날짜.

## 대안(구방식) — 포털이 MAMF/online-price 를 직접 빌드
포털 앱에 Git 저장소 `MAMF/online-price` + `read_repository` 토큰을 등록하면 별도 저장소 없이 배포된다.
단, 토큰 만료 시 빌드가 `HTTP Basic: Access denied` 로 실패하고 재시도로는 복구되지 않아(포털 저장 변수 재사용) 권장하지 않는다.

## (참고) 이전 설계 — 스케줄 파이프라인
루트 `.gitlab-ci.yml` 의 `deploy_colab` 잡은 스케줄 실행 + `COLAB_DEPLOY_CONFIGURED=1` 일 때만 도는 자리표시자다.
포털 방식에서는 필요 없으며, 프로젝트에 Runner 도 없어 실제로 돌지 않는다.
