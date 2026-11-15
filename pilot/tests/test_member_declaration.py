"""成员声明生产者的守卫。

核心红线：生产者转录出的 members 必须让**默认形状**的消费者真的评估 visibility，
并且 private  callee 仍然判违规 —— 否则「把修饰符全当 public」这类变异会静默存活。
第二条红线：上游缺 modifiers 时跳过并计数，绝不补空串伪造 package_private。
"""
import hashlib
import itertools
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "engine"))
import arch_rules as ar
import member_declaration as md

PILOT = Path(__file__).resolve().parents[1]
ENGINE = PILOT / "engine"

C, S = "a.controller.C", "a.service.S"
LAYERS = ("layers:\n"
          "  - id: function\n    categories:\n      - {id: boundary, desc: \"x\", oracle: [static_analysis]}\n"
          "  - id: file\n    categories:\n      - {id: cohesion, desc: \"x\", oracle: [static_analysis]}\n"
          "  - id: class\n    categories:\n      - {id: encapsulation_leak, desc: \"x\", oracle: [static_analysis]}\n"
          "  - id: service\n    categories:\n      - {id: authz, desc: \"x\", oracle: [static_analysis]}\n"
          "  - id: integration\n    categories:\n      - {id: end_to_end_business, desc: \"x\", oracle: [e2e_scenario]}\n")
UPSTREAM_SHAPE = {"records": "functions", "class": "class", "method": "method", "modifiers": "modifiers"}
NET_SHAPE = {"records": "nodes", "class": "cls", "method": "method", "modifiers": "modifiers"}


def visibility_rule(rid="A1", **kw):
    base = {"id": rid, "type": "visibility", "severity": "high", "claim": "封装边界被穿透",
            "levels": ["private", "package_private"], "exempt_same_top_level_class": True,
            "layer": "class", "category": "encapsulation_leak"}
    base.update(kw)
    return base


def config(*rules, member_source=None):
    cfg = {"schema": ar.CONFIG_SCHEMA,
           "roles": [{"id": "controller", "class_patterns": ["*.controller.*"]},
                     {"id": "service", "class_patterns": ["*.service.*"]}],
           "rules": list(rules)}
    if member_source is not None:
        cfg["member_source"] = member_source
    return cfg


def declaration(rows):
    return {"functions": rows}


def member(cls, method, modifiers):
    return {"class": cls, "method": method, "modifiers": modifiers}


def graph(callee_methods=("secret", "open")):
    units = [{"unit_key": f"{C}#list", "kind": "method", "class": C, "symbol": "C.list", "file": "src/C.java"}]
    edges = []
    for index, method in enumerate(callee_methods):
        units.append({"unit_key": f"{S}#{method}", "kind": "method", "class": S,
                      "symbol": f"S.{method}", "file": "src/S.java"})
        edges.append({"edge_id": f"E{index}", "kind": "call", "from_agent_id": f"fn:{C}#list",
                      "to_agent_id": f"fn:{S}#{method}", "resolution": "static", "call_count": 1})
    return {"repo_id": "demo", "units": units}, {"edges": edges}


class ProducerTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        (self.root / "layers.yaml").write_text(LAYERS, encoding="utf-8")
        units, edges = graph()
        (self.root / "units.json").write_text(json.dumps(units), encoding="utf-8")
        (self.root / "edges.json").write_text(json.dumps(edges), encoding="utf-8")
        (self.root / "decl.json").write_text(json.dumps(declaration([
            member(C, "list", "public"), member(S, "secret", "private"), member(S, "open", "public")])),
            encoding="utf-8")
        # 消费者配置**不声明 member_source** —— 即零配置入口的默认形状 {records: "members"}
        (self.root / "rules_default.json").write_text(json.dumps(config(visibility_rule())), encoding="utf-8")
        (self.root / "rules_upstream.json").write_text(
            json.dumps(config(visibility_rule(), member_source=[UPSTREAM_SHAPE, NET_SHAPE])), encoding="utf-8")

    def tearDown(self):
        self.tmp.cleanup()

    def write(self, name, data):
        (self.root / name).write_text(json.dumps(data), encoding="utf-8")

    def produce(self, *inputs, rules="rules_upstream.json", out="members.json"):
        argv = [sys.executable, str(ENGINE / "member_declaration.py"), "--out", out]
        for label, path in inputs:
            argv += ["--input", f"{label}={path}"]
        if rules:
            argv += ["--rules", rules]
        return subprocess.run(argv, cwd=self.root, capture_output=True, text=True)

    def consume(self, members="members.json", rules="rules_default.json", out="audit.json"):
        argv = [sys.executable, str(ENGINE / "arch_rules.py"), "--units", "units.json",
                "--edges", "edges.json", "--rules", rules, "--layers", "layers.yaml", "--out", out]
        if members:
            argv += ["--members", members]
        return subprocess.run(argv, cwd=self.root, capture_output=True, text=True)

    def report(self, name="audit.json"):
        return json.loads((self.root / name).read_text(encoding="utf-8"))

    def produced(self, name="members.json"):
        return json.loads((self.root / name).read_text(encoding="utf-8"))

    def visibility(self, report):
        return next(r for r in report["rules"] if r["type"] == "visibility")

    # ---- 核心红线：转录 → 默认形状消费者 → private callee 仍判违规

    def test_produced_members_unlock_visibility_under_default_shape(self):
        proc = self.produce(("A", "decl.json"))
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(self.produced()["schema"], md.SCHEMA)
        audit = self.consume()
        self.assertEqual(audit.returncode, 0, audit.stderr)
        rule = self.visibility(self.report())
        self.assertEqual(rule["counts"]["violation"], 1, "private callee 必须判违规，否则守卫无牙")
        self.assertEqual(rule["counts"]["compliant"], 1)
        self.assertEqual(rule["measured_edges"], 2)
        self.assertEqual(rule["in_scope_edges"], 2)
        self.assertEqual(self.report()["member_coverage"]["declaration_missing"], 0)
        self.assertEqual(self.report()["member_coverage"]["source_records"], "members")
        self.assertEqual(self.report()["findings"][0]["to"]["method"], "secret")

    def test_raw_upstream_artifact_is_rejected_by_default_shape(self):
        """证明生产者不是多余的：默认形状吃不下 functions 容器。"""
        proc = self.consume(members="decl.json")
        self.assertNotEqual(proc.returncode, 0)
        self.assertIn("members", proc.stderr)

    def test_visibility_guard_without_members_is_not_weakened(self):
        proc = self.consume(members=None)
        self.assertNotEqual(proc.returncode, 0)
        self.assertIn("visibility", proc.stderr)
        self.assertFalse((self.root / "audit.json").exists())

    # ---- 诚实性：缺字段不补空串

    def test_missing_modifiers_field_is_skipped_and_counted(self):
        self.write("decl.json", declaration([member(C, "list", "public"),
                                             {"class": S, "method": "secret"},
                                             member(S, "open", "public")]))
        proc = self.produce(("A", "decl.json"))
        self.assertEqual(proc.returncode, 0, proc.stderr)
        data = self.produced()
        self.assertEqual(data["sources"][0]["skipped_reasons"], {"modifiers_missing": 1})
        self.assertEqual(data["sources"][0]["records_emitted"], 2)
        self.assertNotIn({"class": S, "method": "secret", "modifiers": ""}, data["members"])
        audit = self.consume()
        self.assertEqual(audit.returncode, 0, audit.stderr)
        rule = self.visibility(self.report())
        self.assertEqual(rule["counts"]["violation"], 0, "缺声明不得被补成 package_private 而伪造违规")
        self.assertEqual(rule["counts"]["unknown"], 1)
        self.assertEqual(rule["reasons"]["member_declaration_missing"], 1)
        self.assertEqual(self.report()["member_coverage"]["declaration_missing"], 1)

    def test_non_string_modifiers_is_skipped_not_coerced(self):
        self.write("decl.json", declaration([member(S, "secret", None), member(S, "open", "public")]))
        proc = self.produce(("A", "decl.json"))
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(self.produced()["sources"][0]["skipped_reasons"], {"modifiers_missing": 1})

    def test_empty_modifiers_is_preserved_as_package_private_fact(self):
        self.write("decl.json", declaration([member(S, "secret", ""), member(S, "open", "public")]))
        proc = self.produce(("A", "decl.json"))
        self.assertEqual(proc.returncode, 0, proc.stderr)
        data = self.produced()
        self.assertEqual(data["coverage"]["empty_modifiers"], 1)
        self.assertEqual(data["sources"][0]["empty_modifiers"], [{"class": S, "method": "secret"}])
        self.assertIn(member(S, "secret", ""), data["members"])
        self.assertEqual(self.consume().returncode, 0)
        self.assertEqual(self.visibility(self.report())["counts"]["violation"], 1)

    # ---- 空/退化容器：硬报错不出数

    def test_empty_container_exits_nonzero_and_writes_nothing(self):
        self.write("decl.json", declaration([]))
        proc = self.produce(("A", "decl.json"))
        self.assertNotEqual(proc.returncode, 0)
        self.assertIn("容器为空", proc.stderr + proc.stdout)
        self.assertFalse((self.root / "members.json").exists())

    def test_all_rows_unqualified_exits_nonzero_with_attribution(self):
        self.write("decl.json", {"functions": [{"class": S, "method": "secret"}, "not-an-object"]})
        proc = self.produce(("A", "decl.json"))
        self.assertNotEqual(proc.returncode, 0)
        self.assertIn("全不合格", proc.stderr)
        self.assertIn("modifiers_missing", proc.stderr)
        self.assertFalse((self.root / "members.json").exists())

    def test_downstream_empty_members_guard_still_fires(self):
        self.write("members.json", {"members": []})
        proc = self.consume()
        self.assertNotEqual(proc.returncode, 0)
        self.assertIn("成员声明输入不可用", proc.stderr)

    # ---- 多载体：冲突不挑赢家

    def test_conflicting_carriers_both_emitted_and_consumer_judges_unknown(self):
        self.write("net.json", {"nodes": {f"{S}#secret": {"cls": S, "method": "secret", "modifiers": "public"}}})
        proc = self.produce(("A", "decl.json"), ("B", "net.json"))
        self.assertEqual(proc.returncode, 0, proc.stderr)
        data = self.produced()
        self.assertEqual(data["coverage"]["conflicting_keys"], 1)
        self.assertEqual(data["coverage"]["conflicts"][0]["modifiers"], ["private", "public"])
        self.assertEqual(sum(1 for r in data["members"] if r["method"] == "secret"), 2)
        self.assertEqual(self.consume().returncode, 0)
        rule = self.visibility(self.report())
        self.assertEqual(rule["counts"]["violation"], 0)
        self.assertEqual(rule["counts"]["compliant"], 1)
        self.assertEqual(rule["reasons"]["member_modifiers_ambiguous"], 1)
        self.assertEqual(self.report()["member_coverage"]["modifiers_ambiguous"], 1)

    def test_overload_conflict_inside_one_carrier_is_not_resolved(self):
        """同一载体内重载同名、修饰符不同 —— 不许挑赢家，必须留给消费者判 unknown。"""
        self.write("decl.json", declaration([member(S, "secret", "private"), member(S, "secret", "public"),
                                             member(S, "open", "public")]))
        proc = self.produce(("A", "decl.json"))
        self.assertEqual(proc.returncode, 0, proc.stderr)
        data = self.produced()
        self.assertEqual(data["coverage"]["records_emitted"], 3)
        self.assertEqual(data["coverage"]["duplicate_records"], 1)
        self.assertEqual(data["coverage"]["conflicting_keys"], 1)
        self.assertEqual(data["coverage"]["conflicts"][0]["modifiers"], ["private", "public"])
        self.assertEqual(self.consume().returncode, 0)
        rule = self.visibility(self.report())
        self.assertEqual(rule["counts"]["violation"], 0)
        self.assertEqual(rule["reasons"]["member_modifiers_ambiguous"], 1)
        self.assertEqual(self.report()["member_coverage"]["modifiers_ambiguous"], 1)

    def test_agreeing_carriers_add_records_without_changing_keys(self):
        self.write("net.json", {"nodes": {f"{S}#secret": {"cls": S, "method": "secret", "modifiers": "private"}}})
        proc = self.produce(("A", "decl.json"), ("B", "net.json"))
        self.assertEqual(proc.returncode, 0, proc.stderr)
        data = self.produced()
        self.assertEqual(data["coverage"]["conflicting_keys"], 0)
        self.assertEqual(data["coverage"]["distinct_keys"], 3)
        self.assertEqual(data["coverage"]["duplicate_records"], 1)
        self.assertEqual(self.consume().returncode, 0)
        self.assertEqual(self.visibility(self.report())["counts"]["violation"], 1)

    def test_heterogeneous_net_rows_are_skipped_with_reason(self):
        self.write("net.json", {"nodes": {"table:x": {"type": "TABLE", "cls": "x"},
                                          f"{S}#secret": {"cls": S, "method": "secret", "modifiers": "private"}}})
        proc = self.produce(("B", "net.json"))
        self.assertEqual(proc.returncode, 0, proc.stderr)
        source = self.produced()["sources"][0]
        self.assertEqual(source["container"], "nodes")
        self.assertEqual(source["records_seen"], 2)
        self.assertEqual(source["records_emitted"], 1)
        self.assertEqual(source["records_skipped"], 1)

    def test_source_without_members_is_reported_not_hidden(self):
        self.write("empty.json", declaration([]))
        proc = self.produce(("A", "decl.json"), ("B", "empty.json"))
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(self.produced()["coverage"]["sources_without_members"], ["B"])

    # ---- 形状 / 出处 / 不覆盖输入

    def test_shape_defaults_to_canonical_members_container(self):
        self.write("canonical.json", {"members": [member(S, "secret", "private")]})
        proc = self.produce(("A", "canonical.json"), rules=None)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(self.produced()["sources"][0]["container"], "members")

    def test_unknown_container_fails_loud(self):
        proc = self.produce(("A", "units.json"))
        self.assertNotEqual(proc.returncode, 0)
        self.assertIn("找不到任何已声明的记录容器", proc.stderr)

    def test_provenance_records_resolved_path_and_sha256(self):
        proc = self.produce(("A", "decl.json"))
        self.assertEqual(proc.returncode, 0, proc.stderr)
        source = self.produced()["sources"][0]
        self.assertEqual(source["label"], "A")
        self.assertEqual(source["path"], str((self.root / "decl.json").resolve()))
        self.assertEqual(source["sha256"], hashlib.sha256((self.root / "decl.json").read_bytes()).hexdigest())

    def test_out_must_not_clobber_input(self):
        before = (self.root / "decl.json").read_bytes()
        proc = self.produce(("A", "decl.json"), out="decl.json")
        self.assertNotEqual(proc.returncode, 0)
        self.assertEqual((self.root / "decl.json").read_bytes(), before)

    # ---- 覆盖守卫必须锁「文件身份等价类」，不只锁路径字符串相等

    def canonical_decl(self):
        """规范形状输入：确保走到写入分支，而不是被形状拒绝提前挡掉（否则测不到覆盖）。"""
        self.write("canonical.json", {"members": [member(S, "secret", "private")]})
        return self.root / "canonical.json"

    def test_out_relative_vs_input_absolute_same_file_is_refused(self):
        src = self.canonical_decl()
        before = hashlib.sha256(src.read_bytes()).hexdigest()
        proc = self.produce(("A", str(src)), rules=None, out="canonical.json")
        self.assertEqual(proc.returncode, 2, proc.stderr)
        self.assertEqual(hashlib.sha256(src.read_bytes()).hexdigest(), before)

    def test_out_symlink_alias_of_input_is_refused(self):
        src = self.canonical_decl()
        alias = self.root / "alias.json"
        alias.symlink_to(src)
        before = hashlib.sha256(src.read_bytes()).hexdigest()
        proc = self.produce(("A", str(src)), rules=None, out=str(alias))
        self.assertEqual(proc.returncode, 2, proc.stderr)
        self.assertEqual(hashlib.sha256(src.read_bytes()).hexdigest(), before)

    def test_out_hardlink_alias_of_input_is_refused(self):
        """硬链接：同 inode 不同路径，resolve() 看不出，必须按文件身份挡。"""
        src = self.canonical_decl()
        alias = self.root / "hard.json"
        os.link(src, alias)
        self.assertEqual(os.stat(src).st_ino, os.stat(alias).st_ino)
        before = hashlib.sha256(src.read_bytes()).hexdigest()
        proc = self.produce(("A", str(src)), rules=None, out=str(alias))
        self.assertEqual(proc.returncode, 2, proc.stderr)
        self.assertEqual(hashlib.sha256(src.read_bytes()).hexdigest(), before, "上游证据被覆盖")

    def test_out_clobbering_rules_config_is_refused(self):
        before = hashlib.sha256((self.root / "rules_upstream.json").read_bytes()).hexdigest()
        proc = self.produce(("A", "decl.json"), out="rules_upstream.json")
        self.assertEqual(proc.returncode, 2, proc.stderr)
        self.assertEqual(hashlib.sha256((self.root / "rules_upstream.json").read_bytes()).hexdigest(), before)

    def test_out_clobbering_any_later_input_is_refused(self):
        """C1b：多输入时**每一个**输入都受保护。只靠多输入才能杀 —— rules 那条守卫杀不了它。"""
        sources = []
        for index in range(3):
            path = self.root / f"carrier{index}.json"
            path.write_text(json.dumps({"members": [member(f"a.S{index}", "m", "private")]}), encoding="utf-8")
            sources.append((f"L{index}", str(path)))
        for index, (_, path) in enumerate(sources):
            with self.subTest(target_input=index):
                before = hashlib.sha256(Path(path).read_bytes()).hexdigest()
                proc = self.produce(*sources, rules=None, out=path)
                self.assertEqual(proc.returncode, 2, proc.stderr)
                self.assertEqual(hashlib.sha256(Path(path).read_bytes()).hexdigest(), before,
                                 f"第 {index} 个输入产物被覆盖")

    def test_distinct_out_in_same_directory_is_allowed(self):
        """反向：合法输入不得误红 —— 同目录不同文件必须放行。"""
        proc = self.produce(("A", "decl.json"), out="members.json")
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertTrue((self.root / "members.json").is_file())

    def test_out_symlink_to_unrelated_file_is_allowed(self):
        """反向：符号链接本身不是罪，指向非输入文件必须放行。"""
        src = self.canonical_decl()
        unrelated = self.root / "unrelated.json"
        unrelated.write_text("{}", encoding="utf-8")
        alias = self.root / "out_alias.json"
        alias.symlink_to(unrelated)
        proc = self.produce(("A", str(src)), rules=None, out=str(alias))
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(json.loads(unrelated.read_text(encoding="utf-8"))["schema"], md.SCHEMA)

    def test_out_not_yet_existing_is_allowed(self):
        """反向：out 不存在时 samefile 无从比起，不得因此误红。"""
        src = self.canonical_decl()
        target = self.root / "nested" / "fresh.json"
        target.parent.mkdir()
        proc = self.produce(("A", str(src)), rules=None, out=str(target))
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertTrue(target.is_file())

    def test_out_dangling_symlink_is_allowed(self):
        """反向：悬空符号链接（目标尚不存在）不得误红，写入应创建目标。"""
        src = self.canonical_decl()
        alias = self.root / "dangling.json"
        alias.symlink_to(self.root / "not_created_yet.json")
        self.assertFalse(alias.exists())
        proc = self.produce(("A", str(src)), rules=None, out=str(alias))
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(json.loads((self.root / "not_created_yet.json").read_text(encoding="utf-8"))["schema"],
                         md.SCHEMA)

    def test_out_hardlink_to_non_input_file_is_allowed(self):
        """反向：硬链接本身不是罪 —— 指向非输入文件必须放行（只禁指向输入的那种类）。"""
        src = self.canonical_decl()
        before = hashlib.sha256(src.read_bytes()).hexdigest()
        unrelated = self.root / "unrelated.json"
        unrelated.write_text("{}", encoding="utf-8")
        alias = self.root / "hard_unrelated.json"
        os.link(unrelated, alias)
        proc = self.produce(("A", str(src)), rules=None, out=str(alias))
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(json.loads(unrelated.read_text(encoding="utf-8"))["schema"], md.SCHEMA)
        self.assertEqual(hashlib.sha256(src.read_bytes()).hexdigest(), before, "输入产物不得被动到")

    def test_out_same_basename_different_directory_is_allowed(self):
        """反向：同 basename 不同目录是不同文件，不得按名字误红。"""
        src = self.canonical_decl()
        other = self.root / "other"
        other.mkdir()
        target = other / src.name
        proc = self.produce(("A", str(src)), rules=None, out=str(target))
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertTrue(target.is_file())
        self.assertNotEqual(os.stat(src).st_ino, os.stat(target).st_ino)

    def test_bad_input_spec_fails_loud(self):
        proc = subprocess.run([sys.executable, str(ENGINE / "member_declaration.py"),
                               "--input", "no-label-separator", "--out", "members.json"],
                              cwd=self.root, capture_output=True, text=True)
        self.assertNotEqual(proc.returncode, 0)
        self.assertIn("LABEL=PATH", proc.stderr)

    # ---- 测量诚实性：域内 0 边 ≠ 测得合规

    def test_zero_in_scope_is_no_edge_not_clean(self):
        units, edges = graph(callee_methods=())
        edges["edges"] = [{"edge_id": "E0", "kind": "call", "from_agent_id": f"fn:{S}#open",
                           "to_agent_id": f"fn:{S}#secret", "resolution": "static", "call_count": 1}]
        units["units"].append({"unit_key": f"{S}#open", "kind": "method", "class": S,
                               "symbol": "S.open", "file": "src/S.java"})
        self.write("units.json", units)
        self.write("edges.json", edges)
        self.assertEqual(self.produce(("A", "decl.json")).returncode, 0)
        self.assertEqual(self.consume().returncode, 0)
        rule = self.visibility(self.report())
        self.assertEqual(rule["in_scope_edges"], 0)
        self.assertEqual(rule["measured_edges"], 0)
        self.assertEqual(rule["verdict"], ar.VERDICT_NO_EDGE)
        self.assertIn("A1", self.report()["measurement"]["rules_without_measurement"])

    def test_measured_clean_is_a_different_verdict(self):
        self.write("decl.json", declaration([member(S, "secret", "public"), member(S, "open", "public")]))
        self.assertEqual(self.produce(("A", "decl.json")).returncode, 0)
        self.assertEqual(self.consume().returncode, 0)
        rule = self.visibility(self.report())
        self.assertEqual(rule["measured_edges"], 2)
        self.assertEqual(rule["counts"]["violation"], 0)
        self.assertEqual(rule["verdict"], ar.VERDICT_CLEAN)
        self.assertNotEqual(ar.VERDICT_NO_EDGE, ar.VERDICT_CLEAN)


    def test_whitespace_only_modifiers_is_bookkept_as_package_private(self):
        """V6 判别用例：消费者把纯空白也读成 package_private，簿记漏列就是口径声明说谎。"""
        for value in ("   ", "\t", "\n", " \t "):
            with self.subTest(modifiers=value):
                self.write("decl.json", declaration([member(S, "secret", value), member(S, "open", "public")]))
                proc = self.produce(("A", "decl.json"))
                self.assertEqual(proc.returncode, 0, proc.stderr)
                data = self.produced()
                self.assertEqual(ar.visibility_level(value), "package_private")
                self.assertEqual(data["coverage"]["empty_modifiers"], 1, "纯空白必须与空串同样列进复核清单")
                self.assertEqual(data["sources"][0]["empty_modifiers"], [{"class": S, "method": "secret"}])
                self.assertEqual(self.consume().returncode, 0)
                self.assertEqual(self.visibility(self.report())["counts"]["violation"], 1)


