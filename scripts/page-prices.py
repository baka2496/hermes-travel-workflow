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
from email.message import Message
import re
import sys
import urllib.request
from urllib.parse import urlsplit

import page_evidence

UA = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/130 Safari/537.36",
      "Accept-Language": "zh-TW,zh;q=0.9,en;q=0.5"}
META_CHARSET = re.compile(rb"""<meta[^>]+charset\s*=\s*["']?([\w-]+)""", re.I)


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


def fetch(url):
    request = urllib.request.Request(url, headers=UA)
    with urllib.request.urlopen(request, timeout=20) as response:
        data = response.read(5_000_000)
        message = Message()
        message["content-type"] = response.headers.get("Content-Type", "")
        return decode(data, message.get_param("charset")), response.geturl()


def main():
    for stream in (sys.stdout, sys.stderr):
        stream.reconfigure(encoding="utf-8")
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("url", help="the place's own https page with its prices")
    parser.add_argument("--hermes-root", default=page_evidence.HERMES_ROOT, help=argparse.SUPPRESS)  # tests only
    parser.add_argument("--html-file", help=argparse.SUPPRESS)  # tests only: the page's HTML from a file
    args = parser.parse_args()
    if urlsplit(args.url).scheme != "https":
        parser.error(f"https addresses only: {args.url}")
    try:
        if args.html_file:
            html, final = open(args.html_file, encoding="utf-8").read(), args.url
        else:
            html, final = fetch(args.url)
    except Exception as error:  # 404, timeout, refused: report it; do not use a remembered price
        print(f"打不開這個網頁（{error}）：這裡的價格不能用。換官網上別的頁面，或寫「沒查到價格」。", file=sys.stderr)
        return 1
    text, headings = page_evidence.read_page(html)
    page_evidence.save_snapshot(args.hermes_root, args.url, text, headings)
    if page_evidence.page_key(final) != page_evidence.page_key(args.url):
        page_evidence.save_snapshot(args.hermes_root, final, text, headings)  # redirected: either address counts
    lines = page_evidence.price_lines(text, headings)
    if not lines:
        print("這個網頁上沒有看到價格。可能是用程式載入的，可用瀏覽器打開再讀；不然就換一頁，或寫「官網沒寫價格」。")
        return 0
    print(f"網頁上的價格（{urlsplit(args.url).netloc}）：")
    for line in lines:
        print(f"• {line}")
    print("只用上面這些價格；trip-cost.py 的來源寫 @" + args.url)
    return 0


if __name__ == "__main__":
    sys.exit(main())
