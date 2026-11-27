"""地點庫：查過、確定價格讀得到的地方，給 bot 挑。

2026-11-26: 「我想出去玩」「這週末想去泡溫泉」 always got the same 玫瑰園／瀧乃湯／湯布苑: the bot searched a
few pages each time and only those few showed a price it could read. The owner: 「這是一個很嚴重的問題」.
This keeps every place found once, so the next question starts from many, the least recently recommended first.

  places.py pick --kind 溫泉 [--area 礁溪]     挑候選：最久沒推的排前面（過期、失效的不給）；
                                              要過夜用 --kind 住宿（溫泉飯店也列）
  places.py add --name … --area … --kind … --official <官網> [--price-page <價格頁>]
                                              打開網頁，價格或「免費」讀得到才收進來
  places.py recheck [--days 7]                重開超過幾天沒查的網頁；讀不到就標失效（不刪）
  places.py list

Only this script writes places.json (the plugin refuses a hand edit): the file stays valid, every place
keeps where its prices came from and when they were read.
"""
import argparse
from concurrent.futures import ThreadPoolExecutor
import datetime as dt
import importlib.util
import json
from pathlib import Path
import re
import sqlite3
import sys
import time
import urllib.request

HERE = Path(__file__).resolve().parent
KINDS = ("溫泉", "住宿", "自然步道、看風景", "海邊", "老街、小吃、夜市", "其他")
# 2026-11-27: 「礁溪住一晚」 asked pick --kind 住宿, got 「地點庫沒有」 though seven 礁溪 hot-spring hotels were kept
# as 溫泉, then add --kind 住宿 was refused. A room can be at a 溫泉 place too, so 住宿 picks both.
ALSO = {"住宿": ("溫泉",)}


def kind_problem(kind, required=False):
    """Why --kind is wrong; pick may leave it out, add may not (Antigravity's review: --kind "" got in)."""
    if (kind or required) and kind not in KINDS:
        return f"--kind 要是其中一種：{'、'.join(KINDS)}（要過夜就用 住宿，溫泉飯店也會列出來）"
    return None
STALE_DAYS = 30          # prices not read again for this long are not offered
FREE = re.compile(r"免費入園|免費參觀|免費開放|免費入場|免門票|免收費|不收費|不需門票|無需門票|免入園費"
                  r"|(門票|入園|入場|參觀)[^。\n]{0,6}免費")


def load(name, filename):
    spec = importlib.util.spec_from_file_location(name, HERE / filename)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


PLACES_SCHEMA = """
CREATE TABLE IF NOT EXISTS places (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  name TEXT NOT NULL,
  area TEXT NOT NULL,
  kind TEXT NOT NULL,
  country_code TEXT NOT NULL DEFAULT 'tw',
  official TEXT NOT NULL,
  price_page TEXT NOT NULL,
  phone TEXT NOT NULL DEFAULT '',
  prices_json TEXT NOT NULL DEFAULT '[]',
  free_json TEXT NOT NULL DEFAULT 'false',
  checked_at TEXT NOT NULL,
  added_at TEXT NOT NULL,
  status TEXT NOT NULL DEFAULT 'ok',
  fail_reason TEXT,
  extra_json TEXT NOT NULL DEFAULT '{}'
);
CREATE INDEX IF NOT EXISTS idx_places_kind_status ON places (kind, status);
CREATE INDEX IF NOT EXISTS idx_places_area_status ON places (area, status);
CREATE INDEX IF NOT EXISTS idx_places_country ON places (country_code);
"""

_STANDARD_PLACE_KEYS = {
    "name", "area", "kind", "country_code", "official", "price_page",
    "phone", "prices", "free", "checked_at", "added_at", "status", "fail_reason",
}


def is_sqlite_path(path):
    return Path(path).suffix.lower() in {".db", ".sqlite", ".sqlite3"}


