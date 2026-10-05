# 100 MiB benchmark

Python 3.12.3, WSL2 (Linux 6.18, x86-64). Corpus: 100 MiB
deterministic PRNG bytes (seed 20261005, incompressible), gzip
level 1 unless noted. Decoded with 64 KiB input/output chunks and a
streaming sha256 sink. `tracemalloc_peak` = peak Python allocations
during decode; `rss_peak` = process resident peak (ru_maxrss).

| case | coded size | seconds | MiB/s | tracemalloc peak | RSS peak |
| --- | --- | --- | --- | --- | --- |
| streaming gzip (incompressible) | 100.0 MiB | 0.107 | 935.7 | 0.2 MiB | 18.7 MiB |
| streaming gzip (text, level 6, 7.3 MiB body) | 7.3 MiB | 0.112 | 890.0 | 0.4 MiB | 18.9 MiB |
| streaming gzip,deflate chain (incompressible) | 100.1 MiB | 0.161 | 621.0 | 0.3 MiB | 18.8 MiB |
| one-shot gzip reference (incompressible) | 100.0 MiB | 0.283 | 353.5 | 309.4 MiB | 318.5 MiB |

**Byte equivalence at scale**: streaming and one-shot produce the
same plaintext sha256
`1f8fc8aee096fedd28701307346c77a30bd897fa7b337cb40aa4b899c339fdac` for 104,857,600 bytes.

Streaming decode is 2.6x faster and uses 1357x less peak memory than one-shot here (one-shot must hold body + plaintext: ~200+ MiB of data plus zlib working area).
