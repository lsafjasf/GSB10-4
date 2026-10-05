# Redirect Chain Tracer

Python 3, standard library only. Traces HTTP redirect chains hop by hop,
records every hop, strips credential headers across origins, and terminates
with a classified reason instead of following forever.

## Files

- `redirect_trace.py` — library (tracer, URL normalizer, header policy,
  scripted + real HTTP fetchers)
- `test_redirect_trace.py` — 23 unittest cases
- `run_demo.py` — scenario demo + 10k-chain benchmark, writes `out/`
- `out/chain_records.json` — full hop records for all demo scenarios
- `out/policy_matrix.md` / `.json` — header-stripping policy comparison data
- `out/perf.json` — benchmark timing and depth stats

## Run

```sh
python3 -m unittest -v test_redirect_trace   # self-tests
python3 run_demo.py                          # demo + benchmark, writes out/
```

## Header policy

Sensitive headers: `Authorization`, `Proxy-Authorization`, `Cookie`.

They are forwarded only within the same origin `(scheme, host, effective
port)`. Any cross-scheme or cross-host (or cross-port) hop strips them,
including HTTPS→HTTP downgrade on the same host. Once stripped, they are
never re-added later in the chain. URL userinfo (`user:pass@host`) is
recorded per hop (`url_has_credentials`) and removed from the normalized
URL. Full comparison table: `out/policy_matrix.md`.

## Termination classification

| status | meaning |
|---|---|
| `ok` | non-redirect terminal status reached |
| `loop` | a normalized URL repeats in the chain |
| `too_many_redirects` | chain exceeded `max_hops` (default 20) |
| `invalid_url` | bad start URL, bad redirect target (non-http(s) scheme, control chars, missing host, bad port), or redirect status without `Location` |

## URL normalization

Relative targets resolve via RFC 3986 against the current hop; dot segments
are removed; percent-encoded unreserved characters are decoded (reserved
encodings like `%2F` preserved, case-folded); scheme/host lowercased;
default ports dropped; userinfo extracted and flagged. Edge cases covered
by tests: `../` escapes, `%2e%2e`, `%7E`, explicit default ports,
`javascript:` targets, whitespace/control characters, port 99999,
scheme-less hosts.

## Benchmark (10,000 chains, this machine)

See `out/perf.json` for the current run: ~1.8 s total, mean ~175 µs/chain,
max observed depth 20 (capped by `max_hops`), terminal-status breakdown
included.
