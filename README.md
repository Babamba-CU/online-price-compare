<<<<<<< HEAD
# 성지폰 + 공시지원금 대시보드

`3사 경쟁현황.html` 과 동일한 톤·앤·매너로 만든 **두 개의 독립 대시보드**.

| 대시보드 | 데이터 | 갱신 |
|---|---|---|
| **성지폰 단가현황.html** | 온라인 성지폰 사이트의 현금완납가 (휴리스틱 파싱) | `run_daily.sh` |
| **공시지원금_3사비교.html** | SKT/KT/LGU+ 공식 사이트의 공시지원금 (24개월·디폴트 요금제) | `run_subsidy_daily.sh` · Claude Routine `subsidy-daily-crawl-3carriers` |

```
성지폰/
├── 성지폰 단가현황.html        ← [대시보드 1] 박스플롯 (단말 × 3사 분포)
├── 공시지원금_3사비교.html      ← [대시보드 2] 그룹 바차트 + 단말묶음 표
│
├── seongji_data.js / .db        ← 대시보드 1 데이터
├── subsidy_data.js / .db        ← 대시보드 2 데이터
│
├── schema.sql                   ← 성지폰 SQLite/Supabase DDL
├── subsidy_schema.sql           ← 공시지원금 SQLite/Supabase DDL
│
├── seongji_db.py / seongji_parser.py / seongji_crawler.py / seongji_build.py
├── seongji_supabase_sync.py     ← (옵션) Supabase 동기화 (성지폰)
├── seed_sample.py               ← 샘플 시드 (성지폰)
│
├── subsidy_db.py                ← 공시지원금 DB 헬퍼
├── subsidy_crawler.py           ← 3사 공식 사이트 Playwright 크롤러
├── subsidy_build.py             ← SQLite → subsidy_data.js
├── subsidy_supabase_sync.py     ← (옵션) Supabase 동기화 (공시지원금)
├── subsidy_seed.py              ← 샘플 시드 (공시지원금 30종)
│
├── run_daily.sh                 ← 성지폰 일단위 파이프라인
├── run_subsidy_daily.sh         ← 공시지원금 일단위 파이프라인
├── crontab.example              ← cron 예시
├── requirements.txt
└── README.md
```

## 데이터 소스

| 코드          | 사이트                       | 비고                                  |
| ------------- | ---------------------------- | ------------------------------------- |
| `ppomppu`     | 뽐뿌 휴대폰 게시판           | 가장 활성도 높은 성지가 공유 게시판   |
| `algosa`      | algosa.kr                    | 단가표 정형 사이트                    |
| `ppasak`      | ppasak.com                   | 온라인 휴대폰 가격 비교               |
| `sajangnim`   | sajangnim.com                | 도매/판매상 가격표                    |
| `moyoplan`    | moyoplan.com                 | 모요 — SPA, 메타만 베스트 에포트      |
| `modusj`      | modusj.com                   | 구조화된 상품 카드 페이지             |

크롤러는 `requests + BeautifulSoup` 기반의 휴리스틱 파서이고, 각 사이트의 HTML
변경에 취약합니다. `seongji_crawler.py` 의 어댑터 함수 6개만 손보면 됩니다.

## 빠른 시작

### 1) 의존성

```bash
cd "/Users/taeholee/Documents/대시보드/성지폰"
python3 -m pip install -r requirements.txt
```

### 2) 샘플 데이터로 대시보드 미리보기

```bash
python3 seed_sample.py        # 14일치 가짜 가격 시드
python3 seongji_build.py      # SQLite → seongji_data.js
open "성지폰 단가현황.html"
```

### 3) 실제 크롤

```bash
python3 seongji_crawler.py --sources ppomppu algosa --max-pages 2
python3 seongji_build.py
```

옵션:
- `--sources` : 일부 사이트만 (`ppomppu algosa ppasak sajangnim moyoplan modusj`)
- `--max-pages` : 사이트별 리스트 페이지 깊이 (기본 2)
- `--fetch-bodies` : 본문까지 받아 더 정밀하게 파싱 (느림)

### 4) cron 등록

```bash
crontab crontab.example   # 또는 crontab -e 후 복사
# 30 4 * * * /Users/.../성지폰/run_daily.sh >> .../logs/cron.log 2>&1
```

## DB 스키마

`schema.sql` 한 파일로 **SQLite 와 Supabase(PostgreSQL) 양쪽**을 커버합니다.

