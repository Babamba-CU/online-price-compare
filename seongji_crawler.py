"""
성지폰 단가 일단위 크롤러.

소스:
  - ppomppu   : 뽐뿌 휴대폰 게시판 (PHP 게시판, 리스트 페이지 HTML)
  - algosa    : algosa.kr (정형 단가표)
  - ppasak    : ppasak.com (가격비교)
  - sajangnim : sajangnim.com (도매가)
  - moyoplan  : moyoplan.com (성지/요금제 SPA — best-effort 정적 메타만)
  - modusj    : modusj.com (구조화된 상품 페이지)

원칙:
  - robots.txt 와 User-Agent 명시
  - 한 사이트당 요청 간 1.0~2.0s sleep
  - 리스트 페이지만 가볍게 가져오고, 본문은 필요한 만큼만
  - 실패해도 다른 소스는 계속 진행
  - 모든 결과는 SQLite (seongji_prices.db) 에 적재

크롤러는 휴리스틱이며 사이트 HTML 변경에 취약하다.
"adapters/{source}.py" 로 분리해 갈아끼우기 쉽게 했다.
"""
from __future__ import annotations

import argparse
import logging
import sys
import time
from dataclasses import dataclass
from datetime import datetime, date, timezone
from typing import Callable, Iterable

import requests
from bs4 import BeautifulSoup

from seongji_db import (
    init_db, connect, upsert_post, insert_prices, log_run, aggregate_daily,
)
from seongji_parser import parse_post_text, to_db_rows

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)
log = logging.getLogger("seongji")

UA = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 14_4) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"
)
HEADERS = {"User-Agent": UA, "Accept-Language": "ko-KR,ko;q=0.9,en;q=0.5"}
REQUEST_TIMEOUT = 15
SLEEP_BETWEEN = 1.2


@dataclass
class Post:
    source: str
    url: str
    title: str
    posted_at: str | None = None
    body: str = ""
    source_post_id: str | None = None


# ------------------------------------------------------------------
# 어댑터 — 각각 list_posts(max_pages) -> Iterable[Post]
# ------------------------------------------------------------------

def _get(url: str) -> requests.Response | None:
    try:
        r = requests.get(url, headers=HEADERS, timeout=REQUEST_TIMEOUT)
        if r.status_code >= 400:
            log.warning("HTTP %s %s", r.status_code, url)
            return None
        return r
    except requests.RequestException as e:
        log.warning("fetch fail %s: %s", url, e)
        return None


def crawl_ppomppu(max_pages: int = 2) -> Iterable[Post]:
    """뽐뿌 휴대폰 뽐뿌 게시판."""
    base = "https://www.ppomppu.co.kr/zboard/zboard.php?id=phone"
    for page in range(1, max_pages + 1):
        url = f"{base}&page={page}"
        r = _get(url)
        if not r:
            continue
        soup = BeautifulSoup(r.content, "lxml")
        # 게시글 리스트 — 클래스명은 자주 바뀌므로 a[href*=view.php] 휴리스틱
        for a in soup.select('a[href*="view.php"]'):
            href = a.get("href", "")
            title = a.get_text(strip=True)
            if not title or len(title) < 6:
                continue
            full = "https://www.ppomppu.co.kr/zboard/" + href.lstrip("./")
            yield Post(source="ppomppu", url=full, title=title)
        time.sleep(SLEEP_BETWEEN)


def crawl_algosa(max_pages: int = 1) -> Iterable[Post]:
    """algosa.kr 메인 — 단가표 직접 페이지가 동적이므로 메인 페이지에서 링크 수집."""
    r = _get("https://algosa.kr/")
    if not r:
        return
    soup = BeautifulSoup(r.content, "lxml")
    for a in soup.select("a[href]"):
        href = a["href"]
        title = a.get_text(strip=True)
        if not title or len(title) < 4:
            continue
        if href.startswith("/"):
            href = "https://algosa.kr" + href
        if "algosa.kr" not in href:
            continue
        yield Post(source="algosa", url=href, title=title)


def crawl_ppasak(max_pages: int = 1) -> Iterable[Post]:
    r = _get("https://www.ppasak.com/")
    if not r:
        return
    soup = BeautifulSoup(r.content, "lxml")
    for a in soup.select("a[href]"):
        title = a.get_text(strip=True)
        if not title or len(title) < 4:
            continue
        href = a["href"]
        if href.startswith("/"):
            href = "https://www.ppasak.com" + href
        if "ppasak.com" not in href:
            continue
        yield Post(source="ppasak", url=href, title=title)


def crawl_sajangnim(max_pages: int = 1) -> Iterable[Post]:
    r = _get("https://sajangnim.com/")
    if not r:
        return
    soup = BeautifulSoup(r.content, "lxml")
    for a in soup.select("a[href]"):
        title = a.get_text(strip=True)
        if not title or len(title) < 4:
            continue
        href = a["href"]
        if href.startswith("/"):
            href = "https://sajangnim.com" + href
        if "sajangnim.com" not in href:
            continue
        yield Post(source="sajangnim", url=href, title=title)


