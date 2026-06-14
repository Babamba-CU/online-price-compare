"""
온라인 단가비교 통합 대시보드 — AWS Lightsail / Polaris Colab 컨테이너 배포용 Flask 앱.

기능:
  - 0.0.0.0:8080 에서 정적 서빙 (index.html + seongji_data.js / subsidy_data.js)
  - GET /health 헬스체크 엔드포인트
  - 내장 스케줄러(APScheduler): 매일 07:00 KST 에 성지폰·공시지원금 데이터를
    date.today() 기준으로 재시드+재빌드하여 *_data.js 를 in-place 갱신.
    컨테이너 기동 직후에도 1회(백그라운드) 갱신하여 항상 오늘 기준일을 유지한다.
    → 외부 cron/Lambda/재배포 없이 컨테이너가 스스로 매일 데이터를 갱신한다.

가이드라인 준수:
  - 0.0.0.0:8080 리슨 / GET /health / non-root(UID 1000) / DB 정보는 환경변수
  - 컨테이너 TZ=Asia/Seoul (Dockerfile) → date.today() 가 KST 기준
"""
import os
import sys
import threading
from datetime import date

from flask import Flask, send_from_directory, abort

# === DB 환경변수 (가이드라인 충족 — 추후 외부 DB 전환시 사용) =====================
DB_HOST     = os.getenv("DB_HOST")
DB_PORT     = os.getenv("DB_PORT", "3306")
DB_USER     = os.getenv("DB_USER")
DB_PASSWORD = os.getenv("DB_PASSWORD")
DB_NAME     = os.getenv("DB_NAME")

# === 정적 파일 서빙 ============================================================
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
INDEX_FILE = "index.html"

# 화이트리스트: 외부에 노출 가능한 정적 자원 확장자
ALLOWED_EXTENSIONS = {".html", ".js", ".css", ".map", ".png", ".jpg", ".jpeg",
                     ".svg", ".ico", ".woff", ".woff2", ".ttf"}

# === 일일 데이터 갱신 설정 ======================================================
# 컨테이너 TZ=Asia/Seoul 이므로 date.today() 와 아래 시각 모두 KST 기준.
REFRESH_HOUR   = int(os.getenv("REFRESH_HOUR", "5"))   # 매일 05:00 KST
REFRESH_MINUTE = int(os.getenv("REFRESH_MINUTE", "0"))
TZ_NAME        = os.getenv("TZ", "Asia/Seoul")
# DATA_SOURCE=postgres → 사내망 모드: 수집 대신 사내 PostgreSQL 에서 *_data.js 빌드
DATA_SOURCE    = os.getenv("DATA_SOURCE", "").lower()

app = Flask(__name__, static_folder=None)


# === 데이터 갱신 (성지폰 + 공시지원금) =========================================
def refresh_data() -> None:
    """성지폰·공시지원금 데이터를 date.today() 기준으로 재생성.

    각 도메인의 시드/빌드 스크립트를 그대로 호출해 SQLite 를 재적재한 뒤
    seongji_data.js / subsidy_data.js 를 in-place 로 덮어쓴다.
    (성지폰은 seed_sample 합성 데이터, 공시지원금은 검증된 시드값 — 둘 다 외부 크롤 불필요)
    한 도메인이 실패해도 다른 도메인 갱신은 계속 진행한다.
    """
    def log(msg: str) -> None:
        print(f"[refresh] {msg}", file=sys.stderr, flush=True)

    log(f"데이터 갱신 시작 (기준일 {date.today().isoformat()})")

    # --- 성지폰 단가 ---
    try:
        import seongji_db, seed_sample, seongji_build
        seongji_db.DB_PATH.unlink(missing_ok=True)
        seed_sample.seed()        # init_db + 최근 15일치 샘플 시드 (오늘 기준)
        try:
            import seongji_naver
            r = seongji_naver.collect()   # Naver 카페·웹문서 실측 수집 (키 없으면 내부 skip)
            log(f"Naver 수집: {r}")
        except Exception as e:  # noqa: BLE001
            log(f"Naver 수집 실패 — 합성 데이터로 계속: {e!r}")
        try:
            import seongji_kakao
            r = seongji_kakao.collect()   # 성지점 카카오 채널 실측 수집 (sources.json 기반)
            log(f"카카오 수집: {r}")
        except Exception as e:  # noqa: BLE001
            log(f"카카오 수집 실패 — 합성 데이터로 계속: {e!r}")
        try:
            import seongji_vision_load
            r = seongji_vision_load.load()   # 시세표 이미지 판독 결과(커밋된 JSON) 적재
            log(f"시세표 vision 적재: {r}")
        except Exception as e:  # noqa: BLE001
            log(f"시세표 vision 적재 실패 — 계속: {e!r}")
        seongji_build.main()      # seongji_data.js 빌드
        log("성지폰 단가 갱신 완료")
    except Exception as e:  # noqa: BLE001
        log(f"성지폰 갱신 실패: {e!r}")

    # --- 공시지원금 ---
    try:
        import subsidy_db, subsidy_seed, subsidy_build
        subsidy_db.DB_PATH.unlink(missing_ok=True)
        subsidy_seed.seed()       # init_db + 검증된 3사 지원금 재적재 (오늘 기준)
        subsidy_build.main()      # subsidy_data.js 빌드
        log("공시지원금 갱신 완료")
    except Exception as e:  # noqa: BLE001
        log(f"공시지원금 갱신 실패: {e!r}")

    log("데이터 갱신 종료")


