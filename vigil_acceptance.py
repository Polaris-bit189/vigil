"""M1 出口验收的抽样工具 —— 生成便于人工判读的核对表。

不是产品代码，是验收证据的生成器。输出两样东西：

1. **分类准确率抽验**：随机抽 N 条 item，每条附源消息原文，你逐条判断
   「类别对不对」。这是 spec 定的 M1 出口标准之一。
2. **可回溯性核验**：随机抽 M 条，机械化核对「来源内容里是否真的含有
   item 的依据」，标出可疑项。

⚠️ **它是仪器，判据坏了会误导里程碑验收**（M6 终审 F3，Important）。
`vigil_exit_check.py` 的末尾明确指引用户跑它（`--n 100 --m 20 > 新抽样.md`），
而 M1 那个「95/100」的出口材料就是它出的 ⇒ 它印出来的**每一个字都是证据**。
所以在 M6 T5 把「一个条目的类目是什么」改成**有效类目**（overlay 覆盖 +
两级别名）之后，本脚本必须跟着走，口径与 `store` 的读路径**同源**：

  · 类目走 `store._EFFECTIVE_KIND`（+ `store._ITEM_JOINS` 的两个别名 JOIN）
    —— 否则改名之后它会印出一个**已经不存在的**类目名（界面上同一刻显示的
    是新名字），手工改过类目的条目同理；
  · **软删的条目不进抽样**（`store._NOT_DELETED`，D15：软删条目在**所有**
    读取路径上都应当消失）—— 否则人工会对着一条界面上根本不存在的条目
    判「这条类别对不对」；
  · 中文名**现读** `config/categories.toml`（`load_categories()`），不再硬编码
    M1 那七个 slug 的名字 —— 用户改名/新增/删除类目之后，硬编码表立刻过期。

⚠️ 三条边界如实写在这里（与 `vigil_exit_check.py` 同族）：

  · 脚本**只读**主库（`file:...?mode=ro` + `uri=True`：URI 形式只在连接带
    `SQLITE_OPEN_URI` 时才被解析）；
  · overlay 库**不存在时会被就地建一个空的**（`store._ensure_overlay` →
    `attach_readonly` 的既定语义，读取路径必须能在"从没写过干预"的库上工作）
    ——**这一点与 `vigil_exit_check.py` 刻意不同**（那里"库不存在 ⇒ 退化"），
    如实记在这里；
  · 读取路径会顺带做一次 `overrides.reconcile_if_stale()`（把 `kind_alias`
    对齐到当前 slug 集），所以若 `categories.toml` 被手改过，本脚本会
    **顺带对平** overlay —— 与任何一次 `/api/items` 请求做的事一模一样。

跑法（仓库根）：
    PYTHONIOENCODING=utf-8 .venv/Scripts/python.exe vigil_acceptance.py
    PYTHONIOENCODING=utf-8 .venv/Scripts/python.exe vigil_acceptance.py --n 100 --m 20
"""

from __future__ import annotations

import argparse
import datetime as dt
import random
import re
import sqlite3
import sys

from vigil import store
from vigil.categories import load_categories

DB = "data/vigil.db"

# 归一化：剥空白与常见中英文标点，用于「标题是否能在来源里找到依据」的机械核对
_PUNCT = re.compile(r"[\s，。！？、：；「」『』【】（）()\[\]…~～\-—]+")


def _norm(s: str) -> str:
    return _PUNCT.sub("", s or "")


def kind_labels() -> dict[str, str]:
    """`slug → 中文名`，**每次现读** `config/categories.toml`（M6 终审 F3）。

    ⚠️ 从前是一张**硬编码**的 M1 七类表：用户改名（`life → life_v2`）之后，
    这个脚本会给一条 `life_v2` 的条目印出「生活」——一个**已经不存在的**
    类目名，而网页上同一刻显示的是新名字。手工改过类目的条目同理。

    ⚠️ 读不出来时**不要吞**：`load_categories` 抛 `CategoryError` 是响亮的，
    而人工验收看到一条"类目表读不出来"的报错，比看到一份**印着假类目名**的
    抽样材料安全得多（后者会被当成证据写进里程碑出口）。
    """
    return {c.slug: c.label for c in load_categories()}


