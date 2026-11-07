#!/bin/bash
# 分层审计流水线：函数层 -> 文件层 -> 服务层 -> 类层 -> 集权层 -> 清理
set -e
cd "$(dirname "$0")"
echo "=== 函数层（字节码声明 / JVM 直调 / 差分）==="
python3 fn_static.py
python3 fn_dynamic.py
python3 fn_diff.py
echo "=== 文件层（字节码声明 / 整类实例化实测 / 差分）==="
python3 file_static.py
python3 file_dynamic.py
python3 file_diff.py
echo "=== 服务层（静态声明 / 动态实测 / 差分）==="
python3 contract_static.py
python3 contract_dynamic.py
python3 contract_diff.py
echo "=== 类层（DDL+实体声明 / DTO反射契约+DB实况 / 数据库实测 / 差分）==="
python3 class_contract.py
python3 class_static.py
python3 class_dynamic.py
python3 class_diff.py
echo "=== 集权层（三方声明对账 / 实测回放 / 真三方对账 / 攻击链 / 差分）==="
python3 integration_static.py
python3 integration_dynamic.py
python3 integration_reconcile.py
python3 integration_chain.py
python3 integration_diff.py
echo "=== 清理 ==="
./cleanup.sh
echo "=== 证据归一化 + 根因聚合（统一 schema / 根因归并 / 覆盖度矩阵）==="
# 退出码 1 = 结构性违规（阻断）；2 = 契约缺口 category 装不下（不阻断，但要人决策）
normalize_rc=0
python3 normalize_findings.py || normalize_rc=$?
if [ "$normalize_rc" -eq 1 ]; then
  echo "  ↑ 归一化结构性违规，阻断流水线"
  exit 1
elif [ "$normalize_rc" -eq 2 ]; then
  echo "  ↑ 契约缺口：category 装不下，需要人工决策是否修订 layers.yaml（见 audit_report.md）"
fi
python3 aggregate_findings.py
echo "=== 集权层门禁（与基线比对，回归即失败退出码 1）==="
python3 integration_gate.py
