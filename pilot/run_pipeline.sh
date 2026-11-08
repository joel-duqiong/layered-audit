#!/bin/bash
# 分层审计流水线：函数层 -> 文件层 -> 服务层 -> 类层 -> 集权层 -> 负载实测 -> 收口 -> 门禁
set -e
cd "$(dirname "$0")"

# 并发保护：各层脚本共用固定工作目录（/tmp/jiuyan_fn、/tmp/jiuyan_file），
# 两个流水线同时跑会互相覆盖 class 文件，表现为 javap 随机失败。
LOCK_DIR=/tmp/layered_audit.lock
if ! mkdir "$LOCK_DIR" 2>/dev/null; then
  echo "已有流水线在跑（锁目录 $LOCK_DIR 存在）。"
  echo "如确认没有进程在跑，手动 rm -rf $LOCK_DIR 后重试。"
  exit 2
fi
trap 'rmdir "$LOCK_DIR" 2>/dev/null || true' EXIT
# 增量调度：没受本次改动影响的层整层跳过（产物文件原样保留 = 复用上次的）。
# 退出码：0=跑 1=跳过 2=判断不出来（按「跑」处理，宁可多跑不可漏跑）
run_layer () {
  local layer="$1"; shift
  python3 engine/incremental_plan.py --should-run "$layer" >/dev/null 2>&1
  local rc=$?
  if [ "$rc" -eq 1 ]; then
    echo "  [增量] 跳过 $layer（未受本次改动影响，复用上次产物）"
  else
    "$@"
  fi
}


# 变更影响要先算出来 —— 调度闸门要用它（它依赖上一轮的单元索引，首次运行没有就全跑）
python3 engine/change_impact.py
python3 engine/incremental_plan.py --write

echo "=== 函数层（字节码声明 / JVM 直调 / 差分）==="
run_layer function python3 fn_static.py
# 语义地图：把「每个单元该干什么」编译成可执行断言（复用函数层执行器，不新增语义）
python3 semantic_probe.py
# 增量：只重跑指纹变了的用例，其余复用分片；缺分片 -> 退出码 2（没查完，不是绿）
run_layer function python3 fn_dynamic.py --incremental --extra-cases semantic_cases.json
run_layer function python3 fn_diff.py
echo "=== 文件层（字节码声明 / 整类实例化实测 / 差分）==="
run_layer file python3 file_static.py
run_layer file python3 file_dynamic.py
run_layer file python3 file_diff.py
echo "=== 服务层（静态声明 / 动态实测 / 差分）==="
run_layer service python3 contract_static.py
run_layer service python3 contract_probe.py    # 通用版：场景配置在 service_scenarios.json
run_layer service python3 contract_diff.py
echo "=== 类层（DDL+实体声明 / DTO反射契约+DB实况 / 数据库实测 / 差分）==="
run_layer class python3 class_contract.py
run_layer class python3 class_static.py
run_layer class python3 class_dynamic.py
run_layer class python3 class_diff.py
echo "=== 集权层（三方声明对账 / 实测回放 / 真三方对账 / 攻击链 / 差分）==="
run_layer integration python3 integration_static.py
run_layer integration python3 integration_dynamic.py
run_layer integration python3 integration_reconcile.py
run_layer integration python3 integration_chain.py
run_layer integration python3 integration_diff.py
echo "=== 清理 ==="
./cleanup.sh
echo "=== 引擎自检（通用引擎不许写回项目专属的东西 + 换绑定可跑 + 增量闸门会跳）==="
python3 engine/check_engine_purity.py
python3 engine/incremental_plan.py --selftest
./test_portability.sh
echo "=== 探针作业调度（按 max_agents 闸门分轮 admit，产物并集合并）==="
python3 engine/scheduler.py
echo "=== Agent 群构建 + 变更影响分析（改一处只动受影响的部分）==="
python3 engine/build_agent_graph.py
python3 engine/agent_coverage.py
echo "=== 证据归一化 + 根因聚合（统一 schema / 根因归并 / 覆盖度矩阵）==="
# 退出码 1 = 结构性违规（阻断）；2 = 契约缺口 category 装不下（不阻断，但要人决策）
normalize_rc=0
python3 engine/normalize_findings.py || normalize_rc=$?
if [ "$normalize_rc" -eq 1 ]; then
  echo "  ↑ 归一化结构性违规，阻断流水线"
  exit 1
elif [ "$normalize_rc" -eq 2 ]; then
  echo "  ↑ 契约缺口：category 装不下，需要人工决策是否修订 layers.yaml（见 audit_report.md）"
fi
python3 engine/aggregate_findings.py
echo "=== 特性覆盖矩阵（特性 × 载体）==="
python3 engine/feature_coverage.py
echo "=== 集权层门禁（与基线比对，回归即失败退出码 1）==="
python3 integration_gate.py

echo "=== 涌现 FDR 门禁（第三态：0 绿 / 1 红 / 2 没查完）==="
# 本目标没有注入目录 -> 涌现这一维**没查完**。这不等于「没问题」，
# 所以单独报出来（覆盖度矩阵里 integration.emergent_masking 也是零覆盖）。
# 门禁的第三态语义见 layers.yaml 决议 D8：没查完必须与查了没问题区分开。
emergence_rc=0
python3 engine/emergence_gate.py || emergence_rc=$?
if [ "$emergence_rc" -eq 1 ]; then
  echo "  ↑ 涌现门禁 RED：有注入的涌现链没被检出或没被正确归属"
  exit 1
elif [ "$emergence_rc" -eq 2 ]; then
  echo "  ↑ 涌现维度 INCOMPLETE：本目标没有注入目录，这一维尚未检查（不是绿灯）"
  echo "    涌现能力在 demo-shop 上是跑通的（demos/shop/run_pipeline.sh）"
fi

echo "=== 残留守卫（探针容器 / ai_ 行 / 非种子上传，必须全 0）==="
# 先自检：守卫必须能证明自己抓得到（谓词写错时「0 行」是假的）
python3 probe_residue_check.py --selftest
python3 probe_residue_check.py
