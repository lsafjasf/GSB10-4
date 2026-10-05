"""Content-Encoding / Accept-Encoding negotiation (stdlib only).

Implements the parts of RFC 9110 (HTTP Semantics) sections 8.4 and 12.5.3
needed by a client that wants to advertise decodable codings and by a
server that wants to pick one coding chain for a response.
"""

from .errors import HeaderParseError, NegotiationError

#: Canonical supported codings. Aliases are normalised to these names.
SUPPORTED = ("identity", "gzip", "deflate")

_ALIASES = {
    "x-gzip": "gzip",
}

#: Codings we can decode, in preference order, used to build
#: Accept-Encoding headers and as the server's default preference.
DECODABLE = ("gzip", "deflate", "identity")


def normalize_token(token):
    """Lower-case a coding token and resolve aliases."""
    t = token.strip().lower()
    return _ALIASES.get(t, t)


def parse_content_encoding(value):
    """Parse a Content-Encoding header value into an ordered token list.

    The returned list is in *declaration order*: the first element is the
    outermost coding (the one that must be removed first when decoding).
    Tokens are normalised (lower-cased, aliases resolved) but NOT checked
    against the supported set; that is the decoder's job so it can report
    the exact layer of an unknown coding.
    """
    if value is None:
        return []
    tokens = []
    for raw in value.split(","):
        tok = raw.strip()
        if not tok:
            raise HeaderParseError(
                "malformed Content-Encoding %r: empty token" % (value,)
            )
        if any(c in tok for c in ' \t;="'):
            raise HeaderParseError(
                "malformed Content-Encoding %r: bad token %r" % (value, tok)
            )
        tokens.append(normalize_token(tok))
    return tokens


def parse_accept_encoding(value):
    """Parse an Accept-Encoding header into {token: qvalue}.

    Tokens are normalised. A syntactically invalid qvalue raises
    HeaderParseError (strict mode: better to reject than to guess).
    """
    result = {}
    if value is None:
        return result
    for item in value.split(","):
        item = item.strip()
        if not item:
            continue
        parts = item.split(";")
        token = normalize_token(parts[0])
        if not token:
            raise HeaderParseError(
                "malformed Accept-Encoding %r: empty token" % (value,)
            )
        q = 1.0
        for param in parts[1:]:
            param = param.strip()
            if not param:
                raise HeaderParseError(
                    "malformed Accept-Encoding %r: empty parameter"
                    % (value,)
                )
            if "=" not in param:
                raise HeaderParseError(
                    "malformed Accept-Encoding %r: bad parameter %r"
                    % (value, param)
                )
            name, _, pval = param.partition("=")
            if name.strip().lower() == "q":
                try:
                    q = float(pval.strip())
                except ValueError:
                    raise HeaderParseError(
                        "malformed Accept-Encoding %r: bad qvalue %r"
                        % (value, pval)
                    ) from None
                if not 0.0 <= q <= 1.0:
                    raise HeaderParseError(
                        "malformed Accept-Encoding %r: qvalue %r out of "
                        "range" % (value, pval)
                    )
        result[token] = q
    return result


def build_accept_header():
    """Accept-Encoding header value advertising everything we can decode."""
    return ", ".join(DECODABLE)


def negotiate(accept_encoding, supported=DECODABLE):
    """Pick the best mutually acceptable coding.

    accept_encoding: raw Accept-Encoding header value (may be None).
    supported: iterable of codings the sender can produce, in the
        sender's preference order.

    Returns the chosen coding token. Raises NegotiationError (HTTP 406
    territory) when nothing is mutually acceptable.

    Rules (RFC 9110 12.5.3):
      * absent header            -> only identity is acceptable
      * identity is acceptable unless explicitly refused with q=0
        (and not rescued by a wildcard)
      * '*' matches any coding not explicitly listed
      * ranking: highest qvalue wins; ties break towards explicitly
        named codings, then wildcard matches, then the implicit
        identity default, then sender preference order
    """
    supported = [normalize_token(s) for s in supported]
    if accept_encoding is None or not accept_encoding.strip():
        if "identity" in supported:
            return "identity"
        raise NegotiationError(
            "no Accept-Encoding given and identity is not supported"
        )

    accepted = parse_accept_encoding(accept_encoding)
    star = accepted.get("*")

    def rank_for(coding):
        # (qvalue, specificity): named > wildcard > implicit identity
        if coding in accepted:
            return accepted[coding], 2
        if star is not None:
            return star, 1
        if coding == "identity":
            return 1.0, 0
        return 0.0, 0

    best = None
    best_rank = (0.0, -1)
    for coding in supported:
        rank = rank_for(coding)
        if rank[0] > 0 and rank > best_rank:
            best = coding
            best_rank = rank
    if best is None:
        raise NegotiationError(
            "no mutually acceptable content coding for Accept-Encoding %r"
            % (accept_encoding,)
        )
    return best
