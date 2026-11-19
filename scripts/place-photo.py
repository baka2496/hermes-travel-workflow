"""Save a place's photo from the place's own page and print the MEDIA line for the reply.

Example (a made-up place; never copy it into a reply):
  place-photo.py "範例溫泉旅館" --page https://www.example.com/ --list
  place-photo.py "範例溫泉旅館" --page https://www.example.com/ \
      --image https://www.example.com/images/room.jpg --label 範例湯屋 --for "範例湯屋平日 1 小時"

--list prints every photo on the page (background pictures too) with the words the page puts
beside it. The photo must appear on --page: the page is fetched and must contain the image's
address. --label must be words the page puts right beside that photo, and --for is what the plan
recommends, as the plan writes it; the two must name the same thing (a 溪景湯屋 photo for
溪景湯屋, not the lobby). 2026-11-19: a plan for 「溪景湯屋平日 1 小時」 came with the lobby photo.
That page must be the place's official site or its own booking-site page, never a blog,
an image search, or an unrelated picture. A record of where the photo came from is saved next to
it, and phone-check.py refuses MEDIA lines that have no such record. If no real photo can be
found, write "照片：未取得" in the reply instead of a MEDIA line.
"""
import argparse
from datetime import datetime, timezone
import hashlib
import html as html_lib
import json
from pathlib import Path
import re
import sys
from urllib.parse import unquote, urljoin, urlsplit
import urllib.request

DEFAULT_DIR = Path(__file__).resolve().parent / "photos"
UA = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/130 Safari/537.36"}
EXTENSIONS = {"image/jpeg": ".jpg", "image/png": ".png", "image/webp": ".webp"}
MIN_BYTES = 8_000  # icons, logos and tracking pixels are smaller than a real photo


def fetch(url, limit):
    request = urllib.request.Request(url, headers=UA)
    with urllib.request.urlopen(request, timeout=20) as response:
        return response.read(limit), response.headers.get_content_type()


REF = re.compile(r"""(?:src|href|srcset|data-src|data-bg|content|url)\s*[=(:]\s*["']?([^"'\s)>]+)""")


def image_on_page(html, page, image):
    """True when the page's HTML references the image (absolute, relative, or by its path)."""
    text = page_text(html)
    target = urlsplit(image)
    for ref in REF.findall(text):
        absolute = urlsplit(urljoin(page, ref))
        if absolute.netloc == target.netloc and absolute.path == target.path:
            return True
    return f"//{target.netloc}{unquote(target.path)}" in text  # e.g. inside JSON or a script


CAPTION_ATTR = re.compile(r"""(?:alt|data-caption|title|aria-label)\s*=\s*["']([^"']*)["']""")
TAG = re.compile(r"<[^>]*>")
GENERIC = {"Image description", "image", "photo", "img"}
PHOTO = re.compile(r"\.(?:jpe?g|png|webp)(?:[?#]|$)", re.I)
ICON = re.compile(r"logo|favicon|icon|sprite", re.I)
HEAD = 20  # the caption is the first words after a photo; its longer description may name other rooms


def page_text(html):
    return unquote(html.replace("\\/", "/").replace("&amp;", "&"))


def photos_on_page(html, page):
    """Every photo the page references (img, lazy, background), with the words the page puts beside
    it: the tag's alt, data-caption or title, then the visible text after it up to the next image.
    Logos and icons are left out."""
    text = page_text(html)
    text = re.sub(r"(?is)<(script|style)\b.*?</\1>", " ", text)
    # 2026-11-19: 「📷 雙人池 Double Pool -->」: the page's HTML comment leaked into the caption.
    text = re.sub(r"(?s)<!--.*?-->", " ", text)
    refs = [m for m in REF.finditer(text) if PHOTO.search(m.group(1)) and not ICON.search(m.group(1))]
    photos = {}
    for n, ref in enumerate(refs):
        url = urljoin(page, ref.group(1))
        start, end = text.rfind("<", 0, ref.start()), text.find(">", ref.end())
        tag = text[start:end + 1] if start >= 0 and end >= 0 else ""
        stop = refs[n + 1].start() if n + 1 < len(refs) else len(text)
        cut = text.rfind("<", end + 1, stop)  # drop the next photo's own unfinished tag
        stop = cut if cut > end else stop
        after = re.sub(r"\s+", " ", html_lib.unescape(TAG.sub(" ", text[end + 1:stop]))).strip()
        words = [w.strip() for w in CAPTION_ATTR.findall(tag) if w.strip() and w.strip() not in GENERIC]
        words += [after[:HEAD]] if after else []
        photos.setdefault(url, [])
        photos[url] += [w for w in words if w not in photos[url]]
    return photos


