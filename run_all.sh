#!/usr/bin/env bash
# 一键运行：演示展开 -> 单元测试 -> 边界用例 -> 5 万段基准
set -euo pipefail
cd "$(dirname "$0")"

python3 demo.py
echo
python3 tests.py
echo
python3 edge_cases.py
echo
python3 bench_50k.py