| 테이블                 | 용도                                                              |
| ---------------------- | ----------------------------------------------------------------- |
| `seongji_posts`        | 원본 게시글 (raw text 포함)                                       |
| `seongji_prices`       | 파싱된 가격 (post 1개에 N개 가능)                                 |
| `seongji_daily_stats`  | 일별 min / median / avg 머터리얼 — 대시보드 라인차트 소스         |
| `seongji_crawl_runs`   | 크롤 실행 로그 (성공/실패/건수)                                   |

```
upsert key:
  seongji_posts:       (source, url)
  seongji_prices:      (post_id, snapshot_date, model_name, carrier, subscription_type)
  seongji_daily_stats: (snapshot_date, model_name, carrier, subscription_type)
```

## Supabase 연동

1. Supabase 프로젝트에서 SQL Editor 로 `schema.sql` 실행
   - Postgres 환경에선 `INTEGER PRIMARY KEY AUTOINCREMENT` → `BIGSERIAL PRIMARY KEY` 로 치환 필요
   - 파일 하단의 RLS / `CREATE POLICY` 주석 해제

2. 환경변수 셋업

   ```bash
   export SUPABASE_URL="https://xxxx.supabase.co"
   export SUPABASE_SERVICE_ROLE_KEY="eyJ..."
   ```

3. 동기화 실행

   ```bash
   python3 seongji_supabase_sync.py --since 2026-05-01
   ```

4. (옵션) 대시보드를 Supabase 직접 호출로 바꾸기
   - 현재는 정적 `seongji_data.js` 를 읽는 구조
   - HTML 의 `<script src="seongji_data.js">` 부분을
     `https://cdn.jsdelivr.net/npm/@supabase/supabase-js@2/+esm` 로딩 + 쿼리로 교체

## 대시보드 기능

- **KPI 카드** : 통신사별 최저가 (모델 + 출처 사이트 표시)
- **라인차트** : 통신사별 일자별 최저가 추이 (현금완납가, 만원 단위)
- **상세 테이블** : 최신일자 사이트별 단가 / 신뢰도 / 출처 게시글 링크
- **필터** : 기준일 / 최근 일수 / 통신사 / 가입유형 / 약정유형 / 최소 신뢰도 / 단말기 다중선택

## 신뢰도(`confidence`) 점수

파서는 게시글 텍스트에서 통신사·가입유형·현금가가 얼마나 명확히 추출됐는지에 따라
0.3 ~ 0.95 점수를 부여합니다. 기본 필터는 0.5 이상만 노출합니다.

| 조건                       | 가중치 |
| -------------------------- | ------ |
| 통신사 + 가입유형 모두 검출 | +0.2   |
| 현금완납가 검출             | +0.3   |
| 약정유형 검출               | +0.1   |
| (베이스)                   | 0.3    |

## 운영 팁

- **사이트별 robots.txt 존중 / 요청 간 sleep 1.2s** 가 기본
- 사이트 HTML 이 바뀌면 `seongji_crawler.py` 의 어댑터 함수만 손보면 됩니다
- SPA(모요 등)는 정적 추출 한계가 있어 정확도 향상이 필요하면 `playwright` 어댑터 추가
- 30일치 데이터 = 약 2~5 MB 수준이라 SQLite 로 충분, Supabase 는 다중 디바이스 공유용


---

# 공시지원금 3사 비교 대시보드

`공시지원금_3사비교.html` — SKT, KT, LGU+ 공식 사이트에서 일단위로 공시지원금을 수집해
**단말별 3사를 나란히 비교**하는 대시보드. 약정 24개월·요금제는 각 사이트 디폴트 기준.

## 데이터 소스 (사용자 확인 스크린샷 기준)

| 통신사 | URL | 표 컬럼 | DB 매핑 |
|---|---|---|---|
| **SKT**  | https://shop.tworld.co.kr/wireless/product/subsidy/main | 단말 / 256G / 공시일자 / 출고가 / **공통지원금** / **추가지원금** / 구매가 / 선약비교 | `공통지원금` → `subsidy_public`, `추가지원금` → `subsidy_additional` |
| **KT**   | https://shop.kt.com/smart/supportAmtList.do | 단말기 / 펫네임 / **모델명 (SM-Sxxx)** / ①출고가 / **②공시지원금** / **③추가지원금** / 판매가(①-②-③) / 공시일자 | `②공시지원금` → `subsidy_public`, `③추가지원금` → `subsidy_additional` |
| **LGU+** | https://www.lguplus.com/mobile/financing-model | 기기명/모델명 / 출고가(A) / 공시일자 / 요금제유지기간 / **이통사지원금** / **유통망지원금** / 지원금총액(B) / 추천할인 / 구매가(A-B) | `이통사지원금` → `subsidy_public`, `유통망지원금` → `subsidy_additional` ※ `24개월 유지` 행만 채택 |

