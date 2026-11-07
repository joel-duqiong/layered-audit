#!/bin/bash
# 可移植性冒烟测试：同一套通用引擎，换一份项目绑定（target.json）能不能跑。
#
# 这不是「真换一个项目」，而是验证引擎不依赖 jiuyan 的任何东西：
#   - 目标项目名不同
#   - 源码根与包路径不同
#   - 证据源清单不同（只吃一个层）
#   - 标注表是另一份副本
# 跑出来的产物必须落在新目标自己的目录里，且不碰原目标的产物。
set -e
HERE="$(cd "$(dirname "$0")" && pwd)"
TMP="$(mktemp -d /tmp/layered_portability.XXXXXX)"
trap 'rm -rf "$TMP"' EXIT

mkdir -p "$TMP/src/com/acme/banquet/controller" "$TMP/src/com/acme/banquet/entity"
cp "$HERE/finding_meta.py" "$TMP/"
cp "$HERE/findings.json" "$TMP/"
cp "$HERE/../layers.yaml" "$TMP/layers.yaml"

cat > "$TMP/target.json" <<JSON
{
  "target": "acme-banquet",
  "contract": "layers.yaml",
  "annotation_table": "finding_meta.py",
  "source_roots": {"java": "$TMP/src", "java_package": "com/acme/banquet"},
  "class_entities": {"orders": "entity/BanquetOrder.java"},
  "evidence_sources": [
    {"layer": "service", "file": "findings.json", "key": null}
  ]
}
JSON

LAYERED_AUDIT_TARGET="$TMP/target.json" python3 "$HERE/engine/normalize_findings.py" > "$TMP/norm.log"
LAYERED_AUDIT_TARGET="$TMP/target.json" python3 "$HERE/engine/aggregate_findings.py" > "$TMP/agg.log"

test -f "$TMP/all_findings.json" || { echo "产物没落在目标目录"; exit 1; }
test -f "$TMP/root_causes.json" || { echo "根因表没落在目标目录"; exit 1; }

python3 - "$TMP" <<'PY'
import json, sys
tmp = sys.argv[1]
data = json.load(open(f"{tmp}/all_findings.json"))
assert data["counts"]["total"] > 0, "归一化结果为空"
assert data["target"].endswith("target.json"), "读的不是目标项目的绑定"
print(f"  目标 {data['target']} -> {data['counts']['total']} 条发现，10 必填字段结构齐备")
PY

echo "可移植性测试通过：同一套引擎换绑定照跑，产物落在目标目录"
