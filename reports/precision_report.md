# 精度丢失报告

## 可能丢精度的类型组合

| 组合 | 是否可能丢精度 | 说明 |
| --- | --- | --- |
| BOOL → INT | 否 | True/False → 1/0，精确 |
| BOOL → FLOAT | 否 | 1/0 在 float64 中精确 |
| BOOL → DECIMAL | 否 | 精确 |
| INT → FLOAT | 是 | \|int\| > 2**53 时 float64 无法精确表示 |
| INT → DECIMAL | 否 | Decimal 任意精度，精确 |
| FLOAT → DECIMAL 组合 | 是 | 统一类型为 FLOAT，Decimal 端可能丢精度 |
| DECIMAL → FLOAT | 是 | 十进制小数大多无法精确表示为二进制浮点 |

## 实测：丢精度用例的处理结果

策略：不静默、不报错中断——值照常转换，事件计入 `PrecisionReport`
（全量计数 + 采样，含集合号、行号、原值、来源/目标类型）。

| 用例 | 输入 | 统一类型 | 转换结果 | 精度报告 |
| --- | --- | --- | --- | --- |
| INT → FLOAT | [9007199254740993], [0.5] | FLOAT | 0.5, 9007199254740992.0 | INT -> FLOAT: 1（位置 collection 0, row 0）|
| INT → FLOAT | [1152921504606859321], [1.0] | FLOAT | 1.0, 1.1529215046068593e+18 | INT -> FLOAT: 1（位置 collection 0, row 0）|
| DECIMAL → FLOAT | [Decimal('0.1')], [1.0] | FLOAT | 0.1, 1.0 | DECIMAL -> FLOAT: 1（位置 collection 0, row 0）|
| DECIMAL → FLOAT | [Decimal('1.1')], [2.0] | FLOAT | 1.1, 2.0 | DECIMAL -> FLOAT: 1（位置 collection 0, row 0）|

## 实测：不丢精度的对照用例

| 用例 | 输入 | 统一类型 | 精度报告 |
| --- | --- | --- | --- |
| INT → FLOAT（小整数） | [42], [0.5] | FLOAT | no precision loss |
| DECIMAL → FLOAT（可精确表示） | [Decimal('0.5')], [1.0] | FLOAT | no precision loss |
| INT → DECIMAL（大整数） | [1267650600228229401496703205376], [Decimal('1.5')] | DECIMAL | no precision loss |
| BOOL → INT | [True, False], [2] | INT | no precision loss |
