import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "engine"))
import arch_rules as ar
import findings_schema

PILOT = Path(__file__).resolve().parents[1]
CATEGORIES = {"function": ("boundary",), "file": ("cohesion",),
              "class": ("encapsulation_leak", "invariant"),
              "service": ("authz",), "integration": ("end_to_end_business",)}
ROLES = [{"id": "controller", "class_patterns": ["*.controller.*"]},
         {"id": "service", "class_patterns": ["*.service.*"]},
         {"id": "repository", "class_patterns": ["*.repository.*"]},
         {"id": "entity", "class_patterns": ["*.entity.*"]}]

C, S, R, E = "a.controller.C", "a.service.S", "a.repository.R", "a.entity.E"


def config(*rules):
    return {"schema": ar.CONFIG_SCHEMA, "roles": ROLES, "rules": list(rules)}


def rule(rid, rtype, **kw):
    base = {"id": rid, "type": rtype, "severity": "high", "claim": "禁止边"}
    base.update(kw)
    return base


def pair(rid, rtype, frm, to, **kw):
    return rule(rid, rtype, **{"from": frm, "to": to}, **kw)


def unit(key, cls, file="", kind="method"):
    return {"unit_key": key, "kind": kind, "class": cls, "symbol": key, "file": file}


def units(*rows):
    return {"repo_id": "demo", "units": list(rows)}


def edge(eid, frm, to, kind="call", resolution="static", **extra):
    row = {"edge_id": eid, "kind": kind, "from_agent_id": frm, "to_agent_id": to,
           "resolution": resolution, "call_count": 1}
    row.update(extra)
    return row


def edges(*rows):
    return {"edges": list(rows)}


def graph():
    return units(unit(f"{C}#list", C, "src/C.java"), unit(f"{S}#list", S, "src/S.java"),
                 unit(f"{R}#save", R, "src/R.java"), unit(f"{E}#getId", E, "src/E.java"))


def by_id(report):
    return {r["id"]: r for r in report["rules"]}


class CompliantGraphTest(unittest.TestCase):
    def test_compliant_graph_yields_zero_findings(self):
        report = ar.audit(graph(), edges(edge("E1", f"fn:{C}#list", f"fn:{S}#list"),
                                         edge("E2", f"fn:{S}#list", f"fn:{R}#save")),
                          config(pair("A1", "forbidden_call", "controller", "repository"),
                                 rule("A2", "layer_skip", chain=["controller", "service", "repository"]),
                                 pair("A3", "dependency_direction", "entity", "service")),
                          CATEGORIES)
        self.assertEqual(report["findings"], [])
        self.assertEqual(report["counts"]["findings"], 0)
        rules = by_id(report)
        self.assertEqual(rules["A2"]["verdict"], ar.VERDICT_CLEAN)
        self.assertEqual(rules["A2"]["measured_edges"], 2)
        self.assertEqual(rules["A1"]["verdict"], ar.VERDICT_NO_EDGE)
        self.assertEqual(rules["A3"]["verdict"], ar.VERDICT_NO_EDGE)
        self.assertEqual(rules["A1"]["measured_edges"], 0)
        self.assertEqual(rules["A1"]["edges_examined"], 2)
        self.assertEqual(report["measurement"]["rules_with_measurement"], ["A2"])
        self.assertEqual(report["measurement"]["rules_without_measurement"], ["A1", "A3"])
        self.assertEqual(report["measurement"]["judgments_made"], 2)
        self.assertEqual(rules["A2"]["in_scope_edges"], 2)
        self.assertEqual(rules["A2"]["counts"][ar.COMPLIANT], 2)
        self.assertEqual(rules["A1"]["in_scope_edges"], 0)
        self.assertEqual(rules["A1"]["scope_role_classes"], {"controller": 1, "repository": 1})

    def test_external_callee_is_not_applicable(self):
        report = ar.audit(graph(), edges(edge("E1", f"fn:{C}#list", "ext:java.lang.String.valueOf")),
                          config(pair("A1", "forbidden_call", "controller", "repository")), CATEGORIES)
        self.assertEqual(report["findings"], [])
        self.assertEqual(by_id(report)["A1"]["counts"][ar.NOT_APPLICABLE], 1)
        self.assertEqual(report["role_coverage"]["external_endpoints"], 1)


class ForbiddenEdgeTest(unittest.TestCase):
    def test_forbidden_call_hits_with_evidence(self):
        report = ar.audit(graph(), edges(edge("E1", f"fn:{C}#list", f"fn:{R}#save")),
                          config(pair("A1", "forbidden_call", "controller", "repository")), CATEGORIES)
        self.assertEqual(len(report["findings"]), 1)
        finding = report["findings"][0]
        self.assertEqual(finding["id"], "A1:E1")
        self.assertEqual(finding["status"], ar.VIOLATION)
        self.assertEqual(finding["severity"], "high")
        self.assertEqual(finding["from"]["role"], "controller")
        self.assertEqual(finding["to"]["role"], "repository")
        self.assertEqual(finding["from"]["file"], "src/C.java")
        self.assertEqual(finding["edge"]["edge_id"], "E1")
        self.assertIn("edge:E1", finding["evidence_refs"])
        self.assertEqual(finding["ground_truth"], "unverified")
        self.assertEqual(by_id(report)["A1"]["verdict"], ar.VERDICT_VIOLATIONS)

    def test_dependency_direction_covers_type_use_but_forbidden_call_does_not(self):
        snapshot = edges(edge("E1", f"file:{E}", f"file:{S}", kind="type_use"),
                         edge("E2", f"fn:{E}#getId", f"fn:{S}#list"))
        cfg = config(pair("A1", "dependency_direction", "entity", "service"),
                     pair("A2", "forbidden_call", "entity", "service"))
        report = ar.audit(graph(), snapshot, cfg, CATEGORIES)
        self.assertEqual({f["edge"]["edge_id"] for f in report["findings"] if f["rule_id"] == "A1"}, {"E1", "E2"})
        self.assertEqual({f["edge"]["edge_id"] for f in report["findings"] if f["rule_id"] == "A2"}, {"E2"})
        self.assertEqual(by_id(report)["A2"]["reasons"]["edge_kind_out_of_scope"], 1)

    def test_layer_skip_flags_only_forward_skips(self):
        report = ar.audit(graph(), edges(edge("E1", f"fn:{C}#list", f"fn:{R}#save"),
                                         edge("E2", f"fn:{C}#list", f"fn:{S}#list"),
                                         edge("E3", f"fn:{R}#save", f"fn:{C}#list")),
                          config(rule("A1", "layer_skip", chain=["controller", "service", "repository"])),
                          CATEGORIES)
        self.assertEqual([f["edge"]["edge_id"] for f in report["findings"]], ["E1"])
        counts = by_id(report)["A1"]["counts"]
        self.assertEqual((counts[ar.VIOLATION], counts[ar.COMPLIANT], counts[ar.NOT_APPLICABLE]), (1, 1, 1))


class ThreeStateTest(unittest.TestCase):
    def test_unknown_resolution_is_neither_pass_nor_fail(self):
        report = ar.audit(graph(), edges(edge("E1", f"fn:{C}#list", f"fn:{R}#save", resolution="unknown")),
                          config(pair("A1", "forbidden_call", "controller", "repository")), CATEGORIES)
        self.assertEqual(report["findings"], [])
        entry = by_id(report)["A1"]
        self.assertEqual(entry["counts"][ar.UNKNOWN], 1)
        self.assertEqual(entry["counts"][ar.COMPLIANT], 0)
        self.assertEqual(entry["counts"][ar.VIOLATION], 0)
        self.assertEqual(entry["verdict"], ar.VERDICT_UNKNOWN_ONLY)
        self.assertEqual(entry["in_scope_edges"], 1)
        self.assertEqual(report["unknown_evaluations"][0]["reason"], "edge_resolution_unknown")
        self.assertTrue(report["unknown_evaluations"][0]["in_scope"])
        self.assertEqual(report["unresolved_edges"][0]["resolved_rules_in_scope"], ["A1"])
        self.assertEqual(report["counts"]["unresolved_edges"], 1)

    def test_unresolved_endpoint_keeps_declared_class_but_stays_unknown(self):
        report = ar.audit(graph(), edges(edge("E1", f"fn:{S}#list", f"fn:UNRESOLVED#{R}.save", resolution="unknown")),
                          config(pair("A1", "forbidden_call", "service", "repository")), CATEGORIES)
        self.assertEqual(report["findings"], [])
        self.assertEqual(report["unresolved_edges"][0]["declared_callee_class"], R)
        self.assertEqual(by_id(report)["A1"]["verdict"], ar.VERDICT_UNKNOWN_ONLY)

    def test_unclassified_project_class_is_unknown_not_compliant(self):
        report = ar.audit(graph(), edges(edge("E1", "fn:a.mystery.X#f", f"fn:{R}#save")),
                          config(pair("A1", "forbidden_call", "controller", "repository")), CATEGORIES)
        entry = by_id(report)["A1"]
        self.assertEqual(entry["counts"][ar.UNKNOWN], 1)
        self.assertEqual(entry["counts"][ar.COMPLIANT], 0)
        self.assertEqual(entry["reasons"]["role_unclassified"], 1)
        self.assertEqual(report["role_coverage"]["unclassified_classes"],
                         [{"class": "a.mystery.X", "endpoints": 1, "units": 0}])

    def test_external_callee_wins_over_unclassified_caller(self):
        report = ar.audit(graph(), edges(edge("E1", "fn:a.mystery.X#f", "ext:java.lang.String.valueOf")),
                          config(pair("A1", "forbidden_call", "controller", "repository")), CATEGORIES)
        entry = by_id(report)["A1"]
        self.assertEqual(entry["counts"][ar.NOT_APPLICABLE], 1)
        self.assertEqual(entry["counts"][ar.UNKNOWN], 0)
        self.assertEqual(entry["verdict"], ar.VERDICT_NO_EDGE)
        self.assertEqual(entry["measured_edges"], 0)
        self.assertEqual(report["role_coverage"]["unclassified_classes"],
                         [{"class": "a.mystery.X", "endpoints": 1, "units": 0}])

    def test_visibility_unknown_resolution_private_callee_is_not_violation(self):
        """M1b 守卫：声明已供给且确为 private，只把 resolution 换成 unknown，就必须 unknown。"""
        cfg = config(rule("A1", "visibility", levels=["private", "package_private"]))
        snapshot = edges(edge("E1", f"fn:{C}#list", f"fn:{S}#secret", resolution="unknown"))
        report = ar.audit(graph(), snapshot, cfg, CATEGORIES, members={(S, "secret"): "private"})
        self.assertEqual(report["findings"], [])
        entry = by_id(report)["A1"]
        self.assertEqual((entry["counts"][ar.UNKNOWN], entry["counts"][ar.VIOLATION],
                          entry["counts"][ar.COMPLIANT]), (1, 0, 0))
        self.assertEqual(report["unknown_evaluations"][0]["reason"], "edge_resolution_unknown")
        self.assertTrue(report["unknown_evaluations"][0]["in_scope"])

    def test_visibility_unknown_resolution_public_callee_is_not_compliant(self):
        cfg = config(rule("A1", "visibility", levels=["private"]))
        snapshot = edges(edge("E1", f"fn:{C}#list", f"fn:{S}#open", resolution="unknown"))
        report = ar.audit(graph(), snapshot, cfg, CATEGORIES, members={(S, "open"): "public"})
        entry = by_id(report)["A1"]
        self.assertEqual((entry["counts"][ar.UNKNOWN], entry["counts"][ar.COMPLIANT]), (1, 0))
        self.assertEqual(report["unknown_evaluations"][0]["reason"], "edge_resolution_unknown")

    def test_visibility_static_resolution_same_callee_does_fire(self):
        """反向对照：同一设置只把 resolution 换回 static 必须命中，证明上一条不是恒 unknown。"""
        cfg = config(rule("A1", "visibility", levels=["private", "package_private"]))
        snapshot = edges(edge("E1", f"fn:{C}#list", f"fn:{S}#secret"))
        report = ar.audit(graph(), snapshot, cfg, CATEGORIES, members={(S, "secret"): "private"})
        self.assertEqual(len(report["findings"]), 1)
        self.assertEqual(by_id(report)["A1"]["counts"][ar.UNKNOWN], 0)

    def test_visibility_missing_resolution_is_unknown(self):
        cfg = config(rule("A1", "visibility", levels=["private"]))
        row = edge("E1", f"fn:{C}#list", f"fn:{S}#secret")
        del row["resolution"]
        report = ar.audit(graph(), edges(row), cfg, CATEGORIES, members={(S, "secret"): "private"})
        self.assertEqual(report["findings"], [])
        self.assertEqual(report["unknown_evaluations"][0]["reason"], "edge_resolution_unknown")

    def test_missing_member_declaration_is_unknown(self):
        cfg = config(rule("A1", "visibility", levels=["private", "package_private"]))
        snapshot = edges(edge("E1", f"fn:{C}#list", f"fn:{S}#secret"))
        report = ar.audit(graph(), snapshot, cfg, CATEGORIES, members={(S, "other"): "public"})
        self.assertEqual(report["unknown_evaluations"][0]["reason"], "member_declaration_missing")
        self.assertEqual(report["findings"], [])
        self.assertEqual(report["member_coverage"]["declaration_missing"], 1)
        self.assertTrue(any("覆盖不全" in w for w in report["input_warnings"]))

    def test_visibility_without_members_refuses_to_emit_numbers(self):
        cfg = config(rule("A1", "visibility", levels=["private"]))
        with self.assertRaises(ValueError) as ctx:
            ar.audit(graph(), edges(edge("E1", f"fn:{C}#list", f"fn:{S}#secret")), cfg, CATEGORIES)
        self.assertIn("A1", str(ctx.exception))

    def test_role_rules_without_members_are_unaffected(self):
        report = ar.audit(graph(), edges(edge("E1", f"fn:{C}#list", f"fn:{R}#save")),
                          config(pair("A1", "forbidden_call", "controller", "repository")), CATEGORIES)
        self.assertEqual(len(report["findings"]), 1)
        self.assertFalse(report["member_coverage"]["supplied"])

    def test_overload_modifiers_conflict_is_unknown(self):
        members = ar.load_members({"members": [
            {"class": S, "method": "secret", "modifiers": "private"},
            {"class": S, "method": "secret", "modifiers": "public"}]})
        self.assertIsNone(members[(S, "secret")])
        report = ar.audit(graph(), edges(edge("E1", f"fn:{C}#list", f"fn:{S}#secret")),
                          config(rule("A1", "visibility", levels=["private"])), CATEGORIES, members)
        self.assertEqual(report["unknown_evaluations"][0]["reason"], "member_modifiers_ambiguous")
        self.assertEqual(report["findings"], [])