class HonestyClaimTest(unittest.TestCase):
    """口径声明本身也是不变式：notes 被篡改成假话必须红（V16 报此处零守卫）。"""

    # 声明侧锚点放在**测试里**，外生于被验对象（坑 2：不从被验对象的构造路径再推导一遍）
    NOTE_CLAIMS = [
        ("转录", "不是新测量", "sha256"),
        ("规范 members 形状", "select_source"),
        ("空串", "package_private", "empty_modifiers", "skipped_reasons"),
        ("重载", "不挑赢家", "unknown"),
        ("conflicting_keys", "sources_without_members", "不得当成等价口径"),
    ]

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        (self.root / "decl.json").write_text(json.dumps(declaration([member(S, "secret", "private")])),
                                             encoding="utf-8")

    def tearDown(self):
        self.tmp.cleanup()

    def produced(self):
        return md.build([("A", self.root / "decl.json")], UPSTREAM_SHAPE)

    def test_notes_state_every_honesty_claim(self):
        notes = self.produced()["notes"]
        self.assertEqual(len(notes), len(self.NOTE_CLAIMS), "口径声明条数变了必须显式复核，不许静默增删")
        for index, (note, anchors) in enumerate(zip(notes, self.NOTE_CLAIMS)):
            for anchor in anchors:
                self.assertIn(anchor, note, f"notes[{index}] 丢了关键语义 {anchor!r} —— 口径声明被篡改")

    # 极性词本身是断言对象。短语必须带**左上下文**：只钉「低于」会被「不低于」蒙混（子串仍满足），
    # 只钉关键词 + 条数不变时，N1/N2 这类极性翻转实测 41+4 全绿。
    NOTE_POLARITY = [
        ("是转录，不是新测量", "否定：转录 ≠ 新测量"),
        ("喂本文件会被 select_source 拒掉", "方向：本文件被拒，不是被接受"),
        ("缺 modifiers 字段的行不在此列", "否定：缺字段行不在复核清单里"),
        ("修饰符冲突不挑赢家", "否定：不挑赢家"),
        ("本产物的判定力低于完整输入", "方向：判定力下降，不是「不低于」"),
    ]

    def test_note_polarity_is_pinned_not_just_keywords(self):
        notes = self.produced()["notes"]
        self.assertEqual(len(notes), len(self.NOTE_POLARITY))
        for index, (note, (phrase, meaning)) in enumerate(zip(notes, self.NOTE_POLARITY)):
            self.assertIn(phrase, note, f"notes[{index}] 极性被翻转：{meaning}")

    def test_polarity_phrases_do_not_match_their_own_flips(self):
        """自证量具：每个极性短语必须**不**被它的翻转形态包含，否则这条守卫是摆设。"""
        flips = ["是新测量", "不会被 select_source 拒掉", "也在此列", "冲突挑赢家", "判定力不低于完整输入"]
        for (phrase, _), flip in zip(self.NOTE_POLARITY, flips):
            self.assertNotIn(phrase, flip, f"短语 {phrase!r} 被翻转形态 {flip!r} 包含 —— 锚点选错了")

    def test_claim_transcription_not_new_measurement_is_verifiable(self):
        data = self.produced()
        for source in data["sources"]:
            self.assertEqual(source["sha256"],
                             hashlib.sha256(Path(source["path"]).read_bytes()).hexdigest())
        self.assertEqual(data["members"], [member(S, "secret", "private")])

    def test_claim_empty_modifiers_semantics_matches_consumer(self):
        for value in ("", "   ", "\t"):
            self.assertEqual(ar.visibility_level(value), "package_private")
        self.assertIsNone(ar.visibility_level(None))

    def test_claim_candidate_list_caveat_is_behaviorally_true(self):
        """notes 声称「候选只声明 functions/nodes 时喂本文件会被拒」—— 两侧都要实测。"""
        data = self.produced()
        with self.assertRaises(ValueError):
            ar.select_source(data, [UPSTREAM_SHAPE, NET_SHAPE], "members")
        self.assertEqual(ar.select_source(data, ar.DEFAULT_MEMBER_SOURCE, "members")["records"], "members")
        raw = json.loads((self.root / "decl.json").read_text(encoding="utf-8"))
        self.assertEqual(ar.select_source(raw, [UPSTREAM_SHAPE, NET_SHAPE], "members")["records"], "functions")

    def test_claim_degraded_power_is_not_equivalent(self):
        """conflicting_keys>0 时判定力真的下降：域内边判不出来，不是「等价口径」。"""
        (self.root / "net.json").write_text(json.dumps(
            {"nodes": {f"{S}#secret": {"cls": S, "method": "secret", "modifiers": "public"}}}), encoding="utf-8")
        data = md.build([("A", self.root / "decl.json"), ("B", self.root / "net.json")],
                        [UPSTREAM_SHAPE, NET_SHAPE])
        self.assertEqual(data["coverage"]["conflicting_keys"], 1)
        members = ar.load_members(data, ar.DEFAULT_MEMBER_SOURCE)
        self.assertIsNone(members[(S, "secret")], "冲突键必须不可判定，否则「判定力下降」这句是假话")