def _open_places_db(path):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(str(path), timeout=10.0)
    con.row_factory = sqlite3.Row
    con.execute("PRAGMA journal_mode = WAL;")
    con.execute("PRAGMA busy_timeout = 10000;")
    con.executescript(PLACES_SCHEMA)
    return con


def _read_places_json(path):
    try:
        data = json.loads(Path(path).read_text(encoding="utf-8-sig"))
    except (OSError, ValueError):
        return []
    places = data.get("places") if isinstance(data, dict) else None
    return [p for p in places if isinstance(p, dict)] if isinstance(places, list) else []


def _row_to_place(row):
    try:
        prices = json.loads(row["prices_json"])
        if not isinstance(prices, list):
            prices = []
    except (TypeError, ValueError):
        prices = []
    try:
        free = json.loads(row["free_json"])
    except (TypeError, ValueError):
        free = False
    try:
        extra = json.loads(row["extra_json"])
        if not isinstance(extra, dict):
            extra = {}
    except (TypeError, ValueError):
        extra = {}
    place = {
        **extra,
        "name": row["name"],
        "area": row["area"],
        "kind": row["kind"],
        "country_code": row["country_code"] or "tw",
        "official": row["official"],
        "price_page": row["price_page"],
        "phone": row["phone"] or "",
        "prices": prices,
        "free": free,
        "checked_at": row["checked_at"],
        "added_at": row["added_at"],
        "status": row["status"],
    }
    if row["fail_reason"] is not None:
        place["fail_reason"] = row["fail_reason"]
    return place


def read_places(path):
    """Read places from places.db (SQLite) or legacy places.json; auto-migrates sibling places.json on first DB read."""
    path = Path(path)
    if not is_sqlite_path(path):
        return _read_places_json(path)
    if not path.exists():
        legacy = path.with_suffix(".json")
        if legacy.exists():
            migrated = _read_places_json(legacy)
            if migrated:
                write_places(path, migrated)
                return read_places(path)
        return []
    con = _open_places_db(path)
    try:
        rows = con.execute(
            "SELECT name, area, kind, country_code, official, price_page, phone, "
            "prices_json, free_json, checked_at, added_at, status, fail_reason, extra_json "
            "FROM places ORDER BY id ASC"
        ).fetchall()
        return [_row_to_place(r) for r in rows]
    finally:
        con.close()


def write_places(path, places):
    """Atomically persist places to places.db (WAL + online backup) or legacy places.json."""
    path = Path(path)
    if not is_sqlite_path(path):
        # The new version first: a failed write leaves places.json as it was, never missing (Antigravity's review).
        temporary = path.with_name(path.name + ".tmp")
        temporary.write_text(json.dumps({"schema_version": 1, "places": places}, ensure_ascii=False, indent=1) + "\n",
                             encoding="utf-8")
        if path.exists():
            backup = path.with_name("places.backup.json")
            backup.write_bytes(path.read_bytes())
        temporary.replace(path)
        return

    if path.exists():
        backup = path.with_name("places.backup.db")
        try:
            src = sqlite3.connect(str(path), timeout=10.0)
            dst = sqlite3.connect(str(backup), timeout=10.0)
            try:
                src.backup(dst)
            finally:
                dst.close()
                src.close()
        except sqlite3.Error:
            pass

    con = _open_places_db(path)
    try:
        with con:
            con.execute("DELETE FROM places")
            rows = []
            for p in places:
                extra = {k: v for k, v in p.items() if k not in _STANDARD_PLACE_KEYS}
                rows.append((
                    str(p.get("name") or ""),
                    str(p.get("area") or ""),
                    str(p.get("kind") or ""),
                    str(p.get("country_code") or "tw"),
                    str(p.get("official") or ""),
                    str(p.get("price_page") or ""),
                    str(p.get("phone") or ""),
                    json.dumps(p.get("prices") if isinstance(p.get("prices"), list) else [], ensure_ascii=False),
                    json.dumps(p.get("free", False), ensure_ascii=False),
                    str(p.get("checked_at") or ""),
                    str(p.get("added_at") or ""),
                    str(p.get("status") or "ok"),
                    p.get("fail_reason"),
                    json.dumps(extra, ensure_ascii=False),
                ))
            con.executemany(
                "INSERT INTO places (name, area, kind, country_code, official, price_page, phone, "
                "prices_json, free_json, checked_at, added_at, status, fail_reason, extra_json) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                rows,
            )
    finally:
        con.close()


