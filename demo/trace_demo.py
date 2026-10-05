"""重连序列下的状态轨迹演示。运行：python3 demo/trace_demo.py"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from reconnect import Client, FullReplay, ReconnectDropped, Resume, Server


def tag(decision):
    if isinstance(decision, Resume):
        return f"RESUME  +{len(decision.events)} events"
    return f"FULL    ({decision.reason})"


def main():
    server = Server()
    client = Client()
    print(f"{'step':<5}{'action':<34}{'decision':<26}"
          f"{'cli.seq':<8}{'cli.digest':<12}{'cli.state'}")
    print("-" * 110)
    step = 0

    def show(action, decision, note=""):
        nonlocal step
        step += 1
        cp = client.checkpoint
        print(f"{step:<5}{action:<34}{tag(decision):<26}{cp.seq:<8}"
              f"{cp.digest[:10]:<12}{client.state}{('  ' + note) if note else ''}")

    def show_drop(action, exc):
        nonlocal step
        step += 1
        cp = client.checkpoint
        print(f"{step:<5}{action:<34}{'DROP    ' + f'{exc.delivered}/{exc.total} delivered':<26}"
              f"{cp.seq:<8}{cp.digest[:10]:<12}{client.state}  (未提交, 位点不动)")

    # 1) 初始全量
    for i in range(5):
        server.publish("inc", "a", 1)
    show("server: 5 events; client connect #1", client.sync(server))

    # 2) 正常续传
    for i in range(3):
        server.publish("inc", "a", 1)
        server.publish("set", "b", i)
    show("server: +6 events; reconnect #2", client.sync(server))

    # 3) 重连期间再次断开（30 条只到 4 条），随后重试成功
    for i in range(15):
        server.publish("inc", "a", 2)
    try:
        client.sync(server, deliver=4)
    except ReconnectDropped as e:
        show_drop("reconnect #3: 15 events, dropped mid-batch", e)
    show("reconnect #4: retry", client.sync(server))

    # 4) 位点过期 -> 被迫全量
    for i in range(20):
        server.publish("inc", "c", 1)
    server.compact(keep_last=5)
    show("server: compact(keep 5); reconnect #5", client.sync(server))

    # 5) 版本更替 -> 被迫全量
    server.rotate_version()
    for i in range(2):
        server.publish("set", "mode", "v2")
    show("server: rotate_version; reconnect #6", client.sync(server))

    # 6) 交错重连：第二个客户端插入，互不影响
    other = Client()
    d = other.sync(server)
    print(f"  ... other client first connect: {tag(d)}, state == server: {other.state == server.state}")
    for i in range(4):
        server.publish("inc", "a", 1)
    show("reconnect #7 (interleaved w/ other)", client.sync(server))
    d = other.sync(server)
    print(f"  ... other client reconnect:     {tag(d)}, state == server: {other.state == server.state}")

    ok = client.state == server.state == other.state
    print("-" * 110)
    print(f"final: client == server == other -> {ok}")
    sys.exit(0 if ok else 1)


if __name__ == "__main__":
    main()
