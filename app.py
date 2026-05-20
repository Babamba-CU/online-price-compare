"""
온라인 단가비교 통합 대시보드 — Polaris Colab 컨테이너 배포용 Flask 앱.

가이드라인 준수:
  - 0.0.0.0:8080 리슨
  - GET /health 헬스체크 엔드포인트
  - DB 접속 정보는 환경변수에서만 (현재 정적 HTML 서빙이므로 미사용, 추후 외부 DB 전환시 사용)
  - non-root (UID 1000) 실행
  - 영구 저장은 /tmp 또는 외부 DB (SQLite 파일은 read-only 로 이미지에 포함)
"""
import os
import sys
from flask import Flask, send_from_directory, abort

# === DB 환경변수 (가이드라인 충족 — 추후 외부 DB 전환시 사용) =====================
DB_HOST     = os.getenv("DB_HOST")
DB_PORT     = os.getenv("DB_PORT", "3306")
DB_USER     = os.getenv("DB_USER")
DB_PASSWORD = os.getenv("DB_PASSWORD")
DB_NAME     = os.getenv("DB_NAME")

# === 정적 파일 서빙 ============================================================
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
INDEX_FILE = "통합대시보드.html"

# 화이트리스트: 외부에 노출 가능한 정적 자원 확장자
ALLOWED_EXTENSIONS = {".html", ".js", ".css", ".map", ".png", ".jpg", ".jpeg",
                     ".svg", ".ico", ".woff", ".woff2", ".ttf"}

app = Flask(__name__, static_folder=None)


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
    app.run(host="0.0.0.0", port=8080)
