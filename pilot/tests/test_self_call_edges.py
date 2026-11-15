"""同类自调用边回归守卫。

修前实测：fn_declaration 的 343 条 internal_calls 里同类边 0 条
（javap 对同类 invoke 省略 owner，上游抽取正则整行丢弃）。
本测试守住：抽取器认无 owner 行 / 源码证据产边 / 跨类不受影响 /
代理三态可区分 / 证据不足是 unknown / 空输入不崩溃。
"""
import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "engine"))
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import build_agent_graph as bag
import fn_static

CLS = "demo.svc.DemoService"

JAVA_SOURCE = """package demo.svc;

import org.springframework.beans.factory.annotation.Autowired;
import org.springframework.context.annotation.Lazy;

public class DemoService {

    @Autowired
    @Lazy
    private DemoService self;

    private DemoService plain;

    private final OtherClient client;

    public DemoService(OtherClient client) {
        this.client = client;
    }

    public String create(String input) {
        String no = generateNo();
        this.audit(no);
        self.audit(no);
        plain.audit(no);
        client.audit(no);
        other.helper();
        Runnable r = this::generateNo;
        Runnable x = Other::helper;
        // generateNo() 注释里的假调用
        String s = "generateNo() 字符串里的假调用";
        return no;
    }

    private String generateNo() {
        return "x";
    }

    private void audit(String no) {
    }

    static class Inner {
        void create() {
            generateNo();
        }
    }
}
"""


def _units():
    return {"function": [
        {"unit_kind": "method", "unit_key": f"{CLS}#{m}", "class": CLS,
         "symbol": f"DemoService.{m}", "file": "", "facts": {}}
        for m in ("create", "generateNo", "audit")
    ]}


def _facts_with_source(tmp: Path, source: str = JAVA_SOURCE,
                       root: Path | None = None):
    rel = "src/main/java/demo/svc/DemoService.java"
    path = tmp / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(source, encoding="utf-8")
    decl = {"files": [{"class": CLS, "file": rel}]}
    with mock.patch.object(bag, "_load", return_value=decl):
        return bag._self_call_facts(root or tmp, _units()), path