def migrate(args):
    """Explicitly migrate legacy places.json into places.db (SQLite), keeping the original JSON intact."""
    src = Path(args.source) if args.source else Path(args.places).with_suffix(".json")
    dst = Path(args.places)
    if not is_sqlite_path(dst):
        return f"目標路徑必須是 SQLite（.db），目前為：{dst}"
    if not src.exists():
        return f"找不到來源 JSON：{src}"
    places = _read_places_json(src)
    for p in places:
        p.setdefault("country_code", "tw")
    write_places(dst, places)
    by_kind = {}
    for p in places:
        if p.get("status") == "ok":
            by_kind[p.get("kind", "?")] = by_kind.get(p.get("kind", "?"), 0) + 1
    print(f"已將 {len(places)} 筆台灣地點從 {src.name} 遷移到 {dst.name}（有效 {sum(by_kind.values())} 筆）。")
    if by_kind:
        print("各類分布：" + "、".join(f"{k} {v}" for k, v in sorted(by_kind.items(), key=lambda kv: -kv[1])))
    return None


def today(now=None):
    return dt.date.fromtimestamp(now or time.time()).isoformat()


def age_days(place, now=None):
    try:
        checked = dt.date.fromisoformat(place.get("checked_at", ""))
    except ValueError:
        return 10 ** 6
    return (dt.date.fromtimestamp(now or time.time()) - checked).days


def read_page(url, args):
    """(title, price lines, phones, says free) for one page, or (None, error)."""
    prices = load("page_prices", "page-prices.py")
    page, error = prices.read(url, args)
    if page is None:
        return None, error
    title, lines, phones = page
    evidence = load("page_evidence", "page_evidence.py")
    text = evidence.latest_page_text(args.hermes_root, url) or ""
    return (title, lines, phones, bool(FREE.search(text))), None


def identity(place):
    plan = load("format_plan", "format-plan.py")
    return plan.place_identity(place.get("name"), place.get("official"), place.get("phone"))


def last_recommended(place, history):
    """When this place was last recommended (0 when never), from format-plan.py's recommended.json."""
    plan = load("format_plan", "format-plan.py")
    me = identity(place)
    return max((float(e.get("at", 0)) for e in history if plan.same_place(me, e)), default=0)


def add(args):
    if kind_problem(args.kind, required=True):
        return kind_problem(args.kind, required=True)
    page_url = args.price_page or args.official
    found, error = read_page(page_url, args)
    if found is None:
        return error
    title, lines, phones, free = found
    if not lines and not free and args.no_fee_listed:
        free = "unlisted"  # the owner chose (2026-11-26): recommend it, saying the page lists no fee
    if not lines and not free:
        return (f"{page_url} 上讀不到價格，也沒寫免費入園：不收進地點庫。換這間官網上寫著價格的那頁（--price-page），"
                "或換一個地方")
    place = {"name": args.name.strip(), "area": args.area.strip(), "kind": args.kind, "official": args.official,
             "price_page": page_url, "phone": args.phone or (phones[0].split()[-1] if phones else ""),
             "prices": lines[:6], "free": free, "checked_at": today(args.now), "status": "ok"}
    places = read_places(args.places)
    plan = load("format_plan", "format-plan.py")
    for i, old in enumerate(places):
        if plan.same_place(identity(old), identity(place)):
            places[i] = {**old, **place, "added_at": old.get("added_at", today(args.now))}
            write_places(args.places, places)
            print(f"更新：{place['name']}（{place['area']}，{len(place['prices'])} 個價格）")
            return None
    places.append({**place, "added_at": today(args.now)})
    write_places(args.places, places)
    print(f"收進地點庫：{place['name']}（{place['area']}・{place['kind']}，"
          + ("官方頁沒列收費" if free == "unlisted" else "免費" if free and not lines
             else f"{len(place['prices'])} 個價格") + "）")
    return None


