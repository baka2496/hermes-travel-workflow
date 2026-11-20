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
