# 사내망 배포 — 화면(GitLab) + DB(사내 PostgreSQL) 연결

수집은 외부망(Claude)에서 수행하고, 사내망은 **화면 코드(GitLab)** 와 **데이터(PostgreSQL)** 를
각각 받아 연결해 서비스한다. 화면 컨테이너가 기동·매일 05:00 KST 에 사내 PostgreSQL 을 읽어
`seongji_data.js` / `subsidy_data.js` 를 생성하고 `index.html` 을 서빙한다. (인터넷 불필요)

```
[외부망 — Claude]                         [사내망 — 폐쇄]
수집 → SQLite → export_pack.py            ① DB 적재:  psql -f *.sql  (price-pack 반입)
   → price-pack-YYYYMMDD.tar.gz ─반입─▶   ② 화면 배포: git clone(GitLab) → docker build/run
                                              화면 컨테이너 ── DATA_SOURCE=postgres ──▶ 사내 PG 읽어 빌드·서빙
```

## 1) DB 적재 (한 번 / 매 갱신)
price-pack 의 SQL 을 사내 PostgreSQL 에 적재. 멱등(같은 날짜 재적재 시 그 날짜만 교체).
```bash
psql "$DATABASE_URL" -v ON_ERROR_STOP=1 -f seongji_pg.sql
psql "$DATABASE_URL" -v ON_ERROR_STOP=1 -f subsidy_pg.sql
```
조회용 계정(읽기 전용) 하나만 화면 컨테이너에 주면 된다.

## 2) 화면 배포 (GitLab 코드)
```bash
git clone <GitLab-repo> price-onprem && cd price-onprem
cp deploy/onprem/.env.example deploy/onprem/.env   # 사내 DB 접속정보 입력
docker build -f deploy/onprem/Dockerfile -t price-onprem .
docker run -d --name price -p 8080:8080 --env-file deploy/onprem/.env price-onprem
# → http://<host>:8080/  ·  GET /health == 200
```
- 기동 직후 1회 + 매일 05:00 KST 자동으로 사내 PG → 화면 데이터 재빌드.
- DB 가 갱신되면 컨테이너 재시작 없이 다음날 05:00 에 반영(즉시 원하면 컨테이너 재시작).

## 핵심 연결 메커니즘
- `build_from_pg.py` 가 환경변수(`DB_HOST/DB_PORT/DB_USER/DB_PASSWORD/DB_NAME`)로 PG 접속 →
  `seongji_build.py`/`subsidy_build.py` 와 **동일 스키마**의 `*_data.js` 생성(검증: 카운트 완전 일치).
- 화면(`index.html`)은 그 JS 만 읽음 → DB 직접 쿼리·API 서버 불필요(폐쇄망에 강함).
- 폰트·xlsx 는 `vendor/` 로컬 번들 → 외부 CDN 의존 0.

## 대안 (선택)
- **항상 최신**이 필요하면: 화면 컨테이너의 `REFRESH_HOUR` 외에 `docker restart` 로 즉시 빌드,
  또는 `build_from_pg.py` 를 사내 cron 으로 더 자주 실행.
- **API 방식**으로 가려면: `build_from_pg` 의 쿼리를 그대로 Flask 엔드포인트로 노출해 프론트가
  fetch 하도록 전환 가능(현재는 정적 빌드 방식이 가장 단순·견고).
