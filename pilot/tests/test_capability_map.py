"""能力图回归守卫 —— 每条都对应一个已被独立验证者判定 FAIL 的真缺陷。

 fixture 用的是真实案例的最小复现（OrderService#apply 的注解串味、
 CreateCategoryRequest 的 record 误判、Python 测试套件 × Java 源码的跨语言子串），
 不依赖任何项目路径，换项目照样跑。
"""
from __future__ import annotations

import json
import re
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "engine"))
import capability_map as cm

ORDER_SERVICE = """package com.example.service;

public class OrderService {

    @Transactional
    public OrderResponse updateStatus(Long id, OrderStatus status) {
        BanquetOrder order = findOrder(id);
        order.setStatus(status);
        BanquetOrder saved = orderRepository.save(order);
        log.info("updated order status id={}", saved.getId());
        return OrderMapper.toResponse(saved);
    }

    private void apply(BanquetOrder order, String customerName, String customerPhone, String banquetName,
                       String banquetType, String address, OffsetDateTime reservationTime, Integer tableCount,
                       Long packageId, List<PackageItemRequest> packageItems, List<AddedItemRequest> addedItems,
                       Long planTotalCent, Boolean useFinalPrice, Long finalPriceCent, Long depositCent,
                       OrderStatus status, String remark) {
        if (planTotalCent == null || planTotalCent < 0) {
            throw new BusinessException(ErrorCode.PRICE_INVALID, "金额不能小于 0");
        }
        order.setCustomerName(customerName.trim());
        order.setStatus(status);
    }

    private record DateRange(OffsetDateTime start, OffsetDateTime end) {
    }
}
"""

CATEGORY_CONTROLLER = """package com.example.controller;

@RestController
@RequestMapping("/api/categories")
@Tag(name = "分类")
public class CategoryController {

    @GetMapping
    @Operation(summary = "获取商品分类")
    public List<CategoryResponse> list() {
        return categoryService.list();
    }

    @PostMapping
    @Operation(summary = "新增商品分类", description = "同名分类幂等返回。")
    public CategoryResponse create(@Valid @RequestBody CreateCategoryRequest request) {
        return categoryService.create(request);
    }

    @DeleteMapping("/{id}")
    @Operation(summary = "删除商品分类")
    public CategoryResponse delete(@PathVariable Long id) {
        return categoryService.delete(id);
    }

    @PutMapping("/{id}")
    public CategoryResponse update(@PathVariable Long id, @Valid @RequestBody CreateCategoryRequest request) {
        return categoryService.update(id, request);
    }
}
"""

# 真实事故形态：Python HTTP 测试套件全文，里面全是裸动词，一个 Java 符号都没引用
PYTHON_TESTS_NO_HTTP = """import pytest

def test_list_things() -> list[dict]:
    d = {}
    d.update({"a": 1})
    d.get("a")
    d.delete = None
    return []

@pytest.hookimpl(hookwrapper=True)
def pytest_runtest_call(item):
    yield
"""

PYTHON_TESTS_HTTP = """def test_list_categories(api):
    body = api.get("/api/categories")

def test_create_category(api):
    body = api.post("/api/categories", json={"name": "x"})

def test_delete_category(api, category):
    body = api.delete(f"/api/categories/{category['id']}")
"""


def fn_unit(fqcls: str, method: str) -> dict:
    return {"unit_key": f"{fqcls}#{method}", "kind": "method", "layer": "function",
            "class": fqcls, "symbol": f"{fqcls.rsplit('.', 1)[-1]}.{method}", "file": ""}


def svc_node(taint: dict) -> dict:
    return {"type": "SVC_OP", "taint": taint}


def field_node(entity: str, field: str, table: str) -> dict:
    return {"type": "ENTITY_FIELD", "entity": entity, "field": field, "table": table}


def oracle(oid: str, target: dict, assertion: dict, kind: str = "ddl") -> dict:
    return {"id": oid, "level": "L1_executable_constraint", "confirmation": "confirmed",
            "source": {"kind": kind, "reference": "x:1", "text": "t"},
            "target": target, "assert": assertion}


