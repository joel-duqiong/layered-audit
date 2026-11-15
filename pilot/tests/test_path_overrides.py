"""路径覆盖入口的契约守卫（W19）。

锁的是**正确行为**，不是引擎当前输出的字符串（LESSONS 坑 2）：
  · 显式覆盖必须真的改读取点与写入点，且给了覆盖就不再依赖项目绑定；
  · 不给覆盖时默认行为一字不变；
  · 复用字节码模式绝不编译、绝不删目录、绝不 docker cp；
  · 没测量的东西不许报成「逐字节一致」（硬约束 3 三态）；
  · 产物字节可复现（不随 PYTHONHASHSEED 漂移）。

全部合成夹具 + 临时目录：不写 pilot/ 产物、不写被测仓库、不跑 maven/javac/docker。
"""
from __future__ import annotations

import hashlib
import json
import os
import subprocess
import sys
import time
from pathlib import Path

import pytest

PILOT = Path(__file__).resolve().parents[1]
ENGINE = PILOT / "engine"
sys.path.insert(0, str(PILOT))

import file_static  # noqa: E402
import fn_static  # noqa: E402

LAYERS_YAML = """version: 0.1
schema_version: 1
layers:
  - id: function
    categories:
      - {id: boundary, desc: "x"}
  - id: file
    categories:
      - {id: cohesion, desc: "x"}
  - id: class
    categories:
      - {id: encapsulation_leak, desc: "x"}
  - id: service
    categories:
      - {id: contract_alignment, desc: "x"}
  - id: integration
    categories:
      - {id: emergent_masking, desc: "x"}
"""

# 合成声明：类名/表名/不变式 id 都是独有的，读错目录必然对不上。
FN_DECL = {"functions": [{"class": "com.syn.SynService", "method": "synMethod",
                          "modifiers": "public", "returns": "void", "params": [],
                          "internal_calls": [], "external_calls": [], "side_effects": [],
                          "branch_count": 0, "is_pure": True}]}
# 另一世代的声明：只改一个方法名，指纹必须变。
FN_DECL_ALT = {"functions": [{**FN_DECL["functions"][0], "method": "anotherMethod"}]}
CLASS_DECL = {"ddl": {"tables": {"syn_table": {"columns": {"syn_col": {"type": "INT"}},
                                              "constraints": {}}}, "indexes": []},
              "jpa": {"SynEntity": {"file": "SynEntity.java", "table": "syn_table",
                                    "table_explicit": True, "columns": [],
                                    "extends_mapped_superclass": False}}}
OPENAPI = {"openapi": "3.0.0", "info": {"title": "syn", "version": "1"},
           "paths": {"/api/syn": {"get": {"tags": ["syn-controller"],
                                          "operationId": "synGet", "responses": {"200": {}}}}}}
INVARIANTS = {"invariants": [{"id": "BI-SYN-ONLY", "entity": "syn_table",
                              "assert": {"op": "gte", "field": "syn_col", "value": 0}}]}


def _write(path: Path, data) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
    return path


def _binding(tmp_path: Path, artifacts: Path, with_artifacts_key: bool = True) -> dict[str, str]:
    """最小项目绑定：只含 load_target 的必填段 + 图构建器读的段。"""
    root = tmp_path / "binding"
    root.mkdir(parents=True, exist_ok=True)
    (root / "layers.yaml").write_text(LAYERS_YAML, encoding="utf-8")
    paths = {"backend": str(tmp_path), "java_package": "com/syn",
             "work_dir": str(tmp_path / "work")}
    if with_artifacts_key:
        paths["artifacts_dir"] = str(artifacts)
    _write(root / "target.json", {
        "target": "synthetic", "contract": "layers.yaml",
        "annotation_table": "finding_meta.py", "evidence_sources": {},
        "source_roots": {"java": str(tmp_path / "src")},
        "carriers": {}, "layer_carriers": {}, "obligation_templates": {},
        "paths": paths,
    })
    return {**os.environ, "LAYERED_AUDIT_TARGET": str(root / "target.json")}


def _run(script: Path, argv: list[str], env: dict[str, str]) -> subprocess.CompletedProcess:
    return subprocess.run([sys.executable, str(script), *argv],
                          capture_output=True, text=True, env=env, cwd=str(script.parent))


# ---------------------------------------------------------------- relation_net

