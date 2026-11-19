"""Save a place's photo from the place's own page and print the MEDIA line for the reply.

Example (a made-up place; never copy it into a reply):
  place-photo.py "範例溫泉旅館" --page https://www.example.com/ --for "範例湯屋 1 小時"

With --for alone it saves up to three photos that may show that item (captioned with its name
first, then uncaptioned ones) and says how to pick one: look at each with vision_analyze and use
only a photo that shows the item itself. It prints 照片：未取得 when the page has none. --list
prints the page's photos; --image with --label saves a chosen one.

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


def related_name(name, recommended):
    a, b = compact(name), compact(recommended)
    if not a or not b:
        return False
    return a in b or b in a or a[-2:] in b


def related(label, recommended):
    """The name the label gives (see `names`) that is the same kind of thing as the recommended
    item, else None: its last word (湯屋, 套房, 大廳, 雙人房) is in the recommended item.
    「溪景套房」 shares 「溪景」 with 「溪景湯屋」 but is another room."""
    return next((name for name in names(label) if related_name(name, recommended)), None)


def names(caption):
    """The name at the head of a caption, as a list. Only the first word counts: 2026-11-20, a
    photo of 玫瑰園's corridor and shop sat right before the section 「提供服務 溫泉湯屋 …」."""
    words = [w for w in re.split(r"[\s/|｜，,、:：。]+", caption or "") if w and not re.fullmatch(r"B?\d+F", w)]
    return words[:1]


def first_words(caption):
    """The name at the head of a caption: 「1F 休憩大廳 時尚的…」 -> 「休憩大廳」."""
    words = names(caption)
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
    if not args.list and not args.recommended:
        parser.error("give --for (what the plan recommends), or --list to see the page's photos")
    if args.image and not args.label:
        parser.error("--image needs --label (the words the page puts beside it)")
    for url in (args.page, args.image or args.page):
        if urlsplit(url).scheme != "https":
            parser.error(f"https addresses only: {url}")
    try:
        html, _ = fetch(args.page, 3_000_000)
    except Exception as error:  # network or HTTP error: report it, do not fall back to another picture
        print(f"cannot open --page ({error})", file=sys.stderr)
        print(NONE)
        return 0
    html = html.decode("utf-8", "replace")
    photos = photos_on_page(html, args.page)
    if args.list:
        for url, words in photos.items():
            print(f"{url}｜{' / '.join(words) or '（旁邊沒有說明）'}")
        return 0
    if not args.image:
        return candidates(args, photos)
    if not image_on_page(html, args.page, args.image):
        print("this image is not on --page; use a photo the place's own page shows, or write 照片：未取得", file=sys.stderr)
        return 1
    caption = caption_of(html, args.page, args.image)
    fitting = [f"{url}｜{' / '.join(words)}" for url, words in photos.items()
               if any(related(w, args.recommended) for w in words)]
    hint = ("網頁上跟它有關的照片：\n" + "\n".join(fitting)) if fitting else "網頁上沒有它的照片，寫「照片：未取得」"
    if compact(args.label) not in compact(caption):
        print(f"網頁上這張照片旁邊寫的是「{caption or '（沒有說明）'}」，不是「{args.label}」；"
              f"--label 要用網頁上的字。{hint}", file=sys.stderr)
        return 1
    shown = related(args.label, args.recommended)
    if not shown:
        # 2026-11-20: 「兩人一間（50分鐘）」 named no room, so the 溫泉湯屋 photo could not match it.
        print(f"這張是「{args.label}」的照片，不是推薦的「{args.recommended}」。"
              "--for 要寫推薦的是什麼（湯屋、房型）加時長（例如「溫泉湯屋 50分鐘」）。"
              + (hint if fitting else "照這樣寫還是沒有它的照片，就寫「照片：未取得」"), file=sys.stderr)
        return 1
    path = save(args, args.image, args.label)
    if not path:
        print(f"write {NONE}", file=sys.stderr)
        return 1
    print(f"MEDIA:{path.as_posix()}")
    print(f"📷 {shown}（{urlsplit(args.page).netloc}）")
    print("送出前先用 vision_analyze 看這張，確定拍到推薦的項目本身；沒看過的照片不會送出。", file=sys.stderr)
    return 0


NONE = "照片：未取得"
MAX_CANDIDATES = 3


def candidates(args, photos):
    """Save up to three photos that may show the recommended item, for the agent to look at.

    2026-11-20: the words beside a photo do not say what it shows. On 玫瑰園's site the photo
    captioned 「溫泉湯屋」 was the shop front and the one before 「提供服務 溫泉湯屋」 the corridor,
    while the real hot-spring room photos had no caption at all. So the photos whose caption
    names the item come first, then the uncaptioned ones, and the agent must look at a photo
    (vision_analyze) before using it; the reply check drops photos nobody looked at."""
    named = [url for url, words in photos.items() if any(related(w, args.recommended) for w in words)]
    bare = [url for url, words in photos.items() if not words]
    saved = []
    for url in named + bare:
        if len(saved) == MAX_CANDIDATES:
            break
        path = save(args, url, " / ".join(photos[url]) or "（旁邊沒有說明）")
        if path:
            saved.append((path, " / ".join(photos[url])))
    if not saved:
        print(f"官網這頁沒有可能是「{args.recommended}」的照片。", file=sys.stderr)
        print(NONE)
        return 0
    netloc = urlsplit(args.page).netloc
    print(f"先用 vision_analyze 一張一張看（image_url 填路徑），挑真的拍到「{args.recommended}」本身的那張；"
          "外觀、招牌、大廳、走廊、商店或 logo 都不算。沒看過的照片不會送出。")
    for n, (path, caption) in enumerate(saved, 1):
        print(f"{n}. {path.as_posix()}（官網旁邊寫：{caption or '沒有說明'}）")
    print(f"挑好後在回覆裡貼這兩行（路徑換成那張）：\nMEDIA:<路徑>\n📷 {args.recommended}（{netloc}）")
    print(f"都不是就寫「{NONE}」。")
    return 0


def save(args, image, label):
    """Download the photo and keep a record of where it came from; the saved path, or None."""
    try:
        data, kind = fetch(image, 15_000_000)
    except Exception as error:
        print(f"cannot download {image} ({error})", file=sys.stderr)
        return None
    if kind not in EXTENSIONS or len(data) < MIN_BYTES:
        print(f"{image} is not a photo ({kind}, {len(data)} bytes)", file=sys.stderr)
        return None
    args.out.mkdir(parents=True, exist_ok=True)
    path = args.out / f"{hashlib.sha256(data).hexdigest()[:16]}{EXTENSIONS[kind]}"
    path.write_bytes(data)
    record = {"name": args.name, "label": label, "page": args.page, "image": image,
              "sha256": hashlib.sha256(data).hexdigest(), "saved_at": datetime.now(timezone.utc).isoformat()}
    path.with_name(path.name + ".source.json").write_text(json.dumps(record, ensure_ascii=False, indent=2), encoding="utf-8")
    return path


if __name__ == "__main__":
    sys.exit(main())
