"""
Naver Search API 기반 성지폰 단가 수집기.

  - cafearticle (네이버 카페 글)  → source = naver_cafe
  - webkr      (웹문서 — 알고사/빠삭/블로그 등 추가 사이트) → source = naver_web

검색 결과의 제목+요약(description)을 seongji_parser.parse_post_text 로 파싱해
모델이 검출된 게시글만 seongji_posts 에 적재하고, 현금완납가가 추출되고
신뢰도가 임계값 이상인 행만 seongji_prices 에 적재한다.

환경변수:
  NAVER_CLIENT_ID / NAVER_CLIENT_SECRET  (필수 — 없으면 graceful skip)
  NAVER_QUERIES                          (선택 — 콤마구분 쿼리 오버라이드)

배포 컨테이너 제약: requests / seongji_db / seongji_parser 만 의존.
(seongji_crawler 등 .dockerignore 제외 파일 import 금지)
"""
from __future__ import annotations

import html
import os
import re
import sys
import time
from datetime import date, datetime

import requests

from seongji_db import (
    aggregate_daily,
    connect,
    init_db,
    insert_prices,
    log_run,
    upsert_post,
)
from seongji_parser import parse_post_text, to_db_rows

API = "https://openapi.naver.com/v1/search/{ep}.json"

# webkr 은 sort 파라미터 미지원(400) — cafearticle 만 sort=date
ENDPOINTS: list[tuple[str, str]] = [
    ("cafearticle", "naver_cafe"),
    ("webkr", "naver_web"),
]

DEFAULT_QUERIES = [
    "휴대폰 성지 시세",
    "성지 좌표 휴대폰",
    "갤럭시 S26 성지",
    "갤럭시 Z플립7 성지",
    "갤럭시 Z폴드7 성지",
    "아이폰 17 성지 시세",
    "아이폰 17 프로 성지",
]

DISPLAY = 100                          # 엔드포인트당 최대 결과 수
REQUEST_SLEEP = 0.3                    # Naver 10QPS 제한 대비
MIN_PRICE_CONFIDENCE = 0.6             # prices 적재 게이트 (모델+현금가 이상)
PRICE_SANITY = (-500_000, 3_000_000)   # 현금완납가 타당 범위 (마이너스=차익 지급)

TAG_RE = re.compile(r"<[^>]+>")


def _log(msg: str) -> None:
    print(f"[seongji_naver] {msg}", file=sys.stderr, flush=True)


def _queries() -> list[str]:
    override = os.getenv("NAVER_QUERIES", "").strip()
    if override:
        return [q.strip() for q in override.split(",") if q.strip()]
    return DEFAULT_QUERIES


def _clean(s: str | None) -> str:
    """Naver API 는 검색어를 <b> 로 감싼 HTML 을 반환 — 태그 제거 + 엔티티 해제."""
    return html.unescape(TAG_RE.sub("", s or "")).strip()


def _fetch(ep: str, query: str, cid: str, csec: str) -> list[dict]:
    params: dict[str, object] = {"query": query, "display": DISPLAY, "start": 1}
    if ep == "cafearticle":
        params["sort"] = "date"
    r = requests.get(
        API.format(ep=ep),
        headers={"X-Naver-Client-Id": cid, "X-Naver-Client-Secret": csec},
        params=params,
        timeout=15,
    )
    r.raise_for_status()
    return r.json().get("items", [])


def collect(snapshot: date | None = None) -> dict:
    """전체 수집 실행. 반환: 엔드포인트별 {fetched, parsed, errors} 요약."""
    cid = os.getenv("NAVER_CLIENT_ID")
    csec = os.getenv("NAVER_CLIENT_SECRET")
    if not cid or not csec:
        _log("NAVER_CLIENT_ID/SECRET 미설정 — 수집 건너뜀")
        return {"skipped": True}

    snapshot = snapshot or date.today()
    snap_iso = snapshot.isoformat()
    queries = _queries()
    init_db()

    summary: dict[str, dict] = {}
    seen_urls: set[str] = set()   # 런 내 중복 제거 (런 간은 upsert 가 처리)

    with connect() as conn:
        for ep, source in ENDPOINTS:
            started = datetime.utcnow()
            fetched = parsed = errors = 0
            last_error: str | None = None

            for query in queries:
                try:
                    items = _fetch(ep, query, cid, csec)
                except Exception as e:  # noqa: BLE001
                    errors += 1
                    last_error = repr(e)
                    _log(f"{ep} '{query}' 호출 실패: {e!r}")
                    time.sleep(REQUEST_SLEEP)
                    continue

                for it in items:
                    link = (it.get("link") or "").strip()
                    if not link or link in seen_urls:
                        continue
                    seen_urls.add(link)

                    title = _clean(it.get("title"))
                    desc = _clean(it.get("description"))
                    prices = parse_post_text(title, desc)
                    if not prices:
                        continue   # 모델 미검출 게시글은 피드 품질 위해 제외

                    post_id = upsert_post(conn, {
                        "source": source,
                        "url": link,
                        "title": title,
                        # cafearticle/webkr 응답엔 게시일 없음 → crawled_at 로 정렬
                        "posted_at": None,
                        "raw_text": (title + "\n" + desc)[:1000],
                    })
                    fetched += 1

                    rows = [
                        r for r in to_db_rows(prices, snap_iso)
                        if r.get("cash_price") is not None
                        and r.get("confidence", 0) >= MIN_PRICE_CONFIDENCE
                        and PRICE_SANITY[0] <= r["cash_price"] <= PRICE_SANITY[1]
                    ]
                    if rows:
                        parsed += insert_prices(conn, post_id, rows)

                time.sleep(REQUEST_SLEEP)

            status = "failed" if errors and not fetched else ("partial" if errors else "ok")
            log_run(conn, source, started, datetime.utcnow(),
                    fetched, parsed, errors, status, last_error)
            summary[source] = {"fetched": fetched, "parsed": parsed, "errors": errors}
            _log(f"{source}: 게시글 {fetched}건 / 가격 {parsed}건 / 오류 {errors}건 ({status})")

        aggregate_daily(conn, snapshot)

    return summary


if __name__ == "__main__":
    print(collect(), file=sys.stderr)
