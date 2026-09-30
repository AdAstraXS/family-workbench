import unittest

try:
    from .contracts import Scope, EvidenceReference, ThesisLink, require_version
    from .config import parse_settings
except ImportError:
    from contracts import Scope, EvidenceReference, ThesisLink, require_version
    from config import parse_settings


class ContractTests(unittest.TestCase):
    def test_private_scope_includes_family_and_owner(self):
        for other in [Scope(1, 2), Scope(2, 1)]:
            with self.assertRaises(PermissionError):
                Scope(1, 1).require(other)

    def test_candidate_cannot_claim_support(self):
        with self.assertRaisesRegex(ValueError, "keyword_match"):
            ThesisLink(
                Scope(1, 1), 1, "pillar:0", "support", "rule_candidate", "matched"
            )

    def test_verdict_needs_traceable_evidence(self):
        with self.assertRaisesRegex(ValueError, "verdict_requires"):
            ThesisLink(Scope(1, 1), 1, "pillar:0", "weaken", "model", "reason")
        with self.assertRaises(ValueError):
            EvidenceReference("m1", 1, "quote", "")

    def test_revision_change_invalidates_old_relation(self):
        link = ThesisLink(
            Scope(1, 1), 4, "question:0", "unknown", "rule_candidate", "candidate"
        )
        self.assertFalse(link.is_stale(4))
        self.assertTrue(link.is_stale(5))
        with self.assertRaisesRegex(ValueError, "version_conflict"):
            require_version(5, 4)

    def test_budget_parser_fails_closed(self):
        self.assertFalse(parse_settings({}).model_enabled)
        for raw in [
            {"model_enabled": "false"},
            {"monthly_model_budget_cny": "-1"},
            {"monthly_model_budget_cny": "NaN"},
            {"daily_model_budget_cny": "31"},
            {"api_key": "not-allowed"},
        ]:
            with self.assertRaises(ValueError):
                parse_settings(raw)


if __name__ == "__main__":
    unittest.main()
