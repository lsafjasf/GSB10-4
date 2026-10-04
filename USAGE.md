# config_rewrite — 字节保留式配置原地重写（Python 3，仅标准库）

## 运行命令

```sh
# 全部自测（25 个：字节差异、幂等、原子性、断电、只读、重复键、续行…）
python3 test_config_rewrite.py -v

# 基准（1MB / 16MB 的耗时与内存峰值）
python3 bench_config_rewrite.py

# 命令行单值修改：FILE SECTION KEY VALUE（顶层键 SECTION 用 -）
python3 config_rewrite.py app.ini server host new.example
python3 config_rewrite.py app.ini - topkey value
```

库调用：`config_rewrite.rewrite_value(path, key, value, section=None)`，
返回变更报告 dict；出错抛 `ConfigError` 子类且文件不动。

## 格式约定与处理策略

- 行类型：空行、注释（首个非空白为 `#`/`;`）、`[section]`、`key = value`（也支持 `:`）。
- 键大小写敏感，文件中原始大小写、缩进、分隔符两侧空白一律原样保留。
- 键后注释：空白后跟 `#`/`;`（或值本身以其开头）视为注释，改值时注释及前置空白原样保留；
  值中间无空白分隔的 `#`（如 `a#b`）属于值。
- 续行：比键行缩进更深的非空非注释行是该键值的续行；替换该键时整个多行值被单行新值替换，
  续行删除（报告里给出 `continuation_lines_removed`）。
- 重复键：同一 section 内重复即拒绝，报出两次的行号；不同 section 同名键合法。
- 被注释掉的键永不修改。顶层（第一个 section 之前）允许键，用 `section=None` 定位。
- 非法行/空 section 名：报 1 起始行号与原因，先全量校验再写，绝不产生半成品。
- 新值不允许含换行；文件不存在、键不存在、只读文件均有专门错误。

## 写入与原子性

- 先全量解析+校验，再在同目录建 `.<name>.<rand>.tmp`，写入、`fsync`、恢复原权限、
  `os.replace` 原子换名、再 `fsync` 目录；任何异常清理临时文件。
- 只读文件（无任一写位或实际不可写）在读取前拒绝，文件与目录都不变。
- 断电点模拟：环境变量 `CONFIG_REWRITE_CRASH_BEFORE_RENAME=1` 时，临时文件落盘 fsync 后、
  rename 前 `SIGKILL`。测试用子进程验证：原文件逐字节不变，遗留临时文件内容完整非截断。

## 幂等

值未变化时直接返回 `changed: False`，不触碰文件；改两次第二次与第一次结果逐字节相同
（测试中断言差异偏移列表为空）。

## 基准结果（本机）

见 `bench_result.txt`：1MB ≈ 43 ms、峰值 RSS ≈ 18 MB；16MB ≈ 0.6 s、峰值 RSS ≈ 107 MB。
解析为流式扫描，不保留逐行对象，额外内存主要为内容双份与重复键检测集合。

## 文件

- `config_rewrite.py` — 库 + CLI
- `test_config_rewrite.py` — 25 个自测（含字节级 diff 报告输出）
- `bench_config_rewrite.py` / `bench_result.txt` — 基准脚本与结果
- `test_run.log` — 最近一次完整测试输出
