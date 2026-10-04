| 情形 | 从 | 到 | 剥离的头 | 下一跳带 Authorization | 下一跳带 Cookie | 下一跳带自定义头 | 下一跳携带凭据 |
|---|---|---|---|---|---|---|---|
| 同源同路径跳转 | `http://example.com/a` | `http://example.com/b` | （无） | 是 | 是 | 是 | 是 |
| 同源显式默认端口 | `http://example.com/a` | `http://example.com:80/b` | （无） | 是 | 是 | 是 | 是 |
| 跨协议降级 https->http | `https://example.com/a` | `http://example.com/b` | Authorization, Cookie | 否 | 否 | 是 | 否 |
| 跨协议升级 http->https | `http://example.com/a` | `https://example.com/b` | Authorization, Cookie | 否 | 否 | 是 | 否 |
| 跨主机同协议 | `https://a.example.com/x` | `https://b.example.com/y` | Authorization, Cookie | 否 | 否 | 是 | 否 |
| 跨端口 | `http://example.com:8080/a` | `http://example.com:9090/b` | Authorization, Cookie | 否 | 否 | 是 | 否 |
| 目标 URL 内嵌 userinfo | `https://example.com/a` | `https://user:pass@evil.example.com/` | Authorization, Cookie | 否 | 否 | 是 | 是 |
