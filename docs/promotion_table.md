# 类型提升表（join 表）

单元格 = 行类型与列类型提升后的结果类型；`✗` = 不可比，拒绝运算并报列位置；
`⚠` 后缀 = 该组合可能丢精度，逐值检测并写入 PrecisionReport。

提升规则摘要：

1. `null + T -> T`（空值作为值参与运算，不改变结果类型）。
2. 整数链 `bool < int32 < int64 < bigint`，取较高者。
3. `decimal + 整数 -> decimal`（精确）；`decimal + float -> float64`（可能丢精度）。
4. `bool + float32 -> float32`；`int32 + float32 -> float64`（避免 24 位尾数丢精度）；
   `int64/bigint + float -> float64`（可能丢精度）；`float32 + float64 -> float64`。
5. `date + datetime -> datetime`（date 取当日 00:00:00，精确）。
6. string / bytes / 数值 / 日期 跨族组合一律 `✗`，绝不静默 str()。

| 左 \\ 右 | null | bool | int32 | int64 | bigint | float32 | float64 | decimal | string | bytes | date | datetime |
|---|---|---|---|---|---|---|---|---|---|---|---|---|
| **null** | null | bool | int32 | int64 | bigint | float32 | float64 | decimal | string | bytes | date | datetime |
| **bool** | bool | bool | int32 | int64 | bigint | float32 | float64 | decimal | ✗ | ✗ | ✗ | ✗ |
| **int32** | int32 | int32 | int32 | int64 | bigint | float64 | float64 | decimal | ✗ | ✗ | ✗ | ✗ |
| **int64** | int64 | int64 | int64 | int64 | bigint | float64 ⚠ | float64 ⚠ | decimal | ✗ | ✗ | ✗ | ✗ |
| **bigint** | bigint | bigint | bigint | bigint | bigint | float64 ⚠ | float64 ⚠ | decimal | ✗ | ✗ | ✗ | ✗ |
| **float32** | float32 | float32 | float64 | float64 ⚠ | float64 ⚠ | float32 | float64 | float64 ⚠ | ✗ | ✗ | ✗ | ✗ |
| **float64** | float64 | float64 | float64 | float64 ⚠ | float64 ⚠ | float64 | float64 | float64 ⚠ | ✗ | ✗ | ✗ | ✗ |
| **decimal** | decimal | decimal | decimal | decimal | decimal | float64 ⚠ | float64 ⚠ | decimal | ✗ | ✗ | ✗ | ✗ |
| **string** | string | ✗ | ✗ | ✗ | ✗ | ✗ | ✗ | ✗ | string | ✗ | ✗ | ✗ |
| **bytes** | bytes | ✗ | ✗ | ✗ | ✗ | ✗ | ✗ | ✗ | ✗ | bytes | ✗ | ✗ |
| **date** | date | ✗ | ✗ | ✗ | ✗ | ✗ | ✗ | ✗ | ✗ | ✗ | date | datetime |
| **datetime** | datetime | ✗ | ✗ | ✗ | ✗ | ✗ | ✗ | ✗ | ✗ | ✗ | datetime | datetime |

## 可能丢精度的组合（⚠）

| 组合 | 提升结果 | 检测方式 | 处理 |
|---|---|---|---|
| int64 + float32/float64 | float64 | `int(float(v)) != v` 或溢出为 inf | 继续运算，计入 PrecisionReport |
| bigint + float32/float64 | float64 | 同上（含超出 float 范围 -> ±inf） | 同上 |
| decimal + float32/float64 | float64 | `Decimal.from_float(float(v)) != v` | 同上 |

说明：`int32 + float32` 直接提升到 `float64`（而非 `float32`），从规则上消除该组合的精度丢失。
