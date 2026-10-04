# 路径解析与去重规则说明

## 1. 解析顺序（每条路径独立执行）

1. **空值检查**：空字符串直接判 `empty_path`，不进入后续流程。
2. **绝对 / 相对判定**（`os.path.isabs`）：
   - 绝对路径：直接使用，不需要基准目录。
   - 相对路径：**必须**显式声明基准目录 `base_dir`，与其拼接；
     未声明时判 `missing_base` 并拒绝——**绝不回退到进程当前工作目录（cwd）**。
     基准目录在每次去重前统一校验一次：不存在判 `base_not_found`，
     不是目录判 `base_not_directory`；基准失效只影响相对路径条目，
     绝对路径条目照常解析。
3. **词法归一**（`os.path.normpath`，纯字符串、不触盘）：
   - 折叠重复分隔符（`a//b` → `a/b`）；
   - 消除 `.` 与 `..` 分量（`sub/../a` → `a`）；
   - Windows 上同时把 `/` 统一为 `\`。
4. **物理归一**（`os.path.realpath(strict=True)`，触盘）：
   - 逐级展开符号链接（目录别名、文件别名都收敛到真实目标）；
   - `strict=True` 要求目标真实存在，不存在即抛错分类；
   - 符号链接成环由内核返回 `ELOOP`，分类为 `symlink_loop`。
5. **大小写归一**（`os.path.normcase`）：
   - Windows：折叠大小写（`AbC.MP3` ≡ `abc.mp3`），与 NTFS 默认语义一致；
   - POSIX（Linux/macOS）：保留大小写，与 ext4 等大小写敏感文件系统一致。
   - 即大小写是否等价**由运行平台默认文件系统语义决定**，不主观猜测。

## 2. 去重判定依据

- **身份键 = `(st_dev, st_ino)`**（设备号 + inode），即内核视角的"同一个文件"。
- 推论：**硬链接**（不同路径、同 inode）会被判为同一文件并合并；
  符号链接在第 4 步已展开，自然也收敛。
- 规范路径字符串（`canonical`）仅用于展示与调试，不作为判等依据
  （避免 bind mount 等"同文件多路径"场景的漏判）。

## 3. 去重语义

- 保持**首次出现顺序**：第一次出现的条目为幸存项（`unique`）。
- 重复条目**不丢弃**：保留原始字符串、原始下标，并记录
  `merged_into` = 被并入幸存项的原始下标，可完整还原合并关系。
- 解析失败的条目进入 `errors`，不参与去重，原始信息同样保留。

## 4. 错误分类（绝不混成一句"找不到"）

| 分类 | 含义 | 触发条件 |
|---|---|---|
| `empty_path` | 空字符串 | 条目为空 |
| `missing_base` | 相对路径但未声明基准目录 | 拒绝按 cwd 猜测 |
| `base_not_found` | 声明的基准目录不存在 | `stat` 返回 `ENOENT` |
| `base_not_directory` | 基准路径存在但不是目录 | `stat` 后 `isdir` 为假 |
| `not_found` | 目标或父目录不存在 | `ENOENT` |
| `not_a_directory` | 路径中间分量是普通文件 | `ENOTDIR` |
| `permission_denied` | 权限不足 | `EACCES` / `EPERM` |
| `symlink_loop` | 符号链接成环 | `ELOOP` |
| `os_error` | 其他系统错误（兜底） | 未列出的 errno |

## 5. 平台说明

- 仅使用 Python 标准库，要求 Python ≥ 3.10（`os.path.realpath` 的 `strict` 参数）。
- 分隔符与大小写规则随平台（见上）；POSIX 前导双斜杠 `//` 由 `realpath`
  按系统语义收敛（Linux 下折叠为 `/`）。
