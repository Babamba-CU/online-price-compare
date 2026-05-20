-- ===============================================
-- 통신 3사 공시지원금 비교 DB 스키마
-- SQLite / Supabase(PostgreSQL) 공통 DDL
-- 약정 기준: 24개월 / 요금제 기준: 각 사이트 디폴트
-- ===============================================

-- 1) 단말기 마스터 (모델 정규화용)
CREATE TABLE IF NOT EXISTS subsidy_devices (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    model_name      TEXT NOT NULL UNIQUE,    -- 정규화된 이름 (예: Galaxy S26 Ultra)
    manufacturer    TEXT,                    -- Samsung | Apple | Google | ...
    released_at     DATE,                    -- 출시일 (정렬용)
    storage_options TEXT,                    -- JSON: ["256GB","512GB","1TB"]
    aliases         TEXT                     -- JSON: 사이트별 표기 매핑
);

CREATE INDEX IF NOT EXISTS idx_devices_released ON subsidy_devices(released_at DESC);

-- 2) 일별 공시지원금 스냅샷 (3사 × 단말 × 가입유형 × 일자)
CREATE TABLE IF NOT EXISTS subsidy_offers (
    id                  INTEGER PRIMARY KEY AUTOINCREMENT,
    snapshot_date       DATE    NOT NULL,    -- 수집일 (KST)
    carrier             TEXT    NOT NULL,    -- SKT | KT | LGU+
    model_name          TEXT    NOT NULL,    -- subsidy_devices.model_name 와 동일 정규화
    storage_gb          INTEGER,             -- 256 / 512 / 1024
    subscription_type   TEXT    NOT NULL DEFAULT 'MNP',  -- 010신규 | MNP | 기변
    color               TEXT,                -- 색상 (있을 경우)

    -- 가격 (원 단위)
    retail_price        INTEGER,             -- 출고가
    plan_name           TEXT,                -- 기준 요금제 (각 사이트 디폴트)
    plan_monthly_fee    INTEGER,             -- 요금제 월 납부

    -- 지원금
    subsidy_public      INTEGER,             -- 공시지원금
    subsidy_additional  INTEGER,             -- 추가지원금 (대리점/유통망)
    subsidy_total       INTEGER,             -- 공시 + 추가 (계산필드)

    -- 선택약정 비교용
    select_discount_24mo INTEGER,            -- 선택약정 25% × 요금제 × 24개월

    -- 약정/할부
    contract_months     INTEGER DEFAULT 24,  -- 24개월 고정
    net_buy_price       INTEGER,             -- 실구매가 = 출고가 - 총지원금
    monthly_device_fee  INTEGER,             -- 단말 월 할부금 (실구매가 / 24)

    -- 원본
    source_url          TEXT NOT NULL,
    source_html_hash    TEXT,                -- 변동 감지용 해시
    raw_payload         TEXT,                -- JSON: 사이트별 원본 필드
    fetched_at          TIMESTAMP NOT NULL,

    UNIQUE(snapshot_date, carrier, model_name, storage_gb, subscription_type)
);

CREATE INDEX IF NOT EXISTS idx_offers_date       ON subsidy_offers(snapshot_date);
CREATE INDEX IF NOT EXISTS idx_offers_carrier    ON subsidy_offers(carrier, snapshot_date);
CREATE INDEX IF NOT EXISTS idx_offers_model      ON subsidy_offers(model_name, snapshot_date);
CREATE INDEX IF NOT EXISTS idx_offers_subscr     ON subsidy_offers(subscription_type, snapshot_date);
CREATE INDEX IF NOT EXISTS idx_offers_lookup     ON subsidy_offers(snapshot_date, model_name, carrier, subscription_type);

-- 3) 변동 이력 (전일 대비 변경 발생시 기록)
CREATE TABLE IF NOT EXISTS subsidy_changes (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    snapshot_date   DATE    NOT NULL,
    carrier         TEXT    NOT NULL,
    model_name      TEXT    NOT NULL,
    storage_gb      INTEGER,
    field           TEXT    NOT NULL,        -- subsidy_public | subsidy_additional | retail_price
    old_value       INTEGER,
    new_value       INTEGER,
    diff            INTEGER,                  -- new - old
    detected_at     TIMESTAMP NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_changes_date ON subsidy_changes(snapshot_date DESC);

-- 4) 크롤링 런 로그
CREATE TABLE IF NOT EXISTS subsidy_crawl_runs (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    started_at      TIMESTAMP NOT NULL,
    finished_at     TIMESTAMP,
    carrier         TEXT NOT NULL,           -- SKT | KT | LGU+
    fetched_models  INTEGER DEFAULT 0,
    upserted        INTEGER DEFAULT 0,
    changed         INTEGER DEFAULT 0,
    errors          INTEGER DEFAULT 0,
    status          TEXT,                    -- success | partial | failed
    error_message   TEXT,
    source_url      TEXT
);

-- ===============================================
-- Supabase(PostgreSQL) 적용시 추가:
--   - INTEGER PRIMARY KEY AUTOINCREMENT  →  BIGSERIAL PRIMARY KEY
--   - ALTER TABLE subsidy_offers   ENABLE ROW LEVEL SECURITY;
--     CREATE POLICY "public read" ON subsidy_offers FOR SELECT USING (true);
--   - ALTER TABLE subsidy_devices  ENABLE ROW LEVEL SECURITY;
--     CREATE POLICY "public read" ON subsidy_devices FOR SELECT USING (true);
--   - service_role 키로만 INSERT/UPDATE 허용
-- ===============================================
