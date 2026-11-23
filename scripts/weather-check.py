"""Fetch a short weather forecast for a trip date via Open-Meteo (no API key required).

Prints one phone-ready line with temperature range, rain chance, and indoor/outdoor advice.
Returns "天氣：未確認（超過預報範圍）" when the date is beyond the 16-day forecast window.

Usage:
  weather-check.py 礁溪 --date 2026-12-14 [--json]
  weather-check.py 自訂地點 --lat 24.82 --lon 121.77 --date 2026-12-14
"""
import argparse
import datetime
import json
import sys
from urllib.parse import urlencode
import urllib.request

PLACES = {
    "台北": (25.0330, 121.5654), "臺北": (25.0330, 121.5654),
    "新北": (25.0120, 121.4657), "烏來": (24.8648, 121.5515), "淡水": (25.1719, 121.4408),
    "九份": (25.1099, 121.8452), "北投": (25.1369, 121.5065), "金山": (25.2216, 121.6376),
    "宜蘭": (24.7570, 121.7530), "礁溪": (24.8235, 121.7720), "羅東": (24.6770, 121.7669),
    "桃園": (24.9936, 121.3010), "新竹": (24.8138, 120.9675), "苗栗": (24.5602, 120.8214),
    "台中": (24.1477, 120.6736), "臺中": (24.1477, 120.6736),
    "南投": (23.9157, 120.6639), "日月潭": (23.8517, 120.9159), "清境": (24.0581, 121.1628),
    "嘉義": (23.4801, 120.4491), "阿里山": (23.5105, 120.8027),
    "台南": (22.9999, 120.2269), "臺南": (22.9999, 120.2269),
    "高雄": (22.6273, 120.3014), "墾丁": (21.9465, 120.7966),
    "花蓮": (23.9872, 121.6016), "台東": (22.7583, 121.1444), "臺東": (22.7583, 121.1444),
    "東京": (35.6762, 139.6503), "大阪": (34.6937, 135.5023),
    "京都": (35.0116, 135.7681), "福岡": (33.5902, 130.4017), "沖繩": (26.2124, 127.6809),
}


def resolve_coords(place, lat=None, lon=None):
    if lat is not None and lon is not None:
        return float(lat), float(lon)
    for name, coords in PLACES.items():
        if name in place:
            return coords
    return None


def fetch_forecast(lat, lon, date_str, timeout=8):
    query = urlencode({
        "latitude": lat,
        "longitude": lon,
        "daily": "temperature_2m_max,temperature_2m_min,precipitation_probability_max",
        "timezone": "Asia/Taipei",
        "start_date": date_str,
        "end_date": date_str,
    })
    url = f"https://api.open-meteo.com/v1/forecast?{query}"
    req = urllib.request.Request(url, headers={"User-Agent": "hermes-travel-workflow/1.0"})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read(200_000).decode("utf-8"))


def summarize(place, date, payload):
    daily = payload.get("daily") or {}
    tmax = (daily.get("temperature_2m_max") or [None])[0]
    tmin = (daily.get("temperature_2m_min") or [None])[0]
    rain = (daily.get("precipitation_probability_max") or [None])[0]
    if tmax is None or tmin is None:
        return {"status": "unconfirmed", "summary": "天氣：未確認（查無預報資料）"}
    rain_pct = int(round(rain)) if rain is not None else None
    rainy = rain_pct is not None and rain_pct >= 50
    icon = "🌧" if rainy else "🌤"
    advice = "建議改室內雨備" if rainy else "適合出遊"
    rain_text = f"，降雨 {rain_pct}%" if rain_pct is not None else ""
    summary = f"{icon} {date.month}/{date.day} {place}：{round(tmin)}–{round(tmax)}°C{rain_text}（{advice}）"
    return {
        "status": "ok",
        "place": place,
        "date": date.isoformat(),
        "tmin": round(tmin),
        "tmax": round(tmax),
        "rain_prob": rain_pct,
        "rainy": rainy,
        "summary": summary,
    }


def check_weather(place, date, *, lat=None, lon=None, today=None, fetcher=fetch_forecast):
    now = today or datetime.date.today()
    delta = (date - now).days
    if delta < 0 or delta > 15:
        return {"status": "out_of_range", "summary": "天氣：未確認（超過 16 天預報範圍）"}
    coords = resolve_coords(place, lat, lon)
    if not coords:
        return {"status": "unknown_place", "summary": f"天氣：未確認（尚未收錄「{place}」座標）"}
    try:
        payload = fetcher(coords[0], coords[1], date.isoformat())
    except Exception as exc:
        return {"status": "error", "error": str(exc), "summary": "天氣：未確認（暫時查不到預報）"}
    return summarize(place, date, payload)


def main():
    for stream in (sys.stdout, sys.stderr):
        stream.reconfigure(encoding="utf-8")
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("place", help="place name (e.g. 礁溪, 烏來, 福岡)")
    parser.add_argument("--date", required=True, type=datetime.date.fromisoformat, help="YYYY-MM-DD")
    parser.add_argument("--lat", type=float)
    parser.add_argument("--lon", type=float)
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args()
    result = check_weather(args.place, args.date, lat=args.lat, lon=args.lon)
    if args.json:
        print(json.dumps(result, ensure_ascii=False))
    else:
        print(result["summary"])
    return 0


if __name__ == "__main__":
    sys.exit(main())