def candidates(places, history, kind=None, area=None, now=None):
    """Usable places of this kind and area, the least recently recommended first."""
    usable = [p for p in places if p.get("status") == "ok" and age_days(p, now) <= STALE_DAYS
              and (not kind or p.get("kind") in (kind, *ALSO.get(kind, ()))) and (not area or area in p.get("area", ""))]
    return sorted(usable, key=lambda p: (last_recommended(p, history), p.get("name", "")))


def pick(args):
    if kind_problem(args.kind):
        return None, kind_problem(args.kind)
    history = load("format_plan", "format-plan.py").read_history(args.history)
    found = candidates(read_places(args.places), history, args.kind, args.area, args.now)
    if not found:
        return None, (f"地點庫沒有{args.area or ''}{args.kind or ''}的地方：用 web_search 找官網，"
                      "page-prices.py 讀得到價格的，用 places.py add 收進來")
    lines = []
    for place in found[:args.limit]:
        when = last_recommended(place, history)
        # Chinese outside strftime: CI's locale cannot encode it (UnicodeEncodeError on 推).
        said = time.strftime("%m/%d", time.localtime(when)) + " 推過" if when else "沒推過"
        price = (place.get("prices") or [""])[0] or ("官方頁沒列收費" if place.get("free") == "unlisted"
                                                  else "免費" if place.get("free") else "未確認")
        lines.append(f"• {place['name']}｜{place['area']}・{place['kind']}｜{said}｜{price}"
                     f"｜價格頁 {place['price_page']}" + (f"｜📞 {place['phone']}" if place.get("phone") else ""))
    lines.append("建議直接用 travel.py local 一次完成查價與組方案；手動重查則先跑 page-prices.py 再用 format-plan.py --package。")
    return "\n".join(lines), None


def recheck(args):
    places, changed = read_places(args.places), []
    for place in places:
        if age_days(place, args.now) < args.days:
            continue
        found, error = read_page(place["price_page"], args)
        if found is None or not (found[1] or found[3] or (place.get("free") == "unlisted" and found[0])):
            if place.get("status") != "broken":
                place["status"] = "broken"
                changed.append(f"失效：{place['name']}（{error or '讀不到價格'}）")
            continue
        free = found[3] or ("unlisted" if place.get("free") == "unlisted" and not found[1] else False)
        place.update(prices=found[1][:6], free=free, checked_at=today(args.now), status="ok")
        changed.append(f"重查：{place['name']}")
    if changed:
        write_places(args.places, places)
    print("\n".join(changed) or "都在期限內，不用重查")


TAIWAN_COUNTIES = {
    "台北": "0001090", "臺北": "0001090",
    "新北": "0001091",
    "基隆": "0001105",
    "桃園": "0001107",
    "宜蘭": "0001106",
    "新竹": "0001108",
    "台中": "0001112", "臺中": "0001112",
    "台南": "0001119", "臺南": "0001119",
    "高雄": "0001121",
    "花蓮": "0001124",
    "台東": "0001123", "臺東": "0001123",
}
DEFAULT_HARVEST_COUNTIES = ("台北", "新北", "基隆", "桃園", "宜蘭")
OLD_STREET_HINTS = ("老街", "夜市", "廟口", "九份", "平溪", "烏來", "金山", "淡水", "深坑", "三峽", "鶯歌", "大溪老街")
BEACH_HINTS = ("海濱", "漁港", "漁人碼頭", "燈塔", "左岸", "蜜月灣", "沙灘", "海岸", "潮境", "富貴角", "淺水灣", "白沙灣", "金色水岸")
TRAIL_HINTS = ("步道", "瀑布", "國家公園", "茶園", "風景特定區", "風景區", "擎天崗", "冷水坑", "象山", "自然保留區", "運動公園")
SKIP_TITLES = ("遊客中心", "水上運動中心", "溫泉", "博物館", "美術館", "紀念館", "文化館", "森林遊樂區", "地質公園", "海水浴場", "官邸", "樂園")
AREA_TOKENS = (
    "北投", "士林", "淡水", "九份", "瑞芳", "烏來", "金山", "萬里", "石門", "三芝", "平溪", "十分",
    "深坑", "三峽", "鶯歌", "八里", "福隆", "貢寮", "鼻頭", "基隆", "大溪", "桃園", "礁溪", "羅東",
    "宜蘭", "陽明山", "貓空", "華西街", "饒河", "寧夏", "野柳", "富貴角", "淡水",
)


