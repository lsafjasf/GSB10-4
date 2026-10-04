# 重定向链追踪与长度限制 — 交付报告

语言：Python 3（仅标准库：`urllib.parse`、`dataclasses`、`re`、`json`、`time`、`unittest`）。

## 文件

- `redirect_tracer.py` — 库：URL 规范化、相对地址解析、逐跳追踪、敏感头剥离、终止分类
- `selftest.py` — 27 个自测用例（unittest）
- `benchmark.py` — 1 万条链耗时 / 深度统计
- `generate_report.py` — 生成跳转链记录与策略对照数据
- `out/chain_records.json` — 8 个典型场景的完整跳转链记录
- `out/policy_matrix.json` / `out/policy_matrix.md` — 策略对照数据
- `out/benchmark.json` — 耗时与深度统计

## 运行命令

```bash
python3 selftest.py -v        # 自测（27 用例）
python3 benchmark.py          # 1 万条链基准，写 out/benchmark.json
python3 generate_report.py    # 生成链记录与策略对照，写 out/
```

## 设计

### 追踪模型

`RedirectTracer(fetch, max_redirects=20)`，`fetch(url, headers) -> (status, headers)`
由调用方注入（自测/基准用内存路由表；真实环境可用 `http.client` 实现，不自动跟随重定向）。
`trace(start_url, headers)` 返回 `TraceResult`，每一跳记录：

- 规范化 URL、协议、主机、端口
- 响应状态码、原始 `Location`、解析出的下一跳规范化地址
- 实际发送的请求头（敏感值记录为 `<redacted>`，不外泄到日志）
- 本跳被剥离的头名、`credentials_carried`（是否携带敏感头或 URL 内嵌 userinfo）

### 敏感头剥离策略

- 敏感头集合：`Authorization`、`Proxy-Authorization`、`Cookie`（头名大小写不敏感）。
- 源（origin）= (scheme, host, port)，默认端口归一（http:80 / https:443）。
- **同源跳转：全部头原样保留。**
- **跨源（跨协议或跨主机或跨端口）：剥离全部敏感头，自定义头保留。**
  https->http 降级与 http->https 升级同样剥离（协议不同即换源）。
- 目标 URL 内嵌 `user:pass@host` 时无法从 URL 剥离，逐跳以
  `url_has_userinfo` / `credentials_carried` 标记，供上层告警。

### 终止分类

| reason | 含义 |
|---|---|
| `OK` | 到达非重定向响应（或重定向状态码缺 Location，按终点处理） |
| `LOOP` | 规范化 URL 在链中重复出现，立即终止 |
| `CHAIN_TOO_LONG` | 跳数超过 `max_redirects`，终止 |
| `INVALID_TARGET` | Location 非法：不支持的协议（ftp/javascript: 等）、缺主机、非法端口、内嵌控制字符；起始 URL 非法同此类 |
| `FETCH_ERROR` | 请求回调抛异常 |

### URL 解析与规范化

- `Location` 先剥离首尾 `\t\n\r\f` 与空格（WHATWG 行为），内嵌控制字符判非法。
- 相对地址经 `urljoin` 按 RFC 3986 解析（支持 `./`、`../`、`/abs`、`//host` 协议相对）。
- 规范化：scheme/host 小写、去默认端口、去 fragment、空路径补 `/`、
  dot-segment 消除、百分号编码规范化（无保留字符解义如 `%7E`->`~`，
  其余转义统一大写十六进制如 `%2f`->`%2F`）。环检测基于规范化结果，
  因此 `~u` 与 `%7Eu` 会被识别为同一地址。

## 策略对照数据

实测输出（`out/policy_matrix.md`，由 `generate_report.py` 生成）：

| 情形 | 剥离的头 | 下一跳带 Authorization | 下一跳带 Cookie | 下一跳带自定义头 | 下一跳携带凭据 |
|---|---|---|---|---|---|
| 同源同路径跳转 | （无） | 是 | 是 | 是 | 是 |
| 同源显式默认端口 | （无） | 是 | 是 | 是 | 是 |
| 跨协议降级 https->http | Authorization, Cookie | 否 | 否 | 是 | 否 |
| 跨协议升级 http->https | Authorization, Cookie | 否 | 否 | 是 | 否 |
| 跨主机同协议 | Authorization, Cookie | 否 | 否 | 是 | 否 |
| 跨端口 | Authorization, Cookie | 否 | 否 | 是 | 否 |
| 目标 URL 内嵌 userinfo | Authorization, Cookie | 否 | 否 | 是 | 是（凭据在 URL 中） |

## 耗时数据（1 万条链）

`python3 benchmark.py` 实测（链深 1..50 轮转，上限 55，全部跑完）：

- 总耗时 **3.375 s**，平均 **0.34 ms/链**，约 **2963 链/秒**
- 总跳数 265000，最大深度 **50**，最小 1，平均 25.5
- 终止分类：10000 条全部 `OK`

## 边界用例（selftest.py，27 个全过）

- 直接跳：同源保留凭据；hop 记录协议/主机/端口；敏感值在记录中打码
- 跨协议跳：https->http、http->https、跨主机、跨端口均剥离敏感头；userinfo 目标被标记
- 成环：自环、双节点环、`%7E` 与 `~` 规范化后识别为环
- 超长链：50 跳链在上限 10 处终止并分类 `CHAIN_TOO_LONG`；恰好不超限时正常 `OK`
- 非法目标：`ftp:`、`javascript:`、端口 99999、Location 内嵌控制字符、非法起始 URL
- 相对/转义：`../other?q=1`、`/top`、`//cdn.example.com/x` 协议相对、
  dot-segment 消除、`%7e`->`~` / `%2f`->`%2F`、默认端口与大小写归一、fragment 丢弃
