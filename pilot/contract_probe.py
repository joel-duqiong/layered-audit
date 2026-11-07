"""服务层探针（通用）：按 OpenAPI schema 生成请求，跑四种场景，记录实测行为。

**通用骨架**在这里；**项目特定的东西**（造什么临时实体、哪些字段算探针字段、
{id} 从哪来）在项目侧的 service_scenarios.json 里。换项目只换那个文件。

四种场景：
  valid             合法请求（带一个伪造 Authorization，顺带验证鉴权是否存在）
  missing_required  缺一个必填字段
  wrong_type        必填字段类型错
  not_found         资源不存在（{id} 换成不存在的 id）

所有写操作只作用在探针前缀的临时实体上，跑完由清理脚本删掉，不碰种子数据。
"""
from __future__ import annotations

import copy
import datetime
import json
import os
import time
import uuid
from pathlib import Path
from typing import Any

import re

import requests

import project

PLACEHOLDER_RE = re.compile(r"\{\{(\w+)\}\}")

D = str(project.artifact_dir())
BASE = os.environ.get("JIUYAN_BASE_URL", "http://127.0.0.1:8081")
S = requests.Session()


def load(name: str) -> Any:
    return json.loads((Path(D) / name).read_text(encoding="utf-8"))


def resolve(spec: dict, schemas: dict) -> dict:
    if isinstance(spec, dict) and "$ref" in spec:
        return schemas.get(spec["$ref"].split("/")[-1], {})
    return spec or {}


def gen(spec: dict, schemas: dict, ctx: dict[str, Any], depth: int = 0,
        prefix: str = "ai_probe_") -> Any:
    """按 OpenAPI schema 生成一个「合法」的值。"""
    if depth > 6:
        return None
    s = resolve(spec, schemas)
    if "enum" in s:
        return s["enum"][0]
    t = s.get("type")
    if t == "object" or "properties" in s:
        out = {}
        for k in (s.get("required") or s.get("properties", {}).keys()):
            out[k] = gen(s["properties"][k], schemas, ctx, depth + 1, prefix)
        return out
    if t == "array":
        return []
    if t == "boolean":
        return True
    if t in ("integer", "number"):
        return ctx.get("id", 1)
    if t == "string":
        if s.get("format") == "date-time":
            return (datetime.datetime.now(datetime.timezone.utc)
                    + datetime.timedelta(days=30)).isoformat()
        return prefix + "str"     # **必须带前缀**：否则清理脚本删不掉，还会污染种子基线
    return None


def build_body(schema_name: str | None, schemas: dict, ctx: dict[str, Any],
               probe_fields: list[str], prefix: str) -> dict | None:
    if not schema_name:
        return None
    s = schemas.get(schema_name, {})
    out: dict[str, Any] = {}
    for k in (s.get("required") or []):
        out[k] = gen(s["properties"][k], schemas, ctx, prefix=prefix)
    for k, v in list(out.items()):
        if k in ctx and isinstance(v, (int, float)):
            out[k] = ctx[k]
    for k in probe_fields:
        if k in out:
            out[k] = prefix + "probe"
    return out


def stamp_prefix(body: dict | None, cfg: dict, prefix: str) -> dict | None:
    """所有场景都强制「前缀字段」带前缀。

    为什么必须每场景都盖：wrong_type 场景会把垃圾值写进某个字段 —— 如果那个字段
    正好是实体的名字字段，造出来的行就**不带前缀**，清理脚本删不掉，
    还会污染种子基线（亲眼见过：products 里留下 'probe-str'）。
    """
    if not isinstance(body, dict):
        return body
    field = cfg.get("prefix_field")
    if field and field in body:
        body[field] = prefix + "probe"
    return body


def fill_template(value: Any, ctx: dict[str, Any], prefix: str) -> Any:
    """把 '{{name}}' 占位符换成上下文里的值（支持字符串里拼接多个占位符）。"""
    if isinstance(value, str):
        return PLACEHOLDER_RE.sub(
            lambda m: str(_resolve_placeholder(m.group(1), ctx, prefix)), value)
    if isinstance(value, dict):
        return {k: fill_template(v, ctx, prefix) for k, v in value.items()}
    if isinstance(value, list):
        return [fill_template(v, ctx, prefix) for v in value]
    return value


def _resolve_placeholder(key: str, ctx: dict[str, Any], prefix: str) -> Any:
    if key == "prefix":
        return prefix
    if key == "rand6":
        return uuid.uuid4().hex[:6]
    if key == "plus30d":
        return (datetime.datetime.now(datetime.timezone.utc)
                + datetime.timedelta(days=30)).isoformat()
    return ctx.get(key)


def fixtures(cfg: dict) -> dict[str, Any]:
    """按配置创建临时实体，供 PUT/PATCH/DELETE 场景用。"""
    ctx: dict[str, Any] = {}
    prefix = cfg.get("probe_prefix", "ai_probe_")
    for spec in cfg.get("fixtures", []):
        body = fill_template(spec.get("body", {}), ctx, prefix)
        r = S.post(BASE + spec["path"], json=body, timeout=15)
        try:
            payload = r.json()
        except ValueError:
            payload = {}
        data = payload.get("data") or {}
        ctx[spec["name"]] = data.get(spec.get("id_field", "id"))
    ctx["_ids"] = {k: v for k, v in ctx.items() if k.endswith("Id")}
    return ctx


def call(method: str, path: str, body: Any = None, headers: dict | None = None) -> dict:
    h = dict(headers or {})
    if body is not None:
        h["Content-Type"] = "application/json"
    t0 = time.perf_counter()
    r = S.request(method, BASE + path, json=body, headers=h, timeout=20)
    ms = (time.perf_counter() - t0) * 1000
    try:
        j = r.json()
    except ValueError:
        j = None
    return {"status": r.status_code, "latency_ms": round(ms, 1),
            "content_type": r.headers.get("Content-Type", ""), "json": j}