class SelfCallSourceEvidence(unittest.TestCase):
    def test_private_self_call_produces_edge_with_line_evidence(self):
        with tempfile.TemporaryDirectory() as td:
            tmp = Path(td)
            facts, path = _facts_with_source(tmp)
            gen = [f for f in facts if f["caller"] == "create"
                   and f["callee"] == "generateNo" and f["via"] == "call"]
            self.assertEqual(len(gen), 1)
            self.assertEqual(gen[0]["proxy_mode"], "bypassed")
            ev = gen[0]["evidence"]
            self.assertEqual(ev["file"], str(path))
            lines = path.read_text(encoding="utf-8").splitlines()
            self.assertIn("generateNo()", lines[ev["line"] - 1])

            units = _units()
            edges = bag._self_call_edges(units, [], facts)
            hit = [e for e in edges
                   if e["to_agent_id"] == f"fn:{CLS}#generateNo"]
            self.assertEqual(len(hit), 1)
            self.assertEqual(hit[0]["resolution"], "static")
            self.assertTrue(hit[0]["self_call"])
            self.assertEqual(hit[0]["from_agent_id"], f"fn:{CLS}#create")
            for e in edges:
                self.assertEqual(bag.validate_edge(e), [])

    def test_comment_and_string_calls_do_not_produce_edges(self):
        with tempfile.TemporaryDirectory() as td:
            facts, _ = _facts_with_source(Path(td))
            gen = [f for f in facts if f["callee"] == "generateNo"
                   and f["caller"] == "create" and f["via"] == "call"]
            self.assertEqual(len(gen), 1)

    def test_nested_class_calls_not_attributed_to_outer(self):
        with tempfile.TemporaryDirectory() as td:
            facts, _ = _facts_with_source(Path(td))
            gen = [f for f in facts if f["callee"] == "generateNo"]
            self.assertEqual(len(gen), 2)
            self.assertEqual({f["caller"] for f in gen}, {"create"})
            self.assertEqual({f["via"] for f in gen}, {"call", "method_reference"})

    def test_source_method_reference_self_call(self):
        with tempfile.TemporaryDirectory() as td:
            facts, path = _facts_with_source(Path(td))
            refs = [f for f in facts if f["via"] == "method_reference"]
            self.assertEqual(len(refs), 1)
            self.assertEqual(refs[0]["callee"], "generateNo")
            self.assertEqual(refs[0]["proxy_mode"], "bypassed")
            lines = path.read_text(encoding="utf-8").splitlines()
            self.assertIn("this::generateNo", lines[refs[0]["evidence"]["line"] - 1])
            self.assertFalse([f for f in facts if f["callee"] == "helper"])

    def test_proxy_bypassed_vs_effective_vs_unknown(self):
        with tempfile.TemporaryDirectory() as td:
            facts, _ = _facts_with_source(Path(td))
            audit = {(f["proxy_mode"]) for f in facts
                     if f["caller"] == "create" and f["callee"] == "audit"}
            self.assertEqual(audit, {"bypassed", "effective", "unknown"})
            edges = bag._self_call_edges(_units(), [], facts)
            hit = [e for e in edges if e["to_agent_id"] == f"fn:{CLS}#audit"]
            self.assertEqual(len(hit), 1)
            self.assertEqual(hit[0]["proxy_mode"], "mixed")
            self.assertEqual(len(hit[0]["evidence"]), 3)

    def test_this_call_is_bypassed(self):
        with tempfile.TemporaryDirectory() as td:
            facts, _ = _facts_with_source(Path(td))
            this_calls = [f for f in facts if f["caller"] == "create"
                          and f["callee"] == "audit"
                          and f["proxy_mode"] == "bypassed"]
            self.assertEqual(len(this_calls), 1)

    def test_foreign_receiver_and_cross_class_produce_no_self_edge(self):
        with tempfile.TemporaryDirectory() as td:
            facts, _ = _facts_with_source(Path(td))
            self.assertFalse([f for f in facts if f["callee"] == "helper"])
            self.assertFalse([f for f in facts if f["callee"] == "audit"
                              and f["proxy_mode"] == "effective"
                              and "client" in str(f)])
            recv_modes = [f["proxy_mode"] for f in facts
                          if f["caller"] == "create" and f["callee"] == "audit"]
            self.assertEqual(len(recv_modes), 3)

    def test_source_root_suffix_resolution(self):
        with tempfile.TemporaryDirectory() as td:
            tmp = Path(td)
            facts, _ = _facts_with_source(tmp, root=tmp / "src/main/java")
            self.assertTrue(facts)

    def test_empty_inputs_do_not_crash(self):
        self.assertEqual(bag._self_call_facts(None, {}), [])
        self.assertEqual(bag._self_call_facts(Path("/nonexistent-w6"), {}), [])
        self.assertEqual(bag._self_call_facts(None, _units()), [])
        self.assertEqual(bag._self_call_edges({}, [], []), [])
        self.assertEqual(bag._self_call_edges(_units(), [], []), [])
        with tempfile.TemporaryDirectory() as td:
            self.assertEqual(bag._self_call_facts(Path(td), _units()), [])


