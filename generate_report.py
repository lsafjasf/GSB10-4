"""生成交付数据：跳转链记录 + 敏感头剥离策略对照。

运行: python3 generate_report.py
输出: out/chain_records.json, out/policy_matrix.json, out/policy_matrix.md
"""

import json

from redirect_tracer import RedirectTracer

AUTH = {"Authorization": "Bearer s3cret", "Cookie": "sid=abc",
        "X-Request-Id": "r-1"}


def chain(start, *hops):
    routes = {}
    seq = [start, *hops]
    for i in range(len(seq) - 1):
        routes[seq[i]] = (302, {"Location": seq[i + 1]})
    routes[seq[-1]] = (200, {})
    return routes


def trace(routes, start, headers=None, max_redirects=20):
    tracer = RedirectTracer(lambda url, h: routes.get(url, (200, {})),
                            max_redirects=max_redirects)
    return tracer.trace(start, headers=headers)


def scenario_chains():
    scenarios = {}

    # 1. 直接跳（同源，凭据保留）
    a, b = "http://example.com/a", "http://example.com/b"
    scenarios["direct_same_origin"] = trace(chain(a, b), a, AUTH).to_dict()

    # 2. 跨协议跳 https -> http（凭据剥离）
    a, b = "https://example.com/a", "http://example.com/b"
    scenarios["cross_protocol_downgrade"] = trace(chain(a, b), a, AUTH).to_dict()

    # 3. 跨主机跳（凭据剥离）
    a, b = "https://a.example.com/x", "https://b.example.com/y"
    scenarios["cross_host"] = trace(chain(a, b), a, AUTH).to_dict()

    # 4. 成环
    a, b = "http://example.com/a", "http://example.com/b"
    routes = {a: (302, {"Location": b}), b: (302, {"Location": a})}
    scenarios["loop"] = trace(routes, a, AUTH).to_dict()

    # 5. 超长链（上限 10）
    urls = [f"http://example.com/{i}" for i in range(30)]
    scenarios["chain_too_long"] = trace(chain(*urls), urls[0], AUTH,
                                        max_redirects=10).to_dict()

    # 6. 非法目标（不支持的协议）
    a = "http://example.com/a"
    scenarios["invalid_target_scheme"] = trace(
        {a: (302, {"Location": "javascript:alert(1)"})}, a, AUTH).to_dict()

    # 7. 相对地址 + 转义路径
    a = "http://example.com/dir/page"
    routes = {a: (302, {"Location": "../%7Euser/%2f?q=1"}),
              "http://example.com/~user/%2F?q=1": (200, {})}
    scenarios["relative_and_escaped"] = trace(routes, a, AUTH).to_dict()

    # 8. URL 内嵌凭据（userinfo）跳转到外站
    a, b = "https://example.com/a", "https://user:pass@evil.example.com/"
    scenarios["userinfo_in_target"] = trace(chain(a, b), a, AUTH).to_dict()

    return scenarios


def policy_matrix():
    """策略对照：各跳转情形下敏感头是否保留。"""
    cases = [
        ("同源同路径跳转", "http://example.com/a", "http://example.com/b"),
        ("同源显式默认端口", "http://example.com/a", "http://example.com:80/b"),
        ("跨协议降级 https->http", "https://example.com/a", "http://example.com/b"),
        ("跨协议升级 http->https", "http://example.com/a", "https://example.com/b"),
        ("跨主机同协议", "https://a.example.com/x", "https://b.example.com/y"),
        ("跨端口", "http://example.com:8080/a", "http://example.com:9090/b"),
        ("目标 URL 内嵌 userinfo", "https://example.com/a",
         "https://user:pass@evil.example.com/"),
    ]
    rows = []
    for name, a, b in cases:
        result = trace(chain(a, b), a, AUTH)
        hop0, hop1 = result.hops[0], result.hops[1]
        rows.append({
            "case": name,
            "from": a,
            "to": b,
            "stripped_headers": hop0.stripped_headers,
            "authorization_sent_on_next": "Authorization" in hop1.request_headers,
            "cookie_sent_on_next": "Cookie" in hop1.request_headers,
            "custom_header_sent_on_next": "X-Request-Id" in hop1.request_headers,
            "credentials_carried_next": hop1.credentials_carried,
            "url_has_userinfo_next": hop1.url_has_userinfo,
        })
    return rows


def matrix_to_md(rows):
    lines = [
        "| 情形 | 从 | 到 | 剥离的头 | 下一跳带 Authorization | 下一跳带 Cookie | 下一跳带自定义头 | 下一跳携带凭据 |",
        "|---|---|---|---|---|---|---|---|",
    ]
    for r in rows:
        lines.append(
            "| {case} | `{from}` | `{to}` | {stripped} | {auth} | {cookie} | {custom} | {cred} |".format(
                case=r["case"], **{"from": r["from"]}, to=r["to"],
                stripped=", ".join(r["stripped_headers"]) or "（无）",
                auth="是" if r["authorization_sent_on_next"] else "否",
                cookie="是" if r["cookie_sent_on_next"] else "否",
                custom="是" if r["custom_header_sent_on_next"] else "否",
                cred="是" if r["credentials_carried_next"] else "否",
            ))
    return "\n".join(lines) + "\n"


def main():
    scenarios = scenario_chains()
    with open("out/chain_records.json", "w") as f:
        json.dump(scenarios, f, ensure_ascii=False, indent=2)

    rows = policy_matrix()
    with open("out/policy_matrix.json", "w") as f:
        json.dump(rows, f, ensure_ascii=False, indent=2)
    with open("out/policy_matrix.md", "w") as f:
        f.write(matrix_to_md(rows))

    print("scenarios:", {k: v["reason"] for k, v in scenarios.items()})
    print(matrix_to_md(rows))


if __name__ == "__main__":
    main()