def brief(obs: dict, envelope: list[str]) -> dict:
    j = obs["json"]
    if isinstance(j, dict):
        obs["body_keys"] = sorted(j.keys())
        obs["code"] = j.get("code")
        data = j.get("data")
        obs["data_keys"] = sorted(data.keys()) if isinstance(data, dict) else None
        obs["envelope_ok"] = all(k in j for k in envelope)
        obs["data_type"] = type(data).__name__
    else:
        obs["body_keys"] = None
        obs["envelope_ok"] = False
    return obs


def upload_scenario(op: dict, path: str, envelope: list[str]) -> list[dict]:
    """上传类 endpoint：合法 multipart + 缺失 part。"""
    png = b"\x89PNG\r\n\x1a\n" + b"\x00" * 64
    out: list[dict] = []
    t0 = time.perf_counter()
    r = S.post(BASE + path, files={"file": ("probe.png", png, "image/png")}, timeout=20)
    ms = (time.perf_counter() - t0) * 1000
    try:
        j = r.json()
    except ValueError:
        j = None
    o = brief({"status": r.status_code, "latency_ms": round(ms, 1),
               "content_type": r.headers.get("Content-Type", ""), "json": j}, envelope)
    o.update({"key": op["key"], "scenario": "valid_multipart", "request": path})
    if j and isinstance(j.get("data"), dict) and j["data"].get("imageUrl"):
        o["_created_file"] = j["data"]["imageUrl"]
    out.append(o)
    r2 = S.post(BASE + path, json={"file": "x"}, timeout=20)
    try:
        j2 = r2.json()
    except ValueError:
        j2 = None
    o2 = brief({"status": r2.status_code, "latency_ms": 0, "content_type": "", "json": j2},
                envelope)
    o2.update({"key": op["key"], "scenario": "missing_multipart_part", "request": path})
    out.append(o2)
    return out


def main() -> None:
    cfg_path = project.path("service_scenarios", "service_scenarios.json")
    cfg = json.loads((project.project_dir() / cfg_path).read_text(encoding="utf-8"))
    prefix = cfg.get("probe_prefix", "ai_probe_")
    envelope = cfg.get("envelope", ["success", "code", "message"])
    probe_fields = cfg.get("probe_fields", [])
    id_sources = cfg.get("id_sources", {})

    decl = load("declaration.json")
    schemas = load("openapi.json")["components"]["schemas"]
    ctx = fixtures(cfg)
    print("临时实体:", ctx.get("_ids"))

    obs: list[dict] = []
    for op in decl["operations"]:
        method, path = op["method"], op["path"]
        body = build_body(op.get("request_schema"), schemas, ctx, probe_fields, prefix)

        p = path
        if "{id}" in p:
            res = next((r for r in id_sources if "/" + r in p), None)
            p = p.replace("{id}", str(ctx.get(id_sources.get(res, ""), 1)))
        if "/uploads/" in p:
            obs.extend(upload_scenario(op, p, envelope))
            continue

        body = stamp_prefix(body, cfg, prefix)
        r = brief(call(method, p, body, {"Authorization": "Bearer not-a-real-token"}), envelope)
        r.update({"key": op["key"], "scenario": "valid", "request": p})
        obs.append(r)

        if body:
            k = op["request_required"][0] if op["request_required"] else None
            if k:
                b2 = stamp_prefix(copy.deepcopy(body), cfg, prefix)
                b2.pop(k, None)
                r = brief(call(method, p, b2), envelope)
                r.update({"key": op["key"], "scenario": "missing_required:" + k, "request": p})
                obs.append(r)
                b3 = stamp_prefix(copy.deepcopy(body), cfg, prefix)
                # 类型探针：**绝不能破坏前缀字段** —— 破坏了，造出的行就清理不掉，
                # 还会污染种子基线（造出过叫 'not-a-number' 的分类）。
                prefix_field = cfg.get("prefix_field")
                candidates = [cfg.get("type_probe_field")] if cfg.get("type_probe_field") else []
                candidates += [f for f in op["request_required"] if f != prefix_field]
                probe_field = next((f for f in candidates if f and f in b3), None)
                if probe_field is None:
                    continue      # 没有可安全破坏的字段 -> 跳过这个场景（不留垃圾）
                k = probe_field
                cur = b3.get(k)
                if isinstance(cur, bool):
                    b3[k] = "not-a-boolean"
                elif isinstance(cur, (int, float)):
                    b3[k] = "not-a-number"
                elif isinstance(cur, str):
                    b3[k] = 12345
                else:
                    b3[k] = "not-a-valid-type"
                r = brief(call(method, p, b3), envelope)
                r.update({"key": op["key"], "scenario": "wrong_type:" + k, "request": p})
                obs.append(r)

        if "{id}" in path:
            p404 = path.replace("{id}", "99999999")
            r = brief(call(method, p404, body), envelope)
            r.update({"key": op["key"], "scenario": "not_found", "request": p404})
            obs.append(r)

    out = Path(D) / "observation.json"
    out.write_text(json.dumps({"ctx": ctx, "observations": obs}, ensure_ascii=False,
                              indent=2), encoding="utf-8")
    from collections import Counter
    print(f"实测 {len(obs)} 次 -> observation.json")
    print("  场景分布:", dict(Counter(o["scenario"].split(":")[0] for o in obs)))
    print("  观察到的状态码:", dict(Counter(o["status"] for o in obs)))


if __name__ == "__main__":
    main()
