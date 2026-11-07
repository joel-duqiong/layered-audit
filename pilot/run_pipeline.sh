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
echo "=== 负载实测（端到端延迟曲线 + 分页 total 开销）==="
python3 perf_load.py --scales "${PERF_SCALES:-0,2000,10000,50000}" --rounds "${PERF_ROUNDS:-10}"
echo "=== 清理 ==="
./cleanup.sh
echo "=== 函数层 numeric 性质探针（property_based_test）==="
python3 numeric_props.py
echo "=== 服务层并发与重复写探针（concurrency_test / chaos_experiment）==="
python3 concurrency_props.py
echo "=== ORM 查询放大探针（N+1，第二实例开 Hibernate 统计）==="
python3 orm_query_probe.py
echo "=== 配置失败模式探针（克隆实例改配置观察行为）==="
python3 config_probe.py
echo "=== 变异测试（改坏被测代码，看 oracle 抓不抓得住）==="
python3 mutation_test.py
echo "=== 切面生效性探针（静态声明 vs 运行时事务拦截器，差分出单）==="
python3 transaction_probe.py
echo "=== 业务不变式探针（声明式不变量 → 造场景 → 判定）==="
python3 business_probe.py
echo "=== Java/Spring 特性扫描（切面/DI/RPC/动态配置/JPA）==="
python3 java_feature_scan.py
echo "=== 引擎自检（通用引擎不许写回项目专属的东西 + 换绑定可跑）==="
python3 engine/check_engine_purity.py
./test_portability.sh
echo "=== Agent 群构建 + 变更影响分析（改一处只动受影响的部分）==="
python3 engine/build_agent_graph.py
python3 engine/change_impact.py
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

echo "=== 残留守卫（探针容器 / ai_ 行 / 非种子上传，必须全 0）==="
python3 probe_residue_check.py