def classify_taiwan_attraction(title, county):
    """Classify an official taiwan.net.tw attraction title into (kind, area) or None if outside scope."""
    t = (title or "").strip()
    if not t or t.startswith("#") or any(s in t for s in SKIP_TITLES):
        return None
    if any(k in t for k in ("老街", "夜市", "廟口")) or t in ("九份", "平溪", "烏來", "金山"):
        kind = "老街、小吃、夜市"
    elif any(k in t for k in BEACH_HINTS):
        kind = "海邊"
    elif any(k in t for k in TRAIL_HINTS):
        kind = "自然步道、看風景"
    else:
        return None
    area = next((tok for tok in AREA_TOKENS if tok in t), county)
    if area in ("九份", "鼻頭"):
        area = "瑞芳"
    elif area == "十分":
        area = "平溪"
    elif area == "野柳":
        area = "萬里"
    elif area == "富貴角":
        area = "石門"
    elif area == "華西街":
        area = "台北萬華"
    elif area == "貓空":
        area = "台北文山"
    elif area == "陽明山":
        area = "台北士林"
    elif county == "宜蘭" and area == "大溪":
        area = "宜蘭頭城"
    return kind, area


def crawl_taiwan_county(county, timeout=12, max_items=12, html_text=None):
    """Fetch official taiwan.net.tw county attraction list and return candidate dicts."""
    sno = TAIWAN_COUNTIES.get(county)
    if not sno:
        return []
    url = f"https://www.taiwan.net.tw/m1.aspx?sNo={sno}"
    if html_text is None:
        prices = load("page_prices", "page-prices.py")
        try:
            html_text, _ = prices.fetch(url, timeout=timeout)
        except Exception:
            return []
    matches = re.findall(r'href="(m1\.aspx\?sNo=0001\d+&amp;id=\d+)"[^>]*title="([^"]+)"', html_text)
    if not matches:
        matches = re.findall(r'href="(m1\.aspx\?sNo=0001\d+&id=\d+)"[^>]*title="([^"]+)"', html_text)
    found, seen = [], set()
    for href, title in matches:
        full_url = "https://www.taiwan.net.tw/" + href.replace("&amp;", "&")
        if full_url in seen:
            continue
        cls = classify_taiwan_attraction(title, county)
        if not cls:
            continue
        kind, area = cls
        seen.add(full_url)
        display_name = title.strip()
        if display_name in ("九份", "平溪", "烏來", "金山"):
            display_name = display_name + "老街"
        found.append({
            "name": display_name,
            "area": area,
            "kind": kind,
            "official": full_url,
            "price_page": full_url,
            "no_fee_listed": True,
        })
        if len(found) >= max_items:
            break
    return found


