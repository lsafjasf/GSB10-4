# streamdecode — 内容编码协商与流式解压

纯 Python 3 标准库实现（`zlib` / `gzip` / `bz2`，无任何第三方依赖）。

## 支持的编码与协商

- 解码支持：`identity`、`gzip`（含 `x-gzip`）、`deflate`（zlib 封装与 raw
  deflate 自动识别）、`bzip2`（含 `x-bzip2`）。
- `accept_encoding()` 生成只包含可解码编码的 `Accept-Encoding` 头：
  `gzip, deflate, bzip2, identity;q=0.5, *;q=0`。
- `parse_content_encoding()` 解析响应头：大小写归一、别名归一、非法
  token / 重复编码直接报错并给出层号。

## 编码链顺序

`Content-Encoding: gzip, deflate` 表示先 gzip 后 deflate（声明顺序 =
应用顺序），解码按**声明逆序**逐层进行（先 deflate 后 gzip）。
解码前对流的前 8 字节做签名嗅探（gzip `1f 8b 08`、bzip2 `BZh`、
zlib 头校验）：若实际最外层编码与声明的最外层不符，抛
`EncodingOrderError`，报出声明链、实际检测到的编码以及疑似写反的
建议顺序——顺序颠倒会被识别并拒绝，不会解出乱码。嗅探无法判定时
（如 raw deflate 无魔数），由该层解压器抛 `DecodeError`，同样带层号。

## 内存上界（可证明）

每个解码层只有三类常驻内存，全部是与响应大小 N 无关的常数：

| 项 | 上界 |
| --- | --- |
| 输入块 `C_in` | 调用方控制，默认 64 KiB |
| 每层解码器状态 `S_i` | deflate/gzip：32 KiB 窗口 + zlib 内部状态；bzip2：格式固定（≤900 KiB 块，数 MiB 表） |
| 每层待处理输入 `P_i` | ≤ `out_size` + 4 KiB |
| bzip2 输出暂存 `B_i` | 单次 4 KiB 喂入的最大展开（常数）；gzip/deflate 用 `max_length` 排放，无暂存 |
| 在途输出 `C_out` | 每次 yield ≤ `out_size`（默认 64 KiB） |

`M(N) ≤ C_in + Σ(S_i + P_i + B_i) + C_out = O(1)`，与 N 无关。
实测数据（子进程绝对峰值 RSS，响应体 1→100 MiB）见
`reports/benchmark.txt`：峰值恒为 31.51 MiB；同机一次性
`gzip.decompress` 为 415.28 MiB。单测 `MemoryBoundTests` 用
`tracemalloc` 断言 2 MiB 与 32 MiB 响应的峰值差 < 1 MiB。

## 错误模型

所有异常继承 `ContentEncodingError`，携带 `layer`（声明列表中的
0 基下标）与 `encoding`：

- `UnknownEncodingError` — 未知编码名（如 `br`、`zstd`、`compress`）。
- `EncodingOrderError` — 声明顺序与实际流不符（疑似写反）。
- `DecodeError` — 压缩数据损坏、流尾有多余字节。
- `TruncatedStreamError` — 流中途截断（外层/内层均能定位到层）。
- `LengthMismatchError` — 声明 `Content-Length` 与实际字节数不符。

完整异常样例见 `reports/exceptions.txt`（13 个场景）。

## 对拍

`scripts/differential.py`：8 条编码链 × 9 种长度（0 B – 8 MiB）×
随机/混合两种数据，流式输出与一次性标准库 oracle
（`gzip.decompress` / `zlib.decompress` / `bz2.decompress` 按声明逆序
施加）逐字节一致，SHA-256 三向相等。144 例，0 失败，数据见
`reports/differential.txt`。

## 100 MiB 基准

见 `reports/benchmark.txt`（每次测量在独立子进程中进行，峰值 RSS 为
绝对值）：100 MiB 压缩响应流式解压 0.087 s（约 1150 MiB/s），峰值
RSS 31.51 MiB；一次性解压 0.367 s，峰值 415.28 MiB。

## 运行命令

```bash
cd <repo root>
python3 -m unittest discover -s tests -t . -v   # 全部自测（22 个）
python3 scripts/error_report.py                  # 生成 reports/exceptions.txt
python3 scripts/differential.py                  # 生成 reports/differential.txt
python3 bench/benchmark.py                       # 生成 reports/benchmark.txt
```

## 已知限制

- 不支持 `br` / `zstd` / `compress`（标准库无实现），按未知编码拒绝。
- gzip 多成员（multi-member）拼接流按"流尾多余数据"拒绝。
- `deflate` 的 zlib/raw 探测在理论上有歧义角落：raw 流前两字节恰好
  满足 zlib 头校验（CM=8、CINFO≤7、校验和 %31==0，随机流概率约
  1/2048）。此时按 zlib 解释会在数据/校验阶段报 `DecodeError`，
  不会静默产出乱码。
