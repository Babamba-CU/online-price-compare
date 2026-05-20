-- ===============================================
-- 성지폰 단가 모니터링 DB 스키마
-- 동일 DDL을 SQLite / Supabase(PostgreSQL) 양쪽에서 사용
-- ===============================================

-- 1) 원본 게시글 (raw)
CREATE TABLE IF NOT EXISTS seongji_posts (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    source          TEXT NOT NULL,        -- ppomppu | algosa | ppasak | sajangnim | moyoplan | modusj
    source_post_id  TEXT,                 -- 사이트별 게시글 ID
    url             TEXT NOT NULL,
    title           TEXT,
    author          TEXT,
    posted_at       TIMESTAMP,            -- 게시일
    crawled_at      TIMESTAMP NOT NULL,   -- 수집시각
    raw_text        TEXT,
    view_count      INTEGER,
    comment_count   INTEGER,
    UNIQUE(source, url)
);

CREATE INDEX IF NOT EXISTS idx_posts_source_date ON seongji_posts(source, posted_at);
CREATE INDEX IF NOT EXISTS idx_posts_crawled    ON seongji_posts(crawled_at);

-- 2) 파싱된 가격 정보 (한 게시글에 여러 모델/통신사가 있을 수 있음)
CREATE TABLE IF NOT EXISTS seongji_prices (
    id                  INTEGER PRIMARY KEY AUTOINCREMENT,
    post_id             INTEGER NOT NULL,
    snapshot_date       DATE NOT NULL,         -- 일단위 스냅샷 키 (YYYY-MM-DD)
    carrier             TEXT,                  -- SKT | KT | LGU+ | 알뜰 | ?
    subscription_type   TEXT,                  -- 신규 | MNP | 기변
    contract_type       TEXT,                  -- 공시 | 선약 | 자급
    model_name          TEXT NOT NULL,         -- 정규화된 모델명 (Galaxy S26 Ultra 등)
    model_raw           TEXT,                  -- 원본 표기
    storage_gb          INTEGER,               -- 256 / 512 / 1024
    cash_price          INTEGER,               -- 현금완납가 (원)
    monthly_fee         INTEGER,               -- 월 납부액 (원)
    plan_name           TEXT,                  -- 요금제명
    plan_duration_mo    INTEGER,               -- 의무유지 개월
    add_condition       TEXT,                  -- 부가서비스/카드/필수가입 등
    region              TEXT,                  -- 서울/경기 등 (있을 경우)
    confidence          REAL,                  -- 파싱 신뢰도 0.0~1.0
    raw_text            TEXT,
    FOREIGN KEY (post_id) REFERENCES seongji_posts(id) ON DELETE CASCADE
);

CREATE INDEX IF NOT EXISTS idx_prices_snapshot   ON seongji_prices(snapshot_date);
CREATE INDEX IF NOT EXISTS idx_prices_model      ON seongji_prices(model_name, carrier);
CREATE INDEX IF NOT EXISTS idx_prices_model_sub  ON seongji_prices(model_name, carrier, subscription_type, snapshot_date);

-- 3) 일별 통계 (대시보드 빠른 조회용 머터리얼)
CREATE TABLE IF NOT EXISTS seongji_daily_stats (
    snapshot_date       DATE NOT NULL,
    model_name          TEXT NOT NULL,
    carrier             TEXT NOT NULL,
    subscription_type   TEXT NOT NULL,
    sample_count        INTEGER,
    min_price           INTEGER,
    p25_price           INTEGER,
    median_price        INTEGER,
    p75_price           INTEGER,
    max_price           INTEGER,
    avg_price           INTEGER,
    min_source          TEXT,         -- 최저가 사이트
    min_url             TEXT,
    updated_at          TIMESTAMP,
    PRIMARY KEY (snapshot_date, model_name, carrier, subscription_type)
);

-- 4) 크롤링 실행 로그
CREATE TABLE IF NOT EXISTS seongji_crawl_runs (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    started_at      TIMESTAMP NOT NULL,
    finished_at     TIMESTAMP,
    source          TEXT NOT NULL,
    fetched_posts   INTEGER DEFAULT 0,
    parsed_prices   INTEGER DEFAULT 0,
    errors          INTEGER DEFAULT 0,
    error_message   TEXT,
    status          TEXT                  -- success | partial | failed
);

CREATE INDEX IF NOT EXISTS idx_runs_started ON seongji_crawl_runs(started_at);

-- ===============================================
-- Supabase(PostgreSQL) 전용: RLS / Public read 정책
-- (SQLite에서는 무시되므로 별도 분리해서 적용 권장)
--   ALTER TABLE seongji_prices ENABLE ROW LEVEL SECURITY;
--   CREATE POLICY "Public read" ON seongji_prices FOR SELECT USING (true);
--   CREATE POLICY "Public read" ON seongji_daily_stats FOR SELECT USING (true);
--   CREATE POLICY "Public read" ON seongji_posts FOR SELECT USING (true);
-- ===============================================
