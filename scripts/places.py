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
import html
from html.parser import HTMLParser
import importlib.util
import io
import json
from pathlib import Path
import re
import sqlite3
import ssl
import sys
import time
import urllib.parse
import urllib.request
import zipfile

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
CREATE TABLE IF NOT EXISTS harvest_checked (
  url TEXT PRIMARY KEY,
  name TEXT NOT NULL,
  outcome TEXT NOT NULL,
  checked_at TEXT NOT NULL
);
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


def read_harvest_checked(path, max_age_days=30, now=None):
    """Return non-ok URLs already probed by Open Data harvest within max_age_days (SQLite only)."""
    path = Path(path)
    if not is_sqlite_path(path) or not path.exists():
        return set()
    cutoff = (dt.date.fromisoformat(today(now)) - dt.timedelta(days=max_age_days)).isoformat()
    con = _open_places_db(path)
    try:
        rows = con.execute(
            "SELECT url FROM harvest_checked WHERE outcome != 'ok' AND checked_at >= ?", (cutoff,)
        ).fetchall()
        return {r["url"] for r in rows if r["url"]}
    finally:
        con.close()


def record_harvest_checked(path, entries, checked_date):
    """Remember probed Open Data URLs so weekly harvests explore new hotels/spots instead of retrying failed sites."""
    path = Path(path)
    if not entries or not is_sqlite_path(path):
        return
    con = _open_places_db(path)
    try:
        with con:
            con.executemany(
                "INSERT INTO harvest_checked (url, name, outcome, checked_at) VALUES (?, ?, ?, ?) "
                "ON CONFLICT(url) DO UPDATE SET name = excluded.name, outcome = excluded.outcome, checked_at = excluded.checked_at",
                [(url, name, outcome, checked_date) for url, name, outcome in entries],
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
OLD_STREET_HINTS = ("老街", "夜市", "廟口", "商圈", "花市", "紅樓", "九份", "平溪", "烏來", "金山", "淡水", "深坑", "三峽", "鶯歌", "大溪老街")
BEACH_HINTS = ("海濱", "漁港", "漁人碼頭", "燈塔", "左岸", "蜜月灣", "沙灘", "海岸", "潮境", "富貴角", "淺水灣", "白沙灣", "金色水岸",
               "海潮", "奇岩", "石門洞", "南方澳", "基隆港", "基隆嶼", "海洋公園")
TRAIL_HINTS = ("步道", "瀑布", "國家公園", "茶園", "風景特定區", "風景區", "擎天崗", "冷水坑", "象山", "自然保留區", "運動公園",
               "親水公園", "植物園", "自然中心", "水庫", "梯田", "綠色走廊", "公園", "露營區", "慈湖", "砲臺", "砲台", "綠園", "地質區",
               "碼頭", "藍色公路")
CULTURE_OPEN_HINTS = ("紀念堂", "忠烈祠", "龍山寺", "保安宮", "行天宮", "城隍廟", "孔廟", "指南宮", "法鼓山", "祖師廟",
                      "慈祐宮", "青山宮", "關渡宮", "王公廟", "天公廟", "昭應宮", "書院")
SKIP_TITLES = ("遊客中心", "旅遊服務中心", "旅服中心", "水上運動中心", "溫泉", "博物館", "美術館", "紀念館", "文化館", "森林遊樂區", "地質公園", "海水浴場", "官邸", "樂園")
AREA_TOKENS = (
    "北投", "士林", "淡水", "九份", "瑞芳", "烏來", "金山", "萬里", "石門", "三芝", "平溪", "十分",
    "深坑", "三峽", "鶯歌", "八里", "福隆", "貢寮", "鼻頭", "基隆", "大溪", "桃園", "礁溪", "羅東",
    "宜蘭", "陽明山", "貓空", "華西街", "饒河", "寧夏", "野柳", "富貴角", "蘇澳", "頭城", "冬山",
    "三星", "大同", "員山", "五結", "壯圍", "南澳", "中壢", "龍潭", "復興", "新屋", "大園",
    "西門", "信義", "大安", "松山", "萬華", "中山", "中正", "關渡", "碧潭", "板橋", "龍洞", "八斗子", "外木山", "南方澳",
)

TOURISM_ATTRACTIONS_ZIP = "https://media.taiwan.net.tw/XMLReleaseAll_public/v2.0/Zh_tw/Attraction-json.zip"
TOURISM_HOTELS_ZIP = "https://media.taiwan.net.tw/XMLReleaseAll_public/v2.0/Zh_tw/Hotel-json.zip"
OPENDATA_CITY_MAP = {
    "台北": ("臺北市", "台北市"), "臺北": ("臺北市", "台北市"),
    "新北": ("新北市",),
    "基隆": ("基隆市",),
    "桃園": ("桃園市",),
    "宜蘭": ("宜蘭縣",),
    "新竹": ("新竹市", "新竹縣"),
    "台中": ("臺中市", "台中市"), "臺中": ("臺中市", "台中市"),
    "台南": ("臺南市", "台南市"), "臺南": ("臺南市", "台南市"),
    "高雄": ("高雄市",),
    "花蓮": ("花蓮縣",),
    "台東": ("臺東縣", "台東縣"), "臺東": ("臺東縣", "台東縣"),
}
SOCIAL_HOSTS = ("facebook.com", "fb.com", "instagram.com", "line.me", "youtube.com", "youtu.be", "twitter.com", "x.com")
HOT_SPRING_HINTS = ("溫泉", "湯屋", "冷泉", "湯泉", "湯旅", "湯宿", "溫泉會館", "溫泉飯店")
HOT_SPRING_DESC_FACILITY_RE = re.compile(r"館內溫泉|附設湯屋|專屬湯屋|私人湯屋|露天風呂|大眾湯池|獨立湯屋|客房內泡湯")
AMBIGUOUS_DISTRICTS = ("中正", "中山", "信義", "大安", "松山", "大同")
PRIORITY_TOWNS = ("礁溪", "烏來", "北投", "金山", "萬里", "淡水", "瑞芳", "九份", "羅東", "冬山", "頭城", "蘇澳", "大溪", "中正", "大安", "信義", "中山", "萬華", "板橋")
_HOTEL_REAL_RATE_RE = re.compile(
    r"雙人房|四人房|三人房|單人房|家庭房|套房|客房|房型|房價|包棟|住宿|每晚|／晚|/晚|湯屋|泡湯|大眾池|露天風呂|風呂|暖湯|定價|平日|假日|參考房價|2\s*人|雙人"
)
_HOTEL_NON_RATE_RE = re.compile(
    r"加人|加床|加價規定|加價費用|第三人加價|加棉被|加枕頭|人頭費|延退|延遲退房|逾時|清潔費|押金|保證金|賠償|遺失|工本費|小孩滿|罰鍰|補助|折抵|獎助|優惠券|折扣|立減|現折|住宿券|餐券|蛋糕|甜點|停車|導覽|寵物|毛孩|體型|手續費|手績費|場地設備|防疫旅館|居家檢疫|USD|points",
    re.I,
)
_ATTR_TICKET_RE = re.compile(r"全票|半票|門票|入園|入場|參觀|單程|來回|票價|優惠票|敬老票|兒童票|學生票|團體票|DIY|體驗")
_ECOMMERCE_NOISE_RE = re.compile(r"0\s*個商品|免運|運費|購物金|建議售價|熱門商品|購物車|訂金|宅配|超取|爆米花|巧克力|乳液|蛋糕|熱銷組")


def _extract_amounts(text):
    tm = load("travel_money", "travel_money.py")
    nums = []
    for tok in tm.PRICE.findall(text or ""):
        digits = re.sub(r"\D", "", tok)
        if digits:
            nums.append(int(digits))
    return nums


def filter_opendata_price_lines(source, lines):
    """Keep only genuine stay/spa rates or attraction tickets; reject e-commerce shipping, fines, and surcharges."""
    kept = []
    for line in lines:
        nums = _extract_amounts(line)
        if source == "opendata_hotel":
            if _HOTEL_NON_RATE_RE.search(line) or _ECOMMERCE_NOISE_RE.search(line):
                continue
            if not _HOTEL_REAL_RATE_RE.search(line):
                continue
            if not any(n >= 300 for n in nums):
                continue
            kept.append(line)
        elif source == "opendata_attraction":
            if _ECOMMERCE_NOISE_RE.search(line):
                continue
            if not _ATTR_TICKET_RE.search(line):
                continue
            if not any(n >= 10 for n in nums):
                continue
            kept.append(line)
        else:
            kept.append(line)
    return kept


def classify_taiwan_attraction(title, county):
    """Classify an official taiwan.net.tw attraction title into (kind, area) or None if outside scope."""
    t = (title or "").strip()
    if not t or t.startswith("#") or any(s in t for s in SKIP_TITLES):
        return None
    if any(k in t for k in ("老街", "夜市", "廟口", "商圈", "花市", "紅樓")) or t in ("九份", "平溪", "烏來", "金山"):
        kind = "老街、小吃、夜市"
    elif any(k in t for k in BEACH_HINTS):
        kind = "海邊"
    elif any(k in t for k in TRAIL_HINTS):
        kind = "自然步道、看風景"
    elif any(k in t for k in CULTURE_OPEN_HINTS):
        kind = "其他"
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
    elif area in ("華西街", "西門", "萬華"):
        area = "台北萬華"
    elif area == "貓空":
        area = "台北文山"
    elif area == "陽明山":
        area = "台北士林"
    elif area in ("龍洞",):
        area = "貢寮"
    elif area in ("八斗子", "外木山"):
        area = "基隆"
    elif area == "南方澳":
        area = "蘇澳"
    elif county == "宜蘭" and area == "大溪":
        area = "宜蘭頭城"
    elif area in AMBIGUOUS_DISTRICTS:
        area = f"{county.replace('臺', '台')}{area}"
    return kind, area


def _clean_https_url(raw):
    url = (raw or "").strip()
    if not url.startswith("https://") or len(url) > 300:
        return None
    low = url.lower()
    if any(host in low for host in SOCIAL_HOSTS):
        return None
    return url


def _opendata_area(postal, fallback_county):
    city = (postal.get("City") or "").replace("臺", "台")
    town = (postal.get("Town") or "").strip()
    short_city = city.rstrip("縣市") or fallback_county
    short_town = re.sub(r"[區鄉鎮市]$", "", town) if town else ""
    if short_town in ("礁溪", "烏來", "北投", "金山", "萬里", "淡水", "瑞芳", "平溪", "貢寮", "三峽", "鶯歌", "深坑", "八里", "羅東", "蘇澳", "頭城", "冬山", "大溪", "士林", "石門", "三芝"):
        return short_town
    if short_town and short_town != short_city:
        return f"{short_city}{short_town}"
    return short_city


def fetch_tourism_zip_json(url, timeout=30):
    """Download a Taiwan Tourism Administration V2.1 JSON ZIP archive and return its record list."""
    ctx = ssl.create_default_context()
    if hasattr(ssl, "VERIFY_X509_STRICT"):
        ctx.verify_flags &= ~ssl.VERIFY_X509_STRICT
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0 (compatible; hermes-travel-workflow/2.0)"})
    raw = urllib.request.urlopen(req, timeout=timeout, context=ctx).read()
    with zipfile.ZipFile(io.BytesIO(raw)) as zf:
        jnames = [n for n in zf.namelist() if n.lower().endswith(".json")]
        if not jnames:
            return []
        data = json.loads(zf.read(jnames[0]).decode("utf-8-sig"))
    for value in data.values():
        if isinstance(value, list):
            return value
    return []


def parse_opendata_attractions(items, counties, max_items=40, skip_urls=()):
    """Extract live-verifiable HTTPS attractions from Tourism V2.1 AttractionList.json."""
    target_cities = {}
    for c in counties:
        for city_name in OPENDATA_CITY_MAP.get(c, ()):
            target_cities[city_name] = c.replace("臺", "台")
    skip = set(skip_urls)
    found, seen = [], set()
    for rec in items:
        if not isinstance(rec, dict):
            continue
        postal = rec.get("PostalAddress") or {}
        city_key = postal.get("City") or ""
        county = target_cities.get(city_key)
        if not county:
            continue
        name = (rec.get("AttractionName") or "").strip()
        url = _clean_https_url(rec.get("WebsiteURL"))
        if not name or not url or url in skip or url in seen or "遊客中心" in name:
            continue
        seen.add(url)
        if any(k in name for k in HOT_SPRING_HINTS):
            kind = "溫泉"
        elif any(k in name for k in ("老街", "夜市", "廟口", "商圈")):
            kind = "老街、小吃、夜市"
        elif any(k in name for k in BEACH_HINTS):
            kind = "海邊"
        elif any(k in name for k in (*TRAIL_HINTS, "森林", "山", "湖", "谷", "農場", "牧場", "生態")):
            kind = "自然步道、看風景"
        else:
            kind = "其他"
        fee_info = (rec.get("FeeInfo") or "").strip()
        is_explicit_free = (rec.get("IsAccessibleForFree") in (1, True)
                            and bool(re.search(r"免費入園|免費入場|免費參觀|全館免費|無門票|免門票|免收費", fee_info))
                            and not _extract_amounts(fee_info)
                            and not re.search(r"另計|另收|部分|停車", fee_info))
        tels = [t.get("Tel", "").strip() for t in (rec.get("Telephones") or []) if isinstance(t, dict) and t.get("Tel")]
        found.append({
            "name": name,
            "area": _opendata_area(postal, county),
            "kind": kind,
            "official": url,
            "price_page": url,
            "phone": tels[0] if tels else "",
            "no_fee_listed": is_explicit_free,
            "source": "opendata_attraction",
        })
        if len(found) >= max_items:
            break
    return found


def parse_opendata_hotels(items, counties, max_items=60, skip_urls=()):
    """Extract live-verifiable HTTPS legal hotels & hot-spring inns from Tourism V2.1 HotelList.json."""
    target_cities = {}
    for c in counties:
        for city_name in OPENDATA_CITY_MAP.get(c, ()):
            target_cities[city_name] = c.replace("臺", "台")
    skip = set(skip_urls)
    scored, seen = [], set()
    for rec in items:
        if not isinstance(rec, dict):
            continue
        postal = rec.get("PostalAddress") or {}
        city_key = postal.get("City") or ""
        county = target_cities.get(city_key)
        if not county:
            continue
        name = (rec.get("HotelName") or "").strip()
        url = _clean_https_url(rec.get("WebsiteURL"))
        if not name or not url or url in skip or url in seen:
            continue
        seen.add(url)
        desc = (rec.get("Description") or "")
        is_hot_spring = any(k in name for k in HOT_SPRING_HINTS) or bool(HOT_SPRING_DESC_FACILITY_RE.search(desc[:200]))
        area = _opendata_area(postal, county)
        priority = (0 if is_hot_spring else 1, 0 if any(t in area for t in PRIORITY_TOWNS) else 1, name)
        tels = [t.get("Tel", "").strip() for t in (rec.get("Telephones") or []) if isinstance(t, dict) and t.get("Tel")]
        scored.append((priority, {
            "name": name,
            "area": area,
            "kind": "溫泉" if is_hot_spring else "住宿",
            "official": url,
            "price_page": url,
            "phone": tels[0] if tels else "",
            "no_fee_listed": False,  # hotels MUST show live readable prices on their official site
            "source": "opendata_hotel",
        }))
    scored.sort(key=lambda row: row[0])
    return [item for _, item in scored[:max_items]]


_NEW_TAIPEI_ID_RE = re.compile(r"^Attraction_382000000A_(\d+)$")
_CITY_PORTAL_SKIP_EXTRA = ("農場", "牧場", "莊園", "觀光工廠", "百貨", "商場", "購物", "故事館", "體驗館", "影城", "戲院", "劇場", "展覽館", "運動中心", "會館", "火車站", "捷運站", "轉運站", "服務中心")
_CITY_PORTAL_BEACH_HINTS = (
    *BEACH_HINTS, "陰陽海", "海灣", "海岬", "岬角", "礁", "海濱", "濱海",
)
_CITY_PORTAL_SCENIC_STATIONS = ("猴硐車站", "三貂嶺車站", "十分車站", "平溪車站", "菁桐車站", "望古車站", "八斗子車站")
_CITY_PORTAL_TRAIL_HINTS = (
    *TRAIL_HINTS, "古道", "觀景", "不厭亭", "涼亭", "桃源谷", "山谷", "吊橋", "神社",
    *_CITY_PORTAL_SCENIC_STATIONS, "煤礦", "濕地", "溼地", "隧道", "河濱", "老樹", "秘境",
)
_CITY_PORTAL_ICONIC_RE = re.compile(
    r"不厭亭|陰陽海|桃源谷|神社|步道|古道|瀑布|燈塔|海岸|海灣|漁港|吊橋|老街|猴硐車站|三貂嶺車站|煤礦|觀景|隧道|沙灘|濕地|溼地"
)
_CITY_PORTAL_PAID_FEE_RE = re.compile(r"門票|收費|全票|票價|入場費|入園費|需購票|購買")
_CITY_PORTAL_FREE_ALL_RE = re.compile(r"免費參觀|免費入場|免費入園|免費開放|免門票|免收費|不收費|無收費|無需門票|不需門票|全館免費|^\s*免費\s*$")
_CITY_PORTAL_PARTIAL_FREE_RE = re.compile(r"需購買|購買門票|另計|另收|部分收費|部分區域收費|部分展區|停車收費|停車另計|停車另收|兒童免費|幼童免費|未滿\d+歲免費")


def _city_portal_is_paid(fee_info):
    """True when FeeInfo indicates paid or partially-free admission (never treat as open free spot)."""
    if not fee_info:
        return False
    if _extract_amounts(fee_info) or _CITY_PORTAL_PARTIAL_FREE_RE.search(fee_info):
        return True
    if _CITY_PORTAL_PAID_FEE_RE.search(fee_info):
        return not _CITY_PORTAL_FREE_ALL_RE.search(fee_info)
    return False


def _city_portal_title_matches(item_name, page_title):
    """Verify a derived city-portal page title actually belongs to the expected attraction (not a homepage/404 redirect)."""
    t = html.unescape(page_title or "").replace("臺", "台").strip()
    if not t or "404" in t or "找不到" in t:
        return False
    core = re.sub(r"[（(][^）)]*[）)]", "", item_name or "").replace("臺", "台").strip()
    if core and core in t:
        return True
    # Also accept first token before space when official name combines two segments (e.g. "內溝山 翠湖步道")
    first = core.split()[0] if core else ""
    return len(first) >= 2 and first in t


def city_portal_url(rec):
    """Derive official city tourism portal HTTPS URL when Attraction-json.zip omits WebsiteURL."""
    aid = (rec.get("AttractionID") or "").strip()
    m = _NEW_TAIPEI_ID_RE.match(aid)
    if m:
        return f"https://newtaipei.travel/zh-tw/attractions/detail/{m.group(1)}"
    return None


def _portal_item_id(url):
    """Return (portal_host, numeric_id) for multi-attraction official tourism portals."""
    u = url or ""
    if "taiwan.net.tw" in u:
        m = re.search(r"[?&]id=(\d+)", u)
        return ("taiwan.net.tw", m.group(1)) if m else None
    if "newtaipei.travel" in u:
        m = re.search(r"/attractions/detail/(\d+)", u)
        return ("newtaipei.travel", m.group(1)) if m else None
    return None


def parse_city_portal_attractions(items, counties, max_items=30, skip_urls=()):
    """Extract open public scenic spots with derived official city-portal URLs (e.g. newtaipei.travel)."""
    target_cities = {}
    for c in counties:
        for city_name in OPENDATA_CITY_MAP.get(c, ()):
            target_cities[city_name] = c.replace("臺", "台")
    skip = set(skip_urls)
    scored, seen = [], set()
    for rec in items:
        if not isinstance(rec, dict):
            continue
        postal = rec.get("PostalAddress") or {}
        county = target_cities.get(postal.get("City") or "")
        if not county:
            continue
        if _clean_https_url(rec.get("WebsiteURL")):
            continue  # already covered by parse_opendata_attractions
        url = city_portal_url(rec)
        name = (rec.get("AttractionName") or "").strip()
        if not name or not url or url in skip or url in seen:
            continue
        if any(s in name for s in (*SKIP_TITLES, *_CITY_PORTAL_SKIP_EXTRA)):
            continue
        fee_info = (rec.get("FeeInfo") or "").strip()
        if _city_portal_is_paid(fee_info):
            continue  # never admit paid or partial-free venues as open unlisted spots
        if any(k in name for k in ("老街", "夜市", "廟口")):
            kind = "老街、小吃、夜市"
        elif any(k in name for k in _CITY_PORTAL_BEACH_HINTS):
            kind = "海邊"
        elif any(k in name for k in _CITY_PORTAL_TRAIL_HINTS):
            kind = "自然步道、看風景"
        else:
            continue
        seen.add(url)
        area = _opendata_area(postal, county)
        is_iconic = bool(_CITY_PORTAL_ICONIC_RE.search(name))
        is_priority_town = any(t in area for t in PRIORITY_TOWNS)
        tels = [t.get("Tel", "").strip() for t in (rec.get("Telephones") or []) if isinstance(t, dict) and t.get("Tel")]
        priority = (0 if is_iconic else 1, 0 if is_priority_town else 1, name)
        scored.append((priority, {
            "name": name,
            "area": area,
            "kind": kind,
            "official": url,
            "price_page": url,
            "phone": tels[0] if tels else "",
            "no_fee_listed": True,
            "source": "city_portal",
        }))
    scored.sort(key=lambda row: row[0])
    return [item for _, item in scored[:max_items]]


DISCOVER_QUERIES = (
    ("礁溪 雙人湯屋 價目表 官網", "礁溪", "溫泉"),
    ("北投 湯屋 價目表 官網", "北投", "溫泉"),
    ("烏來 溫泉 湯屋 官網", "烏來", "溫泉"),
    ("金山 溫泉 湯屋 價目表 官網", "金山", "溫泉"),
    ("陽明山 溫泉 湯屋 價目表 官網", "台北士林", "溫泉"),
    ("礁溪 溫泉飯店 房型 房價 官網", "礁溪", "溫泉"),
    ("九份 民宿 房價 官網", "瑞芳", "住宿"),
    ("淡水 飯店 房型 房價 官網", "淡水", "住宿"),
    ("宜蘭 觀光農場 門票 票價 官網", "宜蘭", "自然步道、看風景"),
    ("桃園 景觀農場 門票 票價 官網", "桃園", "自然步道、看風景"),
    ("基隆 景點 門票 官網", "基隆", "海邊"),
    ("宜蘭 溫泉民宿 房價 官網", "宜蘭", "溫泉"),
)
DISCOVER_BLOCKED_HOSTS = (
    *SOCIAL_HOSTS,
    "duckduckgo.com", "google.com", "bing.com", "yahoo.com", "baidu.com",
    "klook.com", "kkday.com", "agoda.com", "booking.com", "trip.com", "expedia.com",
    "hotels.com", "eztravel.com.tw", "asiayo.com", "gomaji.com", "17life.com",
    "funtime.com.tw", "kayak.com", "kayak.com.tw", "trivago.com", "trivago.com.tw", "tripadvisor.com",
    "tripadvisor.com.tw", "liontravel.com", "colatour.com.tw", "settour.com.tw",
    "startravel.com.tw", "airbnb.com", "airbnb.com.tw", "owlting.com", "dearbnb.com",
    "okgo.tw", "travelking.com.tw", "taiwanstay.net.tw", "easytravel.com.tw",
    "hotel.com.tw", "fourway.com.tw", "fun-taiwan.com", "taiwan.net.tw", "newtaipei.travel",
    "travel.taipei", "travel.tycg.gov.tw", "travel.yilan.tw", "tour.klcg.gov.tw",
    "travel.taichung.gov.tw", "twtainan.net", "khh.travel",
    "expedia.com.tw", "booking.com.tw", "agoda.com.tw", "trip.com.tw",
    "pixnet.net", "popdaily.com.tw", "walkerland.com.tw", "bjsmile.tw", "travelpark.tw",
    "curly.com.tw", "welcometw.com", "keke.tw", "vocus.cc", "medium.com", "wordpress.com",
    "blogspot.com", "wixsite.com", "weebly.com", "bobowin.blog", "bunnyann.com",
    "mimihan.tw", "fullfenblog.tw", "yoke918.com", "upssmile.com", "alinalife.tw",
    "girlstyle.com", "beauty321.com", "elle.com", "vogue.com.tw", "harpersbazaar.com",
    "marieclaire.com.tw", "cosmopolitan.com", "foodytw.com", "travel.yam.com",
    "newtalk.tw", "storm.mg", "gvm.com.tw", "cw.com.tw", "businessweekly.com.tw",
    "bnext.com.tw", "commonhealth.com.tw", "niusnews.com", "juksy.com", "look-in.com.tw",
    "udn.com", "ettoday.net", "tvbs.com.tw", "cna.com.tw", "nownews.com", "ltn.com.tw",
    "chinatimes.com", "setn.com", "mirrormedia.mg", "mobile01.com", "ptt.cc", "dcard.tw",
    "wikipedia.org", "threads.net", "tiktok.com", "shopee.tw", "momoshop.com.tw",
    "pchome.com.tw", "books.com.tw", "ruten.com.tw", "costco.com.tw", "funnow.com",
    "myfunnow.com", "niceday.tw", "accupass.com", "ibon.com.tw",
)
_LISTICLE_TITLE_RE = re.compile(
    r"\d+\s*[間家個處大選]|推薦|懶人包|總整理|精選|必去|必住|必泡|排名|TOP\s*\d+|攻略|評比|心得|開箱|住宿券|團購|折價券|優惠碼|代訂|比價|區飯店|飯店[，,]\s*\$|住宿[，,]\s*\$",
    re.I,
)
_FOREIGN_TITLE_RE = re.compile(r"日本|東京|大阪|京都|沖繩|北海道|韓國|首爾|釜山|香港|澳門|泰國|曼谷|新加坡")
_OFF_NORTH_AREA_RE = re.compile(r"南投|清境|日月潭|溪頭|埔里|嘉義|阿里山|彰化|雲林|台南|臺南|高雄|屏東|墾丁|花蓮|台東|臺東|澎湖|金門|馬祖|苗栗|新竹|台中|臺中")
_AFFILIATE_BLOG_RE = re.compile(r"klook|kkday|購票碼|折扣碼|優惠碼|點這裡|點此購票|點此預訂", re.I)
_BLOG_PATH_RE = re.compile(r"/(blog|article|articles|news|post|posts|archives|tag|category)/", re.I)
_GENERIC_TITLE_SEGS = {
    "首頁", "官網", "官方網站", "價目表", "房型介紹", "房型", "房價", "房價表", "湯屋", "溫泉",
    "泡湯", "室內湯屋", "景觀湯屋", "雙人湯屋", "獨立湯屋", "露天風呂", "大眾池", "收費標準",
    "票價", "門票", "票價資訊", "票價營業資訊", "票價與交通資訊", "最新消息", "交通資訊", "設施介紹", "住宿", "客房", "客房介紹",
    "湯屋介紹", "泡湯介紹", "溫泉湯屋", "溫泉設施", "home", "index", "price", "rates",
}
_PRICE_URL_HINT_RE = re.compile(r"price|rate|tariff|spring|bath|room|ticket|fac|service|湯屋|房價|門票", re.I)


class _DDGResultParser(HTMLParser):
    """Minimal HTML parser extracting result__a links and titles from DuckDuckGo HTML pages."""

    def __init__(self):
        super().__init__()
        self.results = []
        self._href = None
        self._buf = []

    def handle_starttag(self, tag, attrs):
        if tag != "a":
            return
        attr_map = dict(attrs)
        cls = attr_map.get("class") or ""
        if "result__a" in cls.split():
            self._href = attr_map.get("href") or ""
            self._buf = []

    def handle_data(self, data):
        if self._href is not None:
            self._buf.append(data)

    def handle_endtag(self, tag):
        if tag == "a" and self._href is not None:
            title = re.sub(r"\s+", " ", "".join(self._buf)).strip()
            self.results.append((self._href, title))
            self._href = None
            self._buf = []


def _unwrap_ddg_url(raw_href):
    href = (raw_href or "").strip()
    if href.startswith("//"):
        href = "https:" + href
    parsed = urllib.parse.urlparse(href)
    qs = urllib.parse.parse_qs(parsed.query)
    if "uddg" in qs and qs["uddg"]:
        return qs["uddg"][0]
    return href


def ddg_search(query, timeout=12):
    """Query DuckDuckGo HTML endpoint (Taiwan Traditional Chinese) and return [(url, title)]."""
    q = urllib.parse.quote_plus(query)
    url = f"https://html.duckduckgo.com/html/?q={q}&kl=tw-tzh"
    req = urllib.request.Request(
        url,
        headers={
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                          "(KHTML, like Gecko) Chrome/130.0.0.0 Safari/537.36",
            "Accept-Language": "zh-TW,zh;q=0.9,en;q=0.5",
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            if getattr(resp, "status", 200) != 200:
                return []
            body = resp.read().decode("utf-8", errors="replace")
    except Exception:
        return []
    parser = _DDGResultParser()
    parser.feed(body)
    return [(_unwrap_ddg_url(href), title) for href, title in parser.results if href]


def _norm_host(url):
    try:
        host = (urllib.parse.urlparse(url).hostname or "").lower().strip(".")
    except ValueError:
        return ""
    return host[4:] if host.startswith("www.") else host


def is_official_venue_url(url, title=""):
    """True only for clean HTTPS Taiwan official venue pages (never ads, OTAs, blogs, news, or listicles)."""
    u = (url or "").strip()
    if not u.startswith("https://") or len(u) > 300:
        return False
    parsed = urllib.parse.urlparse(u)
    host = (parsed.hostname or "").lower().strip(".")
    if not host or "." not in host:
        return False
    if any(host == bad or host.endswith("." + bad) for bad in DISCOVER_BLOCKED_HOSTS):
        return False
    if host.endswith((".jp", ".kr", ".cn", ".hk", ".mo")):
        return False
    path = (parsed.path or "").lower()
    if _BLOG_PATH_RE.search(path) or path.endswith((".pdf", ".jpg", ".jpeg", ".png", ".webp", ".zip")):
        return False
    t = (title or "").strip()
    if t and (_LISTICLE_TITLE_RE.search(t) or _FOREIGN_TITLE_RE.search(t) or t.count("。") >= 2):
        return False
    return True


_VENUE_BRAND_RE = re.compile(
    r"酒店|飯店|溫泉|會館|渡假村|度假村|旅店|行館|民宿|山莊|莊園|農場|牧場|樂園|博物館|澡堂|旅宿|文旅|商旅|加賀屋|老爺|晶泉|寒沐|馥蘭朵|麗緻"
)
_SLOGAN_SEG_RE = re.compile(r"首選|推薦|必去|必住|必泡|價目|收費|專案|優惠|介紹|票價|門票|購票|營業資訊|交通資訊|⟫|/|、")


def clean_discovered_name(title):
    """Extract concise Chinese venue name from an official webpage <title>."""
    raw = re.sub(r"\s+", " ", html.unescape(title or "")).strip()
    if not raw:
        return ""
    parts = [
        p.strip()
        for p in re.split(r"\s*[｜|│丨—–]\s*|\s+-\s+|(?<=[\u4e00-\u9fff])-(?=[\u4e00-\u9fff])|::|：", raw)
        if p.strip()
    ]
    cleaned_parts = []
    for seg in parts:
        c = re.sub(r"^(歡迎來到|歡迎光臨|welcome to)\s*", "", seg, flags=re.I)
        c = re.sub(r"(官方網站|官網|首頁)\s*$", "", c).strip()
        if c:
            cleaned_parts.append(c)
    picked = ""
    # Prefer an explicit venue brand segment (e.g. "獨立湯屋 - 北投老爺酒店" -> "北投老爺酒店")
    for seg in cleaned_parts:
        if seg.lower() not in _GENERIC_TITLE_SEGS and _VENUE_BRAND_RE.search(seg) and not _SLOGAN_SEG_RE.search(seg):
            picked = seg
            break
    if not picked:
        for seg in cleaned_parts:
            if seg.lower() not in _GENERIC_TITLE_SEGS:
                picked = seg
                break
    if not picked and cleaned_parts:
        picked = cleaned_parts[0]
    # Trim trailing English subtitle when a clear Chinese name is already present
    m = re.match(r"^([\u4e00-\u9fff][\u4e00-\u9fff0-9・．\s]{1,20}?)\s+[A-Za-z][A-Za-z0-9\s'&.-]{2,}$", picked)
    if m:
        picked = m.group(1).strip()
    picked = re.sub(r"[【】\[\]《》「」]", "", picked).strip()
    if len(picked) > 26:
        picked = picked[:26].strip()
    return picked if len(picked) >= 2 else ""


_DISCOVER_SKIP_TITLES = (
    *(s for s in SKIP_TITLES if s != "溫泉"),
    "臺北旅遊網", "台北旅遊網", "新北市觀光旅遊網", "基隆旅遊網", "桃園觀光導覽網", "宜蘭勁好玩",
)
_AREA_COUNTY_HINTS = {
    "礁溪": ("礁溪", "宜蘭"),
    "宜蘭": ("宜蘭", "礁溪", "羅東", "冬山", "頭城", "蘇澳", "員山", "三星", "五結", "壯圍"),
    "北投": ("北投", "台北", "臺北", "陽明山"),
    "台北士林": ("士林", "陽明山", "菁山", "台北", "臺北"),
    "烏來": ("烏來", "新北"),
    "金山": ("金山", "萬里", "新北"),
    "瑞芳": ("瑞芳", "九份", "金瓜石", "新北"),
    "淡水": ("淡水", "新北"),
    "桃園": ("桃園", "中壢", "大溪", "楊梅", "龍潭", "新屋", "大園", "復興", "埔心"),
    "基隆": ("基隆", "八斗子", "和平島", "外木山"),
}
_ADDRESS_SNIPPET_RE = re.compile(r"(?:地址|位於|位在|交通位置|ADD|ADDRESS)\s*[：:｜|]?\s*([^\n。]{4,60})", re.I)
_TW_FULL_ADDR_RE = re.compile(
    r"(?:台|臺)(?:北|中|南|東)市[\u4e00-\u9fff]{1,4}[區鄉鎮市]|(?:新北|基隆|桃園|新竹|苗栗|彰化|南投|雲林|嘉義|高雄|屏東|宜蘭|花蓮|澎湖|金門)[縣市][\u4e00-\u9fff]{1,4}[區鄉鎮市]"
)
_OFF_NORTH_PHONE_RE = re.compile(r"\b(?:0[45678][-\s]?\d|03[-\s]?8\d|037[-\s]?\d|049[-\s]?\d|089[-\s]?\d)")


def _read_page_text(args, url):
    """Fetch cached plain text of a recently read page for address/area verification."""
    evidence = load("page_evidence", "page_evidence.py")
    return evidence.latest_page_text(args.hermes_root, url) or ""


def _verify_discovered_area(name, raw_title, page_text, phones, default_area):
    """Return a confirmed area from title or explicit address; never guess from phone prefix or loose body copy."""
    title_block = f"{name} {raw_title}"
    addr_parts = _ADDRESS_SNIPPET_RE.findall(page_text or "") + _TW_FULL_ADDR_RE.findall(page_text or "")
    addr_hits = " ".join(addr_parts)
    # 1. Phone area code is ONLY used to exclude mismatched regions (never to confirm a district)
    phone_str = " ".join(phones or ())
    if not _OFF_NORTH_AREA_RE.search(default_area) and _OFF_NORTH_PHONE_RE.search(phone_str):
        return None
    # 2. Reject if title or explicit address clearly points outside northern Taiwan
    if not _OFF_NORTH_AREA_RE.search(default_area):
        if _OFF_NORTH_AREA_RE.search(title_block):
            return None
        if addr_hits and _OFF_NORTH_AREA_RE.search(addr_hits) and not any(
            tok in addr_hits for tok in _AREA_COUNTY_HINTS.get(default_area, (default_area,))
        ):
            return None
    # 3. Confirm specific northern area strictly from title or explicit address blocks (never broad footer/body text)
    for scope in (title_block, addr_hits):
        if not scope:
            continue
        inferred = _infer_discovered_area(scope, "")
        if inferred:
            return inferred
    # 4. Fall back to default_area only if title or explicit address confirms the target county
    expected = _AREA_COUNTY_HINTS.get(default_area, (default_area,))
    if any(tok in f"{title_block} {addr_hits}" for tok in expected):
        return default_area
    return None


def _infer_discovered_area(text, default_area):
    # Check specific towns/districts before broad county names (e.g. "宜蘭縣冬山鄉" -> "冬山", not "宜蘭")
    county_tokens = ("宜蘭", "桃園", "基隆")
    ordered = [t for t in AREA_TOKENS if t not in county_tokens] + [t for t in county_tokens if t in AREA_TOKENS]
    for tok in ordered:
        if tok in text:
            if tok in ("九份", "鼻頭"):
                return "瑞芳"
            if tok == "十分":
                return "平溪"
            if tok == "野柳":
                return "萬里"
            if tok == "富貴角":
                return "石門"
            if tok == "陽明山":
                return "台北士林"
            if tok == "貓空":
                return "台北文山"
            if tok in ("八斗子", "外木山"):
                return "基隆"
            if tok == "南方澳":
                return "蘇澳"
            return tok
    return default_area


def discover(args):
    """Discover trending official venue pages via polite web search, live-verify prices, and save to places.db."""
    existing_places = read_places(args.places)
    existing_hosts = {_norm_host(p.get("official") or p.get("price_page") or "") for p in existing_places}
    existing_hosts.discard("")
    existing_urls = {p.get("price_page") for p in existing_places if p.get("price_page")}
    already_probed = read_harvest_checked(args.places, max_age_days=30, now=args.now)

    max_queries = max(1, min(len(DISCOVER_QUERIES), getattr(args, "max_queries", 5)))
    max_candidates = max(1, getattr(args, "max_candidates", 20))
    delay = max(0.0, float(getattr(args, "delay", 3.5)))
    if getattr(args, "offset", None) is not None:
        offset = int(args.offset) % len(DISCOVER_QUERIES)
    else:
        iso_week = dt.date.fromisoformat(today(args.now)).isocalendar().week
        offset = ((iso_week - 1) * max_queries) % len(DISCOVER_QUERIES)

    queries = [DISCOVER_QUERIES[(offset + i) % len(DISCOVER_QUERIES)] for i in range(max_queries)]
    candidates_list = []
    seen_urls = set()
    host_counts = {}
    empty_streak = 0

    for idx, (query, default_area, default_kind) in enumerate(queries):
        if idx > 0 and delay > 0:
            time.sleep(delay)
        hits = ddg_search(query, timeout=max(10, args.timeout))
        if not hits:
            empty_streak += 1
            if empty_streak >= 2:
                break  # respect search rate limits politely
            continue
        empty_streak = 0
        # Prefer pages whose URL path hints at prices/rooms/hot-springs
        hits.sort(key=lambda row: (0 if _PRICE_URL_HINT_RE.search(row[0]) else 1))
        for url, title in hits:
            if not is_official_venue_url(url, title):
                continue
            host = _norm_host(url)
            if not host or host in existing_hosts or url in existing_urls or url in already_probed or url in seen_urls:
                continue
            if host_counts.get(host, 0) >= 2:
                continue
            seen_urls.add(url)
            host_counts[host] = host_counts.get(host, 0) + 1
            candidates_list.append({
                "url": url,
                "title": title,
                "default_area": default_area,
                "default_kind": default_kind,
                "host": host,
            })
            if len(candidates_list) >= max_candidates:
                break
        if len(candidates_list) >= max_candidates:
            break

    if not candidates_list:
        print("網路熱門探勘完成：本次沒有新的待驗官方頁（可能皆已收錄或暫無搜尋結果）。")
        return None

    def verify_one(item):
        found, err = read_page(item["url"], args)
        return item, found, err

    with ThreadPoolExecutor(max_workers=min(8, len(candidates_list))) as pool:
        results = list(pool.map(verify_one, candidates_list))

    places = existing_places
    added, skipped, probed_entries = [], [], []
    saved_hosts = set()

    for item, found, err in results:
        url, host = item["url"], item["host"]
        if host in saved_hosts:
            continue
        if found is None:
            skipped.append(f"{item['title'] or host}（{err}）")
            probed_entries.append((url, item["title"] or host, "fetch_failed"))
            continue
        page_title, lines, phones, free = found
        raw_title = page_title or item["title"]
        if not is_official_venue_url(url, raw_title):
            skipped.append(f"{raw_title or host}（非單一場所官網頁）")
            probed_entries.append((url, raw_title or host, "non_official"))
            continue
        page_name = clean_discovered_name(page_title)
        hit_name = clean_discovered_name(item["title"])
        if hit_name and _VENUE_BRAND_RE.search(hit_name) and not (_VENUE_BRAND_RE.search(page_name) and not _SLOGAN_SEG_RE.search(page_name)):
            name = hit_name
        else:
            name = page_name or hit_name
        if not name or any(s in f"{name} {raw_title}" for s in (*_DISCOVER_SKIP_TITLES, "懶人包", "攻略", "推薦")):
            skipped.append(f"{raw_title or host}（非單一場所頁或屬入口導覽頁）")
            probed_entries.append((url, raw_title or host, "bad_name"))
            continue
        if any(_AFFILIATE_BLOG_RE.search(line) for line in lines):
            skipped.append(f"{name}（含聯盟推廣碼，疑似部落格文章）")
            probed_entries.append((url, name, "affiliate_blog"))
            continue
        page_text = _read_page_text(args, url)
        area = _verify_discovered_area(name, raw_title, page_text, phones, item["default_area"])
        if not area:
            skipped.append(f"{name}（頁面未確認目標地區或位於其他縣市）")
            probed_entries.append((url, name, "off_area"))
            continue
        kind = "溫泉" if any(k in f"{name} {raw_title}" for k in HOT_SPRING_HINTS) else item["default_kind"]
        filter_src = "opendata_hotel" if kind in ("溫泉", "住宿") else "opendata_attraction"
        lines = filter_opendata_price_lines(filter_src, lines)
        # Web-discovered venues MUST show live readable prices (never guess free from child-free notes on ticket pages)
        if not lines:
            skipped.append(f"{name}（官網讀不到有效價格）")
            probed_entries.append((url, name, "no_price"))
            continue
        if any(p.get("name") == name for p in places):
            probed_entries.append((url, name, "duplicate"))
            continue
        probed_entries.append((url, name, "ok"))
        saved_hosts.add(host)
        live_phone = phones[0].split()[-1] if phones else ""
        places.append({
            "name": name,
            "area": area,
            "kind": kind,
            "country_code": "tw",
            "official": url,
            "price_page": url,
            "phone": live_phone,
            "prices": lines[:6],
            "free": False,
            "checked_at": today(args.now),
            "added_at": today(args.now),
            "status": "ok",
        })
        added.append(f"{name}（{area}・{kind}）")

    if not args.dry_run and added:
        write_places(args.places, places)
    if not args.dry_run and probed_entries:
        record_harvest_checked(args.places, probed_entries, today(args.now))

    mode = "（預覽模式，未寫入）" if args.dry_run else ""
    ok_total = sum(1 for p in places if p.get("status") == "ok")
    print(f"網路熱門探勘完成{mode}：查 {len(queries)} 組關鍵字、實地驗 {len(candidates_list)} 個官網，新增 {len(added)} 筆、略過 {len(skipped)} 筆；有效總數 {ok_total} 筆。")
    if added:
        print("新增場所：" + "、".join(added[:12]))
    if skipped:
        print("略過原因：" + "；".join(skipped[:8]))
    return None


def crawl_taiwan_county(county, timeout=12, max_items=40, html_text=None):
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
    existing_places = read_places(args.places)
    # Only skip recently verified places (<7 days) so older open-data entries still get refreshed weekly
    existing_fresh_urls = {
        p.get("price_page") for p in existing_places
        if p.get("price_page") and age_days(p, args.now) < 7
    }
    already_probed = read_harvest_checked(args.places, max_age_days=30, now=args.now)

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

        # 2b. Official Taiwan Tourism Administration V2.1 Open Data (attractions + city portals + legal hotels)
        use_opendata = getattr(args, "opendata", True) and not getattr(args, "html_file", None)
        if use_opendata:
            skip_opendata = seen_urls | existing_fresh_urls | already_probed
            max_attr = getattr(args, "max_attractions", 40)
            max_city = getattr(args, "max_city_spots", 30)
            max_hot = getattr(args, "max_hotels", 60)
            if max_attr > 0 or max_city > 0:
                try:
                    attr_items = fetch_tourism_zip_json(TOURISM_ATTRACTIONS_ZIP, timeout=max(20, args.timeout))
                    if max_attr > 0:
                        for item in parse_opendata_attractions(attr_items, counties, max_items=max_attr, skip_urls=skip_opendata):
                            if item["price_page"] not in seen_urls:
                                seen_urls.add(item["price_page"])
                                candidates_list.append(item)
                    if max_city > 0:
                        for item in parse_city_portal_attractions(attr_items, counties, max_items=max_city, skip_urls=skip_opendata | seen_urls):
                            if item["price_page"] not in seen_urls:
                                seen_urls.add(item["price_page"])
                                candidates_list.append(item)
                except Exception as exc:
                    print(f"觀光署景點開放資料暫時抓不到（略過）：{exc}", file=sys.stderr)
            if max_hot > 0:
                try:
                    hotel_items = fetch_tourism_zip_json(TOURISM_HOTELS_ZIP, timeout=max(20, args.timeout))
                    for item in parse_opendata_hotels(hotel_items, counties, max_items=max_hot, skip_urls=skip_opendata | seen_urls):
                        if item["price_page"] not in seen_urls:
                            seen_urls.add(item["price_page"])
                            candidates_list.append(item)
                except Exception as exc:
                    print(f"觀光署旅宿開放資料暫時抓不到（略過）：{exc}", file=sys.stderr)

    if not candidates_list:
        return "沒有可抓的候選來源（請指定 --counties 或 --catalog）"

    # 3. Verify every candidate live in parallel through page-prices.py
    def verify_one(item):
        found, err = read_page(item["price_page"], args)
        return item, found, err

    with ThreadPoolExecutor(max_workers=min(10, len(candidates_list))) as pool:
        results = list(pool.map(verify_one, candidates_list))

    places = existing_places
    plan = load("format_plan", "format-plan.py")
    added, updated, skipped = [], [], []
    probed_entries = []

    for item, found, err in results:
        source = str(item.get("source") or "")
        is_opendata = source.startswith("opendata_")
        is_city_portal = source == "city_portal"
        track_probe = is_opendata or is_city_portal
        official_url = item.get("official") or item["price_page"]
        if not official_url.startswith("https://") or not item["price_page"].startswith("https://"):
            skipped.append(f"{item['name']}（非 https 網址，booking-links.py 不接受）")
            if track_probe:
                probed_entries.append((item["price_page"], item["name"], "non_https"))
            continue
        if found is None:
            skipped.append(f"{item['name']}（{err}）")
            if track_probe:
                probed_entries.append((item["price_page"], item["name"], "fetch_failed"))
            continue
        _title, lines, phones, free = found
        if is_city_portal and not _city_portal_title_matches(item["name"], _title):
            skipped.append(f"{item['name']}（縣市觀光網頁標題不符或不存在）")
            probed_entries.append((item["price_page"], item["name"], "bad_page"))
            continue
        if is_opendata:
            lines = filter_opendata_price_lines(source, lines)
        elif is_city_portal:
            lines = filter_opendata_price_lines("opendata_attraction", lines)
        if not lines and not free and item.get("no_fee_listed"):
            free = "unlisted"
        if not lines and not free:
            skipped.append(f"{item['name']}（官網讀不到價格）")
            if track_probe:
                probed_entries.append((item["price_page"], item["name"], "no_price"))
            continue
        if track_probe:
            probed_entries.append((item["price_page"], item["name"], "ok"))
        live_phone = (phones[0].split()[-1] if phones else "") or (item.get("phone") or "")
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
        new_portal_id = _portal_item_id(place["price_page"])
        for i, old in enumerate(places):
            # Match by exact name first
            if old.get("name") == place["name"]:
                matched_idx = i
                break
            # Distinct official portal attraction IDs (taiwan.net.tw / newtaipei.travel) are different places
            old_portal_id = _portal_item_id(old.get("price_page") or "")
            if (new_portal_id and old_portal_id
                    and new_portal_id[0] == old_portal_id[0] and new_portal_id[1] != old_portal_id[1]):
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
    if not args.dry_run and probed_entries:
        record_harvest_checked(args.places, probed_entries, today(args.now))

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
    harv.add_argument("--max-per-county", type=int, default=40)
    harv.add_argument("--max-attractions", type=int, default=40, help="每次從觀光署 V2.1 景點開放資料最多驗證幾筆新 HTTPS 景點")
    harv.add_argument("--max-city-spots", type=int, default=30, help="每次從縣市官方觀光網（如新北觀光旅遊網）最多驗證幾筆熱門免費景點")
    harv.add_argument("--max-hotels", type=int, default=60, help="每次從觀光署 V2.1 旅宿開放資料最多驗證幾間新 HTTPS 飯店／溫泉民宿")
    harv.add_argument("--no-opendata", dest="opendata", action="store_false", default=True,
                      help="停用觀光署 V2.1 開放資料（只跑精選目錄與 taiwan.net.tw 縣市頁）")
    harv.add_argument("--dry-run", action="store_true", help="只預覽結果，不改寫 places.db")
    disc = sub.add_parser("discover", help="從網路搜尋自動探勘熱門湯屋、旅宿與景點官網，實地驗價後收進地點庫")
    disc.add_argument("--max-queries", type=int, default=5, help="每次最多查幾組熱門關鍵字（依週次自動輪流）")
    disc.add_argument("--max-candidates", type=int, default=20, help="每次最多實地驗價幾個新官網")
    disc.add_argument("--delay", type=float, default=3.5, help="兩次搜尋間隔秒數（避免被限流）")
    disc.add_argument("--offset", type=int, help="手動指定從第幾組關鍵字開始（預設依 ISO 週次輪流）")
    disc.add_argument("--dry-run", action="store_true", help="只預覽結果，不改寫 places.db")
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
    elif args.command == "discover":
        problem = discover(args)
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
