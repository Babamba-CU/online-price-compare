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
from seongji_parser import parse_post_text, parse_seongji_lines, to_db_rows

API = "https://openapi.naver.com/v1/search/{ep}.json"

# webkr 은 sort 파라미터 미지원(400) — cafearticle/blog 만 sort=date.
# blog 는 유일하게 게시일(postdate)을 제공 → 신선도 필터 가능 (실측 2026-07-08).
ENDPOINTS: list[tuple[str, str]] = [
    ("cafearticle", "naver_cafe"),
    ("webkr", "naver_web"),
    ("blog", "naver_blog"),
]

# 실측(2026-07-08): 범용어는 모델 검출 2~6/30, 모델명+거래조건 결합형은 90~100%.
DEFAULT_QUERIES = [
    "휴대폰 성지 시세",
    "갤럭시 S26 울트라 성지 가격",
    "갤럭시 S26 울트라 번호이동 차비",
    "S26 울트라 현금완납 실구매가",
    "갤럭시 Z플립7 성지 가격",
    "갤럭시 Z폴드7 성지 시세표",
    "아이폰17 프로 현금완납",
    "아이폰17 프로 성지 시세표 좌표",
    "휴대폰 성지 마이너스폰 오늘",
]

DISPLAY = 100                          # 엔드포인트당 최대 결과 수
REQUEST_SLEEP = 0.3                    # Naver 10QPS 제한 대비
MIN_PRICE_CONFIDENCE = 0.6             # prices 적재 게이트 (모델+현금가 이상)
PRICE_SANITY = (-500_000, 3_000_000)   # 현금완납가 타당 범위 (마이너스=차익 지급)
# 검색 스니펫은 출고가/자급제가 오탐 위험(예: S26U 출고 290만) → 성지가 상한을 더 낮게.
SNIPPET_PRICE_MAX = 2_500_000
BLOG_RECENT_DAYS = 7                   # blog 는 postdate 기준 최근 N일만 적재

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
    if ep in ("cafearticle", "blog"):
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

                    # blog 만 게시일(postdate: YYYYMMDD) 제공 — 오래된 글은 시세 아님
                    posted_at = None
                    pd = (it.get("postdate") or "").strip()
                    if ep == "blog":
                        if len(pd) == 8 and pd.isdigit():
                            posted_at = f"{pd[:4]}-{pd[4:6]}-{pd[6:]}"
                            age = (date.today() - date(int(pd[:4]), int(pd[4:6]), int(pd[6:]))).days
                            if age > BLOG_RECENT_DAYS:
                                continue
                        else:
                            continue   # 게시일 미상 blog 글은 신선도 판별 불가 — 제외

                    # 스니펫은 잘린 시세표 텍스트 — 라인 파서가 수율 높음(실측 0%→7.8%).
                    # parse_post_text(모델 검출 게이트) 결과가 없으면 라인 파서로 폴백.
                    prices = parse_post_text(title, desc)
                    line_prices = parse_seongji_lines(title, desc)
                    if not prices and not line_prices:
                        continue   # 모델 미검출 게시글은 피드 품질 위해 제외
                    priced = line_prices if any(
                        p.cash_price is not None for p in line_prices) else prices

                    post_id = upsert_post(conn, {
                        "source": source,
                        "url": link,
                        "title": title,
                        # cafearticle/webkr 응답엔 게시일 없음 → crawled_at 로 정렬
                        "posted_at": posted_at,
                        "raw_text": (title + "\n" + desc)[:1000],
                    })
                    fetched += 1

                    rows = [
                        {**r, "add_condition": (r.get("add_condition") or "검색스니펫")}
                        for r in to_db_rows(priced, snap_iso)
                        if r.get("cash_price") is not None
                        and r.get("confidence", 0) >= MIN_PRICE_CONFIDENCE
                        and PRICE_SANITY[0] <= r["cash_price"] <= min(PRICE_SANITY[1], SNIPPET_PRICE_MAX)
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
