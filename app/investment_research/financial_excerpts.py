"""Contiguous statement excerpts that retain column dates and units."""
import re


def statement_excerpts(text):
    result = []
    for match in re.finditer(
            r'(?im)^[ \t]*(?:CONDENSED[ \t]+)?CONSOLIDATED[ \t]+STATEMENTS[ \t]+OF[ \t]+(?:INCOME|OPERATIONS|CASH FLOWS)[ \t]*$', text):
        start = match.start()
        block = text[start:start + 1800]
        # A table of contents heading is not a financial table.
        if not re.search(r'(?im)^(?:Net sales|Total revenue|Membership fees|Net income|Net cash provided by operating activities).*\|.*\d', block):
            continue
        end = block.rfind('\n')
        result.append((text[start:start + end] if end > 100 else block, start))
        if len(result) == 2:
            break
    return result
