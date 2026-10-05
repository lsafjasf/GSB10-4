"""生成"续传结果 vs 全量重放结果"的对拍数据 CSV。运行：python3 demo/diff_data.py"""
import csv
import random
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from reconnect import (
    Checkpoint, Client, FullReplay, ReconnectDropped, Resume,
    Server, digest_of,
)

OUT = Path(__file__).resolve().parent.parent / "data" / "diff_data.csv"


def _full_digest(server):
    fresh = Client()
    fresh.sync(server)
    return fresh.checkpoint.seq, fresh.checkpoint.digest


def run_case(name, n_before, n_after, hazard=None, drops=()):
    rng = random.Random(hash(name) & 0xFFFF)
    server = Server()
    client = Client()
    for i in range(n_before):
        server.publish(rng.choice(["inc", "set"]), f"k{i % 4}", rng.randrange(3))
    client.sync(server)
    for i in range(n_after):
        server.publish(rng.choice(["inc", "set", "del"]), f"k{i % 4}", rng.randrange(3))
    if hazard == "compact":
        server.compact(keep_last=2)
    elif hazard == "rotate":
        server.rotate_version()
    elif hazard == "lose":
        server.lose_digests()
    elif hazard == "tamper":
        client.state["k0"] = -999
        client.checkpoint = Checkpoint(
            client.checkpoint.version, client.checkpoint.seq, digest_of(client.state))
    reason, gap = "ok", 0
    for delivered in list(drops) + [None]:
        try:
            d = client.sync(server, delivered)
            reason = d.reason if isinstance(d, FullReplay) else "ok"
            gap = len(d.events) if isinstance(d, Resume) else 0
            break
        except ReconnectDropped:
            reason = "dropped-then-retried"
    full_seq, full_digest = _full_digest(server)
    return [name, n_before + n_after, client.checkpoint.seq, server.base_seq,
            reason, gap, client.checkpoint.digest, full_digest,
            str(client.checkpoint.digest == full_digest)]


def main():
    cases = [
        ("normal-resume", 50, 100, None, ()),
        ("normal-resume-small-gap", 10, 1, None, ()),
        ("normal-resume-zero-gap", 30, 0, None, ()),
        ("drop-mid-batch-once", 40, 80, None, (3,)),
        ("drop-mid-batch-4-times", 20, 200, None, (0, 1, 50, 199)),
        ("forced-version-mismatch", 60, 10, "rotate", ()),
        ("forced-seq-expired", 60, 60, "compact", ()),
        ("forced-digest-unavailable", 40, 20, "lose", ()),
        ("forced-digest-mismatch", 40, 5, "tamper", ()),
    ]
    rows = []
    for name, b, a, h, drops in cases:
        rows.append(run_case(name, b, a, h, drops))
    rng = random.Random(123)
    for t in range(21):
        rows.append(run_case(f"fuzz-{t:02d}", rng.randrange(0, 80),
                             rng.randrange(0, 80),
                             rng.choice([None, None, None, "compact", "rotate", "lose"]),
                             (rng.randrange(0, 5),) if rng.random() < 0.4 else ()))
    header = ["case", "events", "client_final_seq", "server_base_seq",
              "decision", "resumed_events", "resume_digest", "full_digest", "equal"]
    with OUT.open("w", newline="") as f:
        w = csv.writer(f)
        w.writerow(header)
        w.writerows(rows)
    assert all(r[-1] == "True" for r in rows), "存在对拍不一致!"
    print(f"wrote {len(rows)} cases -> {OUT}")
    print(f"{'case':<28}{'decision':<22}{'resumed':>8}  equal  digest(16)")
    for r in rows[:14]:
        print(f"{r[0]:<28}{r[4]:<22}{r[5]:>8}  {r[8]:<5}  {r[6][:16]}")
    print(f"... ({len(rows) - 14} more fuzz rows in CSV, all equal=True)")


if __name__ == "__main__":
    main()
