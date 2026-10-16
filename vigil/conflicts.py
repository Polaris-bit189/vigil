"""时间冲突：**三种完全不同的事，别混成一件**。

实测（2026-10-15）在真库上发现的三类，它们的**处置方向相反**：

  ① **同一件事的重复条目** —— 「今晚七点老地方集合」与「今晚19:00老地方集合」
     是同一条消息被抽了两次（或同群重复发/跨群转发）。
     处置：**合并显示**。⚠️ 给它标"冲突"是**错的叙事** ——
     用户会以为自己有两个会要参加。

  ② **用户自己的时间冲突** —— 两条**不同**的事被排到了重叠的时段。
     处置：**标出来让用户选一个**，不许静默重叠。

  ③ **活动撞课表** —— 群里的讲座正好落在你的课上。
     处置：**提示**（这是学生视角最真实的痛点）。

⚠️ **为什么必须放进同一个模块**：三者的底层判据是同一件事 ——
「两段时间有没有相交」。各写一份 `a.start < b.end and b.start < a.end`
迟早漂移，而漂移的症状（有的地方算冲突、有的不算）极难查。
相交判据只有一份，就在 `overlaps()`。

⚠️ **① 的判据必须保守**：误合并（把两个真活动并成一个）比漏合并**严重得多**
—— 用户会**漏掉一整个活动**，而漏掉的活动在界面上不留痕迹。所以只认
「start_ts 完全相同」这类硬证据，不做"时间相近就算同一件"的模糊匹配。
"""

from __future__ import annotations

import datetime as dt
import re
from dataclasses import dataclass


def overlaps(a_start: int, a_end: int, b_start: int, b_end: int) -> bool:
    """两段时间是否相交。

    ⚠️ **端点相接不算相交**：`a_end == b_start` 是"上一件事刚结束、下一件
    紧接着开始"，那**不是**冲突。写成 `<=` 会把每一个"严丝合缝排下去"的
    日程都报成冲突 —— 而那恰恰是排得好的一种形状。

    ⚠️ 半开区间 `[start, end)` 是这个模块的全局约定：排期器算空闲块时
    也用同一套（`schedule.free_slots`），两边一致才不会出现"排期说不冲突、
    冲突检测说有冲突"。
    """
    return a_start < b_end and b_start < a_end


def overlap_minutes(
    a_start: int, a_end: int, b_start: int, b_end: int
) -> int:
    """重叠了多少分钟（不相交返回 0）。给人看时要一个具体数。"""
    if not overlaps(a_start, a_end, b_start, b_end):
        return 0
    return (min(a_end, b_end) - max(a_start, b_start)) // 60


# ── ① 同一件事的重复 ─────────────────────────────────────────


@dataclass(frozen=True)
class DuplicateGroup:
    """一组指向同一件事的条目。

    ⚠️ `item_ids[0]` 是**代表**（最小的那个 item_id），其余是它的重复。
    用最小 id 而不是"最先发现的"：item_id 单调递增，所以最小 id
    就是**最早抽出来的那条**，与 `_dedupe_batch` 的"按最早的那条留下"同口径。
    """

    item_ids: tuple[int, ...]
    start_ts: int

    @property
    def representative(self) -> int:
        return self.item_ids[0]

    @property
    def extra_count(self) -> int:
        """除了代表之外还有几条重复。"""
        return len(self.item_ids) - 1


def _normalize_title(title: str) -> str:
    """标题归一化：剥掉空白与常见标点，用于包含关系比较。"""
    return "".join(ch for ch in title if ch.strip() and ch not in "，。！？、；：·-—「」【】")


