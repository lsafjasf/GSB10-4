# Exception report

Each row was produced by actually triggering the condition with `tools/report_exceptions.py`.

Layer N = Nth token of Content-Encoding (1 = first listed); the
outermost coding is decoded first and equals the last token.

| scenario | exception | layer | token | message |
| --- | --- | --- | --- | --- |
| unknown coding, layer 1 | UnknownEncodingError | 1 | br | unknown content coding 'br' at layer 1 (layer N = Nth token in Content-Encoding) |
| unknown coding, layer 2 | UnknownEncodingError | 2 | zstd | unknown content coding 'zstd' at layer 2 (layer N = Nth token in Content-Encoding) |
| malformed Content-Encoding | HeaderParseError | - | - | malformed Content-Encoding 'gzip,,deflate': empty token |
| 406 negotiation: everything refused | NegotiationError | - | - | no mutually acceptable content coding for Accept-Encoding '*;q=0' |
| reversed chain A (gzip outer, declared inner) | LayerDecodeError | 2 | deflate | layer 2 (deflate) header error: bad zlib header bytes b'\x1f\x8b'; hint: bytes are a gzip stream, not a zlib stream; the coding chain order is probably reversed. Codings must be removed in reverse declaration order (last Content-Encoding token first) |
| reversed chain B (deflate outer, declared inner) | LayerDecodeError | 2 | gzip | layer 2 (gzip) header error: bad gzip magic bytes b'x\x9c'; hint: bytes are a zlib (deflate) stream, not gzip; the coding chain order is probably reversed. Codings must be removed in reverse declaration order (last Content-Encoding token first) |
| gzip truncated at 1 byte | TruncatedStreamError | 1 | gzip | layer 1 (gzip) truncated: end of input reached before the gzip stream was complete |
| gzip truncated at 10 bytes | TruncatedStreamError | 1 | gzip | layer 1 (gzip) truncated: end of input reached before the gzip stream was complete |
| gzip truncated mid-stream | TruncatedStreamError | 1 | gzip | layer 1 (gzip) truncated: end of input reached before the gzip stream was complete |
| gzip truncated at last byte | TruncatedStreamError | 1 | gzip | layer 1 (gzip) truncated: end of input reached before the gzip stream was complete |
| deflate truncated | TruncatedStreamError | 1 | deflate | layer 1 (deflate) truncated: end of input reached before the deflate stream was complete |
| chain truncated (outer layer 2 cut) | TruncatedStreamError | 2 | deflate | layer 2 (deflate) truncated: end of input reached before the deflate stream was complete |
| chain corruption (byte flipped mid-wire) | LayerDecodeError | 2 | deflate | layer 2 (deflate) integrity error: CRC/checksum failure: Error -3 while decompressing data: incorrect data check |
| Content-Length too large | LengthMismatchError | - | - | Content-Length mismatch: declared 728 bytes, received 718 bytes |
| Content-Length too small | LengthMismatchError | - | - | Content-Length mismatch: declared 708 bytes, received 718 bytes |
| trailing bytes after gzip frame | LayerDecodeError | 1 | gzip | layer 1 (gzip) trailing error: 4 unexpected byte(s) after the gzip frame; the declared length/chain does not match the actual framing |
| gzip CRC trailer corrupted | LayerDecodeError | 1 | gzip | layer 1 (gzip) integrity error: CRC/checksum failure: Error -3 while decompressing data: incorrect data check |
| random garbage where gzip frame expected | LayerDecodeError | 1 | gzip | layer 1 (gzip) header error: bad gzip magic bytes b'\x00\x00' |
