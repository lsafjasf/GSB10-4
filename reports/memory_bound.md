# Memory-bound data

Streaming gzip decode of the incompressible corpus (coded body
size ~= plaintext size; every size genuinely streams ~that many
bytes through the pipeline). Each row is a fresh subprocess.

| plaintext | seconds | tracemalloc peak | RSS peak |
| --- | --- | --- | --- |
| 1 MiB | 0.001 | 0.2 MiB | 21.7 MiB |
| 8 MiB | 0.005 | 0.2 MiB | 22.9 MiB |
| 32 MiB | 0.021 | 0.2 MiB | 22.9 MiB |
| 64 MiB | 0.042 | 0.2 MiB | 23.0 MiB |
| 100 MiB | 0.069 | 0.2 MiB | 23.0 MiB |

From 1 MiB to 100 MiB response the tracemalloc peak does not move
(233.5 KiB). RSS stays in a ~1.3 MiB band after startup warmup
and is independent of response size; the unit test
`tests/test_memory_bound.py` enforces this with tolerances
(4 MiB / 8 MiB) and fails if the curve grows.

Formal bound (see README): per call at most one out_chunk-sized
result exists; the sink consumes it immediately, so live data is
one <= in_chunk input chunk plus one <= out_chunk chunk per stage,
i.e. O(stages * out_chunk + in_chunk), independent of N = total
response length.  zlib state adds a fixed 32 KiB sliding window
per layer.