def test_relation_net_overrides_need_no_project_binding(tmp_path):
    """给了两个目录覆盖就必须能跑，且**完全不读项目绑定**（指向不存在的 target）。"""
    art = tmp_path / "art"
    cfg = tmp_path / "cfg"
    for name, data in (("fn_declaration.json", FN_DECL), ("class_declaration.json", CLASS_DECL),
                       ("openapi.json", OPENAPI)):
        _write(art / name, data)
    _write(cfg / "invariants.json", INVARIANTS)
    env = {**os.environ, "LAYERED_AUDIT_TARGET": str(tmp_path / "no-such-target.json")}
    out = tmp_path / "net.json"
    proc = _run(ENGINE / "relation_net.py",
                ["--artifacts-dir", str(art), "--project-dir", str(cfg), "--out", str(out)], env)
    assert proc.returncode == 0, proc.stderr
    net = json.loads(out.read_text(encoding="utf-8"))
    assert "BI-SYN-ONLY" in net["invariant_anchors"], "读的不是 --project-dir 给的判据"
    assert any(n.get("type") == "TABLE" and "syn_table" in json.dumps(n)
               for n in net["nodes"].values()), "读的不是 --artifacts-dir 给的声明"


def test_relation_net_artifacts_dir_is_not_ignored(tmp_path):
    """两个候选目录内容不同：产物必须来自 --artifacts-dir，不是绑定目录。"""
    art = tmp_path / "art"
    binding_art = tmp_path / "binding_art"
    _write(art / "fn_declaration.json", FN_DECL)
    _write(art / "class_declaration.json", CLASS_DECL)
    _write(art / "openapi.json", OPENAPI)
    _write(binding_art / "fn_declaration.json", {"functions": []})
    _write(binding_art / "class_declaration.json", {"ddl": {"tables": {}, "indexes": []}, "jpa": {}})
    _write(binding_art / "openapi.json", {"paths": {}})
    env = _binding(tmp_path, binding_art)
    out = tmp_path / "net.json"
    proc = _run(ENGINE / "relation_net.py",
                ["--artifacts-dir", str(art), "--project-dir", str(tmp_path), "--out", str(out)], env)
    assert proc.returncode == 0, proc.stderr
    net = json.loads(out.read_text(encoding="utf-8"))
    assert net["counts"]["nodes"] > 0, "覆盖被忽略：读成了绑定目录里的空声明"


def test_relation_net_missing_config_is_tolerated_not_red(tmp_path):
    """反向：新仓库没有 invariants/surfaces 是合法输入，必须照常出图，不许误红。"""
    art = tmp_path / "art"
    _write(art / "fn_declaration.json", FN_DECL)
    _write(art / "class_declaration.json", CLASS_DECL)
    _write(art / "openapi.json", OPENAPI)
    env = {**os.environ, "LAYERED_AUDIT_TARGET": str(tmp_path / "no-such-target.json")}
    out = tmp_path / "net.json"
    proc = _run(ENGINE / "relation_net.py",
                ["--artifacts-dir", str(art), "--project-dir", str(tmp_path / "empty"),
                 "--out", str(out)], env)
    assert proc.returncode == 0, proc.stderr
    net = json.loads(out.read_text(encoding="utf-8"))
    assert net["counts"]["nodes"] > 0 and net["invariant_anchors"] == {}


def test_relation_net_default_path_equals_explicit_binding_path(tmp_path):
    """默认行为不变：同一绑定下，不带覆盖 vs 用覆盖显式指向同两个目录，产物逐字节相同。

    两侧都用合成绑定，且路径只从 env 字典推 —— 不调 rn.project_dir()：
    findings_schema.target_path() 带 lru_cache，进程内缓存会被别的测试改过的
    LAYERED_AUDIT_TARGET 钉住，与 subprocess 的环境解析不一致（实测全量里假红）。
    """
    art = tmp_path / "art"
    _write(art / "fn_declaration.json", FN_DECL)
    _write(art / "class_declaration.json", CLASS_DECL)
    _write(art / "openapi.json", OPENAPI)
    env = _binding(tmp_path, art)
    binding_dir = Path(env["LAYERED_AUDIT_TARGET"]).parent
    _write(binding_dir / "invariants.json", INVARIANTS)

    default_out = tmp_path / "default.json"
    proc = _run(ENGINE / "relation_net.py", ["--out", str(default_out)], env)
    assert proc.returncode == 0, proc.stderr
    explicit_out = tmp_path / "explicit.json"
    proc = _run(ENGINE / "relation_net.py",
                ["--artifacts-dir", str(art), "--project-dir", str(binding_dir),
                 "--out", str(explicit_out)], env)
    assert proc.returncode == 0, proc.stderr
    assert default_out.read_bytes() == explicit_out.read_bytes()