class VocabularyTest(unittest.TestCase):
    """字段名词汇表只许有一个载体（坑 11）：判定函数复用了，词汇表也必须复用。"""

    def test_fields_is_derived_from_consumer_not_a_second_literal(self):
        self.assertEqual(md.FIELDS, tuple(k for k in ar.DEFAULT_MEMBER_SOURCE if k != "records"))
        source = (PILOT / "engine" / "member_declaration.py").read_text(encoding="utf-8")
        self.assertNotIn('FIELDS = ("class", "method", "modifiers")', source,
                         "词汇表退回独立字面量就是第二载体")

    def test_consumer_gate_requires_exactly_these_fields(self):
        spec = ar.DEFAULT_MEMBER_SOURCE
        row = {spec[f]: ("a.S" if f == "class" else "m" if f == "method" else "public") for f in md.FIELDS}
        self.assertTrue(ar.member_row_ok(row, spec))
        for field in md.FIELDS:
            partial = dict(row)
            partial.pop(spec[field])
            self.assertFalse(ar.member_row_ok(partial, spec),
                             f"{field} 不再是消费者必需键 —— 词汇表已漂移")

    def test_consumer_config_validation_uses_the_same_vocabulary(self):
        cfg = config(visibility_rule(), member_source=UPSTREAM_SHAPE)
        validated = ar.validate_config(cfg)["member_source"]
        self.assertEqual(set(validated), {"records", *md.FIELDS})


