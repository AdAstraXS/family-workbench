"""在已保存的 10-K Item 8 正文中定位财务报表和编号附注。"""
import re

from .citations import quote_digest
from .tenk_chapters import tenk_chapter_coverage


_LINE = re.compile(r"(?m)^[ \t]*(?P<title>[^\n]{5,140})[ \t]*$")
_NOTE = re.compile(r"(?i)^notes?\s+(?P<number>\d{1,2})\s*[-—–.:]\s*(?P<topic>\S.*)$")
_STATEMENTS = (
    ("income", "利润表", re.compile(r"^(?:consolidated\s+)?(?:statements?\s+of\s+(?:income|operations|earnings)|income\s+statements?)$", re.I)),
    ("comprehensive", "综合收益表", re.compile(r"^(?:consolidated\s+)?(?:statements?\s+of\s+comprehensive\s+(?:income|loss)|comprehensive\s+income\s+statements?)$", re.I)),
    ("balance", "资产负债表", re.compile(r"^(?:consolidated\s+)?balance\s+sheets?$", re.I)),
    ("cash_flow", "现金流量表", re.compile(r"^(?:consolidated\s+)?(?:statements?\s+of\s+cash\s+flows?|cash\s+flows?\s+statements?)$", re.I)),
    ("equity", "权益变动表", re.compile(r"^(?:consolidated\s+)?(?:statements?\s+of\s+(?:(?:stockholders|shareholders)[’'\s]+equity|redeemable noncontrolling interests and equity)|(?:stockholders|shareholders)[’'\s]+equity\s+statements?)$", re.I)),
)


def tenk_item8_index(version, chapters=None):
    """返回可核对的原文标题坐标；识别不到时不推测附注或报表。"""
    chapter = next((item for item in (chapters if chapters is not None else tenk_chapter_coverage(version))
                    if item["code"] == "8" and item["located"]), None)
    if chapter is None:
        return []
    text = version.content_text
    entries = []
    seen_statements = set()
    last_note = 0
    in_notes = False
    for match in _LINE.finditer(text, chapter["quote_end"], chapter["end"]):
        title = match.group("title").strip()
        if not title or " | " in title:
            continue
        normalized = " ".join(title.split())
        if re.fullmatch(r"notes\s+to\s+(?:consolidated\s+)?financial\s+statements", normalized, re.I):
            in_notes = True
            continue
        kind = None
        label = None
        number = None
        note = _NOTE.fullmatch(normalized)
        if in_notes and note and not normalized.endswith("."):
            number = int(note.group("number"))
            if number != last_note + 1:
                continue
            last_note = number
            kind = "note"
            label = f"附注 {number}"
        elif last_note == 0:
            for code, description, pattern in _STATEMENTS:
                if code not in seen_statements and pattern.fullmatch(normalized):
                    kind = "statement"
                    label = description
                    seen_statements.add(code)
                    break
        if kind is None:
            continue
        start, end = match.span("title")
        quote = text[start:end]
        entries.append({"kind": kind, "label": label, "number": number,
                        "title": title, "start": start, "quote_end": end,
                        "quote_hash": quote_digest(quote)})
    return entries
