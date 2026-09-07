"""
공시지원금 수집 — 스마트초이스(방통위 통신요금정보포털, smartchoice.or.kr) '단말기 기준 조회'.

배경(2026-09-08): 종전 subsidy_seed.py 는 5월 스크린샷 4기종 + '정책 일관 추정'(사실상 임의값)이었고
통신사 3사 사이트는 SPA·reCAPTCHA 라 크롤링이 막힌다. 스마트초이스는 3사 공시지원금을 한 곳에서
제공하며, 조회 폼 POST(/smc/mobile/dantongList.do)에 단말 코드를 넣으면 서버가 표를 렌더링해 준다
(대기열 NetFUNNEL 은 브라우저 JS 전용 — 직접 POST 는 통과. 2026-09-08 실측).

산출:
  · subsidy_offers.db 에 (오늘, 통신사, 기종, 용량, 가입유형) 행 upsert — 요금제는 성지 시세표 기준
    (SKT 109,000 / KT 110,000 / LGU+ 115,000)에 가장 가까운 구간을 대표값으로, 전 구간은 raw_payload.tiers
  · subsidy_snapshot.json (네트워크 없는 곳 — 클라우드 루틴/사내 — 에서 --load 로 재적재)

사용:
  python subsidy_smartchoice.py                 # 수집 → DB 적재 → subsidy_snapshot.json 저장
  python subsidy_smartchoice.py --load FILE     # 스냅샷 JSON 을 DB 에 적재만
  python subsidy_smartchoice.py --dry-run       # 수집·파싱 결과만 출력
대상: 갤럭시 S 계열·Z 폴드/플립 계열·아이폰 계열 전 단말(용량별) — 사용자 확정 2026-09-08.
"""
from __future__ import annotations

import argparse
import html as htmlmod
import http.cookiejar
import json
import re
import ssl
import sys
import time
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

from kst import today_kst
from model_normalize import normalize
from subsidy_db import connect, init_db, log_run, upsert_device, upsert_offer

BASE = "https://www.smartchoice.or.kr"
LIST_URL = BASE + "/smc/mobile/dantongList.do"
PRODUCT_URL = BASE + "/smc/mobile/getProductList.do"
SNAPSHOT_PATH = Path(__file__).parent / "subsidy_snapshot.json"
CARRIERS = ("SKT", "KT", "LGU+")
TARGET_FEE = {"SKT": 109_000, "KT": 110_000, "LGU+": 115_000}   # 성지 시세표 기준 요금제
ADDITIONAL_RATE = 0.15                                         # 추가지원금 상한(공시의 15%)
MAKERS = ("삼성전자", "애플")
# 대상 기종 — 갤럭시 S / Z 폴드·플립 / 아이폰 (2024년 이후 출시)
TARGET_RE = re.compile(r"갤럭시\s*S2[4-9]|갤럭시\s*Z\s*(폴드|플립)|아이폰")
SLEEP = 1.0
UA = "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) price-dashboard/1.0"


def _log(msg: str) -> None:
    print(f"[subsidy-smartchoice] {msg}", file=sys.stderr, flush=True)


def _opener():
    cj = http.cookiejar.CookieJar()
    ctx = ssl.create_default_context()
    op = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(cj), urllib.request.HTTPSHandler(context=ctx))
    op.addheaders = [("User-Agent", UA), ("Referer", LIST_URL)]
    return op


def _post(op, url: str, data: dict, timeout: int = 60) -> str:
    body = urllib.parse.urlencode(data).encode()
    with op.open(urllib.request.Request(url, data=body, method="POST"), timeout=timeout) as r:
        return r.read().decode("utf-8", "ignore")