def crawl_moyoplan(max_pages: int = 1) -> Iterable[Post]:
    """모요 — SPA 라 메타 + 메인 페이지에 노출된 카드 텍스트만."""
    r = _get("https://www.moyoplan.com/")
    if not r:
        return
    soup = BeautifulSoup(r.content, "lxml")
    text = soup.get_text("\n", strip=True)
    if text:
        yield Post(
            source="moyoplan",
            url="https://www.moyoplan.com/",
            title="moyoplan main",
            body=text[:5000],
        )


def crawl_modusj(max_pages: int = 1) -> Iterable[Post]:
    r = _get("https://modusj.com/")
    if not r:
        return
    soup = BeautifulSoup(r.content, "lxml")
    # 상품 카드 — 클래스명은 사이트 변경에 취약하므로 안전한 폴백
    for a in soup.select("a[href]"):
        title = a.get_text(strip=True)
        if not title or len(title) < 4:
            continue
        href = a["href"]
        if href.startswith("/"):
            href = "https://modusj.com" + href
        if "modusj.com" not in href:
            continue
        yield Post(source="modusj", url=href, title=title)


CRAWLERS: dict[str, Callable[..., Iterable[Post]]] = {
    "ppomppu":   crawl_ppomppu,
    "algosa":    crawl_algosa,
    "ppasak":    crawl_ppasak,
    "sajangnim": crawl_sajangnim,
    "moyoplan":  crawl_moyoplan,
    "modusj":    crawl_modusj,
}


# ------------------------------------------------------------------
# 본문 fetch (옵션) — 제목만으로 충분히 파싱되는 경우가 많아 기본은 off
# ------------------------------------------------------------------
def fetch_body(post: Post) -> str:
    if post.body:
        return post.body
    r = _get(post.url)
    if not r:
        return ""
    soup = BeautifulSoup(r.content, "lxml")
    for tag in soup(["script", "style", "noscript"]):
        tag.decompose()
    return soup.get_text("\n", strip=True)[:5000]


# ------------------------------------------------------------------
# 메인 실행
# ------------------------------------------------------------------
def run(sources: list[str], max_pages: int, fetch_bodies: bool) -> None:
    init_db()
    today = date.today().isoformat()

    for src in sources:
        if src not in CRAWLERS:
            log.warning("unknown source: %s", src)
            continue
        started = datetime.now(timezone.utc)
        fetched = parsed = errors = 0
        err_msg: str | None = None
        status = "success"
        try:
            with connect() as conn:
                for post in CRAWLERS[src](max_pages=max_pages):
                    fetched += 1
                    try:
                        body = fetch_body(post) if fetch_bodies else post.body
                        prices = parse_post_text(post.title, body)
                        if not prices:
                            continue
                        post_id = upsert_post(conn, {
                            "source":     post.source,
                            "url":        post.url,
                            "title":      post.title,
                            "posted_at":  post.posted_at,
                            "crawled_at": datetime.now(timezone.utc).isoformat(),
                            "raw_text":   (body or "")[:2000],
                        })
                        rows = to_db_rows(prices, today)
                        parsed += insert_prices(conn, post_id, rows)
                    except Exception as e:        # noqa: BLE001
                        errors += 1
                        log.exception("parse fail: %s", post.url)
                    if fetch_bodies:
                        time.sleep(SLEEP_BETWEEN)
                if errors and parsed == 0:
                    status = "failed"
                elif errors:
                    status = "partial"
        except Exception as e:                    # noqa: BLE001
            status = "failed"
            err_msg = str(e)
            log.exception("crawler crashed: %s", src)
        finally:
            with connect() as conn:
                log_run(
                    conn,
                    source=src,
                    started_at=started,
                    finished_at=datetime.now(timezone.utc),
                    fetched_posts=fetched,
                    parsed_prices=parsed,
                    errors=errors,
                    status=status,
                    error_message=err_msg,
                )
        log.info("[%s] fetched=%d parsed=%d errors=%d status=%s",
                 src, fetched, parsed, errors, status)

    # 일별 통계 머터리얼 갱신
    with connect() as conn:
        n = aggregate_daily(conn, date.today())
    log.info("daily_stats rows for %s: %d", today, n)


def main() -> int:
    p = argparse.ArgumentParser(description="성지폰 단가 크롤러")
    p.add_argument("--sources", nargs="+",
                   default=list(CRAWLERS.keys()),
                   choices=list(CRAWLERS.keys()))
    p.add_argument("--max-pages", type=int, default=2)
    p.add_argument("--fetch-bodies", action="store_true",
                   help="게시글 본문까지 받아서 파싱 (느림)")
    args = p.parse_args()
    run(args.sources, args.max_pages, args.fetch_bodies)
    return 0


if __name__ == "__main__":
    sys.exit(main())
