"""
성지점 카카오톡 채널(rocket-web API) 단가 수집기.

좌표 마스터(seongji_sources.json — import_sources.py 가 엑셀에서 생성)의
카카오 핸들별로 공개 게시글 JSON 을 조회해, 본문 텍스트를 라인 단위 시세표
파서(seongji_parser.parse_seongji_lines)로 파싱하고 seongji_db 에 적재한다.

  GET https://pf.kakao.com/rocket-web/web/profiles/{핸들}/posts
  → items[].contents[{t:"text", v}] / media[] / published_at / id

특징:
  - 키/로그인 불필요 (공개 채널 공개 게시글)
  - 최근 RECENT_DAYS 일 게시글만 적재 (핀 고정 옛글 제외)
  - region(지역)·author(판매점명) 메타를 함께 기록
  - 환경변수: KAKAO_MAX_CHANNELS (PoC 용 표본 제한), KAKAO_RECENT_DAYS

배포 컨테이너 제약: requests / seongji_db / seongji_parser 만 의존.
"""
from __future__ import annotations

import json
import sys
import time
from datetime import date, datetime, timedelta
from pathlib import Path

import requests

from seongji_db import (
    aggregate_daily,
    connect,
    init_db,
    insert_prices,
    log_run,
    upsert_post,
)
from seongji_parser import parse_seongji_lines, to_db_rows

POSTS_URL = "https://pf.kakao.com/rocket-web/web/profiles/{handle}/posts"
SOURCES_PATH = Path(__file__).parent / "seongji_sources.json"

USER_AGENT = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
              "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0 Safari/537.36")
REQUEST_SLEEP = 0.5                    # 채널 간 간격 (정중한 수집)
RECENT_DAYS_DEFAULT = 7                # 이 기간 내 게시글만 적재
MIN_PRICE_CONFIDENCE = 0.6
PRICE_SANITY = (-500_000, 3_000_000)


def _log(msg: str) -> None:
    print(f"[seongji_kakao] {msg}", file=sys.stderr, flush=True)


def _load_sources() -> list[dict]:
    if not SOURCES_PATH.exists():
        return []
    src = json.loads(SOURCES_PATH.read_text(encoding="utf-8"))
    return [s for s in src if s.get("type") == "kakao" and s.get("handle")]


def _fetch_posts(handle: str) -> list[dict]:
    r = requests.get(
        POSTS_URL.format(handle=handle),
        headers={"User-Agent": USER_AGENT},
        timeout=15,
    )
    r.raise_for_status()
    return r.json().get("items", [])


def _post_text(item: dict) -> str:
    return "\n".join(
        c.get("v", "") for c in item.get("contents", []) if c.get("t") == "text"
    )


def collect(snapshot: date | None = None) -> dict:
    """전체 카카오 채널 수집. 반환: {channels, posts, prices, errors, skipped}."""
    import os
    sources = _load_sources()
    if not sources:
        _log("seongji_sources.json 없음/비어있음 — 수집 건너뜀")
        return {"skipped": True}

    max_ch = int(os.getenv("KAKAO_MAX_CHANNELS", "0"))
    if max_ch > 0:
        sources = sources[:max_ch]
    recent_days = int(os.getenv("KAKAO_RECENT_DAYS", str(RECENT_DAYS_DEFAULT)))

    snapshot = snapshot or date.today()
    snap_iso = snapshot.isoformat()
    cutoff_ms = (datetime.now() - timedelta(days=recent_days)).timestamp() * 1000

    init_db()
    started = datetime.utcnow()
    n_posts = n_prices = n_errors = 0
    channels_ok = 0
    last_error: str | None = None

    with connect() as conn:
        for s in sources:
            handle, name, region = s["handle"], s.get("name") or "", s.get("region") or ""
            try:
                items = _fetch_posts(handle)
                channels_ok += 1
            except Exception as e:  # noqa: BLE001
                n_errors += 1
                last_error = f"{handle}: {e!r}"
                time.sleep(REQUEST_SLEEP)
                continue

            for it in items:
                if (it.get("published_at") or 0) < cutoff_ms:
                    continue   # 오래된 핀 고정/과거 글 제외
                text = _post_text(it)
                if not text.strip():
                    continue
                title = (it.get("title") or "").strip()
                prices = parse_seongji_lines(title, text)

                post_id = upsert_post(conn, {
                    "source": "kakao",
                    "source_post_id": f"{handle}/{it.get('id')}",
                    "url": f"https://pf.kakao.com/{handle}/{it.get('id')}",
                    "title": title or text.splitlines()[0][:80],
                    "author": name,
                    "posted_at": datetime.fromtimestamp(
                        it["published_at"] / 1000).isoformat() if it.get("published_at") else None,
                    "raw_text": text[:2000],
                })
                n_posts += 1

                rows = [
                    {**r, "region": region}
                    for r in to_db_rows(prices, snap_iso)
                    if r.get("cash_price") is not None
                    and r.get("confidence", 0) >= MIN_PRICE_CONFIDENCE
                    and PRICE_SANITY[0] <= r["cash_price"] <= PRICE_SANITY[1]
                ]
                if rows:
                    n_prices += insert_prices(conn, post_id, rows)

            time.sleep(REQUEST_SLEEP)

        status = "failed" if n_errors and not channels_ok else ("partial" if n_errors else "ok")
        log_run(conn, "kakao", started, datetime.utcnow(),
                n_posts, n_prices, n_errors, status, last_error)
        aggregate_daily(conn, snapshot)

    result = {"channels": channels_ok, "posts": n_posts,
              "prices": n_prices, "errors": n_errors}
    _log(f"채널 {channels_ok}/{len(sources)} · 게시글 {n_posts}건 · "
         f"가격 {n_prices}건 · 오류 {n_errors}건 ({status})")
    return result


if __name__ == "__main__":
    print(collect(), file=sys.stderr)