def _clean(s: str) -> str:
    return htmlmod.unescape(re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", s))).strip()


def _won(s: str | None) -> int | None:
    if not s:
        return None
    m = re.search(r"-?[\d,]+", s)
    if not m or not re.search(r"\d", m.group(0)):
        return None
    try:
        return int(m.group(0).replace(",", ""))
    except ValueError:
        return None


def _capacity_gb(s: str | None) -> int | None:
    """'256' / '512GB' / '1TB' / '1T' → GB. 단위 없는 숫자는 GB 로 보되 4 이하는 TB 로 본다(스마트초이스가 '1'로 적는 경우)."""
    if not s:
        return None
    m = re.search(r"(\d+)\s*(TB|T|GB|G)?\b", str(s), re.I)
    if not m:
        return None
    n = int(m.group(1))
    unit = (m.group(2) or "").upper()
    if unit.startswith("T") or (not unit and n <= 4):
        return n * 1024
    return n


def _capacity_from_name(name: str) -> int | None:
    """제품명 괄호 '(512GB)', '(1T)', '(256G)' 가 정본 — used_Phone_Capacity 는 RAM 이 섞이는 경우가 있다."""
    m = re.search(r"\((\d+)\s*(TB|T|GB|G)\)", name or "", re.I)
    return _capacity_gb(m.group(1) + m.group(2)) if m else None


def _released(s: str | None) -> str | None:
    m = re.search(r"(\d{4})년\s*(\d{1,2})월", s or "")
    return f"{m.group(1)}-{int(m.group(2)):02d}-01" if m else None


def fetch_products(op, maker: str) -> list[dict]:
    txt = _post(op, PRODUCT_URL, {"dan_Mau": maker, "p_Group": "PHONE", "orderRect": "D", "orderNm": ""})
    try:
        items = json.loads(txt)
    except json.JSONDecodeError:
        _log(f"{maker}: 제품 목록 JSON 파싱 실패 ({txt[:80]!r})")
        return []
    out = []
    for p in items:
        name = p.get("dan_Pc_Name") or ""
        if not TARGET_RE.search(name):
            continue
        code = p.get("skt_Pn") or p.get("kt_Pn") or p.get("lg_Pn")
        if not code:
            continue
        out.append({"maker": maker, "name": name, "code": code, "service": p.get("dan_Service") or "5G",
                    "capacity_gb": _capacity_from_name(name) or _capacity_gb(p.get("used_Phone_Capacity")),
                    "released_at": _released(p.get("used_Phone_Release")), "seq": p.get("dan_Pc_Seq")})
    return out


def _carrier_cells(row_html: str) -> dict[str, str]:
    """<td headers='… divSK'> 형태의 셀을 통신사별 텍스트로."""
    out = {}
    for m in re.finditer(r"<td[^>]*headers=\"[^\"]*div(SK|KT|LG)\"[^>]*>(.*?)</td>", row_html, re.S):
        out[{"SK": "SKT", "KT": "KT", "LG": "LGU+"}[m.group(1)]] = _clean(m.group(2))
    return out


def parse_result(page: str) -> dict:
    """조회 결과 HTML → {retail: {carrier: 원}, tiers: [{tier, plans:{carrier:{name,fee}}, mnp:{}, upgrade:{}, published:{}}]}"""
    tables = re.findall(r"(<table[^>]*>.*?</table>)", page, re.S)
    retail: dict[str, int | None] = {}
    tiers: list[dict] = []
    for t in tables:
        cap = " ".join(re.findall(r"<caption[^>]*>(.*?)</caption>", t, re.S))
        rows = re.findall(r"<tr[^>]*>(.*?)</tr>", t, re.S)
        if "출고가 및 이동통신사별" in cap:
            for r in rows:
                tds = [_clean(c) for c in re.findall(r"<td[^>]*>(.*?)</td>", r, re.S)]
                if len(tds) >= 3:
                    for c, v in zip(CARRIERS, tds[:3]):
                        retail[c] = _won(v)
        elif "요금 구간별" in cap:
            cur = None
            for r in rows:
                ths = [_clean(c) for c in re.findall(r"<th[^>]*>(.*?)</th>", r, re.S)]
                cells = _carrier_cells(r)
                label = " ".join(ths)
                if "요금제 및 월정액" in label:
                    cur = {"tier": ths[0] if ths else "", "plans": {}, "mnp": {}, "upgrade": {}, "select": {}, "published": {}}
                    for c, v in cells.items():
                        fm = re.search(r"월\s*([\d,]+)\s*원", v)
                        cur["plans"][c] = {"name": re.sub(r"월\s*[\d,]+\s*원", "", v).strip() or None,
                                           "fee": int(fm.group(1).replace(",", "")) if fm else None}
                    tiers.append(cur)
                elif cur is None:
                    continue
                elif "선택약정" in label:
                    cur["select"] = {c: _won(v) for c, v in cells.items()}
                elif "기기변경" in label:
                    cur["upgrade"] = {c: _won(v) for c, v in cells.items()}
                elif "번호이동" in label:
                    cur["mnp"] = {c: _won(v) for c, v in cells.items()}
                elif "공시일" in label:
                    cur["published"] = {c: (re.search(r"\d{4}-\d{2}-\d{2}", v).group(0) if re.search(r"\d{4}-\d{2}-\d{2}", v) else None)
                                        for c, v in cells.items()}
    return {"retail": retail, "tiers": tiers}


def fetch_offer_page(op, prod: dict) -> str:
    return _post(op, LIST_URL, {
        "searchType": "searchDantong", "p_Group": "PHONE", "dan_Company": "",
        "dan_Service": prod.get("service") or "5G", "dan_Mau": prod["maker"],
        "productName": prod["code"], "dan_Pc_Name": prod["name"],
        "plan5GChoice": "all", "planLteChoice": "all", "dan_Plan_Code": "",
    })


def pick_tier(tiers: list[dict], carrier: str) -> dict | None:
    """성지 기준 요금제(TARGET_FEE)에 가장 가까운 구간. 지원금 없는 구간은 제외."""
    cands = [t for t in tiers if t["plans"].get(carrier, {}).get("fee") and t["mnp"].get(carrier) is not None]
    if not cands:
        return None
    target = TARGET_FEE[carrier]
    return min(cands, key=lambda t: (abs(t["plans"][carrier]["fee"] - target), -t["plans"][carrier]["fee"]))


def to_offers(prod: dict, parsed: dict, snap: str, source_url: str) -> list[dict]:
    model = normalize(re.sub(r"\s*\([^)]*\)", "", prod["name"]))
    offers = []
    for c in CARRIERS:
        retail = parsed["retail"].get(c)
        tier = pick_tier(parsed["tiers"], c)
        if not retail or not tier:
            continue
        plan = tier["plans"][c]
        pub_mnp, pub_upg = tier["mnp"].get(c), tier["upgrade"].get(c)
        for sub, pub in (("MNP", pub_mnp), ("010신규", pub_mnp), ("기변", pub_upg)):
            if pub is None:
                continue
            offers.append({
                "snapshot_date": snap, "carrier": c, "model_name": model, "storage_gb": prod["capacity_gb"],
                "subscription_type": sub, "color": None, "retail_price": retail,
                "plan_name": plan["name"], "plan_monthly_fee": plan["fee"],
                "subsidy_public": pub, "subsidy_additional": int(round(pub * ADDITIONAL_RATE, -2)),
                "select_discount_24mo": tier["select"].get(c), "contract_months": 24,
                "source_url": source_url, "source_html_hash": None,
                "raw_payload": {"source": "smartchoice", "code": prod["code"], "product": prod["name"],
                                "published": tier["published"].get(c), "tier": tier["tier"],
                                "note": "010신규 는 번호이동 공시값 준용(스마트초이스는 기변/번호이동만 구분)",
                                "tiers": [{"tier": t["tier"], "plan": t["plans"].get(c), "mnp": t["mnp"].get(c),
                                           "upgrade": t["upgrade"].get(c)} for t in parsed["tiers"]]},
            })
    return offers


def load_snapshot(snapshot: dict) -> int:
    init_db()
    n = 0
    with connect() as conn:
        for d in snapshot.get("devices", []):
            upsert_device(conn, d)
        for o in snapshot.get("offers", []):
            upsert_offer(conn, dict(o))
            n += 1
        now = datetime.now(timezone.utc).isoformat()
        for c in CARRIERS:
            log_run(conn, started_at=snapshot.get("fetched_at") or now, finished_at=now, carrier=c,
                    fetched_models=len({(o["model_name"], o.get("storage_gb")) for o in snapshot.get("offers", []) if o["carrier"] == c}),
                    upserted=sum(1 for o in snapshot.get("offers", []) if o["carrier"] == c), changed=0, errors=0,
                    status="success", source_url=LIST_URL)
    return n


def fetch_snapshot() -> dict:
    """스마트초이스에서 대상 단말 전부를 수집해 스냅샷 dict 로 반환(네트워크 필요)."""
    op = _opener()
    with op.open(LIST_URL, timeout=60) as r:   # 세션 쿠키
        r.read()
    snap_date = today_kst().isoformat()
    products: list[dict] = []
    for mk in MAKERS:
        ps = fetch_products(op, mk)
        _log(f"{mk}: 대상 단말 {len(ps)}종")
        products += ps
        time.sleep(SLEEP)
    if not products:
        raise RuntimeError("제품 목록이 비어 있음(사이트 변경 또는 차단)")
    devices: dict[str, dict] = {}
    offers: list[dict] = []
    fails = 0
    for i, p in enumerate(products, 1):
        try:
            parsed = parse_result(fetch_offer_page(op, p))
        except Exception as e:  # noqa: BLE001
            _log(f"  [{i}/{len(products)}] {p['name']} ({p['code']}): 실패 {e!r}")
            fails += 1
            time.sleep(SLEEP)
            continue
        os_ = to_offers(p, parsed, snap_date, LIST_URL + f"#product={p['code']}")
        model = normalize(re.sub(r"\s*\([^)]*\)", "", p["name"]))
        d = devices.setdefault(model, {"model_name": model, "manufacturer": "Apple" if p["maker"] == "애플" else "삼성전자(주)",
                                       "released_at": p["released_at"], "storage_options": [], "aliases": {}})
        if p["capacity_gb"] and p["capacity_gb"] not in d["storage_options"]:
            d["storage_options"].append(p["capacity_gb"])
        d["aliases"][p["code"]] = p["name"]
        _log(f"  [{i}/{len(products)}] {p['name']:26s} → {model:22s} {p['capacity_gb'] or '?':>5}GB 출고 {parsed['retail']} 오퍼 {len(os_)}")
        offers += os_
        time.sleep(SLEEP)
    for d in devices.values():
        d["storage_options"].sort()
    _log(f"수집 완료: 단말 {len(products)}종 → 기종 {len(devices)} · 오퍼 {len(offers)} · 실패 {fails}")
    if not offers:
        raise RuntimeError("수집된 오퍼가 없음")
    return {"source": "smartchoice", "snapshot_date": snap_date, "fetched_at": datetime.now(timezone.utc).isoformat(),
            "devices": list(devices.values()), "offers": offers}


def save_snapshot(snapshot: dict, path: Path | str = SNAPSHOT_PATH) -> None:
    Path(path).write_text(json.dumps(snapshot, ensure_ascii=False, indent=1), encoding="utf-8")


def refresh(prefer_network: bool = True) -> str:
    """app.py/daily_collect 용: 네트워크 수집 → 실패 시 저장된 스냅샷. 반환: 데이터 출처 설명."""
    snap = None
    src = ""
    if prefer_network:
        try:
            snap = fetch_snapshot()
            try:
                save_snapshot(snap)
            except OSError:
                pass   # 컨테이너 읽기전용 FS
            src = f"smartchoice 수집({snap['snapshot_date']})"
        except Exception as e:  # noqa: BLE001
            _log(f"온라인 수집 실패 → 저장된 스냅샷 사용: {e!r}")
    if snap is None and SNAPSHOT_PATH.exists():
        snap = json.loads(SNAPSHOT_PATH.read_text(encoding="utf-8"))
        src = f"저장 스냅샷({snap.get('snapshot_date')})"
    if snap is None:
        raise RuntimeError("공시지원금 스냅샷 없음")
    load_snapshot(snap)
    return src


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--load", help="스냅샷 JSON 을 DB 에 적재만")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--out", default=str(SNAPSHOT_PATH))
    ap.add_argument("--no-db", action="store_true", help="DB 적재 생략(CI 배치용)")
    args = ap.parse_args()
    if args.load:
        snap = json.loads(Path(args.load).read_text(encoding="utf-8"))
        n = load_snapshot(snap)
        _log(f"스냅샷 적재: 오퍼 {n}건 (기준 {snap.get('snapshot_date')})")
        return 0
    snapshot = fetch_snapshot()
    if args.dry_run:
        print(json.dumps(snapshot["devices"], ensure_ascii=False)[:1500])
        return 0
    save_snapshot(snapshot, args.out)
    if not args.no_db:
        n = load_snapshot(snapshot)
        _log(f"DB 적재 {n}건")
    _log(f"스냅샷 저장 {args.out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