# ---------------------------------------------------------------- build_agent_graph

def test_build_agent_graph_out_dir_does_not_touch_input_or_binding(tmp_path):
    art = tmp_path / "art"
    out = tmp_path / "out"
    _write(art / "fn_declaration.json", FN_DECL)
    env = _binding(tmp_path, art)
    proc = _run(ENGINE / "build_agent_graph.py",
                ["--artifacts-dir", str(art), "--out-dir", str(out)], env)
    assert proc.returncode == 0, proc.stderr
    assert (out / "units.json").is_file() and (out / "edges.json").is_file()
    assert not (art / "units.json").exists(), "--out-dir 被忽略：写进了产物目录"
    assert not (art / "edges.json").exists()
    binding_dir = Path(env["LAYERED_AUDIT_TARGET"]).parent
    assert not (binding_dir / "units.json").exists(), "写进了项目绑定目录"


def test_build_agent_graph_artifacts_dir_is_not_ignored(tmp_path):
    art = tmp_path / "art"
    other = tmp_path / "other"
    _write(art / "fn_declaration.json", FN_DECL)
    _write(other / "fn_declaration.json", {"functions": []})
    env = _binding(tmp_path, other)
    out = tmp_path / "out"
    proc = _run(ENGINE / "build_agent_graph.py",
                ["--artifacts-dir", str(art), "--out-dir", str(out)], env)
    assert proc.returncode == 0, proc.stderr
    units = json.loads((out / "units.json").read_text(encoding="utf-8"))
    keys = {u["unit_key"] for u in units["units"]}
    assert "com.syn.SynService#synMethod" in keys, "覆盖被忽略：读成了绑定目录的空声明"


def test_build_agent_graph_bytes_reproducible_across_hash_seeds(tmp_path):
    """layer_roles 曾由集合推导式构造，键序随 PYTHONHASHSEED 漂移 → 同输入不同输出。"""
    art = tmp_path / "art"
    _write(art / "fn_declaration.json", FN_DECL)
    _write(art / "class_declaration.json", CLASS_DECL)
    env = _binding(tmp_path, art)
    shots = []
    for seed in ("1", "2"):
        out = tmp_path / f"out{seed}"
        proc = _run(ENGINE / "build_agent_graph.py",
                    ["--artifacts-dir", str(art), "--out-dir", str(out)],
                    {**env, "PYTHONHASHSEED": seed})
        assert proc.returncode == 0, proc.stderr
        shots.append((out / "units.json").read_bytes())
    assert shots[0] == shots[1], "units.json 字节不可复现（键序随 hash seed 漂移）"


# ---------------------------------------------------------------- class_static

def test_class_static_overrides_read_and_write_given_paths(tmp_path):
    mig = tmp_path / "mig"
    ent = tmp_path / "ent"
    mig.mkdir(parents=True)
    ent.mkdir(parents=True)
    (mig / "V1__syn.sql").write_text(
        "CREATE TABLE syn_only_table (\n  syn_col INT NOT NULL\n);\n", encoding="utf-8")
    (ent / "SynOnlyEntity.java").write_text(
        'package com.syn;\n@Entity\n@Table(name = "syn_only_table")\n'
        'public class SynOnlyEntity {\n  @Column(name = "syn_col") private Integer synCol;\n}\n',
        encoding="utf-8")
    art = tmp_path / "art"
    env = _binding(tmp_path, art)
    out = tmp_path / "elsewhere" / "class_declaration.json"
    proc = _run(PILOT / "class_static.py",
                ["--migration-dir", str(mig), "--entity-dir", str(ent), "--out", str(out)], env)
    assert proc.returncode == 0, proc.stderr
    decl = json.loads(out.read_text(encoding="utf-8"))
    assert "syn_only_table" in decl["ddl"]["tables"], "--migration-dir 被忽略"
    assert "SynOnlyEntity" in decl["jpa"], "--entity-dir 被忽略"
    assert not (art / "class_declaration.json").exists(), "--out 被忽略：写进了默认产物目录"


# ---------------------------------------------------------------- fn_static

