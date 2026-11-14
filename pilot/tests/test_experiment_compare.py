import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "experiments"))
from compare import compare


class ExperimentCompareTest(unittest.TestCase):
    def setUp(self):
        self.manifest = {"cases": [{"id": x} for x in ("bug", "clean", "missed", "unrun")]}
        self.truth = {"cases": [{"id": "bug", "label": "positive"},
                                {"id": "clean", "label": "negative"},
                                {"id": "missed", "label": "positive"},
                                {"id": "unrun", "label": "positive"}]}

    def test_evidence_missing_and_absent_results_are_unknown(self):
        run = {"method": "example", "cases": [
            {"id": "bug", "verdict": "FAIL", "evidence_refs": ["log"]},
            {"id": "clean", "verdict": "FAIL", "evidence_refs": ["log"]},
            {"id": "missed", "verdict": "PASS", "evidence_refs": []}]}
        result = compare(self.manifest, self.truth, [run])["methods"][0]
        self.assertEqual(result["counts"], {"tp": 1, "fp": 1, "fn": 0, "tn": 0,
                                            "unknown_positive": 2, "unknown_negative": 0})
        self.assertEqual(result["precision"], 0.5)
        self.assertEqual(result["positive_detected_fraction"], 1 / 3)
        self.assertEqual(result["completed_fraction"], 0.5)

    def test_no_detections_has_unknown_precision(self):
        result = compare(self.manifest, self.truth, [{"method": "x", "cases": []}])["methods"][0]
        self.assertIsNone(result["precision"])
        self.assertEqual(result["completed_fraction"], 0)

    def test_rejects_duplicate_and_extra_cases(self):
        for cases in ([{"id": "bug"}, {"id": "bug"}], [{"id": "other", "verdict": "PASS"}]):
            with self.assertRaises(ValueError):
                compare(self.manifest, self.truth, [{"method": "x", "cases": cases}])

    def test_rejects_incomplete_or_unverified_truth(self):
        for truth in ({"cases": self.truth["cases"][:-1]}, {"cases": [{"id": x, "label": "unknown"} for x in ("bug", "clean", "missed", "unrun")]}):
            with self.assertRaises(ValueError):
                compare(self.manifest, truth, [{"method": "x", "cases": []}])


if __name__ == "__main__":
    unittest.main()
