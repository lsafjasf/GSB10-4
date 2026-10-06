# coop_timeout

任务超时的协作式中断（Python 3，仅标准库，时间由测试注入）。

- 检查点规则 / 清理结果 / 延迟分布 / 边界用例：见 [CHECKPOINTS.md](CHECKPOINTS.md)
- 自测：`python3 -m unittest coop_timeout.tests -v`
- 场景矩阵 + 对拍：`python3 -m coop_timeout.demo`
- 10 万次超时基准：`python3 -m coop_timeout.bench`