METHOD = {"name": "synMethod", "modifiers": "public", "returns": "void", "params_raw": "",
          "caught": [], "annotations": [], "method_reference_calls": [], "calls": [],
          "constants": [], "branch_count": 0, "return_count": 0, "bytecode_size": 1,
          "null_guard_count": 0}


@pytest.fixture
def stubbed(tmp_path, monkeypatch):
    """把 javap/解析换成桩，只测控制流与路径；编译/部署 helper 一旦被调就炸。"""
    classes = tmp_path / "classes"
    (classes / "com" / "syn").mkdir(parents=True)
    klass = classes / "com" / "syn" / "SynService.class"
    klass.write_bytes(b"\xca\xfe\xba\xbe stub")
    monkeypatch.setattr(fn_static, "CLASSES", classes)
    monkeypatch.setattr(fn_static, "PKG", "com.syn")
    monkeypatch.setattr(fn_static, "javap", lambda *a, **k: "stub javap text")
    monkeypatch.setattr(fn_static, "parse_methods", lambda *a, **k: [dict(METHOD)])

    def _boom(*a, **k):
        raise AssertionError("复用模式绝不允许编译/取部署产物（会 rmtree 字节码目录）")

    monkeypatch.setattr(fn_static, "ensure_compiled", _boom)
    monkeypatch.setattr(fn_static, "ensure_deployed_classes", _boom)
    return classes, klass


def _argv(out: Path, *extra: str) -> list[str]:
    return ["fn_static.py", "--out", str(out), *extra]


def test_reuse_mode_never_compiles_and_never_deletes(tmp_path, monkeypatch, stubbed):
    classes, klass = stubbed
    out = tmp_path / "fn_declaration.json"
    monkeypatch.setattr(sys, "argv", _argv(out, "--classes-dir", str(classes)))
    fn_static.main()
    assert klass.exists(), "复用模式删掉了给它的字节码目录"
    decl = json.loads(out.read_text(encoding="utf-8"))
    assert decl["meta"]["functions"] == 1
    assert decl["meta"]["deployed_artifact_compared"] is False


def test_no_compile_flag_alone_also_skips_helpers(tmp_path, monkeypatch, stubbed):
    classes, _ = stubbed
    out = tmp_path / "fn_declaration.json"
    monkeypatch.setattr(sys, "argv", _argv(out, "--no-compile"))
    fn_static.main()
    assert out.is_file()


def test_unmeasured_drift_is_not_reported_as_identical(tmp_path, monkeypatch, stubbed, capsys):
    """没比对部署产物就不许说「逐字节一致」——unknown 不得变 pass（硬约束 3）。"""
    classes, _ = stubbed
    out = tmp_path / "fn_declaration.json"
    monkeypatch.setattr(sys, "argv", _argv(out, "--classes-dir", str(classes)))
    fn_static.main()
    text = capsys.readouterr().out
    assert "逐字节一致" not in text, "未测量却报了正面结论"
    assert "未比对" in text and "未知" in text


def test_compile_mode_still_compiles(tmp_path, monkeypatch, stubbed):
    """反向：不带复用参数时编译路径必须照旧走，不许过度跳过。"""
    calls = []
    monkeypatch.setattr(fn_static, "ensure_compiled", lambda: calls.append("compiled"))
    monkeypatch.setattr(fn_static, "ensure_deployed_classes", lambda: False)
    out = tmp_path / "fn_declaration.json"
    monkeypatch.setattr(sys, "argv", _argv(out))
    fn_static.main()
    assert calls == ["compiled"], "默认编译路径被误跳过"


def test_missing_classes_dir_exits_nonzero_without_empty_product(tmp_path):
    """缺字节码是缺输入，必须判红且不产空声明（subprocess：同进程变异对 CLI 无效，坑 2）。"""
    out = tmp_path / "fn_declaration.json"
    proc = _run(PILOT / "fn_static.py",
                ["--classes-dir", str(tmp_path / "nope"), "--out", str(out)], os.environ.copy())
    assert proc.returncode != 0, "缺字节码却 exit 0"
    assert not out.exists(), "缺输入却产出了空声明产物"


# ---------------------------------------------------------------- 反向：合法输入不许误红