class GateEquivalenceTest(unittest.TestCase):
    """V7 定性：skip_reason 当闸门与消费者闸门在 219 个行形态上零分歧 = 当前等价。

    等价是绑定当前实现的临时结论（坑 61），所以用差分守卫钉住：
    消费者哪天改了合格判据，这条立刻红，而不是让归因函数静默变成第二套闸门。
    """

    SPEC = UPSTREAM_SHAPE

    def rows(self):
        classes = ["a.S", "", None, 5, [], {"x": 1}]
        methods = ["m", "", None, 7, ["m"]]
        mods = ["public", "", "   ", None, 0, ["public"], {"a": 1}]
        rows = [{"class": c, "method": m, "modifiers": mo}
                for c, m, mo in itertools.product(classes, methods, mods)]
        for drop in ("class", "method", "modifiers"):
            base = member("a.S", "m", "public")
            base.pop(drop)
            rows.append(base)
        rows += [None, "junk", 42, [], {"cls": "a.S", "method": "m", "modifiers": "public"},
                 dict(member("a.S", "m", "public"), extra=1)]
        return rows

    def test_attribution_gate_agrees_with_consumer_on_every_shape(self):
        rows = self.rows()
        disagree = [(row, ar.member_row_ok(row, self.SPEC), md.skip_reason(row, self.SPEC))
                    for row in rows
                    if ar.member_row_ok(row, self.SPEC) != (md.skip_reason(row, self.SPEC) == "not_member_shaped")]
        self.assertEqual(len(rows), 219, "判别空间被截断就是分母造假（坑 25）")
        self.assertEqual(disagree, [], "归因函数与消费者闸门分歧 —— 它已变成第二套判据")

    def test_every_rejection_has_a_specific_reason(self):
        for row in self.rows():
            if not ar.member_row_ok(row, self.SPEC):
                self.assertNotEqual(md.skip_reason(row, self.SPEC), "not_member_shaped")


