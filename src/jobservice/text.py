"""HTML → plain-text helpers and Czech date parsing."""

import html
import re
import unicodedata
from datetime import date, timedelta

_PARA_END = re.compile(r"</(p|ul|ol|blockquote)\s*>", re.I)
# </li> is not here: the next <li> already starts a new line.
_BLOCK_END = re.compile(r"</(div|h[1-6]|tr|section|article)\s*>", re.I)
_BR = re.compile(r"<br\s*/?>", re.I)
_LI = re.compile(r"<li\b[^>]*>", re.I)
_DROP = re.compile(r"<(script|style)\b.*?</\1\s*>", re.I | re.S)
_TAG = re.compile(r"<[^>]+>")


def html_to_text(fragment: str | None) -> str | None:
    """Convert an HTML fragment to readable plain text, keeping paragraph breaks."""
    if not fragment:
        return None
    s = _DROP.sub("", fragment)
    s = _BR.sub("\n", s)
    s = _LI.sub("\n- ", s)
    s = _PARA_END.sub("\n\n", s)
    s = _BLOCK_END.sub("\n", s)
    s = _TAG.sub("", s)
    s = html.unescape(s).replace("\xa0", " ")
    lines = [re.sub(r"[ \t]+", " ", line).strip() for line in s.splitlines()]
    text = re.sub(r"\n{3,}", "\n\n", "\n".join(lines)).strip()
    return text or None


def clean(s: str | None) -> str | None:
    if s is None:
        return None
    s = re.sub(r"\s+", " ", html.unescape(s)).strip()
    return s or None


def slugify(s: str) -> str:
    s = unicodedata.normalize("NFKD", s).encode("ascii", "ignore").decode()
    return re.sub(r"[^a-z0-9]+", "-", s.lower()).strip("-")


_MONTHS = {
    "ledna": 1, "unora": 2, "brezna": 3, "dubna": 4, "kvetna": 5, "cervna": 6,
    "cervence": 7, "srpna": 8, "zari": 9, "rijna": 10, "listopadu": 11, "prosince": 12,
}  # fmt: skip


def parse_czech_date(raw: str | None, today: date) -> str | None:
    """Parse Jobs.cz card dates like '10. listopadu', 'Aktualizováno dnes', 'včera'.

    Returns an ISO date, or None for texts that are not a publish date
    (e.g. 'Končí za 3 dny' — that's the deadline).
    """
    if not raw:
        return None
    s = slugify(raw).replace("-", " ")
    if s.startswith("konci"):
        return None
    if "dnes" in s or re.search(r"\bpred \w*\s*(minut|hodin)", s):
        return today.isoformat()
    if "vcera" in s:
        return (today - timedelta(days=1)).isoformat()
    m = re.search(r"\bpred (\d+) dn", s)
    if m:
        return (today - timedelta(days=int(m.group(1)))).isoformat()
    m = re.search(r"(\d{1,2}) (\w+)(?: (\d{4}))?", s)
    if m and m.group(2) in _MONTHS:
        day, month = int(m.group(1)), _MONTHS[m.group(2)]
        year = int(m.group(3)) if m.group(3) else today.year
        try:
            d = date(year, month, day)
        except ValueError:
            return None
        if not m.group(3) and d > today:  # "20. prosince" seen in January
            d = d.replace(year=year - 1)
        return d.isoformat()
    return None