def test_build_agent_graph_same_input_and_output_dir_is_legal(tmp_path):
    """--out-dir 与 --artifacts-dir 同目录是合法用法（默认行为就是这样），不许拒绝。"""
    art = tmp_path / "art"
    _write(art / "fn_declaration.json", FN_DECL)
    env = _binding(tmp_path, art)
    proc = _run(ENGINE / "build_agent_graph.py",
                ["--artifacts-dir", str(art), "--out-dir", str(art)], env)
    assert proc.returncode == 0, proc.stderr
    assert (art / "units.json").is_file() and (art / "edges.json").is_file()


def test_relation_net_non_canonical_path_is_legal(tmp_path):
    """符号链接目录是合法输入：覆盖参数不得要求规范路径（坑 78 的反向假阳性）。"""
    real = tmp_path / "real"
    _write(real / "fn_declaration.json", FN_DECL)
    _write(real / "class_declaration.json", CLASS_DECL)
    _write(real / "openapi.json", OPENAPI)
    link = tmp_path / "link"
    link.symlink_to(real, target_is_directory=True)
    env = {**os.environ, "LAYERED_AUDIT_TARGET": str(tmp_path / "no-such-target.json")}
    out = tmp_path / "net.json"
    proc = _run(ENGINE / "relation_net.py",
                ["--artifacts-dir", str(link), "--project-dir", str(tmp_path), "--out", str(out)], env)
    assert proc.returncode == 0, proc.stderr
    assert json.loads(out.read_text(encoding="utf-8"))["counts"]["nodes"] > 0


def test_fn_static_reuse_skips_inner_classes_not_red(tmp_path, monkeypatch, stubbed):
    """内部类 .class 是合法字节码：复用模式必须跳过它们而不是报错或算进函数。"""
    classes, _ = stubbed
    (classes / "com" / "syn" / "SynService$Inner.class").write_bytes(b"\xca\xfe\xba\xbe stub")
    out = tmp_path / "fn_declaration.json"
    monkeypatch.setattr(sys, "argv", _argv(out, "--classes-dir", str(classes)))
    fn_static.main()
    decl = json.loads(out.read_text(encoding="utf-8"))
    assert decl["meta"]["compiled_classes"] == 1
    assert {f["class"] for f in decl["functions"]} == {"com.syn.SynService"}


# ---------------------------------------------------------------- file_static

@pytest.fixture
def file_stubbed(tmp_path, monkeypatch):
    classes = tmp_path / "fclasses"
    (classes / "com" / "syn").mkdir(parents=True)
    klass = classes / "com" / "syn" / "SynService.class"
    klass.write_bytes(b"\xca\xfe\xba\xbe stub")
    monkeypatch.setattr(file_static, "CLASSES", classes)
    monkeypatch.setattr(file_static, "PKG", "com.syn")
    monkeypatch.setattr(file_static, "BACKEND", tmp_path)
    monkeypatch.setattr(file_static, "DEPS_TEST", tmp_path / "deps_test.txt")
    monkeypatch.setattr(file_static, "analyse_file",
                        lambda fqcn: {"class": fqcn, "methods": [],
                                      "unreachable_private_methods": [],
                                      "write_methods_without_transaction": []})

    def _boom(*a, **k):
        raise AssertionError("复用模式绝不允许编译 / 跑 mvn（ensure_compiled 会 rmtree 字节码目录）")

    monkeypatch.setattr(file_static, "ensure_compiled", _boom)
    monkeypatch.setattr(file_static, "ensure_test_deps", _boom)
    return classes, klass


def test_file_static_reuse_never_compiles_and_writes_given_out(tmp_path, monkeypatch, file_stubbed):
    classes, klass = file_stubbed
    (tmp_path / "deps_test.txt").write_text("/some/cp.jar", encoding="utf-8")
    out = tmp_path / "elsewhere" / "file_declaration.json"
    monkeypatch.setattr(sys, "argv",
                        ["file_static.py", "--classes-dir", str(classes), "--out", str(out)])
    file_static.main()
    assert klass.exists(), "复用模式删掉了给它的字节码目录"
    assert json.loads(out.read_text(encoding="utf-8"))["meta"]["files"] == 1
    assert (out.parent / "file_deps.txt").read_text(encoding="utf-8") == "/some/cp.jar"


def test_file_static_unknown_test_classpath_is_declared_not_empty(tmp_path, monkeypatch,
                                                                 file_stubbed, capsys):
    """取不到 test classpath 是 unknown，不许静默当成空 classpath（硬约束 3）。"""
    classes, _ = file_stubbed
    out = tmp_path / "fd.json"
    monkeypatch.setattr(sys, "argv",
                        ["file_static.py", "--classes-dir", str(classes), "--out", str(out)])
    file_static.main()
    text = capsys.readouterr().out
    assert "未知" in text and "mvn" in text


