import copy
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "engine"))
from rule_assessment import assess


class RuleAssessmentTest(unittest.TestCase):
    def setUp(self):
        self.rule = {"id": "r", "scope": "example", "source": {"kind": "user", "reference": "requirement:1",
                                             "confirmation": "confirmed", "confirmed_by": "reviewer"},
                     "assert": {"type": "field_eq_field", "left": "expected", "right": "actual"}}

    def run_rule(self, rule, observations):
        return assess({"schema": "business-rules/1", "target": "sample", "rules": [rule]},
                      {"observations": observations})["results"][0]

    def observation(self, row, reached=True, refs=None):
        return {"id": "o", "target": "sample", "scope": "example", "row": row, "reached": reached,
                "evidence_refs": ["evidence:1"] if refs is None else refs}

    def test_missing_null_empty_and_unreached_are_unknown(self):
        for observations in ([], [self.observation({})],
                             [self.observation({"expected": None, "actual": None})],
                             [self.observation({"expected": 1, "actual": 1}, reached=False)],
                             [self.observation({"expected": 1, "actual": 1}, refs=[])]):
            with self.subTest(observations=observations):
                self.assertEqual(self.run_rule(self.rule, observations)["status"], "UNKNOWN")

    def test_when_missing_is_unknown_and_false_is_not_applicable(self):
        rule = {**self.rule, "when": {"field": "state", "op": "ne", "value": "closed"}}
        self.assertEqual(self.run_rule(rule, [self.observation({"expected": 1, "actual": 1})])["status"], "UNKNOWN")
        self.assertEqual(self.run_rule(rule, [self.observation({"expected": 1, "actual": 1, "state": "closed"})])["status"], "NOT_APPLICABLE")
        self.assertEqual(self.run_rule({**self.rule, "when": []}, [self.observation({"expected": 1, "actual": 1})])["status"], "UNKNOWN")

    def test_candidate_remains_unknown_even_with_observed_violation(self):
        rule = copy.deepcopy(self.rule)
        rule["source"]["confirmation"] = "unconfirmed"
        result = self.run_rule(rule, [self.observation({"expected": 1, "actual": 2})])
        self.assertEqual(result["observed_status"], "FAIL")
        self.assertEqual(result["status"], "UNKNOWN")
        self.assertFalse(result["gate_eligible"])

    def test_generic_scalar_rules_and_incompatible_types(self):
        for left, right, expected in (("north", "north", "PASS"), ("north", "south", "FAIL"),
                                      (1, "1", "UNKNOWN"), (True, 1, "UNKNOWN")):
            result = self.run_rule(self.rule, [self.observation({"expected": left, "actual": right})])
            self.assertEqual(result["status"], expected)
        stock = {"id": "stock", "scope": "example", "source": self.rule["source"],
                 "assert": {"type": "field_gte_const", "left": "remaining", "value": 0}}
        self.assertEqual(self.run_rule(stock, [self.observation({"remaining": -1})])["status"], "FAIL")
        self.assertEqual(self.run_rule(stock, [self.observation({"remaining": "10"})])["status"], "UNKNOWN")

    def test_other_scope_cannot_satisfy_a_rule(self):
        observation = {**self.observation({"expected": 1, "actual": 1}), "scope": "other"}
        self.assertEqual(self.run_rule(self.rule, [observation])["status"], "UNKNOWN")
        observation = {**self.observation({"expected": 1, "actual": 1}), "target": "other"}
        self.assertEqual(self.run_rule(self.rule, [observation])["status"], "UNKNOWN")

    def test_mixed_unknown_and_pass_does_not_pass(self):
        first = self.observation({"expected": 1, "actual": 1})
        second = {**self.observation({}), "id": "another"}
        self.assertEqual(self.run_rule(self.rule, [first, second])["status"], "UNKNOWN")

    def test_invalid_source_and_unsupported_operation_stay_unknown(self):
        rule = copy.deepcopy(self.rule)
        rule["source"].pop("confirmed_by")
        self.assertEqual(self.run_rule(rule, [self.observation({"expected": 1, "actual": 1})])["status"], "UNKNOWN")
        rule = {**self.rule, "assert": {"type": "unsupported", "left": "expected"}}
        self.assertEqual(self.run_rule(rule, [self.observation({"expected": 1})])["status"], "UNKNOWN")

    def test_empty_rule_pack_is_unknown(self):
        result = assess({"schema": "business-rules/1", "rules": []}, {"observations": []})
        self.assertEqual(result["intake_status"], "UNKNOWN")


if __name__ == "__main__":
    unittest.main()
