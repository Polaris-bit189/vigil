"""基准采集：把「性能」变成**可复现的数字**，而不是印象。

存在的理由：`BENCHMARK.md` 要写性能数据，而**性能数据必须实测**。
凭印象写「排期很快」在材料里毫无价值，且经不起组委会复现 —— 他们会跑，
而一句"很快"没有任何可对照的东西。

⚠️ **本脚本零外部依赖、零网络、零模型**：基准要能在任何一台机器上
离线跑出结果，否则它自己的结论就没法被复现（而那正是它要证明的东西）。

⚠️ **每条数据都带采集环境**（Python / 平台 / CPU / 核数）：
离开环境的性能数字是没有意义的 —— 同一份代码在 8 核与 24 核上差一个量级，
而读者无从分辨哪个数才对应他那台机器。

用法：
    python scripts/bench.py            # 人读格式
    python scripts/bench.py --json     # 机器可读（给 BENCHMARK.md 抄数）
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import platform
import sys
import time
from pathlib import Path

# 让脚本能从仓库根导入 vigil（scripts/ 是子目录）
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from vigil import conflicts, logs, schedule  # noqa: E402
from vigil.timetable import Course, Timetable  # noqa: E402

# ⚠️ **重复次数**（取最小值，不是平均值）：基准测的是"这个算法最快能多快"，
# 而首次运行的冷启动开销（import、页错误、CPU 频率爬升）只会**拖慢**它。
# 最小值过滤掉这些噪声，比平均数更稳定 —— 平均数会被一次 GC 或调度抖动
# 拉高，而那种抖动在任何机器上都会发生。
REPEATS = 5

# 规模梯度。⚠️ 直到 500 是因为**真实使用量级是几十条**（一屏 20 条、
# 一天几十条通知）—— 测到 500 只是为了看见 O(n²) 的拐点在哪，
# 而不是因为用户会有 500 条待办。
SIZES = (10, 50, 100, 500)


def _env() -> dict:
    """采集环境。**每条基准都必须带它**，否则数字不可比。"""
    return {
        "python": sys.version.split()[0],
        "platform": platform.platform(),
        "cpu": platform.processor() or "(未报告)",
        "cores": os.cpu_count(),
        "collected_at": dt.datetime.now().isoformat(timespec="seconds"),
    }


def _best(fn, repeats: int = REPEATS) -> float:
    """跑 `repeats` 次取**最小值**（毫秒）。见 `REPEATS` 的说明。"""
    best = float("inf")
    for _ in range(repeats):
        t0 = time.perf_counter()
        fn()
        best = min(best, time.perf_counter() - t0)
    return best * 1000


# ── 基准：排期 ────────────────────────────────────────────────


def _tt() -> Timetable:
    """一份典型课表：一周 5 节课，作息 08:00–22:00。"""
    return Timetable(
        courses=(
            Course(weekday=0, start=8 * 60, end=9 * 60 + 40, label="高等数学"),
            Course(weekday=1, start=14 * 60, end=15 * 60 + 40, label="大学英语"),
            Course(weekday=2, start=10 * 60, end=11 * 60 + 40, label="数据结构"),
            Course(weekday=3, start=14 * 60, end=15 * 60 + 40, label="体育"),
            Course(weekday=4, start=8 * 60, end=9 * 60 + 40, label="操作系统"),
        )
    )


def _items(n: int, *, start_day: int = 2026 * 10000 + 1001) -> list:
    """n 条待排条目，死线**均匀铺开**在 30 天里。

    ⚠️ 死线必须铺开而不是全挤一天：全挤一天的话，绝大部分会因"排不下"
    提前返回，那条曲线测的就不是排期算法，而是失败路径。
    """
    base = dt.date(2026, 10, 1)
    out = []
    for i in range(n):
        d = base + dt.timedelta(days=i % 30)
        ts = int(dt.datetime(d.year, d.month, d.day).timestamp())
        out.append(schedule.SchedulableItem(
            item_id=i, title=f"待办{i}", deadline_ts=ts, duration_min=30,
        ))
    return out


def bench_schedule() -> list[dict]:
    """排期算法在不同待办量下的耗时。"""
    out = []
    wf = int(dt.datetime(2026, 10, 1).timestamp())
    wt = int(dt.datetime(2026, 12, 1).timestamp())
    for n in SIZES:
        items = _items(n)
        tt = _tt()
        ms = _best(lambda: schedule.plan_schedule(
            items, tt, window_from=wf, window_to=wt
        ))
        out.append({"name": f"plan_schedule(n={n})", "n": n, "ms": round(ms, 3)})
    return out


# ── 基准：冲突检测 ────────────────────────────────────────────


def bench_conflicts() -> list[dict]:
    """冲突检测（**O(n²) 两两求交**）在不同条目量下的耗时。

    ⚠️ 这一组是**唯一可能真的慢**的地方 —— `find_conflicts` 是 O(n²)，
    而代码注释里写着「同一天十几条，真到上千条再换」。
    这条基准就是**验证那个判断**的依据：如果没有拐点，注释里的取舍成立；
    如果 500 条就要几百毫秒，那注释该改。
    """
    out = []
    wf = int(dt.datetime(2026, 10, 1).timestamp())
    for n in SIZES:
        entries = [
            conflicts.Entry(
                label=f"活动{i}",
                start=wf + i * 3600,
                end=wf + i * 3600 + 5400,
                item_id=i,
                kind="activity",
            )
            for i in range(n)
        ]
        ms = _best(lambda: conflicts.find_conflicts(entries))
        out.append({"name": f"find_conflicts(n={n})", "n": n, "ms": round(ms, 3)})
    return out


def bench_duplicates() -> list[dict]:
    """重复条目检测（按 start_ts 分桶 + 桶内两两比标题）。"""
    out = []
    base = int(dt.datetime(2026, 10, 15, 19).timestamp())
    for n in SIZES:
        # ⚠️ 刻意让**一半**条目落在同一个时刻（真实场景：同一件事被多条消息抽到）
        rows = [
            (i, f"今晚七点老地方集合{i % 3}", base if i % 2 == 0 else base + i * 60)
            for i in range(n)
        ]
        ms = _best(lambda: conflicts.find_duplicate_groups(rows))
        out.append({"name": f"find_duplicate_groups(n={n})", "n": n, "ms": round(ms, 3)})
    return out


# ── 基准：空闲块展开 ──────────────────────────────────────────


def bench_free_slots() -> list[dict]:
    """课表展开 + 空闲块计算（按天迭代，窗口越长线性增长）。"""
    out = []
    for days in (7, 30, 90, 365):
        wf = int(dt.datetime(2026, 10, 1).timestamp())
        wt = wf + days * 86400
        tt = _tt()
        busy = schedule.busy_from_timetable(tt, window_from=wf, window_to=wt)
        ms = _best(lambda: schedule.free_slots(
            tt, busy, window_from=wf, window_to=wt
        ))
        out.append({"name": f"free_slots(days={days})", "days": days, "ms": round(ms, 3)})
    return out


# ── 汇总 ──────────────────────────────────────────────────────


def collect() -> dict:
    """跑全部基准，返回结构化结果。"""
    return {
        "env": _env(),
        "repeats": REPEATS,
        "benchmarks": {
            "schedule": bench_schedule(),
            "conflicts": bench_conflicts(),
            "duplicates": bench_duplicates(),
            "free_slots": bench_free_slots(),
        },
    }


def _fmt(report: dict) -> str:
    e = report["env"]
    lines = [
        "VIGIL 基准数据",
        "=" * 62,
        f"  Python {e['python']}  |  {e['cores']} 核",
        f"  {e['platform']}",
        f"  采集时刻：{e['collected_at']}",
        f"  （每组跑 {report['repeats']} 次，取**最小值**——见脚本里 REPEATS 的说明）",
        "",
    ]
    titles = {
        "schedule": "排期：把 N 条待办排进课表之外的空闲块",
        "conflicts": "冲突检测：N 个条目两两求交（**O(n²)**）",
        "duplicates": "重复检测：N 条按时刻分桶 + 桶内比标题",
        "free_slots": "空闲块展开：课表展开成具体日期并求差集",
    }
    for key, rows in report["benchmarks"].items():
        lines.append(f"── {titles[key]} " + "─" * max(0, 54 - len(titles[key])))
        for r in rows:
            lines.append(f"   {r['name']:<34} {r['ms']:>9.3f} ms")
        lines.append("")
    return "\n".join(lines)


def main() -> int:
    # ⚠️ 中文 Windows 的控制台是 GBK，而本脚本输出里有 `²` 这类编不出的字符
    # ——实测在这里崩过（`UnicodeEncodeError: 'gbk' codec can't encode '²'`）。
    # 复用 `logs.harden_streams`（`cli.py` 用的是同一份），不在这里重写。
    logs.harden_streams()

    ap = argparse.ArgumentParser(description="VIGIL 基准采集")
    ap.add_argument("--json", action="store_true", help="输出 JSON（给人读以外的用法）")
    args = ap.parse_args()

    report = collect()
    if args.json:
        print(json.dumps(report, ensure_ascii=False, indent=2))
    else:
        print(_fmt(report))
    return 0


if __name__ == "__main__":
    sys.exit(main())
