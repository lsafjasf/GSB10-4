# 缓存键头部规范化规则（HK1）

版本标签：`HK1`。规则若发生不兼容变更，必须升级版本标签。

唯一目标：**语义不同的请求必须落到不同的键**；在此前提下，让传输形式不同但
语义等价的请求落到同一个键。规则整体遵循“宁可不合并，也不错误合并”的原则——
欠归一化（等价请求生成不同键）只是缓存命中率损失，过归一化（语义不同的请求
共享一个键）是数据事故。

## 1. 哪些头部参与

- 采用**显式白名单**。只有 `participating` 中声明的头部参与键，其余头部一律
  忽略（白名单外的头部即使取值变化也不会影响键，见对拍 EQ-09）。
- 默认白名单（典型内容协商场景）：
  `accept`、`accept-encoding`、`accept-language`、`authorization`、
  `content-encoding`、`content-language`、`content-type`、`cookie`、`x-api-key`。
- HTTP 缓存应直接使用服务端 `Vary` 头列出的名字构造 `participating`，键不依赖
  任何隐式默认值。
- 头部名称先 `strip` 再**小写**（RFC 7230：字段名大小写不敏感），非法字符
  （空名、空白、分隔符等，违反 RFC 7230 token 语法）直接抛 `HeaderError`。

## 2. 值的归一化策略（逐头部声明，不做全局处理）

| 策略 | 处理 | 用于 |
|---|---|---|
| `raw` | 只去掉首尾 OWS（SP/HTAB），内部一切原样保留，**绝不改大小写、绝不折叠空白** | `authorization`、`cookie`、`x-api-key` 等不透明/敏感值 |
| `token_list` | 逗号分隔为列表；逐元素去首尾 OWS；仅对元素**开头的 RFC token 段**小写；丢弃空元素；**保留顺序与重复** | `accept`、`accept-language` |
| `token_set` | 同 `token_list`，但元素排序去重——仅用于元素顺序与重复在协议上无意义的头部 | `accept-encoding`、`content-encoding` |
| `insensitive` | 去首尾 OWS；仅对**第一个 `;` 之前**的 media-type 段中的 token 小写；参数段（含 `boundary=` 等大小写敏感值）原样保留 | `content-type` |

### 2.1 明确不做的归一化（每一条都对应一个反例）

- **不整体小写值**：凭据大小写敏感，`Bearer ABC ≠ Bearer abc`（NE-01）。
- **不折叠内部空白**：raw 头部内部空格是数据，`a=1;  b=2 ≠ a=1; b=2`。
- **不对 `token_list` 排序去重**：`Accept` 的顺序表达偏好
  （NE-02），重复表达强度（`text/html` 出现两次 ≠ 一次）。
- **不小写 quoted-string 内容**：引号内是不透明字节，`"Hello" ≠ "hello"`
  （NE-04）；大小写折叠只作用于引号外的裸 RFC token。
- **不把参数值当不区分大小写**：`charset=utf-8 ≠ charset=utf-16`（NE-03），
  `boundary=AbC` 与 `boundary=abc` 是不同的 multipart，因此 `insensitive`
  只折叠 media-type 段，参数原样保留。
- **不把引号内逗号当分隔符**：`"gzip, deflate"` 是一个元素，与
  `gzip, deflate` 两个元素语义不同（NE-09）。

### 2.2 允许的等价变换（对应对拍数据）

- 名称大小写、头部在线上的排列顺序不影响键（EQ-01）。
- 同名重复头按 RFC 7230 §3.2.2 合并为逗号连接，与一次发送 CSV 等价（EQ-02）。
- 逗号周围的 OWS（SP 或 HTAB）无意义（EQ-03、EQ-07）。
- 空列表元素（`,,`、尾逗号）被丢弃（EQ-08）。
- `token_set` 下编码名大小写、顺序、重复无意义：`GZIP, BR == gzip, br, gzip`
  （EQ-04）。
- media type 类型名大小写不敏感：`Application/JSON == application/json`（EQ-05）。

## 3. 规范编码（防拼接碰撞）

归一化结果是一个有序记录序列：`(头部名, 存在标志, 元素字节序列...)`，
按头部名排序后，用**长度前缀（netstring 风格）**自描述编码，而不是简单拼接
`k=v&k=v`：

```
HK1, <记录数>,  [ <名长度>:<名>, <存在标志0|1>, <元素数>,
                   (<元素长度>:<元素>)* ]*
```

该编码是**单射（injective）**的：长度前缀使 `:`、`,` 等分隔符出现在值中也无法
伪造结构。例如 `x-a="bc", x-b=空` 与 `x-a="b", x-b="c"` 朴素拼接都会变成
`a=bc;b=` 类形态，本编码永远分开（测试 `TestFramingInjection`）。单射性的形式
检查：测试中用一个独立解码器解码规范字节，再用独立编码器重编码，必须逐字节复原
（`TestInjectivityFuzz`，4000 组随机请求）。

“存在标志”区分三种状态：头部缺失 / 头部存在但为空 / 头部有值（NE-05、NE-06）。
raw 策略下两次重复头合并出的 `a,b` 与一次发送字面值 `"a,b"` 也永远不相等
（NE-08）：元素分别是 `('a,b',)`（raw，含逗号的一个元素）与 `('"a,b"',)`。

## 4. 长度上界与碰撞处理

- 规范字节（canonical payload）硬上界 `MAX_CANONICAL_BYTES = 4096`（可调）。
- 未超界：直接键 `HK1$<base64url(canonical)>`，长度 ≤
  `4 + ceil(4096 * 4/3) = 5466` 个 ASCII 字符。
- 超界时两种显式策略，**绝不静默截断**：
  - `on_too_long="hash"`（默认）：对完整 canonical（含 `HK1$overflow$` 域分离
    前缀）计算 **BLAKE2b/256**，键为固定 48 字符的 `HK1H$<base64url(digest)>`。
    256 bit 摘要的意外碰撞概率可忽略；直接键与哈希键前缀不同（`HK1$` vs
    `HK1H$`），两个命名空间不可能互撞。
  - `on_too_long="reject"`：抛 `KeyTooLongError`，由调用方显式处理。
- 注意：哈希路径同样先做全部归一化，因此等价的超长头部仍然得到同一个哈希键，
  而相差一个字节的两个超长头部得到不同哈希键（测试覆盖）。
- 输入校验：值字节只允许 HTAB / SP / VCHAR / obs-text；**裸 CR、LF、NUL 一律
  拒绝**，防止响应拆分 / obs-fold 注入。

## 5. 数据文件

- `data/equivalent_pairs.json`：10 组等价对拍（键必须相同），含单值、多值、
  大小写混用、重复头、引号内逗号等情形。
- `data/counterexamples.json`：10 组语义不同的反例（键必须不同）。
- 两组数据均由 `tests/test_cachekey.py` 逐条断言。
