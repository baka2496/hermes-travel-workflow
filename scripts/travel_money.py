"""How the travel workflow writes and finds prices: the one place for both.

trip-cost.py prints amounts with `money()`; the reply-check plugin reads them back with `AMOUNT`,
so the two cannot drift apart. `PRICE` finds any price a page or a record states.

Before 2026-11-20 each file had its own copy: the plugin's record check matched 「NT$600」 and
「600元」 but not 「NTD 600」 or 「600 TWD」, which page_evidence.py already read as prices, so a
record written that way went to the bot without the "prices here may be stale" reminder.

Installed beside the scripts (shared/travel-workflow) and inside the plugin folder.
"""
import re

# An amount as trip-cost.py prints it: 「NT$1,234」.
AMOUNT_PATTERN = r"NT\$[\d,]+"
AMOUNT = re.compile(AMOUNT_PATTERN)

# The currency written before a number (「NT$」, 「NTD」, 「TWD」, 「$」) and after it (「元」, 「TWD」).
CURRENCY_BEFORE = r"(?:NT\$|NTD|TWD|\$)"
CURRENCY_AFTER = r"(?:元|TWD)"

# Any stated price: 「NT$600」, 「NTD 600」, 「TWD 600」, 「$600」, 「600元」, 「600 TWD」.
PRICE = re.compile(rf"{CURRENCY_BEFORE}\s*\d[\d,]*|\d[\d,]*\s*{CURRENCY_AFTER}")


def money(value):
    """1234 -> 「NT$1,234」."""
    return f"NT${value:,}"


def amounts(text):
    """The trip-cost.py style amounts in text, in order: 「NT$60（NT$30×2）」 -> ["NT$60", "NT$30"]."""
    return AMOUNT.findall(text or "")


# 2026-11-25: the bot asked 「…兩人整趟預算 NT$2,000…這次想先走哪一趟？」. The owner never gave a budget:
# a handoff on 2026-11-11 had written 「整趟預算：新台幣 2,000 元」 into preferences.json on the owner's
# behalf. A budget is said only when the owner's own words give that amount; otherwise it is 未確認.
# 「預算剩」「超出預算」「預算內」 are what trip-cost.py prints about a budget it was given, not a new claim.
BUDGET_CLAIM = re.compile(
    rf"(?<!超出)(?<!超過)預算(?![剩內])[^\d\n，。,；;剩]{{0,6}}?(?P<a>{CURRENCY_BEFORE}\s*\d[\d,]*|\d[\d,]*\s*{CURRENCY_AFTER})"
    rf"|(?P<b>{CURRENCY_BEFORE}\s*\d[\d,]*|\d[\d,]*\s*{CURRENCY_AFTER})\s*(?:的)?(?:總|整趟)?預算(?![剩])")
DIGITS = {"一": 1, "二": 2, "兩": 2, "三": 3, "四": 4, "五": 5, "六": 6, "七": 7, "八": 8, "九": 9}
SPOKEN = re.compile(r"(\d+(?:\.\d+)?|[一二兩三四五六七八九])\s*([千萬kK])\s*([一二兩三四五六七八九]|\d)?(?![\d千萬])"
                    r"|(\d[\d,]*)")


def spoken_amounts(text):
    """Every amount a person may have written: 「2000以內」「NT$2,000」「兩千」「4萬」「一萬五」「2.5k」."""
    found = set()
    for lead, unit, tail, plain in SPOKEN.findall(text or ""):
        if plain:
            found.add(int(plain.replace(",", "") or 0))
            continue
        base = {"千": 1000, "k": 1000, "K": 1000, "萬": 10000}[unit]
        value = float(DIGITS.get(lead) or lead) * base
        if tail:
            value += (DIGITS.get(tail) or int(tail)) * base // 10
        found.add(int(value))
    return found


def stated_budgets(text):
    """The budgets a message states, as (written text, amount)."""
    claims = []
    for match in BUDGET_CLAIM.finditer(text or ""):
        written = match.group("a") or match.group("b")
        claims.append((match.group(0), int(re.sub(r"\D", "", written))))
    return claims


def owner_budget(preferences):
    """The amount of a preferences.json budget whose `quote` (the owner's own words) gives it, else None.
    A budget without the owner's words is not one (2026-11-25)."""
    budget = (preferences or {}).get("budget") if isinstance(preferences, dict) else None
    if not isinstance(budget, dict) or not isinstance(budget.get("amount"), (int, float)):
        return None
    amount = int(budget["amount"])
    return amount if amount in spoken_amounts(str(budget.get("quote") or "")) else None


def unsaid_budgets(text, owner_texts, preferences=None):
    """Budget claims in `text` whose amount none of the owner's own messages (or a quoted
    preferences.json budget) gives: [(written text, amount)]."""
    said = set().union(*(spoken_amounts(t) for t in owner_texts)) if owner_texts else set()
    kept = owner_budget(preferences)
    if kept is not None:
        said.add(kept)
    return [(written, amount) for written, amount in stated_budgets(text) if amount not in said]


BUDGET_HINT = ("使用者沒說過這個預算（2026-11-25：「兩人整趟預算 NT$2,000」是別人轉交時寫進 preferences.json 的，"
               "使用者本人沒說過）。拿掉這個預算，寫「預算未確認」或不提；要比預算就用按鈕問使用者。")
