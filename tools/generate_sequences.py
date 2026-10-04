"""Generate budget-consumption sequences for the README/data directory.

Runs the real library for the correct ("gated") scenarios and an explicit
arithmetic simulation of the common wrong ("fresh local timeout per attempt")
approach. Pure standard library, deterministic, zero real-time sleeping.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from timeout_budget import (  # noqa: E402
    Budget,
    EventType,
    Outcome,
    Stage,
    SliceTimeout,
    VirtualClock,
    run,
)

DATA = ROOT / "data"


def timed_out(clock):
    def handler(ctx):
        clock.advance(ctx.slice_ticks)
        raise SliceTimeout("always times out")

    return handler


def costs_handler(clock, table):
    def handler(ctx):
        key = (ctx.stage_index, ctx.attempt)
        spec = table.get(key, table[(ctx.stage_index, 1)])
        if spec == "timeout":
            clock.advance(ctx.slice_ticks)
            raise SliceTimeout("scripted timeout")
        if spec == "timeout?":
            clock.advance(min(ctx.slice_ticks, 25))
            raise SliceTimeout("scripted timeout")
        clock.advance(spec)
        return "ok"

    return handler


def scenario(name, specs, total=100, queue=0):
    clock = VirtualClock(0)
    budget = Budget(total, clock)
    if queue:
        clock.advance(queue)
    stages = [
        Stage(
            stage_name,
            weight=w,
            max_attempts=ma,
            backoff_ticks=bo,
            handler=costs_handler(clock, table),
        )
        for stage_name, w, ma, bo, table in specs
    ]
    result = run(stages, budget)
    return name, result


def rows_from_result(result):
    rows = []
    for e in result.events:
        if e.type is EventType.QUEUE:
            kind = "queue"
        elif e.type is EventType.BACKOFF:
            kind = "backoff"
        else:
            kind = "{}-try{}".format(e.name, e.attempt)
        rows.append(
            {
                "kind": kind,
                "start": e.start,
                "duration": e.duration,
                "end": e.end,
                "detail": e.detail,
            }
        )
    return rows


def naive_local_timeouts(total, local_slice, backoff, max_attempts):
    """Wrong approach: every attempt gets a fresh local timeout.

    Returns (rows, would_exceed). ``would_exceed`` shows how far over the
    total deadline the chain would run if no global gate existed.
    """
    rows = []
    cur = 0
    for attempt in range(1, max_attempts + 1):
        rows.append(
            {
                "kind": "try{} (local {})".format(attempt, local_slice),
                "start": cur,
                "duration": local_slice,
                "end": cur + local_slice,
                "detail": "fresh local timeout, global budget ignored",
            }
        )
        cur += local_slice
        if attempt < max_attempts:
            rows.append(
                {
                    "kind": "backoff",
                    "start": cur,
                    "duration": backoff,
                    "end": cur + backoff,
                    "detail": "retry backoff",
                }
            )
            cur += backoff
    return rows, cur


def render_md(title, intro, blocks):
    out = ["# " + title, ""]
    out.append(
        "时间单位：tick（仓库统一取 1 tick = 1 ms）。start/end 为自请求进入起的"
        "绝对 tick 偏移；duration 为该项消耗。所有序列由真实库执行生成。"
    )
    out.append("")
    out.append(intro)
    out.append("")
    for block in blocks:
        out.append("## " + block["heading"])
        out.append("")
        out.append(block["text"])
        out.append("")
        if block.get("rows"):
            out.append("| # | 项 | start | duration | end(cum) | 说明 |")
            out.append("|---|---|---:|---:|---:|---|")
            cum = 0
            for i, r in enumerate(block["rows"], 1):
                cum += r["duration"]
                out.append(
                    "| {} | {} | {} | {} | {} | {} |".format(
                        i, r["kind"], r["start"], r["duration"], r["end"], r["detail"]
                    )
                )
            out.append("")
            if block.get("gated", True):
                charged = block.get("charged", min(cum, block["total"]))
                out.append(
                    "观察到的实际耗时合计：**{}**；计入预算口径（封顶 {}）：**used={}**。".format(
                        cum, block["total"], charged
                    )
                )
                if cum > block["total"]:
                    out.append("")
                    out.append(
                        "> 观察耗时超过预算 {} tick：原因是 handler 无视其切片；运行时在返回点"
                        "立即判定 `budget_exhausted`（`enforce_slice`），后续段未派发，"
                        "预算口径 used 封顶为 {}。".format(cum - block["total"], block["total"])
                    )
            else:
                out.append("累计消耗：**{}** / {}".format(cum, block["total"]))
                out.append("")
                out.append(
                    "> 没有全局闸门时链实际跑到 **{} tick**，超出全局预算 **{} tick**（{}%）。".format(
                        cum,
                        cum - block["total"],
                        round(100 * (cum - block["total"]) / block["total"], 1),
                    )
                )
            out.append("")
        for line in block.get("bullets", []):
            out.append("- " + line)
        out.append("")
    return "\n".join(out)


def main():
    blocks = []
    dump = {}

    # ---- S1: first stage always times out, no backoff --------------------
    name, r = scenario(
        "S1",
        [
            ("A", 1, 12, 0, {(0, 1): "timeout"}),
            ("B", 1, 1, 0, {(1, 1): 10}),
            ("C", 1, 1, 0, {(2, 1): 10}),
        ],
    )
    rows = rows_from_result(r)
    blocks.append(
        {
            "heading": "S1 首段持续超时（3 段等权，预算 100，无 backoff，最多 12 次）",
            "text": "每次重试只从剩余预算里按权重再切一份，序列单调收缩：33→22→15→10→6→4→3→2→1→1→1。",
            "rows": rows,
            "total": r.total,
            "charged": r.used,
            "bullets": [
                "结果：`budget_exhausted`，共派发 {} 次尝试，全部集中在首段，B/C 从未被派发。".format(
                    r.attempts_total
                ),
                "累计消耗 {}，未超过总预算，更没有翻倍（重试次数上限 12 不等于 12×33）。".format(
                    r.used
                ),
                "第 12 次需要的切片 floor(2/3)=0 < min_slice=1，派发前即判耗尽。",
            ],
        }
    )
    dump["S1"] = {"result": r.as_dict(), "rows": rows}

    # ---- S2: same but 2-tick backoff between retries ---------------------
    _, r2 = scenario(
        "S2",
        [
            ("A", 1, 12, 2, {(0, 1): "timeout"}),
            ("B", 1, 1, 0, {(1, 1): 10}),
            ("C", 1, 1, 0, {(2, 1): 10}),
        ],
    )
    rows2 = rows_from_result(r2)
    blocks.append(
        {
            "heading": "S2 首段持续超时 + 每次重试 backoff 2 tick",
            "text": "backoff 与执行尝试共用同一份预算，先扣 backoff，再从剩余里切下一片。",
            "rows": rows2,
            "total": r2.total,
            "charged": r2.used,
            "bullets": [
                "结果：`budget_exhausted`，{} 次执行尝试 + {} 次 backoff，消耗 {} / 100。".format(
                    r2.attempts_total,
                    sum(1 for e in r2.events if e.type is EventType.BACKOFF),
                    r2.used,
                ),
                "下一次 backoff(2) 需要剩余 0 tick 支撑，派发前判耗尽。",
            ],
        }
    )
    dump["S2"] = {"result": r2.as_dict(), "rows": rows2}

    # ---- S3: the wrong way (fresh local timeout per attempt) -------------
    naive_rows, naive_end = naive_local_timeouts(
        total=100, local_slice=40, backoff=5, max_attempts=3
    )
    blocks.append(
        {
            "heading": "S3 错误做法对照：每次重试都拿一份新的本地超时 40 tick（backoff 5）",
            "text": (
                "这是“指望各段各自超时兜底”的典型写法：每段调用方按总预算的 40% 配置"
                "下游超时，且重试不感知全局预算。序列如下（纯算术模拟，非本库执行）。"
            ),
            "rows": naive_rows,
            "total": 100,
            "gated": False,
            "bullets": [
                "本库对照：全局闸门在 end=100 处强制判 `budget_exhausted`，第 3 次尝试"
                "最多只拿到 10 tick 而不是 40。",
                "这就是“重试次数让总时长翻倍”的来源：3 次尝试 × 40 + 2 × 5 = 130。",
            ],
        }
    )
    dump["S3_naive_counterexample"] = {"rows": naive_rows, "would_end_at": naive_end}

    # ---- S4: retry then success ------------------------------------------
    _, r4 = scenario(
        "S4",
        [
            ("A", 1, 3, 2, {(0, 1): "timeout?", (0, 2): 10}),
            ("B", 1, 1, 0, {(1, 1): 10}),
            ("C", 1, 1, 0, {(2, 1): 15}),
        ],
    )
    blocks.append(
        {
            "heading": "S4 首段第一次超时、第二次成功（重试消耗同一份预算）",
            "text": "首次尝试超时 25 tick + backoff 2 + 重试成功 10 + B 10 + C 15。",
            "rows": rows_from_result(r4),
            "total": r4.total,
            "charged": r4.used,
            "bullets": [
                "结果：`success`，消耗 {}，剩余 {} 原样保留，不会因为重试过就吃掉后续段的额度。".format(
                    r4.used, r4.remaining
                ),
            ],
        }
    )
    dump["S4"] = {"result": r4.as_dict()}

    # ---- S5: last stage times out ----------------------------------------
    _, r5 = scenario(
        "S5",
        [
            ("A", 1, 1, 0, {(0, 1): 10}),
            ("B", 1, 1, 0, {(1, 1): 10}),
            ("C", 1, 3, 0, {(2, 1): "timeout"}),
        ],
    )
    blocks.append(
        {
            "heading": "S5 末段超时（前两段各花 10，末段独占剩余权重）",
            "text": "末段剩余权重占 100%，第一次尝试拿到全部 80 tick；用尽后第二次切片为 0，提前停止。",
            "rows": rows_from_result(r5),
            "total": r5.total,
            "charged": r5.used,
            "bullets": [
                "结果：`budget_exhausted`（发生在第 2 次派发前），共 {} 次尝试，消耗 {} / 100。".format(
                    r5.attempts_total, r5.used
                ),
            ],
        }
    )
    dump["S5"] = {"result": r5.as_dict()}

    # ---- S6: queue eats the budget before dispatch -----------------------
    _, r6 = scenario(
        "S6",
        [
            ("A", 1, 1, 0, {(0, 1): 10}),
            ("B", 1, 1, 0, {(1, 1): 10}),
        ],
        queue=95,
    )
    blocks.append(
        {
            "heading": "S6 排队先吃掉 95 tick（排队计入同一口径）",
            "text": "Budget 在入队前创建；派发前采样，剩余 5 tick 时 floor(5/2)=2 的首段切片虽然够，"
            "该段 10 tick 后返回（越过 2 tick 切片），运行时在返回点强制判耗尽，后续段无预算。",
            "rows": rows_from_result(r6),
            "total": r6.total,
            "charged": r6.used,
            "bullets": [
                "结果：`{}`，{}，共 {} 次尝试。".format(
                    r6.outcome.value, r6.reason, r6.attempts_total
                ),
            ],
        }
    )
    dump["S6"] = {"result": r6.as_dict()}

    md = render_md(
        "预算消耗序列（retry budget-consumption sequences）",
        "覆盖首段超时、首段重试后成功、末段超时、排队耗尽、全部成功，"
        "并附一个“各段各自兜底”的错误做法对照。",
        blocks,
    )
    (DATA / "consumption_sequences.md").write_text(md, encoding="utf-8")
    (DATA / "consumption_sequences.json").write_text(
        json.dumps(dump, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    print(md)
    print("\nwritten: data/consumption_sequences.md, data/consumption_sequences.json")


if __name__ == "__main__":
    main()
