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
CN_DIGITS = "零一二兩三四五六七八九"
CN_VALUE = {"零": 0, "一": 1, "二": 2, "兩": 2, "三": 3, "四": 4, "五": 5, "六": 6, "七": 7, "八": 8, "九": 9}
CN_UNITS = {"十": 10, "百": 100, "千": 1000, "k": 1000, "K": 1000, "萬": 10000}
# What follows a count, not an amount: 「4萬 2人」 is NT$40,000 and two people (Antigravity's review of 0783149).
COUNT_WORDS = "人天夜位間個晚月日號點"
COUNTED = COUNT_WORDS + "/:"
NUMBER = re.compile(rf"[\d.,{CN_DIGITS}十百千萬kK]+")


def _number(token, shorthand=True):
    """「2,000」 2000, 「4萬」 40000, 「1萬5千」 15000, 「1萬5000」 15000, 「十二萬」 120000, 「2.5k」 2500.
    One last digit right after a unit is shorthand (「一萬五」 15000, 「兩千五」 2500) unless `shorthand` is
    off. None when it is no amount: a unit with no number before it (「萬一」「千萬」「k2」), except 十.
    Antigravity's review of db4a2e0: 「1萬5000」 was 5,010,000 and 「萬一下雨」 1,000."""
    total = section = 0
    num, last_unit, digits, run = None, None, "", 0
    for ch in token.replace(",", "") + " ":
        if ch.isdigit() or ch == ".":
            digits += ch
            continue
        if digits:
            try:
                num = float(digits)
            except ValueError:
                return None
            run, digits = len(digits), ""
        if ch in CN_DIGITS:
            num, run = float(CN_VALUE[ch]), 1
        elif ch in CN_UNITS:
            unit = CN_UNITS[ch]
            if unit == 10000:
                if num is None and not section:
                    return None
                total += (section + (num or 0)) * unit
                section = 0
            elif num is None and unit != 10:
                return None
            else:
                section += (1 if num is None else num) * unit
            num, last_unit = None, unit
        elif ch != " ":
            return None
    if num is not None:
        short = shorthand and run == 1 and last_unit and last_unit >= 100 and 0 < num < 10
        section += num * (last_unit // 10 if short else 1)
    return int(total + section)


# A count written right after an amount with no space: 「4萬2人」 is 40000 and 2 people, 「兩千兩天」 2000 and 2 days.
UNIT_THEN_COUNT = re.compile(rf"^(.*[十百千萬kK])([\d{CN_DIGITS}]+)$")


def spoken_amounts(text):
    """Every amount a person may have written: 「2000以內」「NT$2,000」「兩千」「4萬」「一萬五」「2.5k」."""
    # 「4 萬」 is 40000, not 4 (a space before the unit).
    text = re.sub(rf"(?<=[\d{CN_DIGITS}十])\s+(?=[千萬kK])", "", text or "")
    found = set()
    for match in NUMBER.finditer(text):
        token = match.group(0).strip(".,")
        if not re.search(rf"[\d{CN_DIGITS}十]", token):
            continue
        counted = match.end() < len(text) and text[match.end()] in COUNTED
        split = UNIT_THEN_COUNT.match(token) if counted else None
        parts = [split.group(1), split.group(2)] if split else [token]
        for part in parts:
            value = _number(part, shorthand=not counted)
            if value is not None:
                found.add(value)
    return found


# An amount as written after 「預算」: with a currency (「NT$2,000/人」「2000元」), with 萬/千/k and what may follow
# (「4萬」「1萬5」「1萬5千」「兩千五」), or three or more plain digits. Only plain digits are checked for what
# comes next (a count or a date: 「預算 2 人」「預算 12/7」); 「/人」 after a currency or unit is per person
# (Antigravity's review of db4a2e0).
_TAIL_NUMBER = rf"(?:\d+[ 	]*千?|[{CN_DIGITS}]千?)(?![\d{COUNT_WORDS}])"
_AMOUNT = (rf"{CURRENCY_BEFORE}[ 	]*\d[\d,]*(?:\.\d+)?[ 	]*[萬千kK]?(?:{_TAIL_NUMBER})?"
           rf"|\d[\d,]*(?:\.\d+)?[ 	]*[萬千kK](?:{_TAIL_NUMBER})?"
           rf"|[{CN_DIGITS}十][{CN_DIGITS}十百千萬]*[千萬](?:[{CN_DIGITS}](?![{COUNT_WORDS}]))?"
           rf"|\d[\d,]*[ 	]*{CURRENCY_AFTER}"
           rf"|(?:\d{{1,3}}(?:,\d{{3}})+|\d{{3,}})(?![\d,]|[ 	]*[{COUNT_WORDS}]|/\d|:\d)")
BUDGET_CLAIM = re.compile(
    rf"(?<!超出)(?<!超過)預算(?![剩內])[^\d\n，。,；;剩{CN_DIGITS}]{{0,6}}?(?P<a>{_AMOUNT})"
    rf"|(?P<b>{_AMOUNT})\s*(?:的)?(?:總|整趟)?預算(?![剩])")


def stated_budgets(text):
    """The budgets a message states, as (written text, amount)."""
    claims = []
    for match in BUDGET_CLAIM.finditer(text or ""):
        written = match.group("a") or match.group("b")
        amounts = spoken_amounts(re.sub(CURRENCY_BEFORE, "", written))
        if amounts:
            claims.append((match.group(0).rstrip(" 	"), max(amounts)))
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