class VisibilityTest(unittest.TestCase):
    def setUp(self):
        self.members = {(S, "secret"): "private", (S, "open"): "public", (S, "pkg"): ""}
        self.cfg = config(rule("A1", "visibility", levels=["private", "package_private"],
                               exempt_same_top_level_class=True,
                               layer="class", category="encapsulation_leak"))

    def test_private_and_package_private_cross_class_calls_hit(self):
        report = ar.audit(graph(), edges(edge("E1", f"fn:{C}#list", f"fn:{S}#secret"),
                                         edge("E2", f"fn:{C}#list", f"fn:{S}#pkg"),
                                         edge("E3", f"fn:{C}#list", f"fn:{S}#open")),
                          self.cfg, CATEGORIES, self.members)
        self.assertEqual({f["edge"]["edge_id"] for f in report["findings"]}, {"E1", "E2"})
        self.assertEqual(by_id(report)["A1"]["counts"][ar.COMPLIANT], 1)
        self.assertEqual(report["findings"][0]["category"], "class.encapsulation_leak")
        self.assertEqual(report["findings"][0]["category_fit"], "exact")

    def test_same_class_and_nestmate_are_exempt(self):
        snapshot = edges(edge("E1", f"fn:{S}#list", f"fn:{S}#secret"),
                         edge("E2", f"fn:{S}$Inner#f", f"fn:{S}#secret"))
        report = ar.audit(graph(), snapshot, self.cfg, CATEGORIES, self.members)
        self.assertEqual(report["findings"], [])
        self.assertEqual(by_id(report)["A1"]["counts"][ar.NOT_APPLICABLE], 2)

    def test_nestmate_exemption_is_configurable(self):
        cfg = config(rule("A1", "visibility", levels=["private"]))
        report = ar.audit(graph(), edges(edge("E1", f"fn:{S}$Inner#f", f"fn:{S}#secret")),
                          cfg, CATEGORIES, self.members)
        self.assertEqual(len(report["findings"]), 1)

    def test_visibility_maps_to_encapsulation_leak(self):
        mapping = ar.resolve_mapping(rule("A1", "visibility", levels=["private"],
                                          layer="class", category="encapsulation_leak"), CATEGORIES)
        self.assertEqual(mapping["category"], "class.encapsulation_leak")
        self.assertEqual(mapping["category_fit"], "exact")


class UnmappedTest(unittest.TestCase):
    def test_rule_without_category_is_kept_as_unmapped(self):
        report = ar.audit(graph(), edges(edge("E1", f"fn:{C}#list", f"fn:{R}#save")),
                          config(pair("A1", "forbidden_call", "controller", "repository")), CATEGORIES)
        self.assertEqual(len(report["findings"]), 1)
        finding = report["findings"][0]
        self.assertEqual(finding["category"], ar.UNMAPPED)
        self.assertEqual(finding["category_fit"], "unmapped")
        self.assertTrue(finding["unmapped_reason"])
        self.assertEqual(report["counts"]["unmapped_findings"], 1)
        self.assertEqual(by_id(report)["A1"]["category"], ar.UNMAPPED)

    def test_category_outside_contract_is_unmapped_with_reason(self):
        for kw in ({"layer": "class", "category": "layering"},
                   {"layer": "nosuch", "category": "encapsulation_leak"},
                   {"layer": "class", "category": "authz"}):
            report = ar.audit(graph(), edges(edge("E1", f"fn:{C}#list", f"fn:{R}#save")),
                              config(pair("A1", "forbidden_call", "controller", "repository",
                                          unmapped_reason="契约装不下", **kw)),
                              CATEGORIES)
            self.assertEqual(len(report["findings"]), 1)
            self.assertEqual(report["findings"][0]["category"], ar.UNMAPPED)
            self.assertEqual(report["findings"][0]["unmapped_reason"], "契约装不下")

    def test_declared_reason_does_not_override_a_valid_mapping(self):
        mapping = ar.resolve_mapping(rule("A1", "visibility", levels=["private"], layer="class",
                                          category="encapsulation_leak", unmapped_reason="x"), CATEGORIES)
        self.assertEqual(mapping["category_fit"], "exact")
        self.assertEqual(mapping["unmapped_reason"], "")


class UnmatchedRuleTest(unittest.TestCase):
    def test_role_pattern_matching_no_class_is_reported_not_green(self):
        roles = [{"id": "controller", "class_patterns": ["*.controller.*"]},
                 {"id": "repository", "class_patterns": ["*.persistence.*"]}]
        cfg = {"schema": ar.CONFIG_SCHEMA, "roles": roles,
               "rules": [pair("A1", "forbidden_call", "controller", "repository")]}
        report = ar.audit(graph(), edges(edge("E1", f"fn:{C}#list", f"fn:{R}#save")), cfg, CATEGORIES)
        entry = by_id(report)["A1"]
        self.assertEqual(report["counts"]["findings"], 0)
        self.assertEqual(entry["verdict"], ar.VERDICT_UNMATCHED)
        self.assertEqual(entry["unmatched_roles"], ["repository"])
        self.assertEqual(entry["scope_role_classes"], {"controller": 1, "repository": 0})
        self.assertEqual(report["counts"]["rules_unmatched"], 1)

    def test_role_populated_in_inventory_but_absent_from_edges_is_not_unmatched(self):
        report = ar.audit(graph(), edges(edge("E1", f"fn:{C}#list", f"fn:{S}#list")),
                          config(pair("A1", "dependency_direction", "entity", "service")), CATEGORIES)
        entry = by_id(report)["A1"]
        self.assertEqual(entry["verdict"], ar.VERDICT_NO_EDGE)
        self.assertEqual(entry["measured_edges"], 0)
        self.assertEqual(entry["unmatched_roles"], [])
        self.assertEqual(entry["scope_role_classes"], {"entity": 1, "service": 1})
        self.assertEqual(entry["in_scope_edges"], 0)

    def test_empty_inputs_do_not_crash(self):
        report = ar.audit(units(), edges(), config(), CATEGORIES)
        self.assertEqual(report["counts"]["edges"], 0)
        self.assertEqual(report["counts"]["findings"], 0)
        self.assertEqual(report["rules"], [])
        report = ar.audit(units(), edges(),
                          config(pair("A1", "forbidden_call", "controller", "repository")), CATEGORIES)
        self.assertEqual(by_id(report)["A1"]["verdict"], ar.VERDICT_UNMATCHED)
        self.assertEqual(report["role_coverage"]["unclassified_classes"], [])

    def test_malformed_input_contract_fails_loud(self):
        with self.assertRaises(ValueError):
            ar.audit({}, {}, config(), CATEGORIES)
        with self.assertRaises(ValueError):
            ar.audit(units(), {"edges": [None]}, config(), CATEGORIES)