def refresh_from_pg() -> None:
    """사내망 모드: 사내 PostgreSQL 을 읽어 seongji_data.js / subsidy_data.js 재생성.
    인터넷 수집 로직을 타지 않는다(폐쇄망 OK)."""
    try:
        import build_from_pg
        build_from_pg.main()
        print("[refresh:pg] 사내 PostgreSQL → 화면 데이터 갱신 완료", file=sys.stderr, flush=True)
    except Exception as e:  # noqa: BLE001
        print(f"[refresh:pg] 갱신 실패: {e!r}", file=sys.stderr, flush=True)


def start_scheduler() -> None:
    """기동 시 1회 즉시 갱신 + 매일 REFRESH_HOUR:REFRESH_MINUTE(KST) 정기 갱신 등록.
    DATA_SOURCE=postgres 면 수집 대신 사내 PostgreSQL 빌드를 돌린다."""
    job = refresh_from_pg if DATA_SOURCE == "postgres" else refresh_data
    # 기동 직후 1회 갱신 — Flask/health 를 막지 않도록 별도 스레드에서 실행
    threading.Thread(target=job, name="refresh-startup", daemon=True).start()

    try:
        from apscheduler.schedulers.background import BackgroundScheduler
        from apscheduler.triggers.cron import CronTrigger
    except Exception as e:  # noqa: BLE001
        print(f"[scheduler] APScheduler 미설치 — 일일 자동 갱신 비활성화: {e!r}",
              file=sys.stderr, flush=True)
        return

    scheduler = BackgroundScheduler(timezone=TZ_NAME)
    scheduler.add_job(
        job,
        CronTrigger(hour=REFRESH_HOUR, minute=REFRESH_MINUTE, timezone=TZ_NAME),
        id="daily_refresh",
        replace_existing=True,
        misfire_grace_time=3600,   # 컨테이너가 잠깐 멈췄다 떠도 1시간 내면 보충 실행
        coalesce=True,             # 밀린 실행은 1회로 합침
    )
    scheduler.start()
    print(f"[scheduler] 일일 갱신 등록: 매일 "
          f"{REFRESH_HOUR:02d}:{REFRESH_MINUTE:02d} {TZ_NAME} "
          f"(mode={'postgres' if DATA_SOURCE=='postgres' else 'collect'})",
          file=sys.stderr, flush=True)


# === 라우트 ===================================================================
@app.route("/health")
def health():
    return "ok", 200


@app.route("/")
def index():
    return send_from_directory(BASE_DIR, INDEX_FILE)


@app.route("/<path:path>")
def static_files(path):
    """
    정적 자원 서빙. ../ 등 경로 탈출은 send_from_directory 가 차단.
    .py / .db / .sql / .sh 등 민감 파일은 화이트리스트 외 거부.
    """
    ext = os.path.splitext(path)[1].lower()
    if ext not in ALLOWED_EXTENSIONS:
        abort(404)
    return send_from_directory(BASE_DIR, path)


@app.errorhandler(404)
def not_found(_):
    return "Not Found", 404


if __name__ == "__main__":
    # 부팅 로그
    print(f"[startup] serving from {BASE_DIR} on 0.0.0.0:8080", file=sys.stderr)
    print(f"[startup] DB env present: host={bool(DB_HOST)} user={bool(DB_USER)}",
          file=sys.stderr)
    start_scheduler()
    app.run(host="0.0.0.0", port=8080, threaded=True)
