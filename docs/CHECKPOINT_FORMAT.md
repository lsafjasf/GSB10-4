# 断点文件格式（checkpoint）

单个 JSON 文件（UTF-8，键排序，无空白压缩），每次状态变更后**原子替换**
（同目录临时文件写入 → `fsync` → `os.replace` → 目录 `fsync`），
崩溃最多丢失最后一次状态变更，不会得到半个文件。

## 顶层字段

| 字段 | 含义 |
|---|---|
| `version` | 格式版本，当前为 `1` |
| `created_at` / `updated_at` | 创建/最近更新时间（Unix 秒） |
| `script_sha256` | 整个源脚本的 SHA-256（防串脚本续跑） |
| `script_bytes` | 源脚本字节数 |
| `chunk_size` | 分块上限（字节） |
| `statement_total` / `chunk_total` | 语句/块总数 |
| `chunks` | 每块一条记录，按下标与 `index` 对应 |

## 块记录字段

| 字段 | 含义 |
|---|---|
| `index` | 块序号 |
| `start_stmt` / `end_stmt` | 语句序号区间 `[start, end)` |
| `byte_start` / `byte_end` | 块文本在源脚本中的字节偏移 |
| `sha256` | 块执行文本的 SHA-256（执行内容的密码学证据） |
| `statements` / `bytes` | 语句数 / 字节数 |
| `oversized` | 单语句超上限独占一块 |
| `status` | `pending` / `done` / `failed_retryable` / `failed_fatal` |
| `attempts` | 总尝试次数（含首次） |
| `retries` | 重试次数 = `attempts - 1` |
| `error_type` / `error_message` | 最近一次失败的异常类型与消息（截断 1000 字符） |
| `started_at` / `finished_at` | 最近一次尝试的开始/结束时间 |

## 断点如何证明执行进度

1. `script_sha256` + `script_bytes` 证明断点属于当前这份脚本；
2. 每块 `sha256` 证明“被执行的字节内容”与当前脚本对应块逐字节一致；
3. `status == done` 的块已提交且不会重放；续跑从第一个非 `done` 块开始。

## 载入校验（任一不满足即抛 `CheckpointCorrupted`，拒绝续跑）

- 文件必须是合法 JSON 且为对象，含全部必需顶层字段；
- `version` 必须受支持；
- `script_sha256` / `script_bytes` 必须与当前脚本一致；
- 块数量、每块 `index`、`sha256`、语句区间、字节数必须与当前脚本
  重新分块的结果一致。

校验失败时**绝不猜测、绝不续跑**，避免重放副作用；用 `--fresh`
显式放弃旧断点从头执行。

## 原子性与崩溃窗口

- 每块在 `BEGIN/COMMIT` 事务中执行，失败整体 `ROLLBACK`：块是副作用的
  最小原子单位，重试/续跑不会看到半个块。
- 已知窗口：块 COMMIT 成功、断点尚未落盘时进程死亡，续跑会重放该块。
  因此脚本语句应当幂等（`INSERT OR REPLACE` / `INSERT ... ON CONFLICT` /
  `CREATE TABLE IF NOT EXISTS` 等），这也是数据库导入脚本的通行实践；
  测试 `test_commit_before_checkpoint_window_idempotent_replay`
  验证了该窗口下幂等重放结果与基线一致。