class CapabilityMapTest(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        self.src = self.root / "src"
        self.src.mkdir()
        self.tests = self.root / "tests"
        self.tests.mkdir()

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def write_src(self, name: str, text: str) -> None:
        (self.src / name).write_text(text, encoding="utf-8")

    def write_tests(self, name: str, text: str) -> None:
        (self.tests / name).write_text(text, encoding="utf-8")

    def mine(self, units: list[dict], with_tests: bool = True):
        return cm.mine_missions([self.src], units, [self.tests] if with_tests else [])

    def scan(self, name: str) -> dict:
        return cm.scan_java(self.src / name, name)

    # ---- 缺陷 3：注解跨声明串味（OrderService#apply 真实案例）

    def test_annotation_does_not_leak_across_declarations(self):
        self.write_src("OrderService.java", ORDER_SERVICE)
        scan = self.scan("OrderService.java")
        by_name = {m["name"]: m for m in scan["members"] if m["kind"] in ("method", "constructor")}
        self.assertIn("updateStatus", by_name)
        self.assertIn("apply", by_name)
        self.assertEqual([a["name"] for a in by_name["updateStatus"]["annotations"]], ["Transactional"])
        self.assertEqual(by_name["apply"]["annotations"], [])
        self.assertIsNone(by_name["apply"]["comment"])
        self.assertGreaterEqual(by_name["apply"]["line"] - by_name["updateStatus"]["line"], 8)

        missions, _ = self.mine([fn_unit("com.example.service.OrderService", "updateStatus"),
                                 fn_unit("com.example.service.OrderService", "apply")], with_tests=False)
        self.assertEqual(missions["com.example.service.OrderService#updateStatus"]["tier"],
                         "confirmed_annotation")
        apply_mission = missions["com.example.service.OrderService#apply"]
        self.assertNotIn("transaction_boundary", apply_mission["roles"])
        self.assertFalse([e for e in apply_mission["evidence"] if e["tier"] == "confirmed_annotation"])
        self.assertEqual(apply_mission["tier"], "naming_hint")

    def test_annotation_before_field_does_not_leak_to_next_method(self):
        self.write_src("S.java", """package com.example;
public class S {
    @Column(nullable = false)
    private String name;

    @Transactional
    public void a() {
        int x = 1;
    }

    public void b() {
        int y = 2;
    }
}
""")
        scan = self.scan("S.java")
        methods = {m["name"]: m for m in scan["members"] if m["kind"] == "method"}
        self.assertEqual([a["name"] for a in methods["a"]["annotations"]], ["Transactional"])
        self.assertEqual(methods["b"]["annotations"], [])

    def test_comment_inside_previous_body_does_not_leak(self):
        self.write_src("S.java", """package com.example;
public class S {
    public void a() {
        // 这是 a 内部的注释
        int x = 1;
    }

    public void b() {
        int y = 2;
    }
}
""")
        methods = {m["name"]: m for m in self.scan("S.java")["members"] if m["kind"] == "method"}
        self.assertIsNone(methods["b"]["comment"])

    def test_same_method_name_in_two_classes_does_not_merge(self):
        self.write_src("OrderService.java", ORDER_SERVICE)
        self.write_src("ProductService.java", """package com.example.service;
public class ProductService {
    @Transactional
    public void apply(Product p) {
        p.setName("x");
    }
}
""")
        missions, _ = self.mine([fn_unit("com.example.service.OrderService", "apply"),
                                 fn_unit("com.example.service.ProductService", "apply")], with_tests=False)
        order = missions["com.example.service.OrderService#apply"]
        product = missions["com.example.service.ProductService#apply"]
        apply_line = [m for m in self.scan("OrderService.java")["members"]
                      if m["name"] == "apply"][0]["line"]
        self.assertEqual(order["references"], [f"OrderService.java:{apply_line}"])
        self.assertEqual(product["tier"], "confirmed_annotation")
        self.assertNotEqual(order["tier"], "confirmed_annotation")
        self.assertFalse([r for r in order["references"] if "ProductService" in r])

    # ---- 缺陷 6：record 不是方法

    def test_record_is_not_a_method(self):
        self.write_src("CreateCategoryRequest.java", """package com.example.dto;
public record CreateCategoryRequest(
    @NotBlank @Size(max = 64) String name
) {
}
""")
        scan = self.scan("CreateCategoryRequest.java")
        self.assertEqual([(t["kind"], t["simple"]) for t in scan["types"]],
                         [("record", "CreateCategoryRequest")])
        self.assertFalse([m for m in scan["members"] if m["name"] == "CreateCategoryRequest"])
        self.assertFalse([m for m in scan["members"] if m["kind"] == "method"])

    def test_nested_record_is_a_type_not_a_method(self):
        self.write_src("OrderService.java", ORDER_SERVICE)
        scan = self.scan("OrderService.java")
        self.assertIn(("record", "DateRange"), [(t["kind"], t["simple"]) for t in scan["types"]])
        self.assertFalse([m for m in scan["members"] if m["name"] == "DateRange"])

    def test_string_literals_with_comment_markers_do_not_break_scan(self):
        self.write_src("S.java", """package com.example;
public class S {
    private static final String URL = "http://example.invalid/a//b";
    @Transactional
    public void a() {
        String s = "/* not a comment */ // neither";
    }
    public void b() {
    }
}
""")
        methods = {m["name"]: m for m in self.scan("S.java")["members"] if m["kind"] == "method"}
        self.assertEqual([x["name"] for x in methods["a"]["annotations"]], ["Transactional"])
        self.assertEqual(methods["b"]["annotations"], [])

    # ---- 缺陷 4：@Operation / @Tag / Javadoc 是真实使命来源

    def test_operation_summary_is_mined_with_reference(self):
        self.write_src("CategoryController.java", CATEGORY_CONTROLLER)
        missions, _ = self.mine([fn_unit("com.example.controller.CategoryController", "create")],
                                with_tests=False)
        m = missions["com.example.controller.CategoryController#create"]
        declared = [e for e in m["evidence"] if e["tier"] == "declared_comment"]
        self.assertEqual(len(declared), 1)
        self.assertEqual(declared[0]["text"], "新增商品分类")
        self.assertEqual(declared[0]["source"], "@Operation")
        self.assertRegex(declared[0]["reference"], r"CategoryController\.java:\d+")
        self.assertEqual(m["statement"], "新增商品分类")
        self.assertEqual(m["statement_tier"], "declared_comment")

    def test_strong_tier_does_not_suppress_declared_mission_text(self):
        self.write_src("CategoryController.java", CATEGORY_CONTROLLER)
        self.write_tests("test_categories.py", PYTHON_TESTS_HTTP)
        missions, _ = self.mine([fn_unit("com.example.controller.CategoryController", "create")])
        m = missions["com.example.controller.CategoryController#create"]
        self.assertEqual(m["tier"], "confirmed_test")
        self.assertEqual(m["statement"], "新增商品分类")
        self.assertEqual(m["statement_tier"], "declared_comment")

    def test_javadoc_is_mined(self):
        self.write_src("S.java", """package com.example;
public class S {
    /**
     * 结算订单尾款。
     */
    public void settle() {
    }
}
""")
        missions, _ = self.mine([fn_unit("com.example.S", "settle")], with_tests=False)
        m = missions["com.example.S#settle"]
        self.assertEqual(m["statement"], "结算订单尾款。")
        self.assertEqual(m["statement_tier"], "declared_comment")

    def test_type_level_tag_is_mined_for_file_unit(self):
        self.write_src("CategoryController.java", CATEGORY_CONTROLLER)
        units = [{"unit_key": "com.example.controller.CategoryController", "kind": "file",
                  "layer": "file", "class": "com.example.controller.CategoryController",
                  "symbol": "CategoryController", "file": ""}]
        missions, _ = self.mine(units, with_tests=False)
        m = missions["com.example.controller.CategoryController"]
        self.assertEqual(m["statement"], "分类")
        self.assertEqual(m["statement_tier"], "declared_comment")

    # ---- 缺陷 1：跨语言子串不是测试证据

    def test_cross_language_substring_is_not_test_evidence(self):
        self.write_src("S.java", """package com.example;
public class S {
    public void list() {}
    public void update() {}
    public void get() {}
    public void ok() {}
    public void delete() {}
    public void health() {}
}
""")
        self.write_tests("test_things.py", PYTHON_TESTS_NO_HTTP)
        units = [fn_unit("com.example.S", n) for n in ("list", "update", "get", "ok", "delete", "health")]
        missions, index = self.mine(units)
        self.assertEqual(index["test_sites"], [])
        for n in ("list", "update", "get", "ok", "delete", "health"):
            m = missions[f"com.example.S#{n}"]
            self.assertFalse([e for e in m["evidence"] if e["tier"] == "confirmed_test"], n)
            self.assertNotEqual(m["tier"], "confirmed_test", n)

    def test_http_path_and_method_match_is_test_evidence(self):
        self.write_src("CategoryController.java", CATEGORY_CONTROLLER)
        self.write_tests("test_categories.py", PYTHON_TESTS_HTTP)
        units = [fn_unit("com.example.controller.CategoryController", n)
                 for n in ("list", "create", "delete", "update")]
        missions, index = self.mine(units)
        self.assertEqual(len(index["test_sites"]), 3)
        base = "com.example.controller.CategoryController#"
        for n, verb in (("list", "GET"), ("create", "POST"), ("delete", "DELETE")):
            ev = [e for e in missions[base + n]["evidence"] if e["tier"] == "confirmed_test"]
            self.assertTrue(ev, n)
            self.assertEqual(ev[0]["http"][0], verb)
            self.assertRegex(ev[0]["reference"], r"test_categories\.py:\d+")
        self.assertEqual(missions[base + "delete"]["evidence"][0]["http"][1], "/api/categories/*")
        update_ev = [e for e in missions[base + "update"]["evidence"] if e["tier"] == "confirmed_test"]
        self.assertEqual(update_ev, [])
        self.assertEqual(missions[base + "update"]["tier"], "confirmed_annotation")

    def test_multiline_http_call_reference_covers_the_path_line(self):
        self.write_src("CategoryController.java", CATEGORY_CONTROLLER)
        self.write_tests("t.py", 'def test_x(api):\n    body = api.get(\n        "/api/categories",\n        params={},\n    )\n')
        missions, index = self.mine([fn_unit("com.example.controller.CategoryController", "list")])
        self.assertEqual(index["test_sites"][0]["reference"], "t.py:2-3")
        ev = [e for e in missions["com.example.controller.CategoryController#list"]["evidence"]
              if e["tier"] == "confirmed_test"]
        self.assertEqual(ev[0]["reference"], "t.py:2-3")

    def test_untested_endpoints_are_reported_per_endpoint_not_per_class(self):
        self.write_src("CategoryController.java", CATEGORY_CONTROLLER)
        self.write_tests("test_categories.py", PYTHON_TESTS_HTTP)
        units = [fn_unit("com.example.controller.CategoryController", n)
                 for n in ("list", "create", "delete", "update")]
        units.append({"unit_key": "com.example.controller.CategoryController", "kind": "file",
                      "layer": "file", "class": "com.example.controller.CategoryController",
                      "symbol": "CategoryController", "file": ""})
        missions, index = self.mine(units)
        report = cm.build_report(units, [], missions,
                                 cm.compose(missions, units, index),
                                 cm.link_oracles([], units, {"nodes": {}, "edges": []}), index)
        c = report["counts"]
        self.assertEqual(c["http_endpoint_cells"], 4)
        self.assertEqual(c["http_endpoints_total"], 4)
        self.assertEqual(c["http_endpoints_tested"], 3)
        self.assertEqual(c["http_endpoints_parser_miss"], 0)
        self.assertEqual(c["http_endpoints_no_test_evidence"], 1)
        self.assertEqual(report["endpoint_no_test_evidence"],
                         {"com.example.controller.CategoryController#update": [["PUT", "/api/categories/*"]]})
        self.assertEqual(report["endpoint_parser_miss"], {})

    # ---- helper 别名：解析器漏抽 ≠ 真的没测试证据

    UPLOAD_CONTROLLER = """package com.example.controller;
@RestController
@RequestMapping("/api/uploads")
public class UploadController {
    @PostMapping(value = "/product-image", consumes = MediaType.MULTIPART_FORM_DATA_VALUE)
    @Operation(summary = "上传商品图片")
    public UploadResponse uploadProductImage(@RequestPart("file") MultipartFile file) {
        return uploadService.uploadProductImage(file);
    }
}
"""
    HELPERS_WITH_ALIAS = """class ApiClient:
    def _send(self, method: str, path: str, expected, **kwargs):
        resp = self.session.request(method, self.base_url + path, timeout=20, **kwargs)
        return resp

    def post(self, path, expected=200, **kwargs):
        return self._send("POST", path, expected, **kwargs)

    def upload(self, path, data, filename, content_type, expected=200):
        return self._send(
            "POST", path, expected, files={"file": (filename, data, content_type)}
        )
"""
    UPLOAD_TEST = """def test_upload_product_image(api):
    body = api.upload(
        "/api/uploads/product-image", PNG_1X1, "ai-test.png", "image/png"
    )
"""

    def test_helper_alias_is_resolved_from_the_test_suite_itself(self):
        self.write_src("UploadController.java", self.UPLOAD_CONTROLLER)
        self.write_tests("helpers.py", self.HELPERS_WITH_ALIAS)
        self.write_tests("test_uploads.py", self.UPLOAD_TEST)
        units = [fn_unit("com.example.controller.UploadController", "uploadProductImage")]
        missions, index = self.mine(units)
        self.assertEqual(cm._helper_aliases(self.HELPERS_WITH_ALIAS)["upload"], "POST")
        m = missions["com.example.controller.UploadController#uploadProductImage"]
        self.assertEqual(m["tier"], "confirmed_test")
        ev = [e for e in m["evidence"] if e["tier"] == "confirmed_test"]
        self.assertEqual(ev[0]["http"], ["POST", "/api/uploads/product-image"])
        self.assertRegex(ev[0]["reference"], r"test_uploads\.py:2-3")
        report = cm.build_report(units, [], missions, cm.compose(missions, units, index),
                                 cm.link_oracles([], units, {"nodes": {}, "edges": []}), index)
        self.assertEqual(report["counts"]["http_endpoints_tested"], 1)
        self.assertEqual(report["counts"]["http_endpoints_parser_miss"], 0)
        self.assertEqual(report["counts"]["http_endpoints_no_test_evidence"], 0)

    def test_unresolvable_helper_is_parser_miss_not_absence_of_tests(self):
        self.write_src("UploadController.java", self.UPLOAD_CONTROLLER)
        self.write_tests("test_uploads.py", self.UPLOAD_TEST)
        units = [fn_unit("com.example.controller.UploadController", "uploadProductImage")]
        missions, index = self.mine(units)
        m = missions["com.example.controller.UploadController#uploadProductImage"]
        self.assertEqual(m["tier"], "confirmed_annotation")
        report = cm.build_report(units, [], missions, cm.compose(missions, units, index),
                                 cm.link_oracles([], units, {"nodes": {}, "edges": []}), index)
        c = report["counts"]
        self.assertEqual(c["http_endpoints_tested"], 0)
        self.assertEqual(c["http_endpoints_parser_miss"], 1)
        self.assertEqual(c["http_endpoints_no_test_evidence"], 0)
        miss = report["endpoint_parser_miss"]["com.example.controller.UploadController#uploadProductImage"]
        self.assertEqual(miss[0]["unresolved_calls"][0]["callee"], "upload")
        self.assertEqual(report["endpoint_no_test_evidence"], {})

    def test_endpoint_with_no_request_at_all_is_no_test_evidence(self):
        self.write_src("UploadController.java", self.UPLOAD_CONTROLLER)
        self.write_tests("test_other.py", 'def test_x(api):\n    api.get("/api/categories")\n')
        units = [fn_unit("com.example.controller.UploadController", "uploadProductImage")]
        missions, index = self.mine(units)
        report = cm.build_report(units, [], missions, cm.compose(missions, units, index),
                                 cm.link_oracles([], units, {"nodes": {}, "edges": []}), index)
        c = report["counts"]
        self.assertEqual(c["http_endpoints_parser_miss"], 0)
        self.assertEqual(c["http_endpoints_no_test_evidence"], 1)

    def test_concrete_path_value_matches_path_variable_template(self):
        self.write_src("CategoryController.java", CATEGORY_CONTROLLER)
        self.write_tests("t.py", 'def test_x(api):\n    api.delete("/api/categories/999999999")\n')
        missions, _ = self.mine([fn_unit("com.example.controller.CategoryController", "delete")])
        m = missions["com.example.controller.CategoryController#delete"]
        self.assertEqual(m["tier"], "confirmed_test")
        self.assertEqual([e for e in m["evidence"] if e["tier"] == "confirmed_test"][0]["http"],
                         ["DELETE", "/api/categories/*"])

    def test_known_verb_call_on_same_path_is_not_a_parser_miss(self):
        self.write_src("CategoryController.java", CATEGORY_CONTROLLER)
        self.write_tests("t.py", 'def test_x(api):\n    api.delete("/api/categories/7")\n')
        units = [fn_unit("com.example.controller.CategoryController", n) for n in ("delete", "update")]
        missions, index = self.mine(units)
        report = cm.build_report(units, [], missions, cm.compose(missions, units, index),
                                 cm.link_oracles([], units, {"nodes": {}, "edges": []}), index)
        c = report["counts"]
        self.assertEqual(c["http_endpoints_tested"], 1)
        self.assertEqual(c["http_endpoints_parser_miss"], 0)
        self.assertEqual(c["http_endpoints_no_test_evidence"], 1)
        self.assertEqual(report["endpoint_no_test_evidence"],
                         {"com.example.controller.CategoryController#update": [["PUT", "/api/categories/*"]]})

    def test_alias_resolution_does_not_invent_a_verb(self):
        text = """class ApiClient:
    def fetch(self, path, expected=200):
        return self.session.request(self.method, self.base_url + path)
"""
        self.assertEqual(cm._helper_aliases(text), {})

    def test_upstream_assertion_vocabulary_drift_is_visible_not_silent(self):
        self.write_src("OrderService.java", self.GUARD_SERVICE)
        units = self._guard_units()
        known = self._gte_oracle("ddl:orders.known", "plan_total_cent")
        drifted = oracle("ddl:orders.drifted", {"table": "orders"},
                         {"type": "brand_new_type", "left": "plan_total_cent", "value": 0})
        missions, index = self.mine(units, with_tests=False)
        report = cm.build_report(units, [known, drifted], missions,
                                 cm.compose(missions, units, index),
                                 cm.link_oracles([known, drifted], units, self._guard_net(),
                                                 index["validation"], index["guards"]), index)
        self.assertEqual(report["counts"]["unrecognized_assertion_types"], {"brand_new_type": 1})
        self.assertEqual(report["counts"]["oracles_with_unrecognized_assertion_type"], 1)
        self.assertIn("上游 schema 漂移", report["semantics"]["unrecognized_assertion_types"])

    def test_prose_field_oracles_are_flagged_and_never_linked(self):
        self.write_src("S.java", "package com.example;\npublic class S {\n}\n")
        units = self._units()
        prose = oracle("doc:PRD:16.table", {"rule": "套餐商品数量 > 0"},
                       {"type": "field_cmp", "field": "套餐商品数量", "op": ">", "value": 0},
                       kind="document")
        code = oracle("ddl:package_items.quantity.not_null",
                      {"table": "package_items", "field": "quantity"},
                      {"type": "not_null", "field": "quantity"})
        missions, index = self.mine(units, with_tests=False)
        report = cm.build_report(units, [prose, code], missions,
                                 cm.compose(missions, units, index),
                                 cm.link_oracles([prose, code], units, self._net(),
                                                 index["validation"], index["guards"]), index)
        c = report["counts"]
        self.assertEqual(c["oracles_with_prose_field"], 1)
        self.assertEqual(c["prose_field_oracles_linked"], 0)
        self.assertEqual(c["prose_field_oracle_ids"], ["doc:PRD:16.table"])
        reason = [u["reason"] for u in report["unlinked_oracles"]
                  if u["oracle_id"] == "doc:PRD:16.table"][0]
        self.assertIn("自然语言名词", reason)
        self.assertIn("上游判据不可连", reason)
        self.assertEqual([e["oracle_id"] for e in report["implemented_by"]],
                         ["ddl:package_items.quantity.not_null"])

    def test_cli_emits_reproduce_block_with_real_input_hashes(self):
        import hashlib
        self.write_src("S.java", "package com.example;\npublic class S {\n    public void a() {}\n}\n")
        units_p = self.root / "units.json"
        oracles_p = self.root / "oracles.json"
        net_p = self.root / "net.json"
        out_p = self.root / "out.json"
        units_p.write_text(json.dumps({"units": [fn_unit("com.example.S", "a")]}), encoding="utf-8")
        oracles_p.write_text(json.dumps({"oracles": []}), encoding="utf-8")
        net_p.write_text(json.dumps({"nodes": {}, "edges": []}), encoding="utf-8")
        argv = ["capability_map.py", "--units", str(units_p), "--oracles", str(oracles_p),
                "--relation-net", str(net_p), "--source-root", str(self.src), "--out", str(out_p)]
        old = sys.argv
        sys.argv = argv
        try:
            self.assertEqual(cm.main(), 0)
        finally:
            sys.argv = old
        report = json.loads(out_p.read_text(encoding="utf-8"))
        rep = report["reproduce"]
        self.assertEqual(rep["command"], " ".join(argv))
        for name, p in (("units", units_p), ("oracles", oracles_p), ("relation_net", net_p)):
            self.assertEqual(rep["inputs"][name]["sha256"],
                             hashlib.sha256(p.read_bytes()).hexdigest())
            self.assertEqual(rep["inputs"][name]["path"], str(p))

    def test_lambda_cells_gap_is_reported_not_hidden(self):
        self.write_src("S.java", "package com.example;\npublic class S {\n}\n")
        units = [fn_unit("com.example.S", "lambda$create$0")]
        missions, index = self.mine(units, with_tests=False)
        report = cm.build_report(units, [], missions, cm.compose(missions, units, index),
                                 cm.link_oracles([], units, {"nodes": {}, "edges": []}), index)
        self.assertEqual(report["counts"]["lambda_cells"], 1)
        self.assertEqual(report["counts"]["lambda_cells_without_declaration"], 1)
        self.assertEqual(missions["com.example.S#lambda$create$0"]["tier"], "unknown")

    def test_http_verb_must_match_not_just_path(self):
        self.write_src("CategoryController.java", CATEGORY_CONTROLLER)
        self.write_tests("t.py", 'def test_x(api):\n    api.put("/api/categories")\n')
        missions, _ = self.mine([fn_unit("com.example.controller.CategoryController", "list")])
        self.assertEqual(missions["com.example.controller.CategoryController#list"]["tier"],
                         "confirmed_annotation")

    def test_downstream_service_does_not_inherit_test_tier(self):
        self.write_src("CategoryController.java", CATEGORY_CONTROLLER)
        self.write_src("CategoryService.java", """package com.example.service;
public class CategoryService {
    public void create() {
    }
}
""")
        self.write_tests("test_categories.py", PYTHON_TESTS_HTTP)
        missions, _ = self.mine([fn_unit("com.example.controller.CategoryController", "create"),
                                 fn_unit("com.example.service.CategoryService", "create")])
        self.assertEqual(missions["com.example.controller.CategoryController#create"]["tier"],
                         "confirmed_test")
        self.assertNotEqual(missions["com.example.service.CategoryService#create"]["tier"],
                            "confirmed_test")

    # ---- 三态：没证据就是 unknown

    def test_no_evidence_stays_unknown(self):
        self.write_src("S.java", """package com.example;
public class S {
    public void frobnicate() {
    }
}
""")
        missions, _ = self.mine([fn_unit("com.example.S", "frobnicate")], with_tests=False)
        m = missions["com.example.S#frobnicate"]
        self.assertEqual(m["tier"], "unknown")
        self.assertIsNone(m["statement"])
        self.assertEqual(m["evidence"], [])

    def test_unit_without_declaration_stays_unknown(self):
        self.write_src("S.java", "package com.example;\npublic class S {\n}\n")
        missions, _ = self.mine([fn_unit("com.example.S", "ghost"),
                                 {"unit_key": "service:api", "kind": "service", "layer": "service",
                                  "class": "", "symbol": "api", "file": ""}], with_tests=False)
        self.assertEqual(missions["com.example.S#ghost"]["tier"], "unknown")
        self.assertEqual(missions["service:api"]["tier"], "unknown")

    # ---- 缺陷 5：实体作用域 + 读写语义

    def _net(self) -> dict:
        return {
            "nodes": {
                "p.svc.PackageTypeService#create": svc_node({"sinks": ["p.entity.PackageType.setName"]}),
                "p.svc.CategoryService#create": svc_node({"sinks": ["p.entity.Category.setName"]}),
                "p.svc.ProductService#create": svc_node({"sinks": ["p.entity.Product.setName"]}),
                "p.svc.OrderService#normalizeAddedItems": svc_node(
                    {"source_fields": ["p.dto.AddedItemRequest.quantity"]}),
                "p.svc.OrderService#normalizePackageItems": svc_node(
                    {"source_fields": ["p.dto.PackageItemRequest.quantity"]}),
                "p.svc.PackageService#applyItems": svc_node({"sinks": ["p.entity.PackageItem.setQuantity"]}),
                "field:PackageType.name": field_node("PackageType", "name", "package_types"),
                "field:Category.name": field_node("Category", "name", "categories"),
                "field:Product.name": field_node("Product", "name", "products"),
                "field:PackageItem.quantity": field_node("PackageItem", "quantity", "package_items"),
            },
            "edges": [],
        }

    def _units(self) -> list[dict]:
        return [fn_unit(k.rsplit("#", 1)[0], k.rsplit("#", 1)[1]) for k in (
            "p.svc.PackageTypeService#create", "p.svc.CategoryService#create",
            "p.svc.ProductService#create", "p.svc.OrderService#normalizeAddedItems",
            "p.svc.OrderService#normalizePackageItems", "p.svc.PackageService#applyItems")]

    def test_field_scope_keeps_entity_no_cross_entity_edges(self):
        oracles = [oracle("ddl:package_types.name.not_null",
                          {"table": "package_types", "field": "name"},
                          {"type": "not_null", "field": "name"})]
        linked = cm.link_oracles(oracles, self._units(), self._net())
        self.assertEqual([e["unit_key"] for e in linked["implemented_by"]],
                         ["p.svc.PackageTypeService#create"])
        report = cm.build_report(self._units(), oracles, {}, {"classes": {}, "services": {}}, linked)
        self.assertEqual(report["counts"]["cross_entity_edges"], 0)
        self.assertEqual(report["counts"]["unverified_edges"], 0)

    def test_read_only_is_consumption_not_implementation(self):
        oracles = [oracle("anno:AddedItemRequest.quantity.min",
                          {"entity": "AddedItemRequest", "field": "quantity"},
                          {"type": "field_cmp", "field": "quantity", "op": ">=", "value": 1},
                          kind="annotation")]
        linked = cm.link_oracles(oracles, self._units(), self._net())
        self.assertEqual(linked["implemented_by"], [])
        self.assertEqual([e["unit_key"] for e in linked["consumes_field"]],
                         ["p.svc.OrderService#normalizeAddedItems"])
        self.assertEqual(linked["consumes_field"][0]["relation"], "consumes_field")
        self.assertEqual(linked["consumes_field"][0]["resolution"], "field_read")
        report = cm.build_report(self._units(), oracles, {}, {"classes": {}, "services": {}}, linked)
        self.assertEqual(report["counts"]["implemented_by_edges"], 0)
        self.assertEqual(report["counts"]["consumes_field_edges"], 1)
        self.assertEqual(report["counts"]["oracles_implemented"], 0)
        self.assertEqual(report["counts"]["oracles_consumed_only"], 1)
        self.assertIn("只读不算实现", report["semantics"]["consumes_field"])

    def test_unscoped_ambiguous_field_is_unlinked_not_name_matched(self):
        oracles = [oracle("doc:PRD:1.quantity", {"field": "quantity"},
                          {"type": "field_cmp", "field": "quantity", "op": ">", "value": 0},
                          kind="document")]
        linked = cm.link_oracles(oracles, self._units(), self._net())
        self.assertEqual(linked["implemented_by"], [])
        self.assertEqual(linked["consumes_field"], [])
        self.assertEqual(len(linked["unlinked_oracles"]), 1)
        self.assertIn("无实体作用域", linked["unlinked_oracles"][0]["reason"])

    def test_table_scoped_check_constraint_only_links_actual_writer(self):
        oracles = [oracle("ddl:package_items.chk_quantity", {"table": "package_items"},
                          {"type": "field_cmp", "field": "quantity", "op": ">", "value": 0})]
        linked = cm.link_oracles(oracles, self._units(), self._net())
        self.assertEqual([e["unit_key"] for e in linked["implemented_by"]],
                         ["p.svc.PackageService#applyItems"])
        self.assertNotIn("p.svc.OrderService#normalizeAddedItems",
                         [e["unit_key"] for e in linked["implemented_by"]])

    def test_table_write_propagates_one_hop_to_caller(self):
        net = {"nodes": {"p.repo.OrderRepository#save": {"type": "REPO_OP"},
                         "table:orders": {"type": "TABLE", "table": "orders"},
                         "field:BanquetOrder.status": field_node("BanquetOrder", "status", "orders")},
               "edges": [{"from": "p.repo.OrderRepository#save", "to": "table:orders", "type": "WRITES"},
                         {"from": "p.svc.OrderService#create", "to": "p.repo.OrderRepository#save",
                          "type": "CALLS"}]}
        units = [fn_unit("p.svc.OrderService", "create")]
        oracles = [oracle("ddl:orders.some_check", {"table": "orders"}, {"type": "not_null"})]
        linked = cm.link_oracles(oracles, units, net)
        self.assertEqual([e["unit_key"] for e in linked["implemented_by"]], ["p.svc.OrderService#create"])
        self.assertEqual(linked["implemented_by"][0]["resolution"], "table_write")

    # ---- 级联校验：可证明的执行点（不是「校验侧连不上」）

    ITEM_DTO = """package com.example.dto;
public record ItemRequest(
    @NotNull Long productId,
    @NotNull @Min(1) Integer quantity
) {
}
"""
    ORDER_DTO_CASCADE = """package com.example.dto;
public record CreateOrderRequest(
    @NotBlank String customerName,
    @Valid List<ItemRequest> items
) {
}
"""
    ORDER_DTO_NO_CASCADE = """package com.example.dto;
public record CreateOrderRequest(
    @NotBlank String customerName,
    List<ItemRequest> items
) {
}
"""
    ORDER_CONTROLLER = """package com.example.controller;
@RestController
@RequestMapping("/api/orders")
public class OrderController {
    @PostMapping
    public OrderResponse create(@Valid @RequestBody CreateOrderRequest request) {
        return orderService.create(request);
    }
}
"""

    def _link(self, units, oracles, net=None):
        missions, index = self.mine(units, with_tests=False)
        return cm.link_oracles(oracles, units, net or {"nodes": {}, "edges": []},
                               index["validation"], index["guards"]), index

    def _cascade_fixture(self, order_dto: str):
        self.write_src("ItemRequest.java", self.ITEM_DTO)
        self.write_src("CreateOrderRequest.java", order_dto)
        self.write_src("OrderController.java", self.ORDER_CONTROLLER)
        return [fn_unit("com.example.controller.OrderController", "create")]

    def test_nested_valid_cascade_proves_enforcement_with_ordered_chain(self):
        units = self._cascade_fixture(self.ORDER_DTO_CASCADE)
        oracles = [oracle("anno:ItemRequest.quantity.min",
                          {"entity": "ItemRequest", "field": "quantity"},
                          {"type": "field_cmp", "field": "quantity", "op": ">=", "value": 1},
                          kind="annotation")]
        oracles[0]["source"]["text"] = "@Min(1)"
        linked, _ = self._link(units, oracles)
        self.assertEqual(len(linked["implemented_by"]), 1)
        e = linked["implemented_by"][0]
        self.assertEqual(e["unit_key"], "com.example.controller.OrderController#create")
        self.assertEqual(e["resolution"], "bean_validation_cascade")
        self.assertEqual(len(e["chain"]), 3)
        self.assertRegex(e["chain"][0], r"OrderController\.java:\d+")
        self.assertRegex(e["chain"][1], r"CreateOrderRequest\.java:\d+")
        self.assertRegex(e["chain"][2], r"ItemRequest\.java:\d+")
        self.assertEqual(e["chain"][-1], e["constraint_reference"])
        self.assertIn("Min", e["constraint_annotations"])
        self.assertEqual(linked["unlinked_oracles"], [])
        self.assertIn("@Valid 入口", e["evidence"][0])
        self.assertIn("约束 @", e["evidence"][-1])
        self.assertEqual([x for x in e["evidence"] if x.startswith("级联")],
                         [f"级联 {r}" for r in e["chain"][1:]])

    def test_missing_cascade_annotation_is_not_linked(self):
        units = self._cascade_fixture(self.ORDER_DTO_NO_CASCADE)
        nested = oracle("anno:ItemRequest.quantity.min",
                        {"entity": "ItemRequest", "field": "quantity"},
                        {"type": "field_cmp", "field": "quantity", "op": ">=", "value": 1},
                        kind="annotation")
        nested["source"]["text"] = "@Min(1)"
        direct = oracle("anno:CreateOrderRequest.customerName.notblank",
                        {"entity": "CreateOrderRequest", "field": "customerName"},
                        {"type": "not_null", "field": "customerName"}, kind="annotation")
        direct["source"]["text"] = "@NotBlank"
        linked, _ = self._link(units, [nested, direct])
        self.assertEqual([e["oracle_id"] for e in linked["implemented_by"]],
                         ["anno:CreateOrderRequest.customerName.notblank"])
        self.assertEqual(len(linked["implemented_by"][0]["chain"]), 2)
        self.assertEqual([u["oracle_id"] for u in linked["unlinked_oracles"]],
                         ["anno:ItemRequest.quantity.min"])

    def test_constraint_annotation_must_be_the_one_the_oracle_claims(self):
        units = self._cascade_fixture(self.ORDER_DTO_CASCADE)
        wrong = oracle("anno:ItemRequest.quantity.notblank",
                       {"entity": "ItemRequest", "field": "quantity"},
                       {"type": "not_null", "field": "quantity"}, kind="annotation")
        wrong["source"]["text"] = "@NotBlank"
        linked, _ = self._link(units, [wrong])
        self.assertEqual(linked["implemented_by"], [])
        self.assertEqual(len(linked["unlinked_oracles"]), 1)

    def test_no_valid_entry_stays_unknown_not_violated(self):
        self.write_src("ItemRequest.java", self.ITEM_DTO)
        self.write_src("PlainController.java", """package com.example.controller;
@RestController
public class PlainController {
    @PostMapping
    public void create(@RequestBody ItemRequest request) {
    }
}
""")
        units = [fn_unit("com.example.controller.PlainController", "create")]
        o = oracle("anno:ItemRequest.quantity.min", {"entity": "ItemRequest", "field": "quantity"},
                   {"type": "field_cmp", "field": "quantity", "op": ">=", "value": 1},
                   kind="annotation")
        o["source"]["text"] = "@Min(1)"
        linked, index = self._link(units, [o])
        self.assertEqual(index["validation"], [])
        self.assertEqual(linked["implemented_by"], [])
        self.assertEqual(len(linked["unlinked_oracles"]), 1)
        self.assertNotIn("violated", json.dumps(linked["unlinked_oracles"]))

    def test_self_referential_dto_terminates(self):
        self.write_src("Node.java", """package com.example.dto;
public record Node(
    @NotNull String name,
    @Valid List<Node> children
) {
}
""")
        self.write_src("NodeController.java", """package com.example.controller;
@RestController
public class NodeController {
    @PostMapping
    public void create(@Valid @RequestBody Node request) {
    }
}
""")
        units = [fn_unit("com.example.controller.NodeController", "create")]
        o = oracle("anno:Node.name.notnull", {"entity": "Node", "field": "name"},
                   {"type": "not_null", "field": "name"}, kind="annotation")
        o["source"]["text"] = "@NotNull"
        linked, index = self._link(units, [o])
        self.assertEqual(len(linked["implemented_by"]), 1)
        self.assertLessEqual(len(linked["implemented_by"][0]["chain"]), 3)

    def test_verifier_catches_tampered_cascade_edge(self):
        units = self._cascade_fixture(self.ORDER_DTO_CASCADE)
        o = oracle("anno:ItemRequest.quantity.min", {"entity": "ItemRequest", "field": "quantity"},
                   {"type": "field_cmp", "field": "quantity", "op": ">=", "value": 1},
                   kind="annotation")
        o["source"]["text"] = "@Min(1)"
        linked, index = self._link(units, [o])
        good = cm.verify_links(linked["implemented_by"], [o], linked["access_index"],
                               index["validation"])
        self.assertEqual(good["unverified_edges"], 0)
        tampered = [dict(linked["implemented_by"][0],
                         unit_key="com.example.controller.OrderController#ghost")]
        bad = cm.verify_links(tampered, [o], linked["access_index"], index["validation"])
        self.assertEqual(bad["unverified_edges"], 1)

    # ---- 方法体 guard：参数级绑定，可证明执行 vs unknown

    GUARD_SERVICE = """package com.example.service;
public class OrderService {
    public void apply(BanquetOrder order, Long planTotalCent, Long finalPriceCent) {
        if (planTotalCent == null || planTotalCent < 0) {
            throw new BusinessException(ErrorCode.PRICE_INVALID, "金额不能小于 0");
        }
        long resolved = resolveFinalPrice(finalPriceCent);
        order.setPlanTotalCent(planTotalCent);
        order.setFinalPriceCent(resolved);
    }

    private long resolveFinalPrice(Long finalPriceCent) {
        if (finalPriceCent == null || finalPriceCent < 0) {
            throw new BusinessException(ErrorCode.PRICE_INVALID, "成交价不能小于 0");
        }
        return finalPriceCent;
    }
}
"""

    def _guard_net(self):
        return {"nodes": {
            "com.example.service.OrderService#apply": svc_node(
                {"sinks": ["com.example.entity.BanquetOrder.setPlanTotalCent",
                           "com.example.entity.BanquetOrder.setFinalPriceCent"]}),
            "field:BanquetOrder.planTotalCent": field_node("BanquetOrder", "planTotalCent", "orders"),
            "field:BanquetOrder.finalPriceCent": field_node("BanquetOrder", "finalPriceCent", "orders"),
        }, "edges": []}

    def _guard_units(self):
        return [fn_unit("com.example.service.OrderService", "apply"),
                fn_unit("com.example.service.OrderService", "resolveFinalPrice")]

    def _gte_oracle(self, oid, field, value=0):
        return oracle(oid, {"table": "orders"},
                      {"type": "field_gte_const", "left": field, "value": value})

    def test_guard_with_parameter_binding_proves_enforcement(self):
        self.write_src("OrderService.java", self.GUARD_SERVICE)
        o = self._gte_oracle("ddl:orders.chk_plan_total_cent", "plan_total_cent")
        linked, index = self._link(self._guard_units(), [o], self._guard_net())
        self.assertEqual(len(linked["implemented_by"]), 1)
        en = linked["implemented_by"][0]["enforcement"]
        self.assertEqual(en["kind"], "guard")
        self.assertEqual(en["param"], "planTotalCent")
        self.assertEqual((en["op"], en["value"]), (">=", 0))
        self.assertRegex(en["reference"], r"OrderService\.java:\d+")
        self.assertEqual(index["guards"]["com.example.service.OrderService#apply"][0]["param"],
                         "planTotalCent")

    def test_guard_in_other_method_does_not_leak_through_local_variable(self):
        self.write_src("OrderService.java", self.GUARD_SERVICE)
        o = self._gte_oracle("ddl:orders.chk_final_price_cent", "final_price_cent")
        linked, _ = self._link(self._guard_units(), [o], self._guard_net())
        self.assertEqual(len(linked["implemented_by"]), 1)
        self.assertEqual(linked["implemented_by"][0]["enforcement"], {"kind": "unknown"})

    def test_guard_without_throw_is_not_enforcement(self):
        self.write_src("S.java", """package com.example;
public class S {
    public void apply(Order order, Long priceCent) {
        if (priceCent == null || priceCent < 0) {
            log.warn("negative price");
        }
        order.setPriceCent(priceCent);
    }
}
""")
        net = {"nodes": {"p.S#apply": svc_node({"sinks": ["p.entity.Order.setPriceCent"]})}, "edges": []}
        o = oracle("ddl:t.chk", {"table": "t"}, {"type": "field_gte_const", "left": "price_cent", "value": 0})
        net["nodes"]["field:Order.priceCent"] = field_node("Order", "priceCent", "t")
        linked, _ = self._link([fn_unit("p.S", "apply")], [o], net)
        self.assertEqual(linked["implemented_by"][0]["enforcement"], {"kind": "unknown"})

    def test_guard_on_local_variable_is_not_parameter_bound(self):
        self.write_src("S.java", """package com.example;
public class S {
    public void apply(Order order, Long priceCent) {
        long local = priceCent;
        if (local < 0) {
            throw new BusinessException(ErrorCode.X, "bad");
        }
        order.setPriceCent(priceCent);
    }
}
""")
        net = {"nodes": {"p.S#apply": svc_node({"sinks": ["p.entity.Order.setPriceCent"]}),
                         "field:Order.priceCent": field_node("Order", "priceCent", "t")}, "edges": []}
        o = oracle("ddl:t.chk", {"table": "t"}, {"type": "field_gte_const", "left": "price_cent", "value": 0})
        linked, _ = self._link([fn_unit("p.S", "apply")], [o], net)
        self.assertEqual(linked["implemented_by"][0]["enforcement"], {"kind": "unknown"})

    def test_guard_bound_must_imply_oracle_bound_not_merely_overlap(self):
        self.write_src("OrderService.java", self.GUARD_SERVICE)
        loose_oracle = self._gte_oracle("ddl:orders.chk_loose", "plan_total_cent", -5)
        strict_oracle = self._gte_oracle("ddl:orders.chk_strict", "plan_total_cent", 1)
        linked, _ = self._link(self._guard_units(), [loose_oracle, strict_oracle], self._guard_net())
        by_id = {e["oracle_id"]: e["enforcement"]["kind"] for e in linked["implemented_by"]}
        self.assertEqual(by_id["ddl:orders.chk_loose"], "guard")
        self.assertEqual(by_id["ddl:orders.chk_strict"], "unknown")

    def test_string_literal_cannot_fake_a_guard_or_throw(self):
        self.write_src("S.java", """package com.example;
public class S {
    public void apply(Order order, Long priceCent) {
        String msg = "if (priceCent < 0) { throw new RuntimeException(); }";
        order.setPriceCent(priceCent);
    }
}
""")
        net = {"nodes": {"p.S#apply": svc_node({"sinks": ["p.entity.Order.setPriceCent"]}),
                         "field:Order.priceCent": field_node("Order", "priceCent", "t")}, "edges": []}
        o = oracle("ddl:t.chk", {"table": "t"}, {"type": "field_gte_const", "left": "price_cent", "value": 0})
        linked, index = self._link([fn_unit("p.S", "apply")], [o], net)
        self.assertEqual(index["guards"], {})
        self.assertEqual(linked["implemented_by"][0]["enforcement"], {"kind": "unknown"})

    def test_unknown_enforcement_is_never_reported_as_violated(self):
        self.write_src("OrderService.java", self.GUARD_SERVICE)
        o = self._gte_oracle("ddl:orders.chk_final_price_cent", "final_price_cent")
        linked, _ = self._link(self._guard_units(), [o], self._guard_net())
        blob = json.dumps({k: v for k, v in linked.items() if k != "access_index"},
                          ensure_ascii=False)
        self.assertNotIn("violated", blob)
        self.assertNotIn("not_implemented", blob)
        self.assertEqual(linked["implemented_by"][0]["enforcement"]["kind"], "unknown")

    def test_unrecognized_assertion_type_never_claims_guard_proof(self):
        self.write_src("OrderService.java", self.GUARD_SERVICE)
        units = self._guard_units()
        missions, index = self.mine(units, with_tests=False)
        self.assertTrue(index["guards"]["com.example.service.OrderService#apply"])
        for atype, extra in (("field_in_set", {"value": ["A", "B"]}),
                             ("unique", {}), ("default", {"value": 0}),
                             ("acceptance_scenario", {}), ("pattern", {"value": "^x"})):
            o = oracle(f"ddl:orders.{atype}", {"table": "orders"},
                       {"type": atype, "left": "plan_total_cent", **extra})
            linked, _ = self._link(units, [o], self._guard_net())
            self.assertEqual(len(linked["implemented_by"]), 1, atype)
            self.assertEqual(linked["implemented_by"][0]["enforcement"], {"kind": "unknown"}, atype)

    def test_not_blank_guard_satisfies_not_null_oracle(self):
        self.write_src("S.java", """package com.example;
public class S {
    public void apply(Order order, String name) {
        if (!StringUtils.hasText(name)) {
            throw new BusinessException(ErrorCode.X, "名称不能为空");
        }
        order.setName(name);
    }
}
""")
        net = {"nodes": {"com.example.S#apply": svc_node({"sinks": ["com.example.entity.Order.setName"]}),
                         "field:Order.name": field_node("Order", "name", "t")}, "edges": []}
        o = oracle("ddl:t.name.not_null", {"table": "t"}, {"type": "not_null", "field": "name"})
        linked, _ = self._link([fn_unit("com.example.S", "apply")], [o], net)
        self.assertEqual(linked["implemented_by"][0]["enforcement"]["kind"], "guard")

    # ---- 字段级判据绝不退化成表级扇出（实测踩过的坑）

    def test_field_bearing_oracle_never_fans_out_to_all_table_writers(self):
        self.write_src("S.java", "package com.example;\npublic class S {\n}\n")
        net = {"nodes": {
            "p.repo.ProductRepository#save": {"type": "REPO_OP"},
            "field:Product.name": field_node("Product", "name", "products"),
            "p.svc.ProductService#updateStatus": svc_node({"sinks": ["p.entity.Product.setIsActive"]}),
        }, "edges": [
            {"from": "p.repo.ProductRepository#save", "to": "table:products", "type": "WRITES"},
            {"from": "p.svc.ProductService#updateStatus", "to": "p.repo.ProductRepository#save",
             "type": "CALLS"}]}
        o = oracle("ddl:products.chk_price_cent", {"table": "products"},
                   {"type": "field_gte_const", "left": "price_cent", "value": 0})
        units = [fn_unit("p.svc.ProductService", "updateStatus")]
        linked, _ = self._link(units, [o], net)
        self.assertEqual(linked["implemented_by"], [])
        self.assertEqual([u["oracle_id"] for u in linked["unlinked_oracles"]],
                         ["ddl:products.chk_price_cent"])

    def test_unresolvable_field_scope_goes_unlinked_not_table_fanout(self):
        self.write_src("S.java", "package com.example;\npublic class S {\n}\n")
        net = {"nodes": {"p.repo.ProductRepository#save": {"type": "REPO_OP"}},
               "edges": [{"from": "p.repo.ProductRepository#save", "to": "table:products",
                          "type": "WRITES"}]}
        o = oracle("ddl:products.chk_price_cent", {"table": "products"},
                   {"type": "field_gte_const", "left": "price_cent", "value": 0})
        linked, _ = self._link([fn_unit("p.svc.ProductService", "create")], [o], net)
        self.assertEqual(linked["implemented_by"], [])
        self.assertEqual(len(linked["unlinked_oracles"]), 1)

    def test_oracle_fields_reads_every_field_operand_spelling(self):
        for assertion, want in (({"type": "field_gte_const", "left": "price_cent", "value": 0}, ["price_cent"]),
                                ({"type": "field_cmp", "field": "quantity", "op": ">", "value": 0}, ["quantity"]),
                                ({"type": "unique", "fields": ["order_no"]}, ["order_no"]),
                                ({"type": "references", "field": "a", "target_field": "b"}, ["a", "b"])):
            o = oracle("x", {"table": "t"}, assertion)
            self.assertEqual(cm._oracle_fields(o), want, assertion)

    def test_verifier_cross_entity_check_is_not_tautological(self):
        self.write_src("S.java", "package com.example;\npublic class S {\n}\n")
        net = self._net()
        o = oracle("ddl:package_types.name.not_null", {"table": "package_types", "field": "name"},
                   {"type": "not_null", "field": "name"})
        linked, _ = self._link(self._units(), [o], net)
        idx = linked["access_index"]
        edge = linked["implemented_by"][0]
        self.assertEqual(cm.verify_links([edge], [o], idx)["cross_entity_edges"], 0)
        tampered = dict(edge, evidence=[x.replace("PackageType.name", "Category.name")
                                        if x.startswith("作用域") else x for x in edge["evidence"]])
        bad = cm.verify_links([tampered], [o], idx)
        self.assertEqual(bad["cross_entity_edges"], 1)
        moved = dict(edge, unit_key="p.svc.CategoryService#create")
        self.assertEqual(cm.verify_links([moved], [o], idx)["unverified_edges"], 1)

    def test_implemented_by_does_not_claim_enforcement(self):
        self.write_src("S.java", "package com.example;\npublic class S {\n}\n")
        oracles = [oracle("ddl:package_types.name.not_null",
                          {"table": "package_types", "field": "name"},
                          {"type": "not_null", "field": "name"})]
        report = self._report(self._units(), oracles, self._net())
        sem = report["semantics"]["implemented_by"]
        self.assertIn("作用点", sem)
        self.assertIn("不证明约束被执行", " ".join(report["limitations"]))
        self.assertEqual(report["counts"]["implemented_by_edges"], 1)

    def test_verifier_is_not_vacuous(self):
        oracles = [oracle("ddl:package_types.name.not_null",
                          {"table": "package_types", "field": "name"},
                          {"type": "not_null", "field": "name"})]
        net = self._net()
        linked = cm.link_oracles(oracles, self._units(), net)
        idx = linked["access_index"]
        good = cm.verify_links(linked["implemented_by"], oracles, idx)
        self.assertEqual(good["cross_entity_edges"], 0)
        self.assertEqual(good["unverified_edges"], 0)
        tampered = [dict(linked["implemented_by"][0], unit_key="p.svc.CategoryService#create")]
        bad = cm.verify_links(tampered, oracles, idx)
        self.assertEqual(bad["unverified_edges"], 1)

    def test_oracle_without_any_dataflow_evidence_is_unlinked(self):
        oracles = [oracle("ddl:categories.created_at.not_null",
                          {"table": "categories", "field": "created_at"},
                          {"type": "not_null", "field": "created_at"})]
        linked = cm.link_oracles(oracles, self._units(), self._net())
        self.assertEqual(linked["implemented_by"], [])
        self.assertEqual(len(linked["unlinked_oracles"]), 1)

    # ---- 缺陷 6：分级之和 / 重复 key / 扇出集中 / 服务降级

    def _report(self, units, oracles, net):
        missions, index = self.mine(units, with_tests=False)
        composed = cm.compose(missions, units, index)
        linked = cm.link_oracles(oracles, units, net)
        return cm.build_report(units, oracles, missions, composed, linked)

    def test_tier_sum_equals_cells_and_duplicates_are_reported(self):
        self.write_src("S.java", """package com.example;
public class S {
    @Transactional
    public void a() {}
    public void b() {}
}
""")
        units = [fn_unit("com.example.S", "a"), fn_unit("com.example.S", "b"),
                 fn_unit("com.example.S", "b")]
        report = self._report(units, [], {"nodes": {}, "edges": []})
        c = report["counts"]
        self.assertEqual(c["cells"], 3)
        self.assertEqual(c["distinct_cells"], 2)
        self.assertEqual(c["duplicate_unit_keys"], 1)
        self.assertEqual(sum(c["missions_by_tier"].values()), c["distinct_cells"])
        self.assertEqual(sum(c["statements_by_tier"].values()), c["distinct_cells"])
        self.assertEqual(c["cells"], c["distinct_cells"] + c["duplicate_unit_keys"])

    def test_naming_hint_is_not_mission_coverage(self):
        self.write_src("S.java", """package com.example;
public class S {
    public void createThing() {}
    public void frobnicate() {}
}
""")
        units = [fn_unit("com.example.S", "createThing"), fn_unit("com.example.S", "frobnicate")]
        c = self._report(units, [], {"nodes": {}, "edges": []})["counts"]
        self.assertEqual(c["missions_by_tier"]["naming_hint"], 1)
        self.assertEqual(c["missions_by_tier"]["unknown"], 1)
        self.assertEqual(c["mission_coverage"], 0.0)
        self.assertEqual(c["stated_mission_coverage"], 0.0)

    def test_fanout_concentration_is_reported_not_hidden(self):
        self.write_src("S.java", "package com.example;\npublic class S {\n}\n")
        net = self._net()
        oracles = [oracle(f"ddl:package_types.name.c{i}", {"table": "package_types", "field": "name"},
                          {"type": "not_null", "field": "name"}) for i in range(4)]
        c = self._report(self._units(), oracles, net)["counts"]
        self.assertEqual(c["implemented_by_edges"], 4)
        self.assertEqual(c["cells_with_edges"], 1)
        self.assertEqual(c["max_fanout"], 1)
        self.assertEqual(c["top5_edge_share"], 1.0)
        self.assertEqual(c["top_cells_by_edges"][0]["unit_key"], "p.svc.PackageTypeService#create")

    def test_services_are_derived_package_grouping_with_stated_limitation(self):
        self.write_src("CategoryController.java", CATEGORY_CONTROLLER)
        units = [fn_unit("com.example.controller.CategoryController", "list")]
        report = self._report(units, [], {"nodes": {}, "edges": []})
        svc = report["composition"]["services"]["com.example.controller"]
        self.assertEqual(svc["tier"], "derived")
        self.assertEqual(svc["grouping_basis"], "java_package")
        self.assertIsNone(svc["mission"])
        self.assertIn("不是业务能力", svc["limitation"])
        self.assertEqual(report["composition"]["classes"]["com.example.controller.CategoryController"]["tier"],
                         "derived")

    # ---- 硬约束：通用引擎零项目字面量

    def test_engine_source_has_no_project_literals(self):
        text = Path(cm.__file__).read_text(encoding="utf-8")
        forbidden = (r"/Users/|/home/|C:\\\\", r"\brequests\.|\burllib\b",
                     r"127\.0\.0\.1|localhost:\d", r"\b(SELECT|INSERT\s+INTO|UPDATE\s+\w+\s+SET)\b",
                     r"docker\s+exec|\bpsql\s+-[a-zA-Z]")
        for pattern in forbidden:
            self.assertIsNone(re.search(pattern, text), pattern)


if __name__ == "__main__":
    unittest.main()