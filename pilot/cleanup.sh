#!/bin/bash
# 清理探针产生的临时数据（只删 ai_probe_ 前缀 + 未被引用的上传文件）
set -e
HERE="$(cd "$(dirname "$0")" && pwd)"
cd /Users/june/tests/auto-api
.venv/bin/python scripts/cleanup_test_data.py --prefix ai_probe_ 2>&1 | tail -2
echo -n "残留 ai_probe_ 订单数: "
docker exec jiuyan-postgres psql -U jiuyan -d jiuyan -tAc \
  "SELECT count(*) FROM orders WHERE customer_name LIKE 'ai_probe_%' OR customer_name LIKE 'AI_AUDIT%';"
python3 "$HERE/cleanup_uploads.py"
# 函数层的上传沙箱（/tmp 下的独立目录，与被测系统无关）
rm -rf /tmp/jiuyan_fn/uploads
# 文件层的上传沙箱与静态根目录试验目录
rm -rf /tmp/jiuyan_file/uploads /tmp/jiuyan_file/loc-test
