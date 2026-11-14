import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "engine"))
from cell_graph import build_graph


class CellGraphTest(unittest.TestCase):
    def test_overload_and_unresolved_edges_remain_visible(self):
        units = {"repo_id": "example", "units": [
            {"kind": "method", "unit_key": "a.C#f", "file": "src/C.java"},
            {"kind": "method", "unit_key": "a.C#f", "file": "src/C.java"},
            {"kind": "method", "unit_key": "a.C#g", "file": ""},
        ]}
        edges = {"edges": [{"kind": "call", "from_agent_id": "fn:a.C#f",
                            "to_agent_id": "fn:UNRESOLVED#x", "resolution": "unknown"}]}
        graph = build_graph(units, edges, {"units": {}, "edges": {}})
        self.assertEqual(graph["counts"]["ambiguous_groups"], 1)
        self.assertEqual(graph["counts"]["missing_source_paths"], 1)
        self.assertEqual(graph["behavior_edges"][0]["from"]["status"], "ambiguous")
        self.assertEqual(graph["behavior_edges"][0]["to"]["status"], "unknown")
        self.assertEqual(graph["structural_edges"][0]["resolution"], "inferred")
        self.assertTrue(all(node["assessment"] == "unknown" for node in graph["nodes"]))
        self.assertEqual(graph, build_graph(units, edges, {"units": {}, "edges": {}}))

    def test_missing_input_contract_fails(self):
        with self.assertRaises(ValueError):
            build_graph({"repo_id": "x"}, {"edges": []}, {})


if __name__ == "__main__":
    unittest.main()
