# Sensitive-Header Policy Matrix

Policy: `Authorization`, `Proxy-Authorization`, `Cookie` are forwarded
only within the same origin `(scheme, host, effective port)`. Any
cross-scheme or cross-host hop strips them; stripped headers are
never re-added later in the chain.

| Case | From | To | Kept | Stripped |
|---|---|---|---|---|
| same origin (identical URL) | `https://a.example/app` | `https://a.example/app` | Accept, Authorization, Cookie | - |
| same origin, default port explicit | `https://a.example/app` | `https://a.example/app` | Accept, Authorization, Cookie | - |
| same origin, different path | `https://a.example/app` | `https://a.example/other` | Accept, Authorization, Cookie | - |
| cross host | `https://a.example/app` | `https://b.example/app` | Accept | Authorization, Cookie |
| cross subdomain | `https://a.example/app` | `https://cdn.a.example/app` | Accept | Authorization, Cookie |
| cross scheme downgrade https->http | `https://a.example/app` | `http://a.example/app` | Accept | Authorization, Cookie |
| cross scheme upgrade http->https | `http://a.example/app` | `https://a.example/app` | Accept | Authorization, Cookie |
| cross port | `https://a.example/app` | `https://a.example:8443/app` | Accept | Authorization, Cookie |