class ConfigValidationTest(unittest.TestCase):
    def test_invalid_configs_raise(self):
        bad = [
            {"schema": "wrong", "roles": ROLES, "rules": []},
            {"schema": ar.CONFIG_SCHEMA, "roles": ROLES},
            config(rule("A1", "no_such_type")),
            config(pair("A1", "forbidden_call", "controller", "ghost")),
            config(rule("A1", "layer_skip", chain=["controller"])),
            config(rule("A1", "layer_skip", chain=["controller", "controller"])),
            config(rule("A1", "visibility", levels=["invisible"])),
            config(rule("A1", "visibility", levels=[])),
            config(pair("A1", "forbidden_call", "controller", "repository", severity="")),
            config(pair("A1", "forbidden_call", "controller", "repository", claim=" ")),
            config(pair("A1", "forbidden_call", "controller", "repository"),
                   rule("A1", "layer_skip", chain=["controller", "service"])),
            config(pair("A1", "forbidden_call", "controller", "repository", edge_kinds=[])),
        ]
        for data in bad:
            with self.assertRaises(ValueError):
                ar.validate_config(data)

    def test_duplicate_role_id_raises(self):
        with self.assertRaises(ValueError):
            ar.validate_config({"schema": ar.CONFIG_SCHEMA, "roles": ROLES + [ROLES[0]], "rules": []})

    def test_members_contract_fails_loud(self):
        with self.assertRaises(ValueError):
            ar.load_members({"functions": []})
        with self.assertRaises(ValueError):
            ar.load_members({"members": [None]})
        self.assertEqual(ar.load_members({"members": [{"class": S, "method": "m"}]}), {})

    def test_heterogeneous_rows_skip_but_surface_as_missing_declarations(self):
        artifact = {"nodes": {"a": {"cls": S, "method": "secret", "modifiers": "private"},
                              "t": {"type": "TABLE"}}}
        spec = {"records": "nodes", "class": "cls", "method": "method", "modifiers": "modifiers"}
        members = ar.load_members(artifact, spec)
        self.assertEqual(members, {(S, "secret"): "private"})
        cfg = config(rule("A1", "visibility", levels=["private"]))
        cfg["member_source"] = spec
        snapshot = edges(edge("E1", f"fn:{C}#list", f"fn:{S}#secret"),
                         edge("E2", f"fn:{C}#list", f"fn:{S}#other"))
        report = ar.audit(graph(), snapshot, cfg, CATEGORIES, members)
        self.assertEqual(report["member_coverage"]["declaration_missing"], 1)
        self.assertTrue(any("覆盖不全" in w for w in report["input_warnings"]))


class SourceLocationTest(unittest.TestCase):
    def test_line_states(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "src").mkdir(exist_ok=True)
            (root / "src" / "one.java").write_text("class C {\n  void f() {\n    r.save(x);\n  }\n}\n", encoding="utf-8")
            (root / "src" / "two.java").write_text("r.save(1);\nr.save(2);\n", encoding="utf-8")
            (root / "src" / "none.java").write_text("int save = 1;\n", encoding="utf-8")
            one = ar.locate_call(root, "src/one.java", "save")
            self.assertEqual((one["line"], one["line_status"]), (3, "resolved"))
            self.assertEqual(one["call_text"], "r.save(x);")
            two = ar.locate_call(root, "src/two.java", "save")
            self.assertEqual((two["line"], two["line_status"]), (None, "ambiguous"))
            self.assertEqual(two["line_candidates"], [1, 2])
            self.assertEqual(two["call_text"], "")
            self.assertEqual(ar.locate_call(root, "src/none.java", "save")["line_status"], "not_found")
            self.assertEqual(ar.locate_call(root, "src/gone.java", "save")["line_status"], "source_missing")
            self.assertEqual(ar.locate_call(None, "src/one.java", "save")["line_status"], "no_source_root")
            self.assertEqual(ar.locate_call(root, "../escape.java", "save")["line_status"], "unsafe_source_path")
            self.assertEqual(ar.locate_call(root, "/etc/passwd", "save")["line_status"], "unsafe_source_path")
            self.assertEqual(ar.locate_call(root, "src/one.java", "")["line_status"], "no_callee_symbol")

    def test_finding_carries_resolved_line(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "src").mkdir(exist_ok=True)
            (root / "src" / "C.java").write_text("class C {\n  void f() {\n    repo.save(x);\n  }\n}\n", encoding="utf-8")
            report = ar.audit(graph(), edges(edge("E1", f"fn:{C}#list", f"fn:{R}#save")),
                              config(pair("A1", "forbidden_call", "controller", "repository")),
                              CATEGORIES, source_root=root)
            location = report["findings"][0]["location"]
            self.assertEqual((location["file"], location["line"], location["line_status"]),
                             ("src/C.java", 3, "resolved"))
            self.assertIn("src/C.java:3", report["findings"][0]["evidence_refs"])

    def test_ambiguous_unit_file_is_not_fabricated(self):
        snapshot = units(unit(f"{C}#list", C, "src/C.java"), unit(f"{C}#list", C, "other/C.java"),
                         unit(f"{R}#save", R, "src/R.java"))
        report = ar.audit(snapshot, edges(edge("E1", f"fn:{C}#list", f"fn:{R}#save")),
                          config(pair("A1", "forbidden_call", "controller", "repository")), CATEGORIES)
        location = report["findings"][0]["location"]
        self.assertEqual((location["file"], location["file_status"]), ("", "ambiguous_unit"))
        self.assertEqual(report["findings"][0]["evidence_refs"], ["edge:E1"])


class ExampleConfigTest(unittest.TestCase):
    def test_example_config_validates_against_real_contract(self):
        data = json.loads((PILOT / "arch_rules.example.json").read_text(encoding="utf-8"))
        cfg = ar.validate_config(data)
        categories = findings_schema.load_layer_categories(PILOT.parent / "layers.yaml")
        mappings = {r["id"]: ar.resolve_mapping(r, categories) for r in cfg["rules"]}
        self.assertEqual(mappings["ARCH-004"]["category"], "class.encapsulation_leak")
        self.assertEqual(mappings["ARCH-004"]["category_fit"], "exact")
        for rid in ("ARCH-001", "ARCH-002", "ARCH-003", "ARCH-005", "ARCH-006", "ARCH-007", "ARCH-008"):
            self.assertEqual(mappings[rid]["category"], "class.architecture_contract")
            self.assertEqual(mappings[rid]["category_fit"], "exact")
            self.assertEqual(mappings[rid]["unmapped_reason"], "")
        self.assertIn("architecture_contract", categories["class"])
        self.assertEqual({r["type"] for r in cfg["rules"]}, set(ar.RULE_TYPES))
        self.assertEqual({r["basis"] for r in cfg["rules"]}, set(ar.BASES))
        report = ar.audit(units(), edges(), data, categories, members={("x.Y", "m"): "public"})
        self.assertEqual(report["counts"]["findings"], 0)
        self.assertEqual(report["counts"]["rules_unmatched"], len(cfg["rules"]) - 1)
        self.assertEqual(report["counts"]["contract_gaps"], 0)
        self.assertEqual(report["counts"]["contract_gaps_without_proposal"], 0)
        self.assertEqual(report["contract_gaps"], [])
        self.assertEqual(report["contract_revision_proposal"]["status"], "adopted")
        self.assertEqual(report["contract_revision_proposal"]["category_id"], "architecture_contract")
        self.assertEqual(report["member_coverage"]["supplied"], True)

    def test_mapping_breaks_if_contract_category_is_removed(self):
        """接缝守卫：映射必须对着活契约校验，契约回退则立刻红，不得静默沿用旧类别。"""
        data = json.loads((PILOT / "arch_rules.example.json").read_text(encoding="utf-8"))
        categories = findings_schema.load_layer_categories(PILOT.parent / "layers.yaml")
        reverted = {k: tuple(c for c in v if c != "architecture_contract") for k, v in categories.items()}
        report = ar.audit(units(), edges(), data, reverted, members={("x.Y", "m"): "public"})
        self.assertEqual(report["counts"]["contract_gaps"], 7)
        for gap in report["contract_gaps"]:
            self.assertEqual(gap["status"], "proposed")
        self.assertTrue(all(r["category"] == ar.UNMAPPED for r in report["rules"] if r["id"] != "ARCH-004"))


class DeclaredTypeTest(unittest.TestCase):
    def test_declared_basis_judges_interface_edge_that_resolved_basis_cannot(self):
        snapshot = edges(edge("E1", f"fn:{C}#list", f"fn:UNRESOLVED#{R}.save", resolution="unknown"))
        cfg = config(pair("A1", "forbidden_call", "controller", "repository", basis="declared_type"),
                     pair("A2", "forbidden_call", "controller", "repository"))
        report = ar.audit(graph(), snapshot, cfg, CATEGORIES)
        self.assertEqual(report["findings"], [])
        self.assertEqual(report["counts"]["findings"], 0)
        self.assertEqual(len(report["declared_type_findings"]), 1)
        finding = report["declared_type_findings"][0]
        self.assertEqual(finding["rule_id"], "A1")
        self.assertEqual(finding["basis"], "declared_type")
        self.assertEqual(finding["runtime_resolution"], "unknown")
        self.assertEqual(finding["to"]["class"], R)
        self.assertEqual(report["unknown_evaluations"][0]["rule_id"], "A2")
        self.assertEqual(report["unknown_evaluations"][0]["reason"], "edge_resolution_unknown")
        self.assertEqual(report["declared_type_unknown_evaluations"], [])
        self.assertEqual(report["unresolved_edges"][0]["resolved_rules_in_scope"], ["A2"])
        self.assertEqual(report["unresolved_edges"][0]["declared_rules_in_scope"], ["A1"])

    def test_declared_layer_skip_covers_blind_edges(self):
        snapshot = edges(edge("E1", f"fn:{C}#list", f"fn:{S}#list"),
                         edge("E2", f"fn:{S}#list", f"fn:UNRESOLVED#{R}.save", resolution="unknown"))
        cfg = config(rule("A1", "layer_skip", chain=["controller", "service", "repository"], basis="declared_type"),
                     rule("A2", "layer_skip", chain=["controller", "service", "repository"]))
        report = ar.audit(graph(), snapshot, cfg, CATEGORIES)
        rules = by_id(report)
        self.assertEqual((rules["A1"]["in_scope_edges"], rules["A1"]["counts"][ar.COMPLIANT],
                          rules["A1"]["counts"][ar.UNKNOWN]), (2, 2, 0))
        self.assertEqual((rules["A2"]["in_scope_edges"], rules["A2"]["counts"][ar.COMPLIANT],
                          rules["A2"]["counts"][ar.UNKNOWN]), (2, 1, 1))
        self.assertEqual(rules["A1"]["verdict"], ar.VERDICT_CLEAN)
        self.assertEqual(rules["A2"]["verdict"], ar.VERDICT_PARTIAL)

    def test_declared_basis_still_unknown_when_type_unrecoverable(self):
        snapshot = edges(edge("E1", f"fn:{C}#list", "fn:UNRESOLVED#save", resolution="unknown"))
        report = ar.audit(graph(), snapshot,
                          config(pair("A1", "forbidden_call", "controller", "repository", basis="declared_type")),
                          CATEGORIES)
        self.assertEqual(report["declared_type_findings"], [])
        self.assertEqual(report["declared_type_unknown_evaluations"][0]["reason"], "role_unclassified")

    def test_declared_skip_is_a_violation_not_only_compliance(self):
        snapshot = edges(edge("E1", f"fn:{C}#list", f"fn:UNRESOLVED#{R}.save", resolution="unknown"))
        report = ar.audit(graph(), snapshot,
                          config(rule("A1", "layer_skip", chain=["controller", "service", "repository"],
                                      basis="declared_type")), CATEGORIES)
        self.assertEqual(len(report["declared_type_findings"]), 1)
        self.assertEqual(report["declared_type_findings"][0]["location"]["file"], "src/C.java")

    def test_visibility_rejects_declared_basis(self):
        with self.assertRaises(ValueError):
            ar.validate_config(config(rule("A1", "visibility", levels=["private"], basis="declared_type")))