class TranscribeUnitTest(unittest.TestCase):
    """纯函数层：合格判据必须与消费者同一个，不另立一套。"""

    def test_accepts_exactly_what_consumer_accepts(self):
        rows = [member(S, "secret", "private"), member(S, "open", ""),
                {"class": S, "method": "x"}, {"class": "", "method": "y", "modifiers": "public"},
                {"class": S, "method": "", "modifiers": "public"}, "junk",
                {"class": S, "method": "z", "modifiers": None}]
        emitted, stats = md.transcribe(declaration(rows), UPSTREAM_SHAPE)
        self.assertEqual(len(emitted), ar.member_record_stats(declaration(rows), UPSTREAM_SHAPE)["records_valid"])
        self.assertEqual(stats["records_emitted"], 2)
        self.assertEqual(stats["records_skipped"], 5)
        self.assertEqual(stats["skipped_reasons"], {"class_missing": 1, "method_missing": 1,
                                                   "modifiers_missing": 2, "not_an_object": 1})

    def test_skip_reason_never_overrides_consumer_gate(self):
        for row in (member(S, "m", "public"), member(S, "m", "")):
            self.assertTrue(ar.member_row_ok(row, UPSTREAM_SHAPE))
        self.assertEqual(md.skip_reason(member(S, "m", None), UPSTREAM_SHAPE), "modifiers_missing")


if __name__ == "__main__":
    unittest.main()