"""Byte-level diff report demo. Run: python3 demo_diff.py"""

import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import cfgrewrite
from cfgrewrite import byte_diff, set_value

SAMPLE = b"""\
# demo config -- comments, blanks and indentation must survive
host = localhost   # primary host
port = 8080

[server]
# workers = 4        <- commented-out key, must NOT be touched
workers = 8
paths = /var/a \\
        /var/b \\
        /var/c     # search path
"""

def show(data):
    return repr(data) if len(data) <= 60 else repr(data[:57]) + "..."

def report(title, before, after):
    regions, equal = byte_diff(before, after)
    print("== %s ==" % title)
    print("  bytes: %d -> %d | equal bytes: %d | changed regions: %d"
          % (len(before), len(after), equal, len(regions)))
    for r in regions:
        print("  [%s] old[%d:%d] %s" % (r["op"], r["old_start"], r["old_end"],
                                        show(r["old_bytes"])))
        print("       new[%d:%d] %s" % (r["new_start"], r["new_end"],
                                        show(r["new_bytes"])))
    if not regions:
        print("  files are byte-identical")
    print()

def main():
    tmpdir = tempfile.mkdtemp(prefix="cfgrewrite-demo-")
    path = os.path.join(tmpdir, "demo.conf")
    with open(path, "wb") as fh:
        fh.write(SAMPLE)
    before = open(path, "rb").read()

    print("### pass 1: change host, workers, paths\n")
    set_value(path, "host", "example.com")
    set_value(path, "workers", "16", section="server")
    set_value(path, "paths", "/single", section="server")
    after1 = open(path, "rb").read()
    report("pass 1 (three keys changed)", before, after1)

    print("### pass 2: identical operations again (idempotency check)\n")
    changed = [set_value(path, "host", "example.com"),
               set_value(path, "workers", "16", section="server"),
               set_value(path, "paths", "/single", section="server")]
    after2 = open(path, "rb").read()
    print("  set_value() returned:", changed, "(False = file not even rewritten)")
    report("pass 2 (must be zero differences)", after1, after2)

    print("### resulting file\n")
    sys.stdout.write("".join("  " + line + "\n"
                             for line in after2.decode().splitlines()))

if __name__ == "__main__":
    main()
