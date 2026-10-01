"""Present frozen report sources without inventing separate historical verdicts."""
from copy import deepcopy

from .thesis_analysis import enforce_market_expectation_boundary

LABELS = {"supports": "有支持", "weakens": "有反证", "mixed": "证据混合", "unknown": "证据不足"}


def source_sections(result, scope):
    result = deepcopy(result)
    has_news = bool(scope.get("news_snapshots") or scope.get("baseline_news_snapshots"))
    for position, original in enumerate(result.get("assessments", [])):
        item = enforce_market_expectation_boundary(original)
        result["assessments"][position] = item
        item["verdict_label"] = LABELS.get(item.get("verdict"), "待核对")
        sections = []
        for kind, title in (("official", "一 · 财报、SEC 与 IR：证据与分析"),
                            ("news", "二 · 相关新闻：证据与分析")):
            saved = item.get(kind + "_analysis")
            if isinstance(saved, dict):
                part = enforce_market_expectation_boundary({**saved, "kind": item.get("kind"),
                                                            "text": item.get("text")})
            elif kind == "official" and not has_news and not any(
                    cite.get("kind") == "news" for cite in item.get("citations", [])):
                part = {key: value for key, value in item.items() if not key.endswith("_analysis")}
                part["note"] = "此历史报告仅根据官方资料形成，保留原分析与判断。"
            else:
                citations = [cite for cite in item.get("citations", [])
                             if cite.get("kind", "official") == kind]
                ids = {cite.get("id") for cite in citations}
                part = {"citations": citations, "cited_facts": [fact for fact in item.get("cited_facts", [])
                                                                 if fact.get("id") in ids],
                        "note": ("此历史报告未保存此类资料的独立分析，不从综合结论推算独立判断。"
                                 if citations else "本报告此项未引用此类证据，尚无独立分析与判断。")}
            part["title"] = title
            if part.get("verdict"):
                part["verdict_label"] = LABELS.get(part["verdict"], "待核对")
            sections.append(part)
        combined = {key: value for key, value in item.items() if not key.endswith("_analysis")}
        combined["title"] = "三 · 综合分析与判断"
        if not has_news:
            combined["note"] = "本报告尚未纳入新闻资料，综合结论目前仅依据官方资料。"
        sections.append(combined)
        item["source_sections"] = sections
    return result