class MechanismRoleTest(unittest.TestCase):
    def test_mechanism_fact_overrides_name_pattern_and_disagreement_is_reported(self):
        facts = {"facts": [{"class": S, "mechanism": "HTTP_ENTRY"}]}
        cfg = config(pair("A1", "forbidden_call", "controller", "repository"))
        cfg["role_mechanisms"] = {"controller": ["HTTP_ENTRY"], "service": ["SVC_OP"]}
        report = ar.audit(graph(), edges(edge("E1", f"fn:{S}#list", f"fn:{R}#save")), cfg, CATEGORIES,
                          role_facts=facts)
        self.assertEqual(len(report["findings"]), 1)
        self.assertEqual(report["findings"][0]["from"]["role"], "controller")
        validation = report["role_validation"]
        self.assertEqual((validation["classes_checked"], validation["agree"], validation["disagree"]), (1, 0, 1))
        self.assertEqual(validation["disagreements"],
                         [{"class": S, "mechanism_role": "controller", "name_pattern_role": "service"}])

    def test_agreement_is_reported_when_mechanism_matches_name(self):
        facts = {"facts": [{"class": C, "mechanism": "HTTP_ENTRY"}, {"class": S, "mechanism": "SVC_OP"},
                           {"class": R, "mechanism": "REPO_OP"}]}
        cfg = config(pair("A1", "forbidden_call", "controller", "repository"))
        cfg["role_mechanisms"] = {"controller": ["HTTP_ENTRY"], "service": ["SVC_OP"], "repository": ["REPO_OP"]}
        report = ar.audit(graph(), edges(edge("E1", f"fn:{C}#list", f"fn:{S}#list")), cfg, CATEGORIES,
                          role_facts=facts)
        validation = report["role_validation"]
        self.assertEqual((validation["classes_checked"], validation["agree"], validation["disagree"]), (3, 3, 0))
        self.assertEqual(report["findings"], [])

    def test_conflicting_mechanisms_fall_back_to_name_pattern(self):
        facts = {"facts": [{"class": S, "mechanism": "HTTP_ENTRY"}, {"class": S, "mechanism": "SVC_OP"}]}
        cfg = config(pair("A1", "forbidden_call", "controller", "repository"))
        cfg["role_mechanisms"] = {"controller": ["HTTP_ENTRY"], "service": ["SVC_OP"]}
        report = ar.audit(graph(), edges(edge("E1", f"fn:{S}#list", f"fn:{R}#save")), cfg, CATEGORIES,
                          role_facts=facts)
        self.assertEqual(report["findings"], [])
        self.assertEqual(report["role_validation"]["fact_conflicts"], [{"class": S, "roles": ["controller", "service"]}])
        self.assertEqual(report["role_validation"]["classes_with_facts"], 0)

    def test_unmapped_mechanism_is_ignored(self):
        facts = {"facts": [{"class": S, "mechanism": "TABLE"}]}
        cfg = config(pair("A1", "forbidden_call", "controller", "repository"))
        cfg["role_mechanisms"] = {"controller": ["HTTP_ENTRY"]}
        report = ar.audit(graph(), edges(), cfg, CATEGORIES, role_facts=facts)
        self.assertEqual(report["role_validation"]["classes_with_facts"], 0)

    def test_role_facts_contract_fails_loud(self):
        with self.assertRaises(ValueError):
            ar.load_role_facts({"nodes": []}, {})
        with self.assertRaises(ValueError):
            ar.load_role_facts([], {})
        self.assertEqual(ar.load_role_facts({"facts": [{"class": S}]}, {}), ({}, []))
        with self.assertRaises(ValueError):
            ar.validate_config({"schema": ar.CONFIG_SCHEMA, "roles": ROLES, "rules": [],
                                "role_mechanisms": {"ghost": ["X"]}})
        with self.assertRaises(ValueError):
            ar.validate_config({"schema": ar.CONFIG_SCHEMA, "roles": ROLES, "rules": [],
                                "opaque_dispatch_patterns": [3]})
        with self.assertRaises(ValueError):
            ar.validate_config(config(pair("A1", "forbidden_call", "controller", "repository", basis="guess")))


class DispatchOpacityTest(unittest.TestCase):
    def setUp(self):
        self.cfg = config(pair("A1", "forbidden_call", "controller", "repository"))
        self.cfg["opaque_dispatch_patterns"] = ["java.lang.invoke.SerializedLambda.*", "java.lang.reflect.*"]

    def test_opaque_dispatch_cells_are_enumerated(self):
        snapshot = edges(edge("E1", f"fn:{S}#list", "ext:java.lang.invoke.SerializedLambda.getImplMethodName"),
                         edge("E2", f"fn:{S}#list", "ext:java.lang.reflect.Method.invoke"),
                         edge("E3", f"fn:{C}#list", f"fn:{S}#list"))
        report = ar.audit(graph(), snapshot, self.cfg, CATEGORIES)
        opacity = report["dispatch_opacity"]
        self.assertEqual(opacity["matched_edges"], 2)
        self.assertEqual(len(opacity["cells"]), 1)
        cell = opacity["cells"][0]
        self.assertEqual(cell["reference"], f"fn:{S}#list")
        self.assertEqual(cell["edge_ids"], ["E1", "E2"])
        self.assertEqual(cell["patterns"], ["java.lang.invoke.SerializedLambda.*", "java.lang.reflect.*"])

    def test_opacity_does_not_change_verdicts(self):
        snapshot = edges(edge("E1", f"fn:{C}#list", f"fn:{R}#save"),
                         edge("E2", f"fn:{S}#list", "ext:java.lang.reflect.Method.invoke"))
        with_opacity = ar.audit(graph(), snapshot, self.cfg, CATEGORIES)
        plain = config(pair("A1", "forbidden_call", "controller", "repository"))
        without = ar.audit(graph(), snapshot, plain, CATEGORIES)
        self.assertEqual(with_opacity["findings"], without["findings"])
        self.assertEqual(with_opacity["rules"], without["rules"])
        self.assertEqual(without["dispatch_opacity"]["matched_edges"], 0)
        self.assertEqual(without["dispatch_opacity"]["cells"], [])


class UnprojectedDispatchTest(unittest.TestCase):
    def setUp(self):
        self.cfg = config(pair("A1", "forbidden_call", "controller", "repository"))
        self.cfg["unprojected_dispatch_patterns"] = [{"id": "method_reference", "regex": r"(\w+)::(\w+)"}]

    def run_scan(self, root, snapshot, source):
        (root / "src").mkdir(exist_ok=True)
        (root / "src" / "S.java").write_text(source, encoding="utf-8")
        return ar.audit(graph(), snapshot, self.cfg, CATEGORIES, source_root=root)

    def test_unprojected_method_reference_is_reported_once_per_file(self):
        with tempfile.TemporaryDirectory() as tmp:
            report = self.run_scan(Path(tmp), edges(edge("E1", f"fn:{S}#list", "ext:java.util.stream.Stream.map")),
                                   "class S {\n  void f() {\n    list.map(Mapper::toResponse);\n  }\n}\n")
            section = report["unprojected_dispatch"]
            self.assertEqual(section["scanned_files"], 1)
            self.assertEqual(len(section["occurrences"]), 1)
            occ = section["occurrences"][0]
            self.assertEqual((occ["file"], occ["line"], occ["text"]), ("src/S.java", 3, "Mapper::toResponse"))
            self.assertEqual((occ["type_hint"], occ["member_hint"]), ("Mapper", "toResponse"))
            self.assertEqual(occ["status"], "unprojected")
            self.assertIsNone(occ["enclosing_cell"])
            self.assertEqual(occ["candidate_cells"], [f"fn:{S}#list"])
            self.assertEqual(section["files_with_unprojected"], ["src/S.java"])
            self.assertEqual(report["counts"]["unprojected_dispatch_occurrences"], 1)
            self.assertEqual(report["counts"]["files_with_unprojected_dispatch"], 1)

    def test_occurrence_is_not_duplicated_per_cell_sharing_the_file(self):
        snapshot = units(unit(f"{S}#list", S, "src/S.java"), unit(f"{S}#other", S, "src/S.java"),
                         unit(f"{R}#save", R, "src/R.java"))
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "src").mkdir(exist_ok=True)
            (root / "src" / "S.java").write_text("Mapper::toResponse;\n", encoding="utf-8")
            report = ar.audit(snapshot, edges(edge("E1", f"fn:{S}#list", "ext:java.util.stream.Stream.map"),
                                              edge("E2", f"fn:{S}#other", "ext:java.util.stream.Stream.map")),
                              self.cfg, CATEGORIES, source_root=root)
            section = report["unprojected_dispatch"]
            self.assertEqual(len(section["occurrences"]), 1)
            self.assertEqual(section["occurrences"][0]["candidate_cells"], [f"fn:{S}#list", f"fn:{S}#other"])
            self.assertEqual(report["counts"]["unprojected_dispatch_occurrences"], 1)

    def test_match_elsewhere_in_file_is_unknown_not_projected(self):
        with tempfile.TemporaryDirectory() as tmp:
            report = self.run_scan(Path(tmp),
                                   edges(edge("E1", f"fn:{S}#list", "ext:java.util.stream.Stream.map"),
                                         edge("E2", f"fn:{S}#list", "fn:a.mapper.Mapper#toResponse")),
                                   "Mapper::toResponse;\n")
            occ = report["unprojected_dispatch"]["occurrences"][0]
            self.assertEqual(occ["status"], "matched_in_file")
            self.assertEqual(occ["matching_edge_ids"], ["E2"])
            self.assertEqual(report["counts"]["unprojected_dispatch_occurrences"], 0)
            self.assertEqual(report["counts"]["dispatch_association_unknown"], 1)
            self.assertEqual(report["unprojected_dispatch"]["files_with_unprojected"], [])

    def test_expression_receiver_is_not_associable(self):
        with tempfile.TemporaryDirectory() as tmp:
            report = self.run_scan(Path(tmp), edges(edge("E1", f"fn:{S}#list", "ext:java.util.List.add")),
                                   "items::add;\n")
            occ = report["unprojected_dispatch"]["occurrences"][0]
            self.assertEqual(occ["status"], "not_associable")
            self.assertEqual(report["counts"]["unprojected_dispatch_occurrences"], 0)
            self.assertEqual(report["counts"]["dispatch_association_unknown"], 1)

    def test_this_receiver_associates_against_own_class(self):
        with tempfile.TemporaryDirectory() as tmp:
            report = self.run_scan(Path(tmp), edges(edge("E1", f"fn:{S}#list", f"fn:{S}#helper")),
                                   "this::helper;\n")
            self.assertEqual(report["unprojected_dispatch"]["occurrences"][0]["status"], "matched_in_file")
            report = self.run_scan(Path(tmp), edges(edge("E1", f"fn:{S}#list", "ext:java.util.List.add")),
                                   "this::helper;\n")
            self.assertEqual(report["unprojected_dispatch"]["occurrences"][0]["status"], "unprojected")

    def test_without_source_root_nothing_is_claimed(self):
        report = ar.audit(graph(), edges(edge("E1", f"fn:{S}#list", "ext:java.util.stream.Stream.map")),
                          self.cfg, CATEGORIES)
        self.assertEqual(report["unprojected_dispatch"]["scanned_files"], 0)
        self.assertEqual(report["unprojected_dispatch"]["occurrences"], [])
        self.assertEqual(report["counts"]["unprojected_dispatch_occurrences"], 0)

    def test_array_constructor_reference_is_covered_by_config_pattern(self):
        self.cfg["unprojected_dispatch_patterns"] = [{"id": "method_reference", "regex": r"(\w+)(?:\[\])?::(\w+)"}]
        with tempfile.TemporaryDirectory() as tmp:
            report = self.run_scan(Path(tmp), edges(edge("E1", f"fn:{S}#list", "ext:java.util.stream.Stream.toArray")),
                                   "s.toArray(String[]::new);\n")
            occ = report["unprojected_dispatch"]["occurrences"][0]
            self.assertEqual((occ["text"], occ["type_hint"], occ["member_hint"]), ("String[]::new", "String", "new"))
            self.assertEqual(occ["status"], "unprojected")

    def test_file_without_outgoing_edges_is_still_scanned(self):
        snapshot = units(unit(f"{E}#getId", E, "src/E.java"))
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "src").mkdir(exist_ok=True)
            (root / "src" / "E.java").write_text("Mapper::toResponse;\n", encoding="utf-8")
            report = ar.audit(snapshot, edges(), self.cfg, CATEGORIES, source_root=root)
            section = report["unprojected_dispatch"]
            self.assertEqual(section["scanned_files"], 1)
            self.assertEqual(section["occurrences"][0]["status"], "unprojected")
            self.assertEqual(section["occurrences"][0]["candidate_cells"], [f"fn:{E}#getId"])

    def test_invalid_form_config_raises(self):
        for forms in ([{"id": "x", "regex": "("}],
                      [{"id": "x", "regex": r"(\w+)"}],
                      [{"id": "x", "regex": r"(\w+)::(\w+)::(\w+)"}],
                      [{"regex": r"(\w+)::(\w+)"}],
                      [{"id": "x"}],
                      [{"id": "x", "regex": r"(\w+)::(\w+)"}, {"id": "x", "regex": r"(\w+)::(\w+)"}],
                      "not-a-list"):
            cfg = config()
            cfg["unprojected_dispatch_patterns"] = forms
            with self.assertRaises(ValueError):
                ar.validate_config(cfg)


