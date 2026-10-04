#!/usr/bin/env bash
set -euo pipefail
echo "==== 1) 单元自测 ===="
python3 -m unittest test_watermark_pool -v
echo "==== 2) 对拍 300 随机种子 ===="
python3 differential.py 300
echo "==== 3) 连接级状态序列 ===="
python3 trace_states.py
echo "==== 4) 100 万获取/释放调度压测 ===="
python3 sim.py 1000000 42