def load(db: str):
    """抽样的行：**有效类目 + 排除软删**，与 `store` 的读路径同源。

    ⚠️ **用 `store` 的常量而不是自己抄一遍**（M6 终审 F3）：`_EFFECTIVE_KIND`
    与 `_NOT_DELETED` 都是**唯一表达式**（T5 把四处手抄收敛成一处），
    `_ITEM_JOINS` 里那两条 `kind_alias` LEFT JOIN 是前者的**前提**——
    少了它们 SQLite 报 `no such column: ka.new_kind`，是**响亮**的失败
    （刻意的：静默的那一半——"某个消费点忘了改"——才是真正危险的）。

    ⚠️ 这里**没有**复用 `store` 的查询函数（`window_items` / `search_items`）：
    它们的返回值里没有源消息原文，而"每条附源消息原文"正是本脚本的产物。
    所以复用**常量**（同一套口径），而不是复用整个查询。
    """
    # `_ensure_overlay` 做两件事：把 overlay 挂成 `ov`（本查询的 `LEFT JOIN ov.…`
    # 靠它）+ 触发一次对账。**每个走 `ov` 的查询入口都必须调它。**
    # ⚠️ 连接必须带 `uri=True`：`attach_readonly` 挂的是 `file:...?mode=ro`，
    # 而这个 URI 形式只在连接带 `SQLITE_OPEN_URI` 时才被解析（否则报
    # `unable to open database: file:...`，一个指向错误方向的诊断）。
    conn = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
    conn.text_factory = lambda b: b.decode("utf-8", "replace")
    store._ensure_overlay(conn)
    rows = conn.execute(
        f"SELECT i.item_id, {store._EFFECTIVE_KIND} AS kind, i.title, i.detail,"
        " i.deadline_ts, i.place, i.amount, i.confidence, i.group_id, m.content, m.ts"
        f"{store._ITEM_JOINS}"
        " JOIN item_sources s ON s.item_id = i.item_id"
        " JOIN messages   m ON m.msg_id  = s.msg_id"
        f" WHERE {store._NOT_DELETED}"
        " ORDER BY i.item_id"
    ).fetchall()
    conn.close()
    return rows


def overlap(title: str, src: str) -> float:
    """标题字符有多大比例能在来源里找到（粗指标，仅供标可疑，不作判据）。"""
    t = _norm(title)
    if not t:
        return 0.0
    s = _norm(src)
    return sum(1 for ch in t if ch in s) / len(t)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--n", type=int, default=100, help="分类抽验条数")
    ap.add_argument("--m", type=int, default=20, help="可回溯核验条数")
    ap.add_argument("--seed", type=int, default=20260914, help="随机种子（可复现）")
    args = ap.parse_args()

    rows = load(DB)
    if not rows:
        print("items 表为空（或全被软删）——先跑 vigil refine")
        return 1

    # ⚠️ 类目名现读（F3）：改名/新增/删除之后，这里印的就是**当前**的中文名。
    labels = kind_labels()

    random.seed(args.seed)
    total = len(rows)
    print("=" * 92)
    # ⚠️ 这个数**只数可见条目**（排除了软删）——写成"items 共 N 条"会与
    # `vigil_exit_check.py` 印的 `COUNT(*) FROM items`（含软删）**对不上**，
    # 而两个仪器各印一个数、没人说清哪个是哪个，正是 R13 那一类自相矛盾。
    print(f"M1 出口验收抽样　｜　可见 items 共 {total:,} 条（已排除软删）　｜　种子 {args.seed}")
    print("=" * 92)

    # ── 第一部分：分类准确率抽验 ──
    n = min(args.n, total)
    print(f"\n【一】分类准确率抽验　随机 {n} 条")
    print("请逐条判断：**【】里的类别对不对**（对 = ✓，错 = 写下应该是什么类）")
    print("-" * 92)
    for idx, r in enumerate(random.sample(rows, n), 1):
        iid, kind, title, detail, dl, place, amount, conf, gid, src, ts = r
        when = dt.datetime.fromtimestamp(ts).strftime("%m-%d %H:%M") if ts else "-"
        dls = dt.datetime.fromtimestamp(dl).strftime("%Y-%m-%d") if dl else "—"
        print(f"\n{idx:>3}. 【{labels.get(kind, kind)}】{title}")
        extra = []
        if detail:
            extra.append(f"细节={detail}")
        if place:
            extra.append(f"地点={place}")
        if amount:
            extra.append(f"金额={amount}")
        if dl:
            extra.append(f"截止={dls}")
        if extra:
            print(f"     {' | '.join(extra)[:110]}")
        print(f"     {when}　群{gid}　置信{conf}")
        print(f"     来源: {src[:104]}")

    # ── 第二部分：可回溯性核验（机械化）──
    m = min(args.m, total)
    print("\n\n" + "=" * 92)
    print(f"【二】可回溯性核验　随机 {m} 条（机械核对：标题能否在来源里找到依据）")
    print("=" * 92)
    judged = []
    for r in random.sample(rows, m):
        iid, kind, title, _, _, _, _, _, _, src, _ = r
        judged.append((iid, title, src, overlap(title, src)))
    bad = [j for j in judged if j[3] < 0.45]
    for iid, title, src, ratio in sorted(judged, key=lambda x: x[3]):
        flag = "✓" if ratio >= 0.45 else "⚠️"
        print(f"  {flag} #{iid:<4} 重合 {ratio:>4.0%}　{title[:44]}")
        if ratio < 0.45:
            print(f"           来源: {src[:80]}")
    print(f"\n  ⚠️ 重合 <45% 的：{len(bad)} / {m}（该指标偏保守，需人工确认是否为真错配）")

    # ── 汇总 ──
    print("\n\n" + "=" * 92)
    print("【三】M1 出口标准对照")
    print("=" * 92)
    print(f"  1. 分类准确率 ≥ 80%　　　　→ 由第一部分人工判定：____ / {n}")
    print(f"  2. 可回溯且内容对得上　　　→ 由第二部分核验：{m - len(bad)} / {m} 机械通过")
    print(f"  3. items 条数（可见、已排除软删）　→ {total:,}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