class InductionTest(unittest.TestCase):
    def cfg(self, basis="declared_type", min_support=1, *rules):
        data = config(*rules)
        data["induction"] = {"min_support": min_support, "basis": basis}
        return data

    def chain(self, resolution="static"):
        return edges(edge("E1", f"fn:{C}#list", f"fn:{S}#list"),
                     edge("E2", f"fn:{S}#list", f"fn:{R}#save", resolution=resolution))

    def test_obligate_pathway_without_direct_binding_is_induced(self):
        report = ar.audit(graph(), self.chain(), self.cfg(), CATEGORIES)
        induced = report["induced_candidates"]
        self.assertTrue(induced["enabled"])
        self.assertEqual(len(induced["candidates"]), 1)
        cand = induced["candidates"][0]
        self.assertEqual((cand["from"], cand["to"], cand["status"]), ("controller", "repository", "candidate"))
        self.assertEqual(cand["shortest_observed_path"], ["controller", "service", "repository"])
        self.assertEqual(cand["confirmation"], "unconfirmed")
        self.assertEqual(cand["support"]["to_role_caller_roles"], ["service"])
        self.assertEqual(cand["support"]["direct_edges"], 0)
        self.assertEqual(report["counts"]["induced_candidates"], 1)
        self.assertEqual(report["findings"], [])
        self.assertEqual(report["rules"], [])

    def test_direct_binding_suppresses_induction(self):
        snapshot = edges(edge("E1", f"fn:{C}#list", f"fn:{S}#list"),
                         edge("E2", f"fn:{S}#list", f"fn:{R}#save"),
                         edge("E3", f"fn:{C}#list", f"fn:{R}#save"))
        report = ar.audit(graph(), snapshot, self.cfg(), CATEGORIES)
        self.assertEqual(report["induced_candidates"]["candidates"], [])

    def test_resolved_basis_cannot_induce_absence_from_unknown_edges(self):
        snapshot = edges(edge("E1", f"fn:{C}#list", f"fn:{S}#list"),
                         edge("E2", f"fn:{S}#list", f"fn:{R}#save"),
                         edge("E3", f"fn:{C}#list", f"fn:{R}#save", resolution="unknown"))
        report = ar.audit(graph(), snapshot, self.cfg(basis="resolved_target"), CATEGORIES)
        cand = report["induced_candidates"]["candidates"][0]
        self.assertEqual(cand["status"], ar.UNKNOWN)
        self.assertEqual(cand["reason"], "absence_not_established_unresolved_edges")
        self.assertEqual(cand["support"]["unresolved_direct_edges"], 1)
        self.assertEqual(report["counts"]["induced_candidates"], 0)
        self.assertEqual(report["counts"]["induced_unknown"], 1)

    def test_declared_basis_treats_unknown_edge_as_observed_binding(self):
        snapshot = edges(edge("E1", f"fn:{C}#list", f"fn:{S}#list"),
                         edge("E2", f"fn:{S}#list", f"fn:{R}#save"),
                         edge("E3", f"fn:{C}#list", f"fn:UNRESOLVED#{R}.save", resolution="unknown"))
        report = ar.audit(graph(), snapshot, self.cfg(), CATEGORIES)
        self.assertEqual(report["induced_candidates"]["candidates"], [])

    def test_min_support_filters_low_traffic_roles(self):
        report = ar.audit(graph(), self.chain(), self.cfg(min_support=5), CATEGORIES)
        self.assertEqual(report["induced_candidates"]["candidates"], [])

    def test_already_declared_pair_is_not_re_induced(self):
        data = self.cfg("declared_type", 1, pair("A1", "dependency_direction", "controller", "repository",
                                                basis="declared_type"))
        report = ar.audit(graph(), self.chain(), data, CATEGORIES)
        self.assertEqual(report["induced_candidates"]["candidates"], [])

    def test_disabled_without_config_and_empty_graph_is_safe(self):
        report = ar.audit(graph(), self.chain(), config(), CATEGORIES)
        self.assertFalse(report["induced_candidates"]["enabled"])
        self.assertEqual(report["induced_candidates"]["candidates"], [])
        report = ar.audit(units(), edges(), self.cfg(), CATEGORIES)
        self.assertEqual(report["induced_candidates"]["candidates"], [])
        self.assertEqual(report["induced_candidates"]["observed_role_pairs"], {})

    def test_self_and_external_pairs_are_not_induced(self):
        snapshot = edges(edge("E1", f"fn:{C}#list", f"fn:{C}#other"),
                         edge("E2", f"fn:{C}#list", "ext:java.lang.String.valueOf"))
        report = ar.audit(graph(), snapshot, self.cfg(), CATEGORIES)
        self.assertEqual(report["induced_candidates"]["observed_role_pairs"], {})
        self.assertEqual(report["induced_candidates"]["candidates"], [])

    def test_invalid_induction_config_raises(self):
        for spec in ({"min_support": 0}, {"min_support": True}, {"min_support": "5"},
                     {"basis": "guess"}, "not-a-dict"):
            data = config()
            data["induction"] = spec
            with self.assertRaises(ValueError):
                ar.validate_config(data)


class UncommittedMarkerTest(unittest.TestCase):
    """上游把 unknown 编码进 resolution 以外的字段时，不得被读成合规或违规。"""

    def cfg(self, *rules, markers=True):
        data = config(*rules)
        if markers:
            data["unknown_markers"] = [{"field": "access", "values": ["unknown"]}]
        return data

    def test_unknown_access_blocks_resolved_channel_even_when_resolution_static(self):
        snapshot = edges(edge("E1", f"fn:{C}#list", f"fn:{R}#save", access="unknown"))
        report = ar.audit(graph(), snapshot,
                          self.cfg(pair("A1", "forbidden_call", "controller", "repository")), CATEGORIES)
        entry = by_id(report)["A1"]
        self.assertEqual(report["findings"], [])
        self.assertEqual((entry["counts"][ar.UNKNOWN], entry["counts"][ar.COMPLIANT],
                          entry["counts"][ar.VIOLATION]), (1, 0, 0))
        self.assertEqual(report["unknown_evaluations"][0]["reason"], "uncommitted_access_unknown")

    def test_unknown_access_blocks_declared_channel_too(self):
        snapshot = edges(edge("E1", f"fn:{C}#list", f"fn:UNRESOLVED#{R}.save",
                              resolution="unknown", access="unknown"))
        report = ar.audit(graph(), snapshot,
                          self.cfg(pair("A1", "forbidden_call", "controller", "repository",
                                        basis="declared_type")), CATEGORIES)
        self.assertEqual(report["declared_type_findings"], [])
        self.assertEqual(report["declared_type_unknown_evaluations"][0]["reason"], "uncommitted_access_unknown")

    def test_unknown_access_blocks_visibility(self):
        snapshot = edges(edge("E1", f"fn:{C}#list", f"fn:{S}#secret", access="unknown"))
        report = ar.audit(graph(), snapshot,
                          self.cfg(rule("A1", "visibility", levels=["private"])), CATEGORIES,
                          members={(S, "secret"): "private"})
        self.assertEqual(report["findings"], [])
        self.assertEqual(report["unknown_evaluations"][0]["reason"], "uncommitted_access_unknown")

    def test_committed_access_is_judged_normally(self):
        snapshot = edges(edge("E1", f"fn:{C}#list", f"fn:{R}#save", access="write"))
        report = ar.audit(graph(), snapshot,
                          self.cfg(pair("A1", "forbidden_call", "controller", "repository")), CATEGORIES)
        self.assertEqual(len(report["findings"]), 1)
        self.assertEqual(report["findings"][0]["edge"]["uncommitted"], "")

    def test_marker_must_be_declared_not_guessed(self):
        snapshot = edges(edge("E1", f"fn:{C}#list", f"fn:{R}#save", access="unknown"))
        report = ar.audit(graph(), snapshot,
                          self.cfg(pair("A1", "forbidden_call", "controller", "repository"), markers=False),
                          CATEGORIES)
        self.assertEqual(len(report["findings"]), 1)

    def test_invalid_marker_config_raises(self):
        for markers in ([{"values": ["unknown"]}], [{"field": "access"}],
                        [{"field": "access", "values": []}], [{"field": "access", "values": "unknown"}],
                        [{"field": "", "values": ["unknown"]}], "not-a-list"):
            data = config()
            data["unknown_markers"] = markers
            with self.assertRaises(ValueError):
                ar.validate_config(data)