### 가입유형별 크롤링 방식 (3패스)

3사 모두 동일 시그니처: `crawl_xxx(playwright, sub_type)`. `run()` 에서:

```
for carrier in [SKT, KT, LGU+]:
  if carrier == "KT":
    offers = crawl_kt()  # 1회 호출 — KT 사이트는 가입유형 미구분
    → 결과를 010신규/MNP/기변 3유형에 동일값 복제 적재
  else:
    for sub_type in [010신규, MNP, 기변]:
      offers = crawl_xxx(sub_type)  # 페이지의 가입유형 셀렉터 토글 후 크롤
      → 각각 적재
```

| 통신사 | 가입유형 셀렉터 | 폴백 체인 |
|---|---|---|
| SKT  | 커스텀 드롭다운 | `SKT_SUB_TRIGGER_SELECTORS` (aria/dropdown-toggle/has-text 6종) + `skt_option_selectors(label)` (role=option/menuitem/button 6종) |
| LGU+ | radio 버튼      | `lgu_radio_selectors(label)` (`input[value]`, `label:has-text`, `[role='radio']` 등 5종) |
| KT   | 셀렉터 없음     | 셀렉터 인터랙션 없음, 단일 크롤 후 복제 |

**진단 로직**: SKT/LGU+ 가 3 sub_type 으로 크롤했을 때 결과 시그니처가 모두 동일하면 "selector 토글 실패 의심" 경고를 로그에 남긴다. Routine 의 SQL 검증 #4 가 이 케이스를 잡아낸다.

### 모델 식별 (`model_codes.py`)

3사 모두 삼성 모델 코드의 4자리 prefix (예: `S942`=Galaxy S26, `S947`=S26+, `S948`=S26 Ultra, `F761`=Z Flip 7 FE) 로 정규화합니다. 용량은 코드 끝의 `512` / `256` 또는 별도 컬럼에서 추출.

| 모델코드 | 정규화 |
|---|---|
| `SM-S942NK`     (KT 256GB)  | `("Galaxy S26", 256)` |
| `SM-S942NK512`  (KT 512GB)  | `("Galaxy S26", 512)` |
| `SM-S942N256`   (LG 256GB)  | `("Galaxy S26", 256)` |
| `SM-S942N512`   (LG 512GB)  | `("Galaxy S26", 512)` |

### Galaxy S26 256GB 검증 정답 (스크린샷 기준, 2026-05 시점)

| 통신사 | 출고가 | 공통지원금 | 추가지원금 | 구매가격 |
|---|---:|---:|---:|---:|
| SKT  | 1,254,000 | 580,000 | 87,000  | 587,000 |
| KT   | 1,254,000 | 600,000 |       0 | 654,000 |
| LGU+ | 1,254,000 | 700,000 | 105,000 | 449,000 |

크롤러 결과가 이 값에서 ±5% 이상 벗어나면 페이지 구조 변경 의심 → 어댑터 점검 필요.

세 사이트 모두 동적 렌더링/봇 차단이 있어 **Playwright** 가 필수입니다.
```bash
pip install playwright
playwright install chromium
```

## 빠른 시작

```bash
# 1) 30종 단말 샘플로 대시보드 동작 확인
python3 subsidy_seed.py
python3 subsidy_build.py
open "공시지원금_3사비교.html"

# 2) 실제 크롤
python3 subsidy_crawler.py --carriers SKT KT LGU+
python3 subsidy_build.py
```

## DB 스키마 (`subsidy_schema.sql`)

| 테이블 | 용도 |
|---|---|
| `subsidy_devices` | 단말 마스터 (모델 정규화 / 출시일 / 제조사) |
| `subsidy_offers`  | 일별 공시지원금 스냅샷 (3사 × 단말 × 일자) |
| `subsidy_changes` | 전일 대비 가격/지원금 변동 이력 |
| `subsidy_crawl_runs` | 크롤 실행 로그 |

업서트 키: `(snapshot_date, carrier, model_name, storage_gb)`. 동일 키 재실행시 UPDATE.

**계산 필드 (자동)**:
- `subsidy_total      = subsidy_public + subsidy_additional`
- `net_buy_price      = retail_price - subsidy_total`
- `monthly_device_fee = net_buy_price / 24`
- `select_discount_24mo = plan_monthly_fee × 24 × 25%`

## 대시보드 기능

