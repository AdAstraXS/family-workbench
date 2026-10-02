import json
from copy import deepcopy
from django.test import SimpleTestCase
from .report_sections import source_sections
from .thesis_analysis import _validate_output


class SourceSectionTests(SimpleTestCase):
    target = {"kind": "pillar", "index": 0, "text": "需求持续增长"}

    def evidence(self):
        return [{"id": "E1", "text": "官方事实", "citations": [
            {"version_id": 1, "start": 0, "end": 4, "document_id": 1, "hash": "a"}]},
            {"id": "E2", "text": "新闻摘录", "citations": [
                {"kind": "news", "version_id": 2, "start": 0, "end": 4,
                 "material_id": 2, "document_id": None, "hash": "b"}]}]

    def test_separate_source_verdicts_and_combined_are_retained(self):
        item = {**self.target, "verdict": "mixed", "reason": "两类资料存在分歧", "evidence_ids": ["E1", "E2"],
                "official_analysis": {"verdict": "supports", "reason": "官方有支持", "evidence_ids": ["E1"]},
                "news_analysis": {"verdict": "weakens", "reason": "新闻有反证", "evidence_ids": ["E2"]}}
        result = _validate_output(json.dumps({"assessments": [item]}), [self.target], self.evidence())
        parts = source_sections(result, {"news_snapshots": [{"version_id": 2}]})["assessments"][0]["source_sections"]
        self.assertEqual([p["verdict"] for p in parts], ["supports", "weakens", "mixed"])
        self.assertEqual(parts[0]["citations"][0]["id"], "E1")
        self.assertEqual(parts[1]["citations"][0]["id"], "E2")

    def test_cross_source_citation_is_rejected(self):
        item = {**self.target, "verdict": "supports", "reason": "总体有支持", "evidence_ids": ["E1"],
                "official_analysis": {"verdict": "supports", "reason": "错误借用新闻", "evidence_ids": ["E2"]}}
        result = _validate_output(json.dumps({"assessments": [item]}), [self.target], self.evidence())
        self.assertEqual(result["assessments"][0]["official_analysis"]["verdict"], "unknown")
        self.assertEqual(result["assessments"][0]["official_analysis"]["citations"], [])
        self.assertEqual(result["invalid_reference_count"], 1)

    def test_legacy_mixed_report_does_not_invent_independent_verdicts_or_change_saved_result(self):
        result = {"assessments": [{**self.target, "verdict": "supports", "reason": "保存结论", "citations": [
            {"id": "E1", "document_id": 1}, {"id": "E2", "kind": "news", "material_id": 2}],
            "cited_facts": [{"id": "E1", "text": "官方事实"}, {"id": "E2", "text": "新闻事实"}]}]}
        frozen = deepcopy(result)
        parts = source_sections(result, {"news_snapshots": [{}]})["assessments"][0]["source_sections"]
        self.assertNotIn("verdict", parts[0])
        self.assertNotIn("verdict", parts[1])
        self.assertEqual(parts[2]["verdict"], "supports")
        self.assertEqual(parts[0]["cited_facts"], [{"id": "E1", "text": "官方事实"}])
        self.assertEqual(result, frozen)

    def test_legacy_official_only_preserves_original_analysis(self):
        result = {"assessments": [{**self.target, "verdict": "supports", "reason": "原官方分析", "citations": []}]}
        parts = source_sections(result, {})["assessments"][0]["source_sections"]
        self.assertEqual(parts[0]["reason"], "原官方分析")
        self.assertNotIn("verdict", parts[1])
        self.assertEqual(parts[2]["verdict"], "supports")