class SelfCallEdgeSemantics(unittest.TestCase):
    def _fact(self, caller="create", callee="audit", mode="bypassed", line=1):
        return {"class": CLS, "caller": caller, "callee": callee,
                "proxy_mode": mode,
                "evidence": {"file": "DemoService.java", "line": line}}

    def test_unresolved_target_is_unknown_not_static(self):
        facts = [self._fact(callee="ghost")]
        edges = bag._self_call_edges(_units(), [], facts)
        self.assertEqual(len(edges), 1)
        self.assertEqual(edges[0]["resolution"], "unknown")
        self.assertTrue(edges[0]["to_agent_id"].startswith("fn:UNRESOLVED#"))

    def test_proxy_unknown_stays_unknown(self):
        edges = bag._self_call_edges(_units(), [], [self._fact(mode="unknown")])
        self.assertEqual(edges[0]["proxy_mode"], "unknown")
        self.assertNotEqual(edges[0]["proxy_mode"], "bypassed")

    def test_existing_cross_class_edges_untouched(self):
        existing = [{"edge_id": "E-000001", "kind": "call",
                     "from_agent_id": f"fn:{CLS}#create",
                     "to_agent_id": "fn:demo.svc.Other#helper",
                     "resolution": "static", "stability": "behavior",
                     "invalidation": "reverify_caller", "call_count": 1}]
        before = [dict(e) for e in existing]
        edges = bag._self_call_edges(_units(), existing, [self._fact()])
        self.assertEqual(len(edges), 2)
        self.assertEqual(edges[0], before[0])
        self.assertEqual(edges[1]["to_agent_id"], f"fn:{CLS}#audit")
        self.assertEqual(existing, before)

    def test_declaration_edge_enriched_not_duplicated(self):
        existing = [{"edge_id": "E-000001", "kind": "call",
                     "from_agent_id": f"fn:{CLS}#create",
                     "to_agent_id": f"fn:{CLS}#audit",
                     "resolution": "static", "stability": "behavior",
                     "invalidation": "reverify_caller", "call_count": 1}]
        edges = bag._self_call_edges(_units(), existing, [self._fact()])
        self.assertEqual(len(edges), 1)
        self.assertEqual(edges[0]["edge_id"], "E-000001")
        self.assertTrue(edges[0]["self_call"])
        self.assertEqual(edges[0]["proxy_mode"], "bypassed")
        self.assertEqual(edges[0]["evidence"],
                         [{"file": "DemoService.java", "line": 1}])
        self.assertNotIn("self_call", existing[0])

    def test_declaration_edge_without_facts_untouched(self):
        existing = [{"edge_id": "E-000001", "kind": "call",
                     "from_agent_id": f"fn:{CLS}#create",
                     "to_agent_id": f"fn:{CLS}#audit",
                     "resolution": "static", "stability": "behavior",
                     "invalidation": "reverify_caller", "call_count": 1}]
        edges = bag._self_call_edges(_units(), existing, [])
        self.assertEqual(edges, existing)
        self.assertNotIn("self_call", edges[0])

    def test_sites_aggregate_with_evidence_and_count(self):
        facts = [self._fact(line=3), self._fact(line=9)]
        edges = bag._self_call_edges(_units(), [], facts)
        self.assertEqual(len(edges), 1)
        self.assertEqual(edges[0]["call_count"], 2)
        self.assertEqual([e["line"] for e in edges[0]["evidence"]], [3, 9])

    def test_distinct_proxy_modes_on_one_pair_are_mixed_not_guessed(self):
        facts = [self._fact(mode="bypassed"), self._fact(mode="effective")]
        edges = bag._self_call_edges(_units(), [], facts)
        self.assertEqual(len(edges), 1)
        self.assertEqual(edges[0]["proxy_mode"], "mixed")
        self.assertEqual(edges[0]["call_count"], 2)

    def test_edge_ids_unique_and_continue_sequence(self):
        existing = [{"edge_id": "E-000001", "kind": "call",
                     "from_agent_id": "fn:x#y", "to_agent_id": "fn:x#z",
                     "resolution": "static", "stability": "behavior",
                     "invalidation": "reverify_caller", "call_count": 1}]
        facts = [self._fact(), self._fact(callee="generateNo")]
        edges = bag._self_call_edges(_units(), existing, facts)
        ids = [e["edge_id"] for e in edges]
        self.assertEqual(ids, ["E-000001", "E-000002", "E-000003"])