def test_file_static_compile_mode_still_compiles(tmp_path, monkeypatch, file_stubbed):
    """反向：不带复用参数时编译与 test 依赖路径必须照旧走，不许过度跳过。"""
    calls = []
    monkeypatch.setattr(file_static, "ensure_compiled", lambda: calls.append("compiled"))
    monkeypatch.setattr(file_static, "ensure_test_deps", lambda: calls.append("deps") or "/cp")
    out = tmp_path / "fd.json"
    monkeypatch.setattr(sys, "argv", ["file_static.py", "--out", str(out)])
    file_static.main()
    assert calls == ["compiled", "deps"]


def test_file_static_missing_classes_dir_exits_nonzero(tmp_path):
    out = tmp_path / "fd.json"
    proc = _run(PILOT / "file_static.py",
                ["--classes-dir", str(tmp_path / "nope"), "--out", str(out)], os.environ.copy())
    assert proc.returncode != 0, "缺字节码却 exit 0"
    assert not out.exists(), "缺输入却产出了空声明产物"


# ---------------------------------------------------------------- 静默分叉 / 空图假绿

def test_graph_divergent_binding_dirs_is_hard_error(tmp_path):
    """绑定声明 artifacts_dir 而构建器读 target 目录 → 曾静默产出 2 单元/0 边且 exit 0。"""
    art = tmp_path / "art"
    _write(art / "fn_declaration.json", FN_DECL)
    env = _binding(tmp_path, art)
    proc = _run(ENGINE / "build_agent_graph.py", [], env)
    assert proc.returncode != 0, "输入目录分叉却 exit 0 出数（假绿）"
    assert "分叉" in proc.stderr
    assert not (art / "units.json").exists()
    assert not (Path(env["LAYERED_AUDIT_TARGET"]).parent / "units.json").exists()


def test_graph_explicit_artifacts_dir_resolves_divergence(tmp_path):
    """反向：显式传 --artifacts-dir 就是调用方自己选了目录，合法，不许误红。"""
    art = tmp_path / "art"
    _write(art / "fn_declaration.json", FN_DECL)
    env = _binding(tmp_path, art)
    out = tmp_path / "out"
    proc = _run(ENGINE / "build_agent_graph.py",
                ["--artifacts-dir", str(art), "--out-dir", str(out)], env)
    assert proc.returncode == 0, proc.stderr
    keys = {u["unit_key"] for u in json.loads((out / "units.json").read_text(encoding="utf-8"))["units"]}
    assert "com.syn.SynService#synMethod" in keys


def test_graph_binding_without_artifacts_dir_key_is_legal(tmp_path):
    """反向：默认绑定（无 artifacts_dir 键，两个目录重合）是既有项目形态，不许误红。"""
    env = _binding(tmp_path, tmp_path / "unused", with_artifacts_key=False)
    binding_dir = Path(env["LAYERED_AUDIT_TARGET"]).parent
    _write(binding_dir / "fn_declaration.json", FN_DECL)
    out = tmp_path / "out"
    proc = _run(ENGINE / "build_agent_graph.py", ["--out-dir", str(out)], env)
    assert proc.returncode == 0, proc.stderr
    assert (out / "units.json").is_file()


def test_graph_missing_declarations_refuses_empty_graph(tmp_path):
    """三份声明全缺 → 只剩 service/integration 占位单元，这是缺输入不是零违规。"""
    art = tmp_path / "art"
    art.mkdir(parents=True)
    env = _binding(tmp_path, art)
    out = tmp_path / "out"
    proc = _run(ENGINE / "build_agent_graph.py",
                ["--artifacts-dir", str(art), "--out-dir", str(out)], env)
    assert proc.returncode != 0, "缺声明却出空图 exit 0"
    assert "缺声明输入" in proc.stderr
    assert not (out / "units.json").exists()


# ---------------------------------------------------------------- 陈旧产物

def _net_inputs(tmp_path: Path) -> tuple[Path, Path]:
    art = tmp_path / "art"
    cfg = tmp_path / "cfg"
    _write(art / "fn_declaration.json", FN_DECL)
    _write(art / "class_declaration.json", CLASS_DECL)
    _write(art / "openapi.json", OPENAPI)
    _write(cfg / "invariants.json", INVARIANTS)
    return art, cfg