def harvest(args):
    """Automatically expand & refresh places.json from official gov tourism indexes + curated catalog."""
    raw_counties = (args.counties or "").strip()
    counties = [] if raw_counties.lower() in ("none", "") else [c.strip() for c in raw_counties.split(",") if c.strip()]
    unknown = [c for c in counties if c not in TAIWAN_COUNTIES]
    if unknown:
        return f"不支援的縣市：{'、'.join(unknown)}（支援：{'、'.join(sorted(set(TAIWAN_COUNTIES)))}）"

    candidates_list = []
    seen_urls = set()

    # 1. Load curated official venue catalog (hot springs, verified in-room-tub stays, ticketed parks)
    catalog_path = Path(args.catalog) if args.catalog else (HERE / "places-catalog.json")
    if catalog_path.is_file():
        try:
            cat = json.loads(catalog_path.read_text(encoding="utf-8-sig"))
            for item in (cat.get("places") or []):
                if isinstance(item, dict) and item.get("name") and item.get("price_page"):
                    candidates_list.append(item)
                    seen_urls.add(item["price_page"])
        except (OSError, ValueError) as exc:
            return f"讀取目錄檔 {catalog_path} 失敗：{exc}"

    # 2. Crawl official taiwan.net.tw regional attraction indexes in parallel
    if counties:
        with ThreadPoolExecutor(max_workers=min(5, len(counties))) as pool:
            crawled_groups = list(pool.map(
                lambda c: crawl_taiwan_county(c, timeout=args.timeout, max_items=args.max_per_county),
                counties,
            ))
        for group in crawled_groups:
            for item in group:
                if item["price_page"] not in seen_urls:
                    seen_urls.add(item["price_page"])
                    candidates_list.append(item)

    if not candidates_list:
        return "沒有可抓的候選來源（請指定 --counties 或 --catalog）"

    # 3. Verify every candidate live in parallel through page-prices.py
    def verify_one(item):
        found, err = read_page(item["price_page"], args)
        return item, found, err

    with ThreadPoolExecutor(max_workers=min(6, len(candidates_list))) as pool:
        results = list(pool.map(verify_one, candidates_list))

    places = read_places(args.places)
    plan = load("format_plan", "format-plan.py")
    added, updated, skipped = [], [], []

    for item, found, err in results:
        official_url = item.get("official") or item["price_page"]
        if not official_url.startswith("https://") or not item["price_page"].startswith("https://"):
            skipped.append(f"{item['name']}（非 https 網址，booking-links.py 不接受）")
            continue
        if found is None:
            skipped.append(f"{item['name']}（{err}）")
            continue
        _title, lines, phones, free = found
        if not lines and not free and item.get("no_fee_listed"):
            free = "unlisted"
        if not lines and not free:
            skipped.append(f"{item['name']}（官網讀不到價格）")
            continue
        live_phone = (phones[0].split()[-1] if phones else "")
        place = {
            "name": item["name"].strip(),
            "area": item["area"].strip(),
            "kind": item["kind"],
            "official": item.get("official") or item["price_page"],
            "price_page": item["price_page"],
            "phone": live_phone,
            "prices": lines[:6],
            "free": free,
            "checked_at": today(args.now),
            "status": "ok",
        }
        matched_idx = None
        new_tw_id = re.search(r"[?&]id=(\d+)", place["price_page"])
        for i, old in enumerate(places):
            # Match by exact name first
            if old.get("name") == place["name"]:
                matched_idx = i
                break
            # Distinct taiwan.net.tw attraction IDs are different places even if sharing the Tourism Bureau hotline
            old_tw_id = re.search(r"[?&]id=(\d+)", old.get("price_page") or "")
            if ("taiwan.net.tw" in place["price_page"] and "taiwan.net.tw" in (old.get("price_page") or "")
                    and new_tw_id and old_tw_id and new_tw_id.group(1) != old_tw_id.group(1)):
                continue
            if old.get("kind") == place["kind"] and plan.same_place(identity(old), identity(place)):
                matched_idx = i
                break
        if matched_idx is not None:
            # Prefer live-verified phone; if page had none today, preserve existing phone rather than wiping it
            if not place["phone"] and places[matched_idx].get("phone"):
                place["phone"] = places[matched_idx]["phone"]
            places[matched_idx] = {**places[matched_idx], **place,
                                   "added_at": places[matched_idx].get("added_at", today(args.now))}
            updated.append(place["name"])
        else:
            places.append({**place, "added_at": today(args.now)})
            added.append(place["name"])

    if not args.dry_run and (added or updated):
        write_places(args.places, places)

    by_kind = {}
    for p in places:
        if p.get("status") == "ok":
            by_kind[p.get("kind", "?")] = by_kind.get(p.get("kind", "?"), 0) + 1
    mode = "（預覽模式，未寫入）" if args.dry_run else ""
    print(f"地點庫自動擴充完成{mode}：新增 {len(added)} 筆、更新 {len(updated)} 筆、略過 {len(skipped)} 筆；有效總數 {sum(by_kind.values())} 筆。")
    print("各類分布：" + "、".join(f"{k} {v}" for k, v in sorted(by_kind.items(), key=lambda kv: -kv[1])))
    if skipped:
        print("略過原因：" + "；".join(skipped[:8]))
    return None