class FnStaticExtractorGuard(unittest.TestCase):
    """根因守卫：javap 同类 invoke 无 owner，抽取器必须归回本类。"""

    JAVAP = """public class demo.svc.DemoService {
  public java.lang.String create();
    descriptor: ()Ljava/lang/String;
    flags: (0x0001) ACC_PUBLIC
    Code:
      stack=1, locals=1, args_size=1
         0: aload_0
         1: invokespecial #1                  // Method java/lang/Object."<init>":()V
         4: invokevirtual #25                 // Method generateNo:()Ljava/lang/String;
         7: invokevirtual #29                 // Method demo/svc/Other.helper:()V
        10: invokestatic  #33                 // Method com/other/Lib.util:()V
        13: areturn
}
"""

    def test_ownerless_invoke_attributed_to_current_class(self):
        ms = fn_static.parse_methods(self.JAVAP, "demo.svc.DemoService")
        calls = ms[0]["calls"]
        self.assertIn("demo.svc.DemoService.generateNo", calls)
        self.assertIn("demo.svc.Other.helper", calls)
        self.assertIn("com.other.Lib.util", calls)
        self.assertFalse([c for c in calls if c.endswith(".com")])

    def test_ownerless_without_class_context_not_fabricated(self):
        ms = fn_static.parse_methods(self.JAVAP, "")
        calls = ms[0]["calls"]
        self.assertIn(".generateNo", calls)
        self.assertFalse([c for c in calls if c.startswith("demo.svc.DemoService")])

    def test_method_with_throws_clause_not_dropped(self):
        javap = """public class demo.svc.DemoRunner {
  public void run(java.lang.String) throws java.io.IOException;
    descriptor: (Ljava/lang/String;)V
    flags: (0x0001) ACC_PUBLIC
    Code:
      stack=1, locals=2, args_size=2
         0: aload_0
         1: invokevirtual #25                 // Method helper:()V
         4: return
  private void helper();
    descriptor: ()V
    flags: (0x0002) ACC_PRIVATE
    Code:
      stack=0, locals=1, args_size=1
         0: return
}
"""
        ms = fn_static.parse_methods(javap, "demo.svc.DemoRunner")
        self.assertEqual([m["name"] for m in ms], ["run", "helper"])
        self.assertIn("demo.svc.DemoRunner.helper", ms[0]["calls"])

    def test_internal_external_split_keeps_same_class_internal(self):
        ms = fn_static.parse_methods(self.JAVAP, "demo.svc.DemoService")
        calls = ms[0]["calls"]
        pkg = "demo.svc"
        internal = sorted({c for c in calls if c.startswith(pkg)})
        self.assertIn("demo.svc.DemoService.generateNo", internal)
        self.assertIn("demo.svc.Other.helper", internal)

    def test_synthetic_enum_values_not_a_unit(self):
        javap = """public class demo.svc.DemoEnum {
  private static demo.svc.DemoEnum[] $values();
    Code:
       0: areturn
}
"""
        ms = fn_static.parse_methods(javap, "demo.svc.DemoEnum")
        self.assertEqual([m["name"] for m in ms], ["$values"])
        kept = [m["name"] for m in ms
                if m["name"] not in ("<init>", "<clinit>", "$values")]
        self.assertEqual(kept, [])


