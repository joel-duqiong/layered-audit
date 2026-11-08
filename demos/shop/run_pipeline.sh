#!/bin/bash
# demo-shop 审计流水线：类层 -> 涌现（关系网 / 候选链 / 真跑裁决 / FDR 门禁）-> 收口 -> 残留守卫
#
# 与 jiuyan 的 pilot/run_pipeline.sh 是两套：
#   jiuyan —— 被测系统本体，测「有没有缺陷」
#   demo   —— 演示目标，测「这套系统本身能不能用」（含 jiuyan 没有的 Java/Spring 特性与跨仓库）
#
# 退出码：
#   0  = 已配置的检查项全绿
#   1  = 有检查项判红（涌现门禁红 = 演示目标里确实有「根因无人 own」的缺陷，这是预期状态）
#   2  = 没查完（输入缺失）
set -e
cd "$(dirname "$0")"
DEMO=$(pwd)
PILOT=$(cd "$DEMO/../.." && pwd)/pilot

export LAYERED_AUDIT_TARGET="$DEMO/target.json"

# 并发保护：各脚本共用固定工作目录，两个流水线同时跑会互相覆盖 class 文件。
LOCK_DIR=/tmp/layered_audit_demo.lock
if ! mkdir "$LOCK_DIR" 2>/dev/null; then
  echo "已有 demo 流水线在跑（锁目录 $LOCK_DIR 存在）。"
  echo "如确认没有进程在跑，手动 rm -rf $LOCK_DIR 后重试。"
  exit 2
fi
trap 'rmdir "$LOCK_DIR" 2>/dev/null || true' EXIT

cd "$PILOT"

echo "=== 类层（DDL+实体声明 / DTO反射契约+DB实况 / 类形状 / 差分）==="
python3 class_contract.py
python3 class_static.py
python3 class_dynamic.py
python3 class_diff.py

echo "=== 文件层（字节码声明 / 整类实例化实测 / 差分）==="
python3 file_static.py
python3 file_dynamic.py
python3 engine/file_diff.py

echo "=== 业务不变式（服务层：造场景 → 读行 → 判定 → 开单）==="
python3 "$DEMO/business_probe.py"

echo "=== 涌现：可执行依赖图（关系网 + 污点 + 入口绑定 + 不变量锚）==="
python3 engine/relation_net.py

echo "=== 涌现：候选链生成（固定种子 + 八条剪枝硬约束 + 召回比对）==="
python3 engine/emergence_candidates.py \
  --net "$DEMO/artifacts/relation_net.json" \
  --invariants "$DEMO/invariants.json" \
  --catalog "$DEMO/emergence_injections.json" \
  --out "$DEMO/artifacts/emergence_candidates.json" \
  --seed 20261107

echo "=== 涌现：候选链真跑裁决（真容器 + 真库 + 零残留自证）==="
python3 "$DEMO/emergence_candidate_run.py"

echo "=== 证据归一化 + 根因聚合 ==="
normalize_rc=0
python3 engine/normalize_findings.py || normalize_rc=$?
if [ "$normalize_rc" -eq 1 ]; then
  echo "  ↑ 归一化结构性违规，阻断流水线"
  exit 1
elif [ "$normalize_rc" -eq 2 ]; then
  echo "  ↑ 契约缺口：category 装不下，需要人工决策是否修订 layers.yaml"
fi
python3 engine/aggregate_findings.py

echo "=== 涌现：FDR 门禁（分母/分子/哈希漂移/分类别 FDR/漏检清单）==="
gate_rc=0
python3 engine/emergence_gate.py || gate_rc=$?
if [ "$gate_rc" -eq 1 ]; then
  echo "  ↑ 涌现门禁 RED：演示目标里存在「根因只被引用、无人 own 开单」的缺陷"
  echo "    这是预期状态 —— 门禁判红的意义就是把这种漏归属逼出来"
elif [ "$gate_rc" -eq 2 ]; then
  echo "  ↑ 涌现门禁 INCOMPLETE：输入缺失（没查完，不是没问题）"
  exit 2
fi

echo "=== 残留守卫（探针容器 / ai_ 行 / 非种子上传，必须全 0）==="
python3 probe_residue_check.py --selftest
python3 probe_residue_check.py

if [ "$gate_rc" -eq 1 ]; then
  exit 1
fi
echo "demo 流水线完成：已配置的检查项全绿。"
