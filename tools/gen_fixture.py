"""Deterministic fixture builders shared by tests, diff-check and bench."""

import os
import random

VALID_PNG = (b"\x89PNG\r\n\x1a\n" + (13).to_bytes(4, "big") + b"IHDR"
             + b"\x00" * 13 + b"\x00" * 4)
VALID_SVG = b'<svg xmlns="http://www.w3.org/2000/svg" width="16" height="16"></svg>\n'
CORRUPT_DATA = b"this is not a valid icon file\n"


def _write(root, rel, data):
    path = os.path.join(root, rel)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "wb") as fh:
        fh.write(data)


def build_diff_fixture(base):
    """Build the hand-verified fixture; return the icon search-path dir."""
    icons = os.path.join(base, "icons")
    m = "maintheme"
    _write(icons, f"{m}/index.theme",
           b"[Icon Theme]\nName=Main Theme\nInherits=parenttheme,ghosttheme\n")
    _write(icons, f"{m}/16x16/apps/exact16.png", VALID_PNG)
    _write(icons, f"{m}/16x16/apps/multi.png", VALID_PNG)
    _write(icons, f"{m}/16x16/apps/onlylow.png", VALID_PNG)
    _write(icons, f"{m}/32x32/apps/broken.png", CORRUPT_DATA)
    _write(icons, f"{m}/32x32/apps/allbroken.png", CORRUPT_DATA)
    _write(icons, f"{m}/32x32/apps/caseext.PNG", VALID_PNG)
    _write(icons, f"{m}/32x32/apps/both.png", VALID_PNG)
    _write(icons, f"{m}/32x32/apps/both.PNG", VALID_PNG + b"upper")
    _write(icons, f"{m}/32x32/apps/prio.png", VALID_PNG)
    _write(icons, f"{m}/32x32/apps/prio.svg", VALID_SVG)
    _write(icons, f"{m}/32x32/Apps/wrongcase.png", VALID_PNG)
    _write(icons, f"{m}/48x48/apps/multi.png", VALID_PNG)
    _write(icons, f"{m}/48x48/apps/broken.png", VALID_PNG)
    _write(icons, f"{m}/48x48/apps/onlyhigh.png", VALID_PNG)
    _write(icons, f"{m}/48X48/apps/casedir.png", VALID_PNG)
    _write(icons, f"{m}/scalable/apps/vec.svg", VALID_SVG)
    _write(icons, f"{m}/scalable/apps/allbroken.svg", CORRUPT_DATA)
    _write(icons, f"{m}/rooticon.png", VALID_PNG)
    _write(icons, "parenttheme/index.theme", b"[Icon Theme]\nName=Parent\n")
    _write(icons, "parenttheme/32x32/apps/fromparent.png", VALID_PNG)
    _write(icons, "hicolor/32x32/apps/fallback.png", VALID_PNG)
    return icons


def build_bench_fixture(base, n_icons=5000, seed=20261005):
    """Build a large deterministic theme set; return (icons_dir, queries)."""
    rng = random.Random(seed)
    icons = os.path.join(base, "bench_icons")
    sizes = [16, 24, 32, 48, 64, 128, 256]
    contexts = ["apps", "actions", "places", "mimetypes"]
    _write(icons, "benchtheme/index.theme",
           b"[Icon Theme]\nName=Bench\nInherits=benchparent\n")
    _write(icons, "benchparent/index.theme",
           b"[Icon Theme]\nName=BenchParent\n")
    queries = []
    for i in range(n_icons):
        name = f"icon{i:04d}"
        ctx = contexts[i % len(contexts)]
        qsize = sizes[rng.randrange(len(sizes))]
        roll = rng.random()
        if roll < 0.55:
            for s in rng.sample(sizes, k=rng.randint(1, 3)):
                _write(icons, f"benchtheme/{s}x{s}/{ctx}/{name}.png", VALID_PNG)
        elif roll < 0.65:
            _write(icons, f"benchtheme/scalable/{ctx}/{name}.svg", VALID_SVG)
        elif roll < 0.72:
            _write(icons, f"benchtheme/{name}.png", VALID_PNG)
        elif roll < 0.79:
            _write(icons, f"benchparent/32x32/{ctx}/{name}.png", VALID_PNG)
        elif roll < 0.86:
            _write(icons, f"hicolor/48x48/{ctx}/{name}.png", VALID_PNG)
        elif roll < 0.93:
            _write(icons, f"benchtheme/{qsize}x{qsize}/{ctx}/{name}.png",
                   CORRUPT_DATA)
        queries.append((name, qsize, ctx))
    return icons, queries


if __name__ == "__main__":
    import sys
    dest = sys.argv[1] if len(sys.argv) > 1 else "fixtures"
    icons = build_diff_fixture(dest)
    print(f"diff fixture written under {icons}")