# 时间表达。**这是"同一件事两种写法"的主要来源**，必须剥掉再比：
#
#   真库实测的那一对：「今晚**七点**老地方集合」/「今晚**19:00**老地方集合，请准时参加」
#   「七点」与「19:00」在字符层面毫无重合，子串判定必然失败。
#
# ⚠️ **剥时间是安全的**，因为它不承载"是不是同一件事"的信息：
# 只有 `start_ts` **完全相同**的条目才进到这一步（`find_duplicate_groups`
# 先按时间分桶），所以"时刻相同"已经被证明过了 —— 此时标题里时间的写法差异
# 只是**措辞**，不是证据。
_TIME_EXPR = re.compile(
    r"\d{1,2}\s*[:：]\s*\d{2}"                 # 19:00 / 19：00
    r"|\d{1,2}\s*[点时]\s*\d{0,2}\s*分?"      # 14点 / 14点30分 / 14时
    r"|[零一二三四五六七八九十两]{1,3}\s*[点时]\s*[半零一二三四五六七八九十]{0,3}"  # 七点 / 七点半
    r"|早上|上午|中午|下午|晚上|凌晨|夜里|今晚|明早|明晚|今天|明天|后天"
)


def _core(title: str) -> str:
    """标题的**核心**：剥掉标点与时间表达。用于"是不是同一件事"的比较。"""
    return _normalize_title(_TIME_EXPR.sub("", title))


def _cores_match(x: str, y: str) -> bool:
    """**已经是 core 形态**（剥过标点与时间）的两个串是否指向同一件事。

    ⚠️ 判据只有这一份，`titles_match` 与 `find_duplicate_groups` **共用**
    —— 后者为了性能预先缓存了 core，如果它自己重抄一遍子串规则，
    两者迟早分叉，而分叉的症状是「量大与量小给出不同的分组」。
    """
    if len(x) < 3 or len(y) < 3:
        return False
    return x in y or y in x


def titles_match(a: str, b: str) -> bool:
    """两个**原始标题**是否指向同一件事。

    ⚠️ **判据是"核心部分互为子串"**：
    「今晚七点老地方集合」与「今晚19:00老地方集合，请准时参加」
    剥掉时间后是「老地方集合」与「老地方集合请准时参加」——
    前者是后者的子串。真实重复条目就是这样（同一件事的详略两种写法）。

    ⚠️ **不用相似度分数**：阈值（如 0.8）需要调参、且**调错了两边都错**
    （调低误合并、调高一条都合不上）。子串判定没有这个旋钮。

    ⚠️ 核心部分太短（< 3 字）就不参与：「集合」「通知」这类词互为子串的
    概率极高，拿它们当依据等于**没有依据**。

    ⚠️ 本函数是**单次比较**的入口（测试与外部调用用）；批量分组走
    `find_duplicate_groups`，它会预先缓存 core —— 两者判据相同（共用
    `_cores_match`），只是后者不重复计算。
    """
    return _cores_match(_core(a), _core(b))


