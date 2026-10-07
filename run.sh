#!/usr/bin/env bash
# 一键运行：自测 + 全部基准（结果写入 RESULTS-raw/）
set -e
cd "$(dirname "$0")"
mkdir -p RESULTS-raw
echo "===== 1/4 自测 ====="
node test/selftest.js | tee RESULTS-raw/selftest.txt
echo "===== 2/4 100万系数基准 ====="
node --expose-gc bench/bench_1m.js | tee RESULTS-raw/bench_1m.txt
echo "===== 3/4 缩放/交叉点基准（约6分钟） ====="
node --expose-gc bench/bench_scaling.js | tee RESULTS-raw/bench_scaling.txt
echo "===== 4/4 多点求值基准（约6分钟） ====="
node --expose-gc bench/bench_eval.js | tee RESULTS-raw/bench_eval.txt
echo "全部完成，原始输出在 RESULTS-raw/"
