"""命令行校验工具: python3 -m vxl.check <file.vxb>"""

import sys

from .format import VXLError
from .reader import Mesh


def main(argv=None):
    argv = sys.argv if argv is None else argv
    if len(argv) != 2:
        print("usage: python3 -m vxl.check <file.vxb>", file=sys.stderr)
        return 2
    path = argv[1]
    with open(path, "rb") as fh:
        data = fh.read()
    try:
        mesh = Mesh(data, strict_indices=False)
    except VXLError as exc:
        print("INVALID %s" % path)
        print("  %s: %s" % (type(exc).__name__, exc))
        return 1
    errors = mesh.find_index_errors()
    if errors:
        print("INVALID %s" % path)
        for exc in errors:
            print("  %s: %s" % (type(exc).__name__, exc))
        return 1
    print("OK %s" % path)
    print("  vertices=%d indices=%d primitive=%s"
          % (mesh.vertex_count, mesh.index_count, mesh.primitive_name))
    for info in mesh.attributes:
        print("  attribute %-12s layout=%-11s components=%d dtype=%s "
              "stride=%d offset=%d"
              % (info.name, info.layout_name, info.components,
                 info.dtype_name, info.stride, info.offset))
    return 0


if __name__ == "__main__":
    sys.exit(main())
