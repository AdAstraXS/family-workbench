"""从已保存的 10-K 正文识别标准 Item 标题，保留原文字符坐标。

这里只索引标题，不重写正文或替代 SEC 原文；识别不到时明确留空。
"""
import re

from .citations import quote_digest
from .research_ai import document_segments


ITEMS = (
    ("1", "业务", "business"),
    ("1A", "风险因素", "risk factors"),
    ("1B", "SEC 未解决意见", "unresolved staff comments"),
    ("1C", "网络安全", "cybersecurity"),
    ("2", "物业", "properties"),
    ("3", "法律程序", "legal proceedings"),
    ("4", "矿山安全", "mine safety"),
    ("5", "股市与股东", "market for"),
    ("6", "保留", "reserved"),
    ("7", "管理层讨论与分析", "management's discussion"),
    ("7A", "市场风险", "quantitative and qualitative"),
    ("8", "财务报表与附注", "financial statements"),
    ("9", "会计师变更", "changes in and disagreements"),
    ("9A", "控制与程序", "controls and procedures"),
    ("9B", "其他信息", "other information"),
    ("9C", "外国司法辖区检查", "disclosure regarding foreign"),
    ("10", "董事与高管", "directors"),
    ("11", "高管薪酬", "executive compensation"),
    ("12", "证券持有情况", "security ownership"),
    ("13", "关联交易", "certain relationships"),
    ("14", "审计费用", "principal accountant"),
    ("15", "附件", "exhibit"),
    ("16", "10-K 摘要", "form 10-k summary"),
)

_LABELS = {code: (label, title) for code, label, title in ITEMS}
_HEADING = re.compile(
    r"(?m)^[ \t]*(?P<item>ITEM|Item)[ \t]+(?P<code>1[0-6]|[1-9])(?P<suffix>[ABC]?)[ \t]*"
    r"(?:[.:\-–][ \t]*|[ \t]+)(?P<title>[^\n]{2,150})"
)


def _candidates(text):
    found = []
    for match in _HEADING.finditer(text):
        code = match.group("code") + match.group("suffix")
        if code not in _LABELS:
            continue
        title = " ".join(match.group("title").strip().split())
        expected = _LABELS[code][1]
        normalized = title.lower().replace("’", "'")
        if not normalized.startswith(expected):
            continue
        # 索引行常带点线和页码；正文标题不应含这类目录格式。
        if re.search(r"\.{3,}|\s\d{1,3}$", title):
            continue
        found.append({"code": code, "start": match.start("item"),
                      "end": match.end("title"), "title": title})
    return found


def _body_candidates(text):
    found = _candidates(text)
    # 目录往往在正文前几千字密集列出多个 Item；只排除明确的目录簇。
    prefix = [item for item in found if item["start"] < min(15000, len(text) // 8)]
    cluster = prefix[:1]
    for item in prefix[1:]:
        if item["start"] - cluster[-1]["start"] > 500:
            break
        cluster.append(item)
    if len({item["code"] for item in cluster}) >= 5:
        floor = cluster[-1]["end"]
        found = [item for item in found if item["start"] > floor]
    # 同一 Item 在分页页眉可能重复；只保留按法定顺序出现的第一个正文标题。
    order = {code: index for index, (code, _, _) in enumerate(ITEMS)}
    accepted = []
    last_order = -1
    for item in found:
        position = order[item["code"]]
        if position > last_order:
            accepted.append(item)
            last_order = position
    return accepted


def tenk_chapter_coverage(version, completed_segment_indexes=()):
    """返回可见章节及机械区段覆盖；未识别章节不会被误记为已读。"""
    if version.document.document_type != "10-k":
        return []
    text = version.content_text
    headings = _body_candidates(text)
    segments = document_segments(version)
    completed = set(completed_segment_indexes)
    chapters = []
    for index, heading in enumerate(headings):
        end = headings[index + 1]["start"] if index + 1 < len(headings) else len(text)
        overlapping = [segment["index"] for segment in segments
                       if segment["start"] < end and segment["end"] > heading["start"]]
        quote = text[heading["start"]:heading["end"]]
        chapters.append({
            "code": heading["code"], "label": _LABELS[heading["code"]][0],
            "located": True,
            "start": heading["start"], "end": end,
            "quote_end": heading["end"], "quote_hash": quote_digest(quote),
            "covered_count": sum(segment in completed for segment in overlapping),
            "segment_count": len(overlapping),
        })
    if not chapters:
        return []
    by_code = {chapter["code"]: chapter for chapter in chapters}
    return [by_code.get(code) or {"code": code, "label": label, "located": False}
            for code, label, _ in ITEMS]