def test_relation_net_warns_when_existing_product_is_stale(tmp_path):
    art, cfg = _net_inputs(tmp_path)
    out = tmp_path / "net.json"
    out.write_text("{}", encoding="utf-8")
    old = time.time() - 100000
    os.utime(out, (old, old))
    env = {**os.environ, "LAYERED_AUDIT_TARGET": str(tmp_path / "no-such-target.json")}
    proc = _run(ENGINE / "relation_net.py",
                ["--artifacts-dir", str(art), "--project-dir", str(cfg), "--out", str(out)], env)
    assert proc.returncode == 0, proc.stderr
    assert "落后于输入" in proc.stdout, "输入比产物新却不告警（陈旧产物会少边）"
    assert "fn_declaration.json" in proc.stdout


def test_relation_net_fresh_product_does_not_warn(tmp_path):
    """反向：产物比输入新是正常状态，不许误告警。"""
    art, cfg = _net_inputs(tmp_path)
    out = tmp_path / "net.json"
    out.write_text("{}", encoding="utf-8")
    future = time.time() + 100000
    os.utime(out, (future, future))
    env = {**os.environ, "LAYERED_AUDIT_TARGET": str(tmp_path / "no-such-target.json")}
    proc = _run(ENGINE / "relation_net.py",
                ["--artifacts-dir", str(art), "--project-dir", str(cfg), "--out", str(out)], env)
    assert proc.returncode == 0, proc.stderr
    assert "落后于输入" not in proc.stdout


# ---------------------------------------------------------------- 世代一致性

def _expected_stamp(fn_decl, class_decl) -> dict:
    """独立参考实现：不复用任何引擎代码（坑 2 —— 守卫的输入必须外生于被验对象）。"""
    def h(obj):
        return "sha256:" + hashlib.sha256(
            json.dumps(obj, sort_keys=True, ensure_ascii=False).encode("utf-8")).hexdigest()[:16]
    return {"fn_declaration": h(fn_decl), "class_declaration": h(class_decl)}


def _build_pair(tmp_path: Path, sub: str, fn_decl: dict) -> tuple[Path, Path]:
    root = tmp_path / sub
    art, out, cfg = root / "art", root / "graph", root / "cfg"
    _write(art / "fn_declaration.json", fn_decl)
    _write(art / "class_declaration.json", CLASS_DECL)
    _write(art / "openapi.json", OPENAPI)
    _write(cfg / "invariants.json", INVARIANTS)
    env = _binding(root, art)
    proc = _run(ENGINE / "build_agent_graph.py",
                ["--artifacts-dir", str(art), "--out-dir", str(out)], env)
    assert proc.returncode == 0, proc.stderr
    net = out / "relation_net.json"
    proc = _run(ENGINE / "relation_net.py",
                ["--artifacts-dir", str(art), "--project-dir", str(cfg), "--out", str(net)], env)
    assert proc.returncode == 0, proc.stderr
    return net, out / "units.json"


def test_generation_stamp_matches_independent_reference_on_both_carriers(tmp_path):
    """两个引擎各写一份指纹，口径必须相同且等于独立参考实现（坑 11 跨载体漂移）。"""
    net, units = _build_pair(tmp_path, "a", FN_DECL)
    want = _expected_stamp(FN_DECL, CLASS_DECL)
    assert json.loads(net.read_text(encoding="utf-8"))["generation"] == want
    assert json.loads(units.read_text(encoding="utf-8"))["generation"] == want


def test_generation_same_inputs_is_consistent(tmp_path):
    """反向：同世代合法输入必须判一致，不许误红。"""
    net, units = _build_pair(tmp_path, "a", FN_DECL)
    proc = _run(ENGINE / "relation_net.py", ["--check-generation", str(net), str(units)],
                os.environ.copy())
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert "世代一致" in proc.stdout


def test_generation_mismatch_is_hard_error(tmp_path):
    """net 与 units 来自不同世代的声明 → 必须判红，不得静默出伪影覆盖率。"""
    net_a, _ = _build_pair(tmp_path, "a", FN_DECL)
    _, units_b = _build_pair(tmp_path, "b", FN_DECL_ALT)
    proc = _run(ENGINE / "relation_net.py", ["--check-generation", str(net_a), str(units_b)],
                os.environ.copy())
    assert proc.returncode == 1, "世代错配却 exit 0"
    assert "世代错配" in proc.stdout and "fn_declaration" in proc.stdout