def main():
    for stream in (sys.stdout, sys.stderr):
        stream.reconfigure(encoding="utf-8")
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--places", default=HERE / "places.db", help=argparse.SUPPRESS)
    parser.add_argument("--history", default=HERE / "recommended.json", help=argparse.SUPPRESS)
    parser.add_argument("--hermes-root", default=HERE.parent.parent, help=argparse.SUPPRESS)
    parser.add_argument("--html-file", help=argparse.SUPPRESS)  # tests only
    parser.add_argument("--now", type=float, help=argparse.SUPPRESS)  # tests only
    parser.add_argument("--timeout", type=float, default=10, help=argparse.SUPPRESS)
    sub = parser.add_subparsers(dest="command", required=True)
    one = sub.add_parser("add")
    one.add_argument("--name", required=True)
    one.add_argument("--area", required=True, help="地區，例如 礁溪、烏來、北投、金山、九份")
    one.add_argument("--kind", required=True, help="、".join(KINDS))
    one.add_argument("--official", required=True, help="這個地方自己的官網")
    one.add_argument("--price-page", help="官網上寫價格（或寫免費入園）的那頁，沒給就用 --official")
    one.add_argument("--phone")
    one.add_argument("--no-fee-listed", action="store_true",
                     help="景點官方頁讀得到但沒列收費（海邊、步道）：照樣收，挑的時候寫「官方頁沒列收費」，不能寫免費")
    some = sub.add_parser("pick")
    some.add_argument("--kind")
    some.add_argument("--area")
    some.add_argument("--limit", type=int, default=6)
    again = sub.add_parser("recheck")
    again.add_argument("--days", type=int, default=7)
    harv = sub.add_parser("harvest", help="從官方觀光網與精選官網目錄自動抓點、即時驗價並收進地點庫")
    harv.add_argument("--counties", default=",".join(DEFAULT_HARVEST_COUNTIES),
                      help="要抓的縣市，逗號分隔（例如 台北,新北,基隆,桃園,宜蘭，或 none 只跑目錄）")
    harv.add_argument("--catalog", help="精選官網 JSON 目錄（預設 scripts/places-catalog.json）")
    harv.add_argument("--max-per-county", type=int, default=12)
    harv.add_argument("--dry-run", action="store_true", help="只預覽結果，不改寫 places.db")
    mig = sub.add_parser("migrate", help="將舊版 places.json 遷移進 SQLite（places.db）")
    mig.add_argument("--from", dest="source", help="來源 places.json 路徑（預設與 --places 同目錄）")
    sub.add_parser("list")
    args = parser.parse_args()
    if args.command == "add":
        problem = add(args)
    elif args.command == "pick":
        text, problem = pick(args)
        if text:
            print(text)
    elif args.command == "recheck":
        problem = recheck(args)
    elif args.command == "harvest":
        problem = harvest(args)
    elif args.command == "migrate":
        problem = migrate(args)
    else:
        for place in read_places(args.places):
            print(f"{place.get('status', '?'):6} {place.get('checked_at', '?')} {place.get('area', '')}・"
                  f"{place.get('kind', '')} {place.get('name', '')}")
        problem = None
    if problem:
        print(problem, file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