- **그룹 바차트**: 단말별 SKT/KT/LGU+ 막대 — 짙은색 = 공시지원금, 연한색 = 추가지원금
- **상세 표**: 출시일 최신순으로 정렬, 같은 단말 3행 묶음. 3사 중 최대 공시지원금에 노란 셀 강조
- **KPI**: 통신사별 최고 공시지원금 단말 + 최근 7일 변동 건수
- **필터**: 기준일 / 제조사 / 정렬 (최신·총지원금·실구매가·출고가) / 표시 단말 수

## Supabase 연동

```bash
# 1) Supabase SQL Editor 에서 subsidy_schema.sql 실행
#    - INTEGER PRIMARY KEY AUTOINCREMENT → BIGSERIAL PRIMARY KEY 로 치환
#    - 파일 하단 RLS / CREATE POLICY 주석 해제

# 2) 환경변수
export SUPABASE_URL="https://xxxx.supabase.co"
export SUPABASE_SERVICE_ROLE_KEY="eyJ..."

# 3) 첫 동기화는 충분히 과거부터
python3 subsidy_supabase_sync.py --since 2026-01-01
# 이후 매일은 --since 전일 (default)
```

## Claude Routine 자동 실행

매일 **07:00 KST** 에 `run_subsidy_daily.sh` 가 자동 실행되도록 Claude Routine 에 등록됨.

- 태스크 ID: **`subsidy-daily-crawl-3carriers`**
- 스크립트: `성지폰/run_subsidy_daily.sh`
  1. `subsidy_crawler.py --carriers SKT KT LGU+` (Playwright, 가입유형 3패스)
  2. `subsidy_build.py` (DB → subsidy_data.js)
  3. `subsidy_supabase_sync.py` (env 설정시)
- 로그: `성지폰/logs/subsidy-YYYYMMDD-HHMMSS.log`

수동 실행 / 일시 비활성 / 시간 변경은 Claude 사이드바의 "Scheduled" 섹션에서 가능.
**중요**: Claude 앱이 닫혀 있는 동안의 실행은 다음 실행 시점에 한 번에 처리됨.

### Routine 검증 SQL (수동 확인용)

```sql
-- 적재 분포 — 9 row (3사 × 3유형)
SELECT carrier, subscription_type, COUNT(*) FROM subsidy_offers
  WHERE snapshot_date = date('now','localtime')
  GROUP BY carrier, subscription_type;

-- KT 동일성 검증 — 0 row 가 정상
SELECT model_name, storage_gb,
       COUNT(DISTINCT subsidy_public || '|' || subsidy_additional) AS d
  FROM subsidy_offers
  WHERE carrier='KT' AND snapshot_date = date('now','localtime')
  GROUP BY model_name, storage_gb HAVING d > 1;

-- SKT/LGU+ 토글 효과 검증 — avg_distinct >= 2 가 정상
SELECT carrier, AVG(d) AS avg_distinct FROM (
  SELECT carrier, model_name, storage_gb,
         COUNT(DISTINCT subsidy_public || '|' || subsidy_additional) AS d
    FROM subsidy_offers
    WHERE carrier IN ('SKT','LGU+') AND snapshot_date = date('now','localtime')
    GROUP BY carrier, model_name, storage_gb
) GROUP BY carrier;
```

### 시드 placeholder 안내

시드 단계에서 SKT/LGU+ 의 010신규/기변 값은 MNP 와 동일한 placeholder 로 채워진다.
실제 크롤러가 실행되면 사이트 셀렉터 토글로 진짜 값을 덮어쓴다. 대시보드 메타바에
"⚠ 010신규/기변 placeholder" 배지가 떠 있으면 아직 시드 상태이며, 실제 크롤이
한 번 돌아야 가입유형별 차등 값이 표시된다.

## 운영시 주의

- 세 사이트의 HTML/CSS 구조 변동이 잦아 어댑터 점검이 필요할 수 있음 (`subsidy_crawler.py`)
- SKT 공시지원금은 보통 첨부 PDF 안에 있어 `pdfminer.six` 로 후속 파싱 필요 (현재 stub)
- KT shopmns 는 대리점 ID(`con_shop_id`)별로 표가 다르므로 신뢰할 만한 매장 ID 를 고정 사용
- LGU+ SPA 가 로딩되기 전 추출되지 않도록 `networkidle + wait_for_selector` 활용
- 일별 데이터량은 30종 × 3사 × 365일 ≈ 33,000 행/년 — SQLite 충분, Supabase 무료티어로도 수십년치 가능
=======
# online-price



## Getting started

To make it easy for you to get started with GitLab, here's a list of recommended next steps.

