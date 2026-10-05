"""对拍 (golden-file differential check).

Each fixture/messages/*.eml is parsed standalone; fixtures/archive.mbox is
parsed with mailsplit.split; the per-message header fields (name + unfolded
value) and raw body bytes must be identical between the two parses.
"""

import glob
import os

import mailsplit

HERE = os.path.dirname(os.path.abspath(__file__))
MSG_DIR = os.path.join(HERE, "fixtures", "messages")
ARCHIVE = os.path.join(HERE, "fixtures", "archive.mbox")

# Messages whose body is intentionally not decodable under its declared
# charset; mailsplit must *report* the mismatch instead of guessing.
EXPECT_CHARSET_ERROR = {"07_charset_mismatch.eml"}


def parse_single(path):
    with open(path, "rb") as fh:
        return mailsplit.parse_one(fh)


def main() -> int:
    singles = [(os.path.basename(p), parse_single(p))
               for p in sorted(glob.glob(os.path.join(MSG_DIR, "*.eml")))]
    with open(ARCHIVE, "rb") as fh:
        archive_msgs = list(mailsplit.split(fh))

    rows = []
    ok = True
    if len(singles) != len(archive_msgs):
        print(f"COUNT MISMATCH: singles={len(singles)} archive={len(archive_msgs)}")
        return 1

    for (name, single), arch in zip(singles, archive_msgs):
        h_single = single.headers()
        h_arch = arch.headers()
        headers_ok = h_single == h_arch
        body_ok = single.body() == arch.body()
        single_sep_ok = single.separator is None
        sep_ok = arch.separator is not None and arch.separator.startswith(b"From ")

        cs_ok = True
        cs_note = "decoded"
        if name in EXPECT_CHARSET_ERROR:
            try:
                single.body_text()
                cs_ok = False
                cs_note = "MISMATCH NOT REPORTED"
            except mailsplit.CharsetError as exc:
                cs_note = f"reported: {str(exc).split(';')[0]}"
        else:
            single.body_text()  # must decode strictly without loss

        passed = headers_ok and body_ok and sep_ok and single_sep_ok and cs_ok
        ok = ok and passed
        rows.append((
            "PASS" if passed else "FAIL",
            name,
            f"headers={len(h_arch)}" + ("" if headers_ok else " DIFF"),
            f"body={len(arch.body())}B" + ("" if body_ok else " DIFF"),
            f"sep={'y' if sep_ok else 'N'}",
            cs_note,
            "warnings=" + (";".join(arch.warnings) if arch.warnings else "-"),
        ))

    width_name = max(len(r[1]) for r in rows)
    print(f"{'RES':4} {'FILE':<{width_name}}  DETAIL")
    for res, name, *details in rows:
        print(f"{res:4} {name:<{width_name}}  " + " | ".join(details))
    print()
    print(f"archive separators parsed: {len(archive_msgs)} / "
          f"single files: {len(singles)}")
    print("ALL FIELDS MATCH" if ok else "DIFFERENCES FOUND")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