class UpstreamEvidenceTest(unittest.TestCase):
    def test_upstream_evidence_wins_over_text_match(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "src").mkdir(exist_ok=True)
            (root / "src" / "C.java").write_text("r.save(x);\n", encoding="utf-8")
            snapshot = edges(edge("E1", f"fn:{C}#list", f"fn:{R}#save",
                                  evidence=[{"file": str(root / "src" / "C.java"), "line": 7}]))
            report = ar.audit(graph(), snapshot,
                              config(pair("A1", "forbidden_call", "controller", "repository")),
                              CATEGORIES, source_root=root)
            location = report["findings"][0]["location"]
            self.assertEqual((location["file"], location["line"]), ("src/C.java", 7))
            self.assertEqual(location["line_status"], "upstream_evidence")
            self.assertIn("src/C.java:7", report["findings"][0]["evidence_refs"])

    def test_conflicting_upstream_evidence_gives_no_unique_line(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            snapshot = edges(edge("E1", f"fn:{C}#list", f"fn:{R}#save",
                                  evidence=[{"file": "src/C.java", "line": 3},
                                            {"file": "src/C.java", "line": 9}]))
            report = ar.audit(graph(), snapshot,
                              config(pair("A1", "forbidden_call", "controller", "repository")),
                              CATEGORIES, source_root=root)
            location = report["findings"][0]["location"]
            self.assertIsNone(location["line"])
            self.assertEqual(location["line_status"], "upstream_evidence_ambiguous")
            self.assertEqual(location["line_candidates"], [3, 9])

    def test_without_evidence_falls_back_to_text_match(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "src").mkdir(exist_ok=True)
            (root / "src" / "C.java").write_text("class C {\n  void f() {\n    r.save(x);\n  }\n}\n", encoding="utf-8")
            report = ar.audit(graph(), edges(edge("E1", f"fn:{C}#list", f"fn:{R}#save")),
                              config(pair("A1", "forbidden_call", "controller", "repository")),
                              CATEGORIES, source_root=root)
            location = report["findings"][0]["location"]
            self.assertEqual((location["line"], location["line_status"]), (3, "resolved"))

    def test_proxy_mode_is_surfaced_not_silently_dropped(self):
        snapshot = edges(edge("E1", f"fn:{C}#list", f"fn:{R}#save", proxy_mode="bypassed"),
                         edge("E2", f"fn:{S}#list", f"fn:{R}#save", proxy_mode="bypassed"),
                         edge("E3", f"fn:{S}#list", f"fn:{R}#findAll"))
        report = ar.audit(graph(), snapshot,
                          config(pair("A1", "forbidden_call", "controller", "repository")), CATEGORIES)
        self.assertEqual(report["proxy_mode_census"], {"bypassed": 2})
        self.assertEqual(report["findings"][0]["edge"]["proxy_mode"], "bypassed")


class InputWarningTest(unittest.TestCase):
    def test_missing_members_is_a_hard_error_not_a_different_number(self):
        cfg = config(rule("A1", "visibility", levels=["private"]))
        snapshot = edges(edge("E1", f"fn:{C}#list", f"fn:{S}#secret"))
        with self.assertRaises(ValueError):
            ar.audit(graph(), snapshot, cfg, CATEGORIES)

    def test_supplied_members_clear_the_warning(self):
        cfg = config(rule("A1", "visibility", levels=["private"]))
        snapshot = edges(edge("E1", f"fn:{C}#list", f"fn:{S}#secret"))
        report = ar.audit(graph(), snapshot, cfg, CATEGORIES, members={(S, "secret"): "private"})
        self.assertFalse(any("成员声明" in w for w in report["input_warnings"]))
        self.assertEqual(report["member_coverage"]["judged"], 1)

    def test_missing_markers_and_source_root_warn(self):
        report = ar.audit(graph(), edges(), config(), CATEGORIES)
        self.assertTrue(any("unknown_markers" in w for w in report["input_warnings"]))
        self.assertTrue(any("源码根" in w for w in report["input_warnings"]))

    def test_mechanism_config_without_facts_warns(self):
        cfg = config()
        cfg["role_mechanisms"] = {"controller": ["HTTP_ENTRY"]}
        report = ar.audit(graph(), edges(), cfg, CATEGORIES)
        self.assertTrue(any("机制事实" in w for w in report["input_warnings"]))


class UnresolvedEncodingTest(unittest.TestCase):
    """上游两种 UNRESOLVED 编码都必须解析出声明侧类型，不得把类名切成包名。"""

    def test_dot_form(self):
        ep = ar.parse_endpoint(f"fn:UNRESOLVED#{R}.save")
        self.assertEqual((ep["class"], ep["method"], ep["scope"]), (R, "save", "unresolved"))

    def test_hash_form(self):
        ep = ar.parse_endpoint(f"fn:UNRESOLVED#{R}#save")
        self.assertEqual((ep["class"], ep["method"], ep["scope"]), (R, "save", "unresolved"))

    def test_hash_form_gets_a_role_not_unclassified(self):
        roles = ar.validate_config(config())["roles"]
        for ref in (f"fn:UNRESOLVED#{R}.save", f"fn:UNRESOLVED#{R}#save"):
            ep = ar.parse_endpoint(ref)
            self.assertEqual(ar.classify(ep, roles), "repository")

    def test_both_encodings_are_judgeable_in_declared_channel(self):
        for ref in (f"fn:UNRESOLVED#{R}.save", f"fn:UNRESOLVED#{R}#save"):
            snapshot = edges(edge("E1", f"fn:{C}#list", ref, resolution="unknown"))
            report = ar.audit(graph(), snapshot,
                              config(pair("A1", "forbidden_call", "controller", "repository",
                                          basis="declared_type"),
                                     pair("A2", "forbidden_call", "controller", "repository")), CATEGORIES)
            self.assertEqual(len(report["declared_type_findings"]), 1)
            self.assertEqual(report["declared_type_findings"][0]["to"]["class"], R)
            self.assertEqual(report["findings"], [])
            self.assertEqual(report["unknown_evaluations"][0]["rule_id"], "A2")
            self.assertEqual(report["unknown_evaluations"][0]["reason"], "edge_resolution_unknown")

    def test_hash_form_without_class_stays_unknown(self):
        ep = ar.parse_endpoint("fn:UNRESOLVED#save")
        self.assertEqual(ep["class"], "save")
        report = ar.audit(graph(), edges(edge("E1", f"fn:{C}#list", "fn:UNRESOLVED#save", resolution="unknown")),
                          config(pair("A1", "forbidden_call", "controller", "repository",
                                      basis="declared_type")), CATEGORIES)
        self.assertEqual(report["declared_type_findings"], [])
        self.assertEqual(report["declared_type_unknown_evaluations"][0]["reason"], "role_unclassified")


class ArtifactShapeTest(unittest.TestCase):
    """从已有项目产物直接取事实，不需要手工中间文件。"""

    def test_members_from_declaration_artifact_shape(self):
        spec = {"records": "functions", "class": "class", "method": "method", "modifiers": "modifiers"}
        artifact = {"meta": {"note": "javap"}, "functions": [
            {"class": S, "method": "secret", "modifiers": "private", "params": []},
            {"class": S, "method": "open", "modifiers": "public", "params": []}]}
        members = ar.load_members(artifact, spec)
        self.assertEqual(members, {(S, "secret"): "private", (S, "open"): "public"})
        cfg = config(rule("A1", "visibility", levels=["private"]))
        cfg["member_source"] = spec
        self.assertEqual(ar.validate_config(cfg)["member_source"], spec)
        report = ar.audit(graph(), edges(edge("E1", f"fn:{C}#list", f"fn:{S}#secret")), cfg, CATEGORIES, members)
        self.assertEqual(len(report["findings"]), 1)

    def test_role_facts_from_relation_net_dict_container(self):
        spec = {"records": "nodes", "class": "cls", "mechanism": "type"}
        artifact = {"nodes": {
            f"{C}#list": {"cls": C, "type": "HTTP_ENTRY", "method": "list"},
            f"{S}#list": {"cls": S, "type": "SVC_OP", "method": "list"},
            "table:x": {"type": "TABLE"},
            f"{R}#save": {"cls": R, "type": "REPO_OP", "method": "save"}}}
        cfg = config(pair("A1", "forbidden_call", "controller", "repository"))
        cfg["role_mechanisms"] = {"controller": ["HTTP_ENTRY"], "service": ["SVC_OP"], "repository": ["REPO_OP"]}
        cfg["role_fact_source"] = spec
        facts, conflicts = ar.load_role_facts(artifact, cfg["role_mechanisms"], spec)
        self.assertEqual(facts, {C: "controller", S: "service", R: "repository"})
        self.assertEqual(conflicts, [])
        report = ar.audit(graph(), edges(edge("E1", f"fn:{C}#list", f"fn:{S}#list")), cfg, CATEGORIES,
                          role_facts=artifact)
        self.assertEqual(report["role_validation"]["classes_checked"], 3)
        self.assertEqual(report["role_validation"]["agree"], 3)

    def test_missing_record_container_fails_loud(self):
        with self.assertRaises(ValueError):
            ar.load_members({"functions": []}, {"records": "members", "class": "class",
                                               "method": "method", "modifiers": "modifiers"})
        with self.assertRaises(ValueError):
            ar.load_role_facts({"facts": []}, {}, {"records": "nodes", "class": "cls", "mechanism": "type"})

    def test_invalid_source_shape_config_raises(self):
        for key in ("member_source", "role_fact_source"):
            for spec in ({"records": ""}, {"records": "x", "class": 3}, "not-a-dict"):
                cfg = config()
                cfg[key] = spec
                with self.assertRaises(ValueError):
                    ar.validate_config(cfg)
        cfg = config()
        cfg["source_root_key"] = ""
        with self.assertRaises(ValueError):
            ar.validate_config(cfg)


class ContractGapTest(unittest.TestCase):
    def test_unmapped_rule_with_proposal_is_surfaced(self):
        proposal = {"action": "add_category", "layer": "class", "category_id": "architecture_contract"}
        report = ar.audit(graph(), edges(edge("E1", f"fn:{C}#list", f"fn:{R}#save")),
                          config(pair("A1", "forbidden_call", "controller", "repository",
                                      contract_proposal=proposal)), CATEGORIES)
        self.assertEqual(report["counts"]["contract_gaps"], 1)
        self.assertEqual(report["counts"]["contract_gaps_without_proposal"], 0)
        gap = report["contract_gaps"][0]
        self.assertEqual(gap["status"], "proposed")
        self.assertEqual(gap["proposal"]["category_id"], "architecture_contract")
        self.assertEqual(gap["rule_id"], "A1")
        self.assertEqual(report["findings"][0]["category"], ar.UNMAPPED)

    def test_unmapped_rule_without_proposal_is_flagged_not_piled_silently(self):
        report = ar.audit(graph(), edges(edge("E1", f"fn:{C}#list", f"fn:{R}#save")),
                          config(pair("A1", "forbidden_call", "controller", "repository")), CATEGORIES)
        self.assertEqual(report["contract_gaps"][0]["status"], "needs_decision_no_proposal")
        self.assertIsNone(report["contract_gaps"][0]["proposal"])
        self.assertEqual(report["counts"]["contract_gaps_without_proposal"], 1)
        self.assertTrue(any("契约缺口无修订提案" in w for w in report["input_warnings"]))

    def test_mapped_rule_produces_no_gap(self):
        report = ar.audit(graph(), edges(edge("E1", f"fn:{C}#list", f"fn:{S}#secret")),
                          config(rule("A1", "visibility", levels=["private"], layer="class",
                                      category="encapsulation_leak")), CATEGORIES,
                          members={(S, "secret"): "private"})
        self.assertEqual(report["contract_gaps"], [])
        self.assertEqual(report["counts"]["contract_gaps"], 0)

    def test_revision_proposal_passes_through_and_is_validated(self):
        cfg = config()
        cfg["contract_revision_proposal"] = {"category_id": "x"}
        self.assertEqual(ar.validate_config(cfg)["contract_revision_proposal"], {"category_id": "x"})
        cfg["contract_revision_proposal"] = "not-a-dict"
        with self.assertRaises(ValueError):
            ar.validate_config(cfg)


class CliReproducibilityTest(unittest.TestCase):
    """复现命令必须只依赖仓库内已有产物，且缺必需输入时拒绝出数。"""

    LAYERS = ("layers:\n"
              "  - id: function\n    categories:\n      - {id: boundary, desc: \"x\", oracle: [static_analysis]}\n"
              "  - id: file\n    categories:\n      - {id: cohesion, desc: \"x\", oracle: [static_analysis]}\n"
              "  - id: class\n    categories:\n      - {id: encapsulation_leak, desc: \"x\", oracle: [static_analysis]}\n"
              "  - id: service\n    categories:\n      - {id: authz, desc: \"x\", oracle: [static_analysis]}\n"
              "  - id: integration\n    categories:\n      - {id: end_to_end_business, desc: \"x\", oracle: [e2e_scenario]}\n")

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        (self.root / "layers.yaml").write_text(self.LAYERS, encoding="utf-8")
        (self.root / "units.json").write_text(json.dumps(graph()), encoding="utf-8")
        (self.root / "edges.json").write_text(json.dumps(edges(edge("E1", f"fn:{C}#list", f"fn:{S}#secret"))), encoding="utf-8")
        (self.root / "decl.json").write_text(json.dumps({"functions": [
            {"class": S, "method": "secret", "modifiers": "private"}]}), encoding="utf-8")
        (self.root / "net.json").write_text(json.dumps({"nodes": {
            f"{S}#secret": {"cls": S, "method": "secret", "modifiers": "private"},
            "table:x": {"type": "TABLE"}}}), encoding="utf-8")
        cfg = config(rule("A1", "visibility", levels=["private"], layer="class", category="encapsulation_leak"))
        cfg["member_source"] = [
            {"records": "functions", "class": "class", "method": "method", "modifiers": "modifiers"},
            {"records": "nodes", "class": "cls", "method": "method", "modifiers": "modifiers"}]
        cfg["member_inputs"] = [{"label": "A_decl", "path": "decl.json", "tracked": False,
                                 "prerequisite": "需上游生成"},
                                {"label": "B_net", "path": "net.json", "tracked": True, "prerequisite": None}]
        cfg["repro_prerequisites"] = ["测试用前置声明"]
        (self.root / "rules.json").write_text(json.dumps(cfg), encoding="utf-8")

    def tearDown(self):
        self.tmp.cleanup()

    def run_cli(self, *extra):
        return subprocess.run([sys.executable, str(PILOT / "engine" / "arch_rules.py"),
                               "--units", "units.json", "--edges", "edges.json", "--rules", "rules.json",
                               "--layers", "layers.yaml", "--out", "out.json", *extra],
                              cwd=self.root, capture_output=True, text=True)

    def test_runs_with_declaration_artifact_and_no_handmade_intermediate(self):
        proc = self.run_cli("--members", "decl.json")
        self.assertEqual(proc.returncode, 0, proc.stderr)
        report = json.loads((self.root / "out.json").read_text(encoding="utf-8"))
        self.assertEqual(len(report["findings"]), 1)
        self.assertEqual(report["member_coverage"]["declarations"], 1)
        self.assertIn("--members decl.json", report["repro_cmd"])
        self.assertEqual(set(report["inputs"]), {"units", "edges", "rules", "layers", "members"})
        self.assertTrue(all(len(v["sha256"]) == 64 for v in report["inputs"].values()))

    def test_missing_members_exits_nonzero_instead_of_emitting_a_different_number(self):
        proc = self.run_cli()
        self.assertNotEqual(proc.returncode, 0)
        self.assertIn("visibility", proc.stderr)
        self.assertFalse((self.root / "out.json").exists())

    def test_repro_cmd_is_verbatim_executable(self):
        proc = self.run_cli("--members", "decl.json")
        self.assertEqual(proc.returncode, 0, proc.stderr)
        report = json.loads((self.root / "out.json").read_text(encoding="utf-8"))
        self.assertTrue(report["repro_cmd"].startswith("python3 "))
        self.assertEqual(report["repro_cwd"], str(self.root.resolve()))
        self.assertEqual(report["repro_prerequisites"], ["测试用前置声明"])
        with tempfile.TemporaryDirectory() as other:
            anywhere = subprocess.run(report["repro_cmd_anywhere"], shell=True, cwd=other,
                                      capture_output=True, text=True)
            self.assertEqual(anywhere.returncode, 0, anywhere.stderr)
            relative_elsewhere = subprocess.run(report["repro_cmd"], shell=True, cwd=other,
                                                capture_output=True, text=True)
            self.assertNotEqual(relative_elsewhere.returncode, 0)
        rerun = json.loads((self.root / "out.json").read_text(encoding="utf-8"))
        self.assertEqual(rerun["counts"], report["counts"])

    def test_both_member_variants_run_verbatim_with_one_config(self):
        proc = self.run_cli("--members", "decl.json")
        self.assertEqual(proc.returncode, 0, proc.stderr)
        report = json.loads((self.root / "out.json").read_text(encoding="utf-8"))
        variants = report["repro_variants"]
        self.assertEqual([v["label"] for v in variants], ["A_decl", "B_net"])
        self.assertEqual(report["member_coverage"]["source_records"], "functions")
        with tempfile.TemporaryDirectory() as other:
            for variant in variants:
                proc = subprocess.run(variant["cmd_anywhere"], shell=True, cwd=other,
                                      capture_output=True, text=True)
                self.assertEqual(proc.returncode, 0, f"{variant['label']}: {proc.stderr}")
        a = json.loads((self.root / "out.A_decl.json").read_text(encoding="utf-8"))
        b = json.loads((self.root / "out.B_net.json").read_text(encoding="utf-8"))
        self.assertEqual(a["member_coverage"]["source_records"], "functions")
        self.assertEqual(b["member_coverage"]["source_records"], "nodes")
        self.assertEqual(len(a["findings"]), 1)
        self.assertEqual(len(b["findings"]), 1)
        self.assertNotEqual(a["inputs"]["members"]["sha256"], b["inputs"]["members"]["sha256"])


class ReproBuilderTest(unittest.TestCase):
    def test_interpreter_prefix_and_non_clobbering_variant_outputs(self):
        argv = ["engine/arch_rules.py", "--units", "units.json", "--members", "a.json", "--out", "rep.json"]
        out = ar.build_repro(argv, "/work dir", [{"label": "A", "path": "a.json"}, {"label": "B", "path": "b.json"}])
        self.assertTrue(out["repro_cmd"].startswith("python3 engine/arch_rules.py "))
        self.assertTrue(out["repro_cmd_anywhere"].startswith("cd '/work dir' && python3 "))
        self.assertIn("rep.A.json", out["repro_variants"][0]["cmd"])
        self.assertIn("rep.B.json", out["repro_variants"][1]["cmd"])
        self.assertIn("--members b.json", out["repro_variants"][1]["cmd"])

    def test_variant_appends_members_when_absent(self):
        out = ar.build_repro(["engine/arch_rules.py", "--out", "r.json"], "/w", [{"label": "A", "path": "a.json"}])
        self.assertIn("--members a.json", out["repro_variants"][0]["cmd"])

    def test_source_shape_list_selects_matching_container(self):
        specs = [{"records": "functions", "class": "class", "method": "method", "modifiers": "modifiers"},
                 {"records": "nodes", "class": "cls", "method": "method", "modifiers": "modifiers"}]
        self.assertEqual(ar.select_source({"nodes": {}}, specs, "members")["records"], "nodes")
        self.assertEqual(ar.select_source({"functions": []}, specs, "members")["records"], "functions")
        with self.assertRaises(ValueError):
            ar.select_source({"other": []}, specs, "members")
        self.assertEqual(ar.load_members({"nodes": {"n": {"cls": S, "method": "m", "modifiers": "private"}}}, specs),
                         {(S, "m"): "private"})


class MeasurementHonestyTest(unittest.TestCase):
    """in_scope=0 是「未产生判定」，不得报成「测得合规」。"""

    def test_zero_in_scope_is_unmeasured_not_compliant(self):
        report = ar.audit(graph(), edges(edge("E1", f"fn:{C}#list", f"fn:{S}#list")),
                          config(pair("A1", "forbidden_call", "controller", "repository")), CATEGORIES)
        entry = by_id(report)["A1"]
        self.assertEqual(entry["verdict"], ar.VERDICT_NO_EDGE)
        self.assertEqual(entry["measured_edges"], 0)
        self.assertEqual(entry["counts"][ar.COMPLIANT], 0)
        self.assertEqual(entry["edges_examined"], 1)
        self.assertEqual(entry["unmatched_roles"], [])
        self.assertEqual(report["counts"]["rules_no_edge_in_scope"], 1)
        self.assertEqual(report["counts"]["rules_measured"], 0)
        self.assertEqual(report["counts"]["judgments_made"], 0)
        self.assertEqual(report["measurement"]["rules_without_measurement"], ["A1"])
        self.assertEqual(report["measurement"]["rules_with_measurement"], [])

    def test_measured_clean_is_a_different_verdict(self):
        report = ar.audit(graph(), edges(edge("E1", f"fn:{C}#list", f"fn:{S}#list")),
                          config(rule("A1", "layer_skip", chain=["controller", "service", "repository"])),
                          CATEGORIES)
        entry = by_id(report)["A1"]
        self.assertEqual(entry["verdict"], ar.VERDICT_CLEAN)
        self.assertEqual(entry["measured_edges"], 1)
        self.assertEqual(report["counts"]["rules_no_edge_in_scope"], 0)
        self.assertEqual(report["measurement"]["rules_with_measurement"], ["A1"])

    def test_zero_findings_does_not_imply_global_compliance(self):
        report = ar.audit(graph(), edges(edge("E1", f"fn:{C}#list", f"fn:{S}#list")),
                          config(pair("A1", "forbidden_call", "controller", "repository"),
                                 rule("A2", "layer_skip", chain=["controller", "service", "repository"])),
                          CATEGORIES)
        self.assertEqual(report["counts"]["findings"], 0)
        self.assertEqual(report["measurement"]["judgments_made"], 1)
        self.assertEqual(report["measurement"]["rules_without_measurement"], ["A1"])
        self.assertIn("不得读成", report["measurement"]["note"])

    def test_unknown_edges_are_not_measurements(self):
        snapshot = edges(edge("E1", f"fn:{C}#list", f"fn:{S}#list"),
                         edge("E2", f"fn:{S}#list", f"fn:{R}#save", resolution="unknown"))
        report = ar.audit(graph(), snapshot,
                          config(rule("A1", "layer_skip", chain=["controller", "service", "repository"])),
                          CATEGORIES)
        entry = by_id(report)["A1"]
        self.assertEqual(entry["counts"][ar.COMPLIANT], 1)
        self.assertEqual(entry["counts"][ar.UNKNOWN], 1)
        self.assertEqual(entry["in_scope_edges"], 2)
        self.assertEqual(entry["measured_edges"], 1)
        self.assertEqual(report["measurement"]["judgments_made"], 1)
        self.assertEqual(report["counts"]["judgments_made"], 1)
        self.assertEqual(report["measurement"]["unknown_evaluations"], 1)

    def test_unmatched_and_no_edge_are_distinct(self):
        roles = [{"id": "controller", "class_patterns": ["*.controller.*"]},
                 {"id": "service", "class_patterns": ["*.service.*"]},
                 {"id": "entity", "class_patterns": ["*.entity.*"]},
                 {"id": "repository", "class_patterns": ["*.persistence.*"]}]
        cfg = {"schema": ar.CONFIG_SCHEMA, "roles": roles,
               "rules": [pair("A1", "forbidden_call", "controller", "repository"),
                         pair("A2", "dependency_direction", "entity", "service")]}
        report = ar.audit(graph(), edges(edge("E1", f"fn:{C}#list", f"fn:{S}#list")), cfg, CATEGORIES)
        rules = by_id(report)
        self.assertEqual(rules["A1"]["verdict"], ar.VERDICT_UNMATCHED)
        self.assertEqual(rules["A2"]["verdict"], ar.VERDICT_NO_EDGE)
        self.assertEqual(rules["A2"]["measured_edges"], 0)
        self.assertEqual(report["counts"]["rules_unmatched"], 1)
        self.assertEqual(report["counts"]["rules_no_edge_in_scope"], 1)


class EmptyMemberContainerTest(unittest.TestCase):
    """坑46 第二条入口：文件存在但容器空/全不合格，必须与缺文件同等拒绝出数。"""

    def cfg(self):
        return config(rule("A1", "visibility", levels=["private"]))

    def snapshot(self):
        return edges(edge("E1", f"fn:{C}#list", f"fn:{S}#secret"))

    def test_empty_members_dict_is_rejected(self):
        with self.assertRaises(ValueError) as ctx:
            ar.audit(graph(), self.snapshot(), self.cfg(), CATEGORIES, members={})
        self.assertIn("为空", str(ctx.exception))

    def test_empty_container_is_rejected_not_silently_numbered(self):
        members = ar.load_members({"members": []})
        self.assertEqual(members, {})
        with self.assertRaises(ValueError):
            ar.audit(graph(), self.snapshot(), self.cfg(), CATEGORIES, members)

    def test_all_rows_invalid_is_rejected(self):
        members = ar.load_members({"members": [{"class": S, "method": "secret"}, {"class": S}]})
        self.assertEqual(members, {})
        with self.assertRaises(ValueError):
            ar.audit(graph(), self.snapshot(), self.cfg(), CATEGORIES, members)

    def test_partial_valid_is_allowed_but_counted_and_warned(self):
        members = ar.load_members({"members": [{"class": S, "method": "secret", "modifiers": "private"},
                                               {"class": S, "method": "other"}]})
        report = ar.audit(graph(), self.snapshot(), self.cfg(), CATEGORIES, members)
        self.assertEqual(len(report["findings"]), 1)
        report2 = ar.audit(graph(), edges(edge("E1", f"fn:{C}#list", f"fn:{S}#secret"),
                                          edge("E2", f"fn:{C}#list", f"fn:{S}#other")),
                           self.cfg(), CATEGORIES, members)
        self.assertEqual(report2["member_coverage"]["declaration_missing"], 1)
        self.assertTrue(any("覆盖不全" in w for w in report2["input_warnings"]))

    def test_empty_members_allowed_when_no_visibility_rule(self):
        report = ar.audit(graph(), edges(edge("E1", f"fn:{C}#list", f"fn:{R}#save")),
                          config(pair("A1", "forbidden_call", "controller", "repository")), CATEGORIES, members={})
        self.assertEqual(len(report["findings"]), 1)

    def test_record_stats_distinguish_empty_from_all_invalid(self):
        empty = ar.member_record_stats({"members": []})
        self.assertEqual((empty["records_seen"], empty["records_valid"], empty["records_skipped"]), (0, 0, 0))
        invalid = ar.member_record_stats({"members": [{"class": S}, {"class": S, "method": "m"}]})
        self.assertEqual((invalid["records_seen"], invalid["records_valid"], invalid["records_skipped"]), (2, 0, 2))
        partial = ar.member_record_stats({"members": [{"class": S, "method": "m", "modifiers": ""}, {"class": S}]})
        self.assertEqual((partial["records_seen"], partial["records_valid"], partial["records_skipped"]), (2, 1, 1))
        self.assertEqual(ar.load_members({"members": [{"class": S, "method": "m", "modifiers": ""}]})[(S, "m")], "")


class EmptyContainerCliTest(unittest.TestCase):
    LAYERS = CliReproducibilityTest.LAYERS

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        (self.root / "layers.yaml").write_text(self.LAYERS, encoding="utf-8")
        (self.root / "units.json").write_text(json.dumps(graph()), encoding="utf-8")
        (self.root / "edges.json").write_text(json.dumps(edges(edge("E1", f"fn:{C}#list", f"fn:{S}#secret"))), encoding="utf-8")
        cfg = config(rule("A1", "visibility", levels=["private"]))
        (self.root / "rules.json").write_text(json.dumps(cfg), encoding="utf-8")

    def tearDown(self):
        self.tmp.cleanup()

    def run_cli(self, payload):
        (self.root / "m.json").write_text(json.dumps(payload), encoding="utf-8")
        out = self.root / "out.json"
        if out.exists():
            out.unlink()
        return subprocess.run([sys.executable, str(PILOT / "engine" / "arch_rules.py"),
                               "--units", "units.json", "--edges", "edges.json", "--rules", "rules.json",
                               "--layers", "layers.yaml", "--members", "m.json", "--out", "out.json"],
                              cwd=self.root, capture_output=True, text=True)

    def test_empty_container_exits_nonzero_and_writes_nothing(self):
        proc = self.run_cli({"members": []})
        self.assertNotEqual(proc.returncode, 0)
        self.assertIn("容器为空", proc.stderr)
        self.assertFalse((self.root / "out.json").exists())

    def test_all_invalid_rows_exits_nonzero_with_distinct_reason(self):
        proc = self.run_cli({"members": [{"class": S, "method": "secret"}]})
        self.assertNotEqual(proc.returncode, 0)
        self.assertIn("全不合格", proc.stderr)
        self.assertFalse((self.root / "out.json").exists())

    def test_partial_valid_runs_and_reports_skips(self):
        proc = self.run_cli({"members": [{"class": S, "method": "secret", "modifiers": "private"},
                                         {"class": S, "method": "other"}]})
        self.assertEqual(proc.returncode, 0, proc.stderr)
        report = json.loads((self.root / "out.json").read_text(encoding="utf-8"))
        coverage = report["member_coverage"]
        self.assertEqual((coverage["records_seen"], coverage["records_valid"], coverage["records_skipped"]), (2, 1, 1))
        self.assertEqual(len(report["findings"]), 1)

    def call_main(self, payload):
        """同进程直调 main()：subprocess 守卫无法被同进程变异杀死，入口层必须另有 in-process 守卫。"""
        (self.root / "m.json").write_text(json.dumps(payload), encoding="utf-8")
        out = self.root / "out.json"
        if out.exists():
            out.unlink()
        argv = [str(PILOT / "engine" / "arch_rules.py")]
        for flag, name in (("--units", "units.json"), ("--edges", "edges.json"), ("--rules", "rules.json"),
                           ("--layers", "layers.yaml"), ("--members", "m.json"), ("--out", "out.json")):
            argv += [flag, str(self.root / name)]
        with mock.patch.object(sys, "argv", argv):
            return ar.main()

    def test_main_rejects_empty_container_in_process(self):
        with self.assertRaises(ValueError) as ctx:
            self.call_main({"members": []})
        self.assertIn("容器为空", str(ctx.exception))
        self.assertFalse((self.root / "out.json").exists())

    def test_main_rejects_all_invalid_rows_in_process(self):
        with self.assertRaises(ValueError) as ctx:
            self.call_main({"members": [{"class": S, "method": "secret"}]})
        self.assertIn("全不合格", str(ctx.exception))
        self.assertFalse((self.root / "out.json").exists())

    def test_main_accepts_partial_and_reports_stats_in_process(self):
        self.call_main({"members": [{"class": S, "method": "secret", "modifiers": "private"}, {"class": S}]})
        report = json.loads((self.root / "out.json").read_text(encoding="utf-8"))
        self.assertEqual(report["member_coverage"]["records_skipped"], 1)
        self.assertEqual(len(report["findings"]), 1)


class SourceRootChannelTest(unittest.TestCase):
    """坑47：路径给了但不存在，必须告警且通道标 INCOMPLETE，不得与「测得 0」混同。"""

    def cfg(self):
        data = config(pair("A1", "forbidden_call", "controller", "repository"))
        data["unprojected_dispatch_patterns"] = [{"id": "method_reference", "regex": r"(\w+)::(\w+)"}]
        return data

    def snapshot(self):
        return edges(edge("E1", f"fn:{C}#list", f"fn:{R}#save"))

    def test_nonexistent_source_root_warns_and_marks_channel(self):
        with tempfile.TemporaryDirectory() as tmp:
            missing = Path(tmp) / "no_such_root"
            report = ar.audit(graph(), self.snapshot(), self.cfg(), CATEGORIES, source_root=missing)
            self.assertEqual(report["unprojected_dispatch"]["channel_status"], "SOURCE_ROOT_MISSING")
            self.assertTrue(any("源码根不存在" in w for w in report["input_warnings"]))
            self.assertTrue(any("SOURCE_ROOT_MISSING" in w for w in report["input_warnings"]))
            self.assertEqual(report["unprojected_dispatch"]["occurrences"], [])
            self.assertEqual(len(report["findings"]), 1)

    def test_absent_source_root_is_a_different_status(self):
        report = ar.audit(graph(), self.snapshot(), self.cfg(), CATEGORIES)
        self.assertEqual(report["unprojected_dispatch"]["channel_status"], "NO_SOURCE_ROOT")
        self.assertTrue(any("未供给源码根" in w for w in report["input_warnings"]))

    def test_root_exists_but_no_scannable_file_is_not_measured_zero(self):
        with tempfile.TemporaryDirectory() as tmp:
            report = ar.audit(graph(), self.snapshot(), self.cfg(), CATEGORIES, source_root=Path(tmp))
            self.assertEqual(report["unprojected_dispatch"]["channel_status"], "NO_SCANNABLE_FILE")
            self.assertTrue(any("NO_SCANNABLE_FILE" in w for w in report["input_warnings"]))

    def test_judgment_numbers_identical_across_all_three_root_states(self):
        with tempfile.TemporaryDirectory() as tmp:
            counts = []
            for root in (None, Path(tmp) / "no_such_root", Path(tmp)):
                report = ar.audit(graph(), self.snapshot(), self.cfg(), CATEGORIES, source_root=root)
                counts.append((report["counts"]["findings"], report["counts"]["unknown_evaluations"],
                               report["unprojected_dispatch"]["channel_status"]))
            self.assertEqual([c[:2] for c in counts], [(1, 0), (1, 0), (1, 0)])
            self.assertEqual([c[2] for c in counts], ["NO_SOURCE_ROOT", "SOURCE_ROOT_MISSING", "NO_SCANNABLE_FILE"])

    def test_readable_root_reports_ok(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "src").mkdir(exist_ok=True)
            (root / "src" / "C.java").write_text("Mapper::toResponse;\n", encoding="utf-8")
            report = ar.audit(graph(), self.snapshot(), self.cfg(), CATEGORIES, source_root=root)
            self.assertEqual(report["unprojected_dispatch"]["channel_status"], "OK")
            self.assertEqual(report["counts"]["unprojected_dispatch_occurrences"], 1)
            self.assertFalse(any("通道状态" in w for w in report["input_warnings"]))


if __name__ == "__main__":
    unittest.main()