def find_duplicate_groups(
    entries: list[tuple[int, str, int]],
) -> tuple[DuplicateGroup, ...]:
    """把条目按「同一件事」分组。`entries` 是 `(item_id, title, start_ts)`。

    ⚠️ **只认 start_ts 完全相同的**（秒级，不设容差）：
    19:00 与 19:30 可能是同一场（有人写集合时间、有人写开始时间），
    也**可能真的是两场**。猜错的代价是**漏掉一个活动**，而漏掉的活动
    在界面上不留痕迹 —— 所以宁可分开列。
    真要合并，应该由用户显式做（那是后续的"人工干预"功能）。

    ⚠️ 返回的组**按 item_id 升序**、组内也升序：输出必须确定，
    否则前端按组折叠时顺序会抖。
    """
    # 先按 start_ts 分桶（桶内才可能重复）
    #
    # ⚠️ `_core` 在这里**算一次并缓存**（M7 性能修复，2026-10-15）。
    # 原实现在两两循环里调 `titles_match`，而后者每次都对**两个**标题重算
    # `_core` —— 实测（250 条同一时刻）：`_core` 被调用 **62,250 次**，
    # 而按条目数只需要 250 次，**重复了 249 倍**。那占了 76ms 里的绝大部分。
    #
    # ⚠️ 缓存 `_core` 而不是 `titles_match`：后者缓存的是**两个标题之间的关系**
    # （O(n²) 个，没有复用价值）；前者是**每个条目一个值**（O(n) 个，天然可复用）。
    #
    # ⚠️ 但判据必须与 `titles_match` **逐字一致** —— 所以两者共用
    # `_cores_match`，不在这里重抄一遍子串规则。抄两份的话，改了一边会让
    # "走缓存"与"走 `titles_match`"给出不同的分组结果，而那种 bug 只在量大时才显形。
    by_ts: dict[int, list[tuple[int, str]]] = {}
    for item_id, title, start_ts in entries:
        by_ts.setdefault(start_ts, []).append((item_id, _core(title)))

    groups: list[DuplicateGroup] = []
    for start_ts, bucket in sorted(by_ts.items()):
        bucket.sort(key=lambda t: t[0])  # 按 item_id 升序：代表取最小的
        # 桶内两两比较标题，做连通分量（并查集太重，桶通常只有 1-3 条）
        n = len(bucket)
        parent = list(range(n))

        def find(i: int) -> int:
            while parent[i] != i:
                parent[i] = parent[parent[i]]
                i = parent[i]
            return i

        for i in range(n):
            for j in range(i + 1, n):
                if _cores_match(bucket[i][1], bucket[j][1]):
                    pi, pj = find(i), find(j)
                    if pi != pj:
                        parent[max(pi, pj)] = min(pi, pj)

        members: dict[int, list[int]] = {}
        for i in range(n):
            members.setdefault(find(i), []).append(bucket[i][0])
        for ids in members.values():
            if len(ids) > 1:  # 只有真的重复才成组
                groups.append(
                    DuplicateGroup(item_ids=tuple(sorted(ids)), start_ts=start_ts)
                )

    groups.sort(key=lambda g: (g.start_ts, g.item_ids))
    return tuple(groups)


# ── ② / ③ 时段冲突 ───────────────────────────────────────────


@dataclass(frozen=True)
class Entry:
    """一个**占了时间**的东西：活动、课、或被排进去的待办。"""

    label: str
    start: int
    end: int
    # `item_id` 为 None 表示它不是一条 item（比如**课表的课**）。
    # ⚠️ 有这个区分，前端才知道该把它渲染成"你的课"还是"群里的活动"。
    item_id: int | None = None
    kind: str = "activity"  # activity | course | task


@dataclass(frozen=True)
class Conflict:
    """两段时间撞了。"""

    a: Entry
    b: Entry
    minutes: int

    def __str__(self) -> str:
        ai = dt.datetime.fromtimestamp(self.a.start)
        return (
            f"{self.a.label} ⟷ {self.b.label}"
            f"（{ai:%m-%d %H:%M}，重叠 {self.minutes} 分钟）"
        )


def find_conflicts(entries: list[Entry]) -> tuple[Conflict, ...]:
    """两两求交，返回所有重叠对。

    ⚠️ **O(n²) 是刻意的**：同一天的条目量级是十几条（实测一屏 20 条以内），
    而区间树/扫描线的实现复杂度换来的是"在这种数据量下毫无差别的性能"，
    外加一个更可能写错的边界处理。真到了上千条再换 —— 那时会有真实的
    性能数据支撑，而不是现在猜。

    ⚠️ **不排除同 `item_id` 的对**：调用方由 ① 去重后再传进来，
    本函数只管几何。把"是不是同一件事"的判断混进这里，会让两个判据
    互相纠缠 —— 而它们该能各自被测试。
    """
    out: list[Conflict] = []
    for i in range(len(entries)):
        for j in range(i + 1, len(entries)):
            a, b = entries[i], entries[j]
            m = overlap_minutes(a.start, a.end, b.start, b.end)
            if m > 0:
                out.append(Conflict(a=a, b=b, minutes=m))
    out.sort(key=lambda c: (c.a.start, c.b.start, c.minutes))
    return tuple(out)
