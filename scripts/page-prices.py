"""Open a place's own page and print the prices a visitor sees on it, each with what it is for.

Example (a made-up page; never copy it into a reply):
  page-prices.py https://www.example.com/hotspring.html

Prints one line per price, tables written out with their row and column headers:
  冬季 (10-4月)：699元 / 60分鐘
and keeps what the page showed as a snapshot. trip-cost.py checks an item's price against that
snapshot (for a few hours), so run this first and pick prices from its lines; a price from a
record, a blog or a search result is not on the page until this shows it.

2026-11-19 (Telegram): asked for a cheaper hot spring, 小居 planned 春和's 「雙人池 40分鐘 NT$250」
from an old record. The official page shows 699元 for 60 minutes in winter; trip-cost.py could
only say the price was not there, and 小居 spent half an hour finding out why.

A page that builds its prices with scripts may show none here: open it with the browser and
read it there (trip-cost.py also accepts a page opened that way), or say the price was not found.
"""
import argparse
from concurrent.futures import ThreadPoolExecutor
import datetime
from email.message import Message
import re
import sys
import urllib.request
from urllib.parse import urlsplit

import page_evidence

UA = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/130 Safari/537.36",
      "Accept-Language": "zh-TW,zh;q=0.9,en;q=0.5"}
META_CHARSET = re.compile(rb"""<meta[^>]+charset\s*=\s*["']?([\w-]+)""", re.I)
DEFAULT_TIMEOUT = 10
MAX_WORKERS = 5


def decode(data, header_charset):
    """The page as text, in the charset its headers or <meta> name (big5 pages exist in Taiwan)."""
    meta = META_CHARSET.search(data[:4096])
    for charset in (header_charset, meta.group(1).decode("ascii", "replace") if meta else None, "utf-8"):
        if not charset:
            continue
        try:
            return data.decode("cp950" if charset.lower() in ("big5", "big-5") else charset)
        except (LookupError, UnicodeDecodeError):
            continue
    return data.decode("utf-8", "replace")


def fetch(url, timeout=DEFAULT_TIMEOUT):
    request = urllib.request.Request(url, headers=UA)
    with urllib.request.urlopen(request, timeout=timeout) as response:
        data = response.read(5_000_000)
        message = Message()
        message["content-type"] = response.headers.get("Content-Type", "")
        return decode(data, message.get_param("charset")), response.geturl()


TITLE = re.compile(r"<title[^>]*>(.*?)</title>", re.I | re.S)


def read(url, args):
    """(page, error) for one URL, keeping its snapshot; page is None when it cannot be opened."""
    try:
        if args.html_file:
            with open(args.html_file, encoding="utf-8") as fh:
                html, final = fh.read(), url
        else:
            html, final = fetch(url, timeout=getattr(args, "timeout", DEFAULT_TIMEOUT))
    except Exception as error:  # 404, timeout, refused: report it; do not use a remembered price
        return None, f"打不開 {url}（{error}）：這裡的價格不能用。換官網上別的頁面，或寫「沒查到價格」。"
    text, headings = page_evidence.read_page(html)
    page_evidence.save_snapshot(args.hermes_root, url, text, headings)
    if page_evidence.page_key(final) != page_evidence.page_key(url):
        page_evidence.save_snapshot(args.hermes_root, final, text, headings)  # redirected: either address counts
    title = TITLE.search(html)
    title = (re.sub(r"\s+", " ", title.group(1)).strip()[:40] if title else "") or urlsplit(url).netloc
    return (title, page_evidence.price_lines(text, headings), page_evidence.phones_on_page(text)), None


def read_all(urls, args):
    """Read URLs concurrently (up to MAX_WORKERS) while preserving input order."""
    if len(urls) <= 1:
        return [(url, *read(url, args)) for url in urls]
    with ThreadPoolExecutor(max_workers=min(MAX_WORKERS, len(urls))) as pool:
        results = list(pool.map(lambda u: read(u, args), urls))
    return [(url, page, error) for url, (page, error) in zip(urls, results)]


def show(url, page, many):
    title, lines, phones = page
    if not lines:
        print(f"{url}：這個網頁上沒有看到價格。可能是用程式載入的，可用瀏覽器打開再讀；不然就換一頁，或寫「官網沒寫價格」。")
        return
    print(f"網頁上的價格（{title}，{urlsplit(url).netloc}）：")
    for line in lines[:12] if many else lines:
        print(f"• {line}")
    if phones:
        print("網頁上的電話：" + "；".join(phones) + f"（booking-links.py 加 --phone <號碼> --phone-source {url}）")
    print(f"trip-cost.py 的來源寫 @{url}")


def main():
    for stream in (sys.stdout, sys.stderr):
        stream.reconfigure(encoding="utf-8")
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("urls", nargs="*", metavar="url", help="the places' own https pages with their prices")
    parser.add_argument("--near", help="also read again the pages read before that mention this place (e.g. 礁溪)")
    parser.add_argument("--date", type=lambda s: datetime.date.fromisoformat(s),
                        help="trip date YYYY-MM-DD: the comparison leaves out other months and the other day's price")
    parser.add_argument("--people", type=int, default=2, help="how many people (for per-person prices)")
    parser.add_argument("--timeout", type=float, default=DEFAULT_TIMEOUT,
                        help=f"per-page fetch timeout in seconds (default: {DEFAULT_TIMEOUT})")
    parser.add_argument("--hermes-root", default=page_evidence.HERMES_ROOT, help=argparse.SUPPRESS)  # tests only
    parser.add_argument("--html-file", help=argparse.SUPPRESS)  # tests only: the page's HTML from a file
    args = parser.parse_args()
    urls = list(args.urls)
    if args.near:
        urls += [u for u in page_evidence.known_pages(args.hermes_root, args.near)
                 if page_evidence.page_key(u) not in {page_evidence.page_key(x) for x in urls}]
    if not urls:
        parser.error("give at least one page (or --near with a place pages were read for before)")
    for url in urls:
        if urlsplit(url).scheme != "https":
            parser.error(f"https addresses only: {url}")
    many = len(urls) > 1
    ranked, opened = [], 0
    for url, page, error in read_all(urls, args):
        if error:
            print(error, file=sys.stderr)
        if page is None:
            continue
        opened += 1
        show(url, page, many)
        best = page_evidence.cheapest(page[1], args.date, args.people)
        if best:
            ranked.append((best[0], page[0], best[1], url))
    if not opened:
        return 1
    if not ranked:
        return 0
    print("項目名稱寫推薦的是什麼加時長或人數（例如「溫泉湯屋 50分鐘」），只用上面這些價格。")
    if many:
        # 2026-11-20: asked for a cheap hot spring, a new conversation recommended 春和's NT$699 though
        # 玫瑰園 (NT$300, read the day before) was nearby. The pages are now compared side by side.
        print("比價（一次的價格，便宜的在前）：")
        for amount, title, line, url in sorted(ranked):
            print(f"{amount:>6,}元｜{title}｜{line}（{url}）")
        print("推薦最便宜的；推薦別家就在回覆裡說為什麼（例如時間、評價）。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
