#!/bin/bash
# 清理探针与回归套件产生的临时数据（只删 ai_ 前缀 + 未被引用的上传文件）
#
# 前缀用 ai_ 而不是 ai_probe_：harness 回归套件造数用的是运行级 tag
# ai_<时间戳>_<uuid6>（tests/helpers.py 的 RUN_TAG），只扫 ai_probe_ 会漏掉它们。
# ai_ 是这两者的超集，且与种子数据无交集（种子名是中文菜名）。
set -e
HERE="$(cd "$(dirname "$0")" && pwd)"
cd /Users/june/tests/auto-api
.venv/bin/python scripts/cleanup_test_data.py --prefix ai_ 2>&1 | tail -2
echo -n "残留 ai_ 订单数: "
docker exec jiuyan-postgres psql -U jiuyan -d jiuyan -tAc \
  "SELECT count(*) FROM orders WHERE customer_name LIKE 'ai\_%' OR customer_name LIKE 'AI_AUDIT%';"
python3 "$HERE/cleanup_uploads.py"
# 函数层的上传沙箱（/tmp 下的独立目录，与被测系统无关）
rm -rf /tmp/jiuyan_fn/uploads
# 文件层的上传沙箱与静态根目录试验目录
rm -rf /tmp/jiuyan_file/uploads /tmp/jiuyan_file/loc-test