def caption_of(html, page, image):
    target = urlsplit(image)
    for url, words in photos_on_page(html, page).items():
        found = urlsplit(url)
        if found.netloc == target.netloc and found.path == target.path:
            return " ".join(words)
    return ""


def compact(text):
    return re.sub(r"[\s　]+", "", text or "")


def related(label, recommended):
    """True when the label and the recommended item name the same kind of thing: the label's last
    word (湯屋, 套房, 大廳, 雙人房) is in the recommended item. 「溪景套房」 shares 「溪景」 with
    「溪景湯屋」 but is another room."""
    a, b = compact(label), compact(recommended)
    if not a or not b:
        return False
    return a in b or b in a or a[-2:] in b


def first_words(caption):
    """The name at the head of a caption: 「1F 休憩大廳 時尚的…」 -> 「休憩大廳」."""
    words = [w for w in re.split(r"[\s/|｜，,、:：。]+", caption or "") if w and not re.fullmatch(r"B?\d+F", w)]
    return words[0] if words else ""


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("name", help="place name")
    parser.add_argument("--page", required=True, help="the place's own page that shows this photo")
    parser.add_argument("--list", action="store_true", help="print the page's photos with the words beside them")
    parser.add_argument("--image", help="the photo's address as used on that page")
    parser.add_argument("--label", help="the words the page puts beside the photo, e.g. 溪景湯屋")
    parser.add_argument("--for", dest="recommended", help="what the plan recommends, as the plan writes it")
    parser.add_argument("--out", type=Path, default=DEFAULT_DIR)
    args = parser.parse_args()
    sys.stdout.reconfigure(encoding="utf-8")
    if not args.list and not (args.image and args.label and args.recommended):
        parser.error("give --image, --label and --for (or --list to see the page's photos)")
    for url in (args.page, args.image or args.page):
        if urlsplit(url).scheme != "https":
            parser.error(f"https addresses only: {url}")
    try:
        html, _ = fetch(args.page, 3_000_000)
    except Exception as error:  # network or HTTP error: report it, do not fall back to another picture
        print(f"cannot open --page ({error}); write 照片：未取得", file=sys.stderr)
        return 1
    html = html.decode("utf-8", "replace")
    photos = photos_on_page(html, args.page)
    if args.list:
        for url, words in photos.items():
            print(f"{url}｜{' / '.join(words) or '（旁邊沒有說明）'}")
        return 0
    if not image_on_page(html, args.page, args.image):
        print("this image is not on --page; use a photo the place's own page shows, or write 照片：未取得", file=sys.stderr)
        return 1
    caption = caption_of(html, args.page, args.image)
    fitting = [f"{url}｜{' / '.join(words)}" for url, words in photos.items()
               if any(related(first_words(w), args.recommended) for w in words)]
    hint = ("網頁上跟它有關的照片：\n" + "\n".join(fitting)) if fitting else "網頁上沒有它的照片，寫「照片：未取得」"
    if compact(args.label) not in compact(caption):
        print(f"網頁上這張照片旁邊寫的是「{caption or '（沒有說明）'}」，不是「{args.label}」；"
              f"--label 要用網頁上的字。{hint}", file=sys.stderr)
        return 1
    if not related(args.label, args.recommended):
        print(f"這張是「{args.label}」的照片，不是推薦的「{args.recommended}」。{hint}", file=sys.stderr)
        return 1
    try:
        data, kind = fetch(args.image, 15_000_000)
    except Exception as error:
        print(f"cannot download --image ({error}); write 照片：未取得", file=sys.stderr)
        return 1
    if kind not in EXTENSIONS or len(data) < MIN_BYTES:
        print(f"not a photo ({kind}, {len(data)} bytes); write 照片：未取得", file=sys.stderr)
        return 1
    args.out.mkdir(parents=True, exist_ok=True)
    path = args.out / f"{hashlib.sha256(data).hexdigest()[:16]}{EXTENSIONS[kind]}"
    path.write_bytes(data)
    record = {"name": args.name, "label": args.label, "page": args.page, "image": args.image,
              "sha256": hashlib.sha256(data).hexdigest(), "saved_at": datetime.now(timezone.utc).isoformat()}
    path.with_name(path.name + ".source.json").write_text(json.dumps(record, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"MEDIA:{path.as_posix()}")
    print(f"📷 {args.label}（{urlsplit(args.page).netloc}）")
    return 0


if __name__ == "__main__":
    sys.exit(main())
