# Differential data: streaming vs one-shot

For each fixture the streaming decoder output is compared
byte-for-byte (`==`) with the one-shot reference decoder at four
feed chunk sizes (1, 7, 4096, 65536 bytes). Fixtures live in
`data/` and are regenerated with `python3 tools/gen_fixtures.py`.

| fixture | encoding | coded/plain bytes | plaintext sha256 | 1B | 7B | 4KiB | 64KiB |
| --- | --- | --- | --- | --- | --- | --- | --- |
| text.txt.gzip | `gzip` | 1196 / 34800 | `b3a6b1a85c4afb32...` | equal | equal | equal | equal |
| text.txt.deflate | `deflate` | 1184 / 34800 | `b3a6b1a85c4afb32...` | equal | equal | equal | equal |
| text.txt.gzip+deflate | `gzip, deflate` | 370 / 34800 | `b3a6b1a85c4afb32...` | equal | equal | equal | equal |
| random.bin.gzip | `gzip` | 65574 / 65536 | `c7adbc955cc6a7b5...` | equal | equal | equal | equal |
| random.bin.deflate | `deflate` | 65562 / 65536 | `c7adbc955cc6a7b5...` | equal | equal | equal | equal |
| random.bin.gzip+deflate | `gzip, deflate` | 65600 / 65536 | `c7adbc955cc6a7b5...` | equal | equal | equal | equal |
| zeros.bin.gzip | `gzip` | 64 / 32768 | `c35020473aed1b46...` | equal | equal | equal | equal |
| zeros.bin.deflate | `deflate` | 52 / 32768 | `c35020473aed1b46...` | equal | equal | equal | equal |
| zeros.bin.gzip+deflate | `gzip, deflate` | 40 / 32768 | `c35020473aed1b46...` | equal | equal | equal | equal |

All 36 comparisons are byte-identical. The same equivalence is
asserted by `tests/test_roundtrip.py` over generated payloads
(including empty, zero, repeated and random data) and 7 chunk
sizes, and at 100 MiB scale in `reports/benchmark.md` (matching
sha256).
