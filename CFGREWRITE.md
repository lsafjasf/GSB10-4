# cfgrewrite — 配置读取与原地重写（Python 3 标准库）

只改目标键的值，其他字节（注释、空行、缩进、CRLF、键大小写）一律不动。
先完整解析校验，再原子写回；写回幂等。

## 配置格式（明确定义的小型 INI 方言）

- 行以 `\n` 结束（`\r\n` 原样保留）；只含空格/tab 的行为空行。
- 行首（允许缩进）是 `#` 或 `;` 的为注释；`# k = old` 这类被注释掉的键**永不匹配、永不修改**。
- 节头为 `[name]`，后面可接行内注释；空节名、未闭合、节头后有杂物都按语法错误拒绝。
- 条目为 `key = value`；`=` 两侧空格、键的大小写与缩进原样保留；键大小写敏感匹配。
- 行内注释：值区域内首个“空白 + `#`/`;`”之后为注释，改值时注释与前面的空白原样保留。
- 续行：值行最后一个字符是 `\` 时，下一物理行属于该值（续行期间的空白行/`#` 开头行都是值字节，不是注释）。改写多行值时坍缩为单行，并把最后一行的行内注释保留到新值之后。
- 重复键：读取时后写覆盖（last-wins）；写入策略 `on_duplicate="all"（默认）|"first"|"last"|"error"`。
- 写入值的约束（违反则在打开文件**之前**抛 `ValueError`，保证不出半成品）：
  不允许换行、首尾空白、以 `\` 结尾（会变成续行）、包含 ` #`/` ;`（回读会被当注释）。

## 用法

```python
from cfgrewrite import get_value, set_value

set_value("app.conf", "host", "example.com")              # 返回 True/False（False=无需写）
set_value("app.conf", "workers", "16", section="server")  # 指定节
get_value("app.conf", "host", section="server")
set_value("app.conf", "k", "0", on_duplicate="last")      # 重复键策略
```

异常：`ConfigError(line, reason)`（带 1 基行号）、`KeyNotFound`、`DuplicateKeyError`。

## 写回语义

1. 读全部字节，按 UTF-8（surrogateescape）解码，解析失败即报错且不动文件；
2. 未找到键抛 `KeyNotFound`，不动文件；新旧文本完全一致时直接返回 `False`，**连写都不写**；
3. 原子写：同目录 `mkstemp` 临时文件 → 写 → flush + `fsync` → 复制原文件 mode → `os.replace` → `fsync` 目录；
4. replace 前任何异常都清理临时文件；进程被 SIGKILL 时最多留下 `.tmp` 残骸，目标文件只能是旧内容或新内容，绝不截断。
   限制说明：原子性在单机 POSIX/Windows 本地文件系统成立；多写者并发不在范围内；文件 uid/gid 不保证复制。

## 运行命令

```bash
python3 -m unittest test_cfgrewrite -v   # 34 个用例：语法错误/重复键/续行/只读文件/只读目录/SIGKILL 断电模拟/幂等
python3 demo_diff.py                     # 字节级差异报告（含第二次零差异验证）
python3 bench.py                         # 1MB / 16MB 耗时与内存峰值
```

## 交付清单

| 文件 | 内容 |
| --- | --- |
| `cfgrewrite.py` | 库：解析、读取、字节保留重写、原子写、`byte_diff` |
| `test_cfgrewrite.py` | 自测：边界用例、幂等逐字节验证、原子性与断电模拟 |
| `demo_diff.py` | 字节级差异报告生成器 |
| `bench.py` | 1MB/16MB 耗时与内存基准 |
| `diff_report.txt` / `test_report.txt` / `bench_report.txt` | 已生成的运行结果 |