class VerboseExtractorGuard(unittest.TestCase):
    """javap -v 守卫：注解原文 / MethodHandle 归因 / 统计不被注解段污染。"""

    QUERY_JAVAP = """public interface demo.svc.DemoRepository {
  public abstract java.lang.Integer findMax();
    descriptor: ()Ljava/lang/Integer;
    flags: (0x0401) ACC_PUBLIC, ACC_ABSTRACT
    RuntimeVisibleAnnotations:
      0: #17(#18=s#19)
        org.springframework.data.jpa.repository.Query(
          value="select coalesce(max(c.sortOrder), 0) from Demo c"
        )
}
"""

    REF_JAVAP = """public class demo.svc.DemoService {
  #10 = MethodHandle       5:#11        // REF_invokeVirtual demo/svc/DemoService.snapshot:(Ljava/lang/Object;)Ljava/lang/Object;
  #12 = InvokeDynamic      #0:#13       // #0:apply:(Ldemo/svc/DemoService;)Ljava/util/function/Function;
  public java.lang.Object build();
    descriptor: ()Ljava/lang/Object;
    flags: (0x0001) ACC_PUBLIC
    Code:
      stack=2, locals=1, args_size=1
         0: aload_0
         1: invokedynamic #12,  0            // InvokeDynamic #0:apply:(Ldemo/svc/DemoService;)Ljava/util/function/Function;
         6: areturn
  private java.lang.Object snapshot(java.lang.Object);
    descriptor: (Ljava/lang/Object;)Ljava/lang/Object;
    flags: (0x0002) ACC_PRIVATE
    Code:
      stack=1, locals=2, args_size=2
         0: aload_1
         1: areturn
}
BootstrapMethods:
  0: #20 REF_invokeStatic java/lang/invoke/LambdaMetafactory.metafactory:(Ljava/lang/invoke/MethodHandles$Lookup;Ljava/lang/String;Ljava/lang/invoke/MethodType;Ljava/lang/invoke/MethodType;Ljava/lang/invoke/MethodHandle;Ljava/lang/invoke/MethodType;)Ljava/lang/invoke/CallSite;
    Method arguments:
      #21 (Ljava/lang/Object;)Ljava/lang/Object;
      #10 REF_invokeVirtual demo/svc/DemoService.snapshot:(Ljava/lang/Object;)Ljava/lang/Object;
      #22 (Ljava/lang/Object;)Ljava/lang/Object;
"""

    def test_query_raw_text_extracted(self):
        ms = fn_static.parse_methods(self.QUERY_JAVAP, "demo.svc.DemoRepository")
        self.assertEqual(len(ms), 1)
        annots = ms[0]["annotations"]
        self.assertEqual(len(annots), 1)
        self.assertEqual(annots[0]["type"],
                         "org.springframework.data.jpa.repository.Query")
        self.assertIn("select coalesce(max(c.sortOrder), 0) from Demo c",
                      annots[0]["raw"])

    def test_annotation_section_does_not_pollute_bytecode_stats(self):
        ms = fn_static.parse_methods(self.QUERY_JAVAP, "demo.svc.DemoRepository")
        self.assertEqual(ms[0]["bytecode_size"], 0)
        self.assertEqual(ms[0]["calls"], [])
        self.assertEqual(ms[0]["constants"], [])

    def test_method_reference_attributed_via_bootstrap_with_cp_evidence(self):
        ms = fn_static.parse_methods(self.REF_JAVAP, "demo.svc.DemoService")
        build = [m for m in ms if m["name"] == "build"][0]
        refs = build["method_reference_calls"]
        self.assertEqual(len(refs), 1)
        self.assertEqual(refs[0]["target"], "demo.svc.DemoService.snapshot")
        self.assertEqual(refs[0]["cp"], "#10")
        self.assertEqual(refs[0]["kind"], "REF_invokeVirtual")
        self.assertEqual(refs[0]["receiver"], "this")
        snap = [m for m in ms if m["name"] == "snapshot"][0]
        self.assertEqual(snap["method_reference_calls"], [])

    def test_method_reference_edge_carries_bytecode_evidence(self):
        ms = fn_static.parse_methods(self.REF_JAVAP, "demo.svc.DemoService")
        fn = {"functions": [
            {"class": "demo.svc.DemoService", "method": m["name"],
             "method_reference_calls": m["method_reference_calls"]}
            for m in ms]}
        units = {"function": [
            {"unit_kind": "method", "unit_key": f"demo.svc.DemoService#{n}",
             "class": "demo.svc.DemoService", "symbol": f"DemoService.{n}",
             "file": "", "facts": {}} for n in ("build", "snapshot")]}
        facts = bag._method_ref_facts(None, units, fn)
        self.assertEqual(len(facts), 1)
        self.assertEqual(facts[0]["proxy_mode"], "bypassed")
        edges = bag._self_call_edges(units, [], facts)
        self.assertEqual(len(edges), 1)
        e = edges[0]
        self.assertTrue(e["self_call"])
        self.assertEqual(e["to_agent_id"], "fn:demo.svc.DemoService#snapshot")
        self.assertEqual(e["resolution"], "static")
        self.assertEqual(e["evidence"][0]["kind"], "bytecode_method_handle")
        self.assertEqual(e["evidence"][0]["cp"], "#10")

    def test_missing_receiver_evidence_is_unknown_not_guessed(self):
        fn = {"functions": [{
            "class": "demo.svc.DemoService", "method": "build",
            "method_reference_calls": [
                {"target": "demo.svc.DemoService.snapshot",
                 "kind": "REF_invokeVirtual", "cp": "#10", "receiver": "unknown"}],
        }]}
        units = {"function": [
            {"unit_kind": "method", "unit_key": f"demo.svc.DemoService#{n}",
             "class": "demo.svc.DemoService", "symbol": f"DemoService.{n}",
             "file": "", "facts": {}} for n in ("build", "snapshot")]}
        facts = bag._method_ref_facts(None, units, fn)
        self.assertEqual(facts[0]["proxy_mode"], "unknown")

    def test_unresolved_method_reference_target_is_unknown(self):
        units = {"function": [
            {"unit_kind": "method", "unit_key": "demo.svc.DemoService#build",
             "class": "demo.svc.DemoService", "symbol": "DemoService.build",
             "file": "", "facts": {}}]}
        edges = bag._self_call_edges(units, [], [
            {"class": "demo.svc.DemoService", "caller": "build",
             "callee": "ghost", "proxy_mode": "unknown", "via": "method_reference",
             "evidence": {"kind": "bytecode_method_handle", "cp": "#99"}}])
        self.assertEqual(edges[0]["resolution"], "unknown")
        self.assertTrue(edges[0]["to_agent_id"].startswith("fn:UNRESOLVED#"))

    def test_cross_class_method_reference_not_a_self_edge(self):
        fn = {"functions": [{
            "class": "demo.svc.DemoService", "method": "build",
            "method_reference_calls": [
                {"target": "demo.svc.Other.helper",
                 "kind": "REF_invokeStatic", "cp": "#10", "receiver": "static"}],
        }]}
        units = {"function": [
            {"unit_kind": "method", "unit_key": "demo.svc.DemoService#build",
             "class": "demo.svc.DemoService", "symbol": "DemoService.build",
             "file": "", "facts": {}}]}
        self.assertEqual(bag._method_ref_facts(None, units, fn), [])

    def test_synthetic_lambda_target_not_a_self_edge(self):
        fn = {"functions": [{
            "class": "demo.svc.DemoService", "method": "build",
            "method_reference_calls": [
                {"target": "demo.svc.DemoService.lambda$build$0",
                 "kind": "REF_invokeStatic", "cp": "#10", "receiver": "static"}],
        }]}
        units = {"function": [
            {"unit_kind": "method", "unit_key": f"demo.svc.DemoService#{n}",
             "class": "demo.svc.DemoService", "symbol": f"DemoService.{n}",
             "file": "", "facts": {}} for n in ("build", "lambda$build$0")]}
        self.assertEqual(bag._method_ref_facts(None, units, fn), [])

    def test_dual_channel_evidence_one_site_not_double_counted(self):
        units = {"function": [
            {"unit_kind": "method", "unit_key": f"demo.svc.DemoService#{n}",
             "class": "demo.svc.DemoService", "symbol": f"DemoService.{n}",
             "file": "", "facts": {}} for n in ("build", "snapshot")]}
        facts = [
            {"class": "demo.svc.DemoService", "caller": "build",
             "callee": "snapshot", "proxy_mode": "bypassed",
             "via": "method_reference",
             "evidence": {"file": "DemoService.java", "line": 20}},
            {"class": "demo.svc.DemoService", "caller": "build",
             "callee": "snapshot", "proxy_mode": "bypassed",
             "via": "method_reference",
             "evidence": {"kind": "bytecode_method_handle", "cp": "#146",
                          "ref": "REF_invokeStatic"}},
        ]
        edges = bag._self_call_edges(units, [], facts)
        self.assertEqual(len(edges), 1)
        self.assertEqual(edges[0]["call_count"], 1)
        self.assertEqual(len(edges[0]["evidence"]), 2)

    def test_baseline_54_self_edges_not_regressed(self):
        fixture = Path(__file__).resolve().parent / "self_call_edges_baseline.json"
        live = Path(__file__).resolve().parents[1] / "edges.json"
        if not fixture.exists() or not live.exists():
            self.skipTest("baseline fixture or live edges missing")
        base = [e for e in json.loads(fixture.read_text())["edges"]
                if e.get("self_call")]
        cur = {(e["from_agent_id"], e["to_agent_id"]): e
               for e in json.loads(live.read_text())["edges"] if e.get("self_call")}
        self.assertEqual(len(base), 54)
        for e in base:
            hit = cur.get((e["from_agent_id"], e["to_agent_id"]))
            self.assertIsNotNone(hit, e["edge_id"])
            self.assertEqual(hit["proxy_mode"], e["proxy_mode"])
            self.assertEqual(hit["resolution"], e["resolution"])
            for ev in e["evidence"]:
                self.assertIn(ev, hit["evidence"])


class UpstreamCrossClassUnchanged(unittest.TestCase):
    def test_cross_class_internal_calls_still_static(self):
        fn = {"functions": [{
            "class": CLS, "method": "create",
            "internal_calls": ["demo.svc.Other.helper"],
            "external_calls": [],
        }]}
        units = _units()
        units["function"].append(
            {"unit_kind": "method", "unit_key": "demo.svc.Other#helper",
             "class": "demo.svc.Other", "symbol": "Other.helper",
             "file": "", "facts": {}})
        with mock.patch.object(bag, "_load", return_value=fn):
            edges = bag._edges(units)
        self.assertEqual(len(edges), 1)
        self.assertEqual(edges[0]["to_agent_id"], "fn:demo.svc.Other#helper")
        self.assertEqual(edges[0]["resolution"], "static")
        self.assertNotIn("self_call", edges[0])


if __name__ == "__main__":
    unittest.main()