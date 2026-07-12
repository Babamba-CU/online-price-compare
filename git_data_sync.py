"""
Git 원격 저장소(raw) → 컨테이너 데이터 미러 — "배포 없이 최신 데이터".

로컬 5am 루틴/CI 가 GitLab·GitHub 에 커밋하는 화면 파일(index.html, *_data.js)을
실행 중인 컨테이너가 주기적으로 직접 받아 /tmp 에 저장하고, app.py 가 그 사본을
우선 서빙한다. → 저장소에 커밋만 되면 재배포 없이 화면·데이터가 최신화된다.

환경변수:
  GIT_RAW_BASE     (필수 — 없으면 이 모듈 비활성)
      GitLab: https://gitlab.tde.sktelecom.com/MAMF/online-price/-/raw/main
      GitHub: https://raw.githubusercontent.com/Babamba-CU/online-price-compare/main
  GIT_SYNC_TOKEN   (선택 — 비공개 저장소 읽기 토큰.
                    호스트에 'gitlab' 포함 시 PRIVATE-TOKEN 헤더, 그 외 Authorization: token)
  GIT_SYNC_FILES   (선택 — 기본 "index.html,seongji_data.js,subsidy_data.js")
  GIT_SYNC_MINUTES (선택 — 폴링 주기, 기본 60분)

설계 원칙:
  - 쓰기는 /tmp 만 (Polaris Colab 제약). 원자적 교체(tmp → rename).
  - 내용 검증 통과 시에만 교체 — 절반 내려온 파일/오류 페이지로 라이브를 깨지 않는다.
  - 실패는 로그만 남기고 기존 서빙 유지 (graceful).
"""
from __future__ import annotations

import hashlib
import os
import sys
from pathlib import Path
from urllib.parse import urlparse
from urllib.request import Request, urlopen

LIVE_DIR = Path("/tmp/git_sync")
DEFAULT_FILES = "index.html,seongji_data.js,subsidy_data.js"
TIMEOUT = 20

# 파일별 최소 검증 — (최소 바이트, 반드시 포함해야 할 문자열)
VALIDATORS: dict[str, tuple[int, str]] = {
    "index.html":      (10_000, "온라인 단가비교"),
    "seongji_data.js": (10_000, "window.SEONGJI_DATA"),
    "subsidy_data.js": (10_000, "window.SUBSIDY_DATA"),
}


def _log(msg: str) -> None:
    print(f"[git-sync] {msg}", file=sys.stderr, flush=True)


def enabled() -> bool:
    return bool(os.getenv("GIT_RAW_BASE", "").strip())


def files() -> list[str]:
    raw = os.getenv("GIT_SYNC_FILES", DEFAULT_FILES)
    return [f.strip() for f in raw.split(",") if f.strip()]


def _headers() -> dict[str, str]:
    h = {"User-Agent": "price-dashboard-sync/1.0"}
    token = os.getenv("GIT_SYNC_TOKEN", "").strip()
    if token:
        host = urlparse(os.getenv("GIT_RAW_BASE", "")).netloc
        if "gitlab" in host.lower():
            h["PRIVATE-TOKEN"] = token
        else:
            h["Authorization"] = f"token {token}"
    return h


def _fetch(base: str, name: str) -> bytes | None:
    url = f"{base.rstrip('/')}/{name}"
    try:
        req = Request(url, headers=_headers())
        with urlopen(req, timeout=TIMEOUT) as r:
            return r.read()
    except Exception as e:  # noqa: BLE001
        _log(f"{name} 다운로드 실패: {e!r}")
        return None


def _valid(name: str, data: bytes) -> bool:
    min_size, must_contain = VALIDATORS.get(name, (100, ""))
    if len(data) < min_size:
        _log(f"{name} 검증 실패: {len(data)}B < {min_size}B (오류 페이지 가능성)")
        return False
    if must_contain and must_contain.encode("utf-8") not in data:
        _log(f"{name} 검증 실패: 필수 마커 '{must_contain}' 없음")
        return False
    return True


def sync_once() -> dict:
    """전체 파일 1회 동기화. 반환: {file: 'updated'|'unchanged'|'failed'|'invalid'}."""
    base = os.getenv("GIT_RAW_BASE", "").strip()
    if not base:
        return {}
    LIVE_DIR.mkdir(parents=True, exist_ok=True)
    result: dict[str, str] = {}
    for name in files():
        data = _fetch(base, name)
        if data is None:
            result[name] = "failed"
            continue
        if not _valid(name, data):
            result[name] = "invalid"
            continue
        dest = LIVE_DIR / name
        if dest.exists() and hashlib.sha256(dest.read_bytes()).digest() == hashlib.sha256(data).digest():
            result[name] = "unchanged"
            continue
        tmp = dest.with_suffix(dest.suffix + ".tmp")
        tmp.write_bytes(data)
        tmp.replace(dest)   # 원자적 교체 — 서빙 중에도 안전
        result[name] = "updated"
    updated = [k for k, v in result.items() if v == "updated"]
    _log(f"동기화 완료: {result}" + (f" → 갱신 {updated}" if updated else " (변경 없음)"))
    return result


if __name__ == "__main__":
    print(sync_once())