Already a pro? Just edit this README.md and make it your own. Want to make it easy? [Use the template at the bottom](#editing-this-readme)!

## Add your files

* [Create](https://docs.gitlab.com/user/project/repository/web_editor/#create-a-file) or [upload](https://docs.gitlab.com/user/project/repository/web_editor/#upload-a-file) files
* [Add files using the command line](https://docs.gitlab.com/topics/git/add_files/#add-files-to-a-git-repository) or push an existing Git repository with the following command:

```
cd existing_repo
git remote add origin https://gitlab.tde.sktelecom.com/MAMF/online-price.git
git branch -M main
git push -uf origin main
```

## Integrate with your tools

* [Set up project integrations](https://gitlab.tde.sktelecom.com/MAMF/online-price/-/settings/integrations)

## Collaborate with your team

* [Invite team members and collaborators](https://docs.gitlab.com/user/project/members/)
* [Create a new merge request](https://docs.gitlab.com/user/project/merge_requests/creating_merge_requests/)
* [Automatically close issues from merge requests](https://docs.gitlab.com/user/project/issues/managing_issues/#closing-issues-automatically)
* [Enable merge request approvals](https://docs.gitlab.com/user/project/merge_requests/approvals/)
* [Set auto-merge](https://docs.gitlab.com/user/project/merge_requests/auto_merge/)

## Test and Deploy

Use the built-in continuous integration in GitLab.

* [Get started with GitLab CI/CD](https://docs.gitlab.com/ci/quick_start/)
* [Analyze your code for known vulnerabilities with Static Application Security Testing (SAST)](https://docs.gitlab.com/user/application_security/sast/)
* [Deploy to Kubernetes, Amazon EC2, or Amazon ECS using Auto Deploy](https://docs.gitlab.com/topics/autodevops/requirements/)
* [Use pull-based deployments for improved Kubernetes management](https://docs.gitlab.com/user/clusters/agent/)
* [Set up protected environments](https://docs.gitlab.com/ci/environments/protected_environments/)

***

# Editing this README

When you're ready to make this README your own, just edit this file and use the handy template below (or feel free to structure it however you want - this is just a starting point!). Thanks to [makeareadme.com](https://www.makeareadme.com/) for this template.

## Suggestions for a good README

Every project is different, so consider which of these sections apply to yours. The sections used in the template are suggestions for most open source projects. Also keep in mind that while a README can be too long and detailed, too long is better than too short. If you think your README is too long, consider utilizing another form of documentation rather than cutting out information.

## Name
Choose a self-explaining name for your project.

## Description
Let people know what your project can do specifically. Provide context and add a link to any reference visitors might be unfamiliar with. A list of Features or a Background subsection can also be added here. If there are alternatives to your project, this is a good place to list differentiating factors.

## Badges
On some READMEs, you may see small images that convey metadata, such as whether or not all the tests are passing for the project. You can use Shields to add some to your README. Many services also have instructions for adding a badge.

## Visuals
Depending on what you are making, it can be a good idea to include screenshots or even a video (you'll frequently see GIFs rather than actual videos). Tools like ttygif can help, but check out Asciinema for a more sophisticated method.

## Installation
Within a particular ecosystem, there may be a common way of installing things, such as using Yarn, NuGet, or Homebrew. However, consider the possibility that whoever is reading your README is a novice and would like more guidance. Listing specific steps helps remove ambiguity and gets people to using your project as quickly as possible. If it only runs in a specific context like a particular programming language version or operating system or has dependencies that have to be installed manually, also add a Requirements subsection.

## Usage
Use examples liberally, and show the expected output if you can. It's helpful to have inline the smallest example of usage that you can demonstrate, while providing links to more sophisticated examples if they are too long to reasonably include in the README.

## Support
Tell people where they can go to for help. It can be any combination of an issue tracker, a chat room, an email address, etc.

## Roadmap
If you have ideas for releases in the future, it is a good idea to list them in the README.

## Contributing
State if you are open to contributions and what your requirements are for accepting them.

For people who want to make changes to your project, it's helpful to have some documentation on how to get started. Perhaps there is a script that they should run or some environment variables that they need to set. Make these steps explicit. These instructions could also be useful to your future self.

You can also document commands to lint the code or run tests. These steps help to ensure high code quality and reduce the likelihood that the changes inadvertently break something. Having instructions for running tests is especially helpful if it requires external setup, such as starting a Selenium server for testing in a browser.

## Authors and acknowledgment
Show your appreciation to those who have contributed to the project.

## License
For open source projects, say how it is licensed.

## Project status
If you have run out of energy or time for your project, put a note at the top of the README saying that development has slowed down or stopped completely. Someone may choose to fork your project or volunteer to step in as a maintainer or owner, allowing your project to keep going. You can also make an explicit request for maintainers.
>>>>>>> gitlab/main