def test_generation_missing_stamp_is_unknown_not_pass(tmp_path):
    """旧产物没有指纹：缺指纹是 UNKNOWN，绝不许当成同世代（硬约束 3）。"""
    net, units = _build_pair(tmp_path, "a", FN_DECL)
    doc = json.loads(units.read_text(encoding="utf-8"))
    del doc["generation"]
    units.write_text(json.dumps(doc), encoding="utf-8")
    proc = _run(ENGINE / "relation_net.py", ["--check-generation", str(net), str(units)],
                os.environ.copy())
    assert proc.returncode == 2, "缺指纹被判成一致或错配，都不是 UNKNOWN"
    assert "UNKNOWN" in proc.stdout and "不是同世代" in proc.stdout


def test_generation_unreadable_product_is_unknown(tmp_path):
    net, _ = _build_pair(tmp_path, "a", FN_DECL)
    proc = _run(ENGINE / "relation_net.py",
                ["--check-generation", str(net), str(tmp_path / "nope.json")], os.environ.copy())
    assert proc.returncode == 2
    assert "UNKNOWN" in proc.stdout


# ---------------------------------------------------------------- 建模域声明

DOMAIN_FN = {"functions": [
    {"class": "com.syn.SynController", "method": "list", "modifiers": "public", "returns": "void",
     "params": [], "internal_calls": ["com.syn.SynService#list"], "external_calls": [],
     "side_effects": [], "branch_count": 0, "is_pure": True},
    {"class": "com.syn.SynService", "method": "list", "modifiers": "public", "returns": "void",
     "params": [], "internal_calls": ["com.syn.SynRepository#findAll"], "external_calls": [],
     "side_effects": ["SynRepository"], "branch_count": 0, "is_pure": False},
    {"class": "com.syn.SynRepository", "method": "findAll", "modifiers": "public",
     "returns": "java.util.List", "params": [], "internal_calls": [], "external_calls": [],
     "side_effects": [], "branch_count": 0, "is_pure": True},
    {"class": "com.syn.SynDto", "method": "getName", "modifiers": "public", "returns": "String",
     "params": [], "internal_calls": [], "external_calls": [], "side_effects": [],
     "branch_count": 0, "is_pure": True},
]}


def test_modeling_domain_matches_actual_node_selection(tmp_path):
    """域声明必须与真实建点行为一致，且**非空泛**：后缀类真建点、非后缀类真不建点。

    实测背景：net 只给 *Controller/*Service/*Repository 建方法节点，
    而 units 含 dto/entity/config 等细胞；不声明域就会把它们静默算成「未知足迹」。
    """
    root = tmp_path / "dom"
    art, cfg = root / "art", root / "cfg"
    _write(art / "fn_declaration.json", DOMAIN_FN)
    _write(art / "class_declaration.json", CLASS_DECL)
    _write(art / "openapi.json", OPENAPI)
    _write(cfg / "invariants.json", INVARIANTS)
    env = {**os.environ, "LAYERED_AUDIT_TARGET": str(root / "no-such-target.json")}
    out = root / "net.json"
    proc = _run(ENGINE / "relation_net.py",
                ["--artifacts-dir", str(art), "--project-dir", str(cfg), "--out", str(out)], env)
    assert proc.returncode == 0, proc.stderr
    doc = json.loads(out.read_text(encoding="utf-8"))
    dom = doc["modeling_domain"]
    assert dom["cell_key_form"] == "FQCN#method"
    assert set(dom["method_node_types"]) <= set(dom["node_types"])

    method_nodes = {k: v for k, v in doc["nodes"].items() if v["type"] in dom["method_node_types"]}
    assert method_nodes, "空泛守卫：一个方法节点都没有，后缀断言恒真"
    for nid, node in method_nodes.items():
        assert any(node["simple"].endswith(s) for s in dom["class_suffixes"]), \
            f"建了点却不落在声明的域里: {nid}"
    # 双向：域声明的后缀必须真的各自建出了点（不是写着好看）
    present = {s for s in dom["class_suffixes"]
               if any(n["simple"].endswith(s) for n in method_nodes.values())}
    assert present == {"Controller", "Service", "Repository"}
    # 反向：非后缀类（dto）绝不建点 —— 域外是 not_applicable，不是 unknown
    assert not any("SynDto" in k for k in doc["nodes"]), "域外类被建点，域声明与行为不符"