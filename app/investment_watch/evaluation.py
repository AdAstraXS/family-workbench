"""Read-only evaluation of frozen, explicitly labelled recall samples."""

from .catalogue import match_rule
from .services import WatchError


def evaluate_recall(rule, cases, versions):
    if not isinstance(cases, list) or not 1 <= len(cases) <= 500:
        raise WatchError("标注集须包含 1–500 条样本。")
    totals = dict(true_positive=0, false_positive=0, true_negative=0, false_negative=0)
    rows = []
    seen = set()
    for case in cases:
        if not isinstance(case, dict):
            raise WatchError("标注样本格式无效。")
        pk, expected = case.get("version_id"), case.get("expected_recall")
        if type(pk) is not int or type(expected) is not bool or pk in seen:
            raise WatchError("样本须包含唯一版本 ID 和布尔召回标注。")
        version = versions.get(pk)
        if (not version or version.material.source.family_id != rule.dossier.family_id
                or case.get("content_hash") != version.content_hash):
            raise WatchError("样本越权或内容版本与标注不一致。")
        seen.add(pk)
        matched, reason = match_rule(rule, version)
        key = ("true_" if matched == expected else "false_") + ("positive" if matched else "negative")
        totals[key] += 1
        rows.append({"version_id": pk, "expected": expected, "matched": matched,
                     "reason": reason, "label_reason": case.get("reason", "")})
    tp, fp, fn = (totals[k] for k in ("true_positive", "false_positive", "false_negative"))
    return {"counts": totals, "precision": tp / (tp + fp) if tp + fp else None,
            "recall": tp / (tp + fn) if tp + fn else None,
            "sample_count": len(rows), "rule_version": rule.version,
            "rows": rows}
