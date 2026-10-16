"""排期：把待办条目排进课表之外的**空闲块**。

存在的理由（`docs/design/消息整合与排期.md` §三）：
「消息整合」已经做完了（日报阶段跨群合并），而「排时间表」缺的从来不是
算法 —— 是**「我的可用时间」这个约束源**。`timetable.py` 把它补上了，
本模块就是那个"其实很简单"的区间调度。

⚠️ **两类条目，两种处置**（这是实跑真实数据后发现的事实，不是设计时的假设）：

  · **有 `start_ts`** —— 活动**已有确定时刻**（「周三14:00在体育馆」）。
    它**占用**时间，是被排进去的**障碍物**，不是待排的任务。
    给它排个新时间是荒谬的：它已经定了。
  · **有 `deadline_ts`** —— 知道**什么时候前要完成**，但**不知道什么时候做**
    （「9月30日前交材料」）。这才是待排的东西。

⚠️ 实测数据里这两类**互不重叠**（14 条有 start 的 ∩ 12 条有 deadline 的 = 0），
这印证了它们是两件事。本模块**两者都处理**，但语义完全不同 —— 混为一谈
会排出一份"给已经定好的活动又安排了一个时间"的荒唐结果。

⚠️ 规则 3（文档原文）是重点：**排不进去时要说出来**，而不是找个缝硬插。
一个"总能排进去"的排期器是假的 —— 它只是在骗用户以为时间够用。
"""

from __future__ import annotations

import datetime as dt
from dataclasses import dataclass, field

from .conflicts import Conflict, Entry, find_conflicts
from .timetable import Timetable


@dataclass(frozen=True)
class Busy:
    """一个**已占用**的时间段（本地时间，分钟级）。"""

    start: int  # unix 秒
    end: int
    label: str  # 「高等数学」/「讲座」—— 给人看
    # ⚠️ `kind` 决定冲突**怎么报**：`activity`（群里的活动）与 `course`（你的课）
    # 处置不同 —— 活动能改期，课不能。没有这个区分就只能报"两件事撞了"，
    # 而用户无法据此决定该动哪个。
    kind: str = "activity"

    def as_entry(self) -> "Entry":
        """转成冲突检测用的 `Entry`（`conflicts.find_conflicts` 的输入）。"""
        return Entry(label=self.label, start=self.start, end=self.end, kind=self.kind)


@dataclass(frozen=True)
class FreeSlot:
    """一个**空闲**时段。这是排期的落点。"""

    start: int
    end: int

    @property
    def minutes(self) -> int:
        return (self.end - self.start) // 60

    def __str__(self) -> str:
        a = dt.datetime.fromtimestamp(self.start)
        return f"{a:%m-%d %H:%M}–{dt.datetime.fromtimestamp(self.end):%H:%M}（{self.minutes} 分钟）"


@dataclass
class SchedulableItem:
    """一条**想被安排**的条目。"""

    item_id: int
    title: str
    deadline_ts: int
    # 预计耗时（分钟）。⚠️ 信息来源有限：抽取没给"这件事要多久"，
    # 所以用**默认值**并让它是显式的 —— 写死一个数在算法里同样糟糕。
    duration_min: int = 30


@dataclass
class Placement:
    """一次排期结果。`slot` 为 None 表示**没排进去**。"""

    item: SchedulableItem
    slot: FreeSlot | None = None
    reason: str = ""

    @property
    def ok(self) -> bool:
        return self.slot is not None


@dataclass
class SchedulePlan:
    """一整份排期结果。

    ⚠️ `unscheduled` **必须单独列出来**（不是塞进 placements 里当 None）：
    用户最需要知道的就是"哪几件我塞不下了"——而那正是**不许隐藏**的一类信息
    （与 `repass` 的三只"不删"桶、`downgrade` 的"跳过"桶同一条纪律）。
    """

    placements: list[Placement] = field(default_factory=list)
    unscheduled: list[Placement] = field(default_factory=list)
    busy: list[Busy] = field(default_factory=list)
    # ⚠️ 排期器**看得到多远**。窗口外的空闲块不参与排期 ——
    # 不报出来的话，用户会以为"这就是全部安排"。
    window_from: int = 0
    window_to: int = 0
    # ⚠️ **冲突分两类，不许混成一个列表**（`conflicts.py` 顶部那段）：
    #   `conflicts`          = 已有确定时刻的东西之间撞了（活动 ⟷ 活动）
    #   `course_conflicts`   = 活动撞上了**你的课**（活动 ⟷ 课）
    # 两者的处置不同：前者要用户**选一个**，后者是**提醒**（课改不了）。
    conflicts: list[Conflict] = field(default_factory=list)
    course_conflicts: list[Conflict] = field(default_factory=list)
    # 被判定为「同一件事的重复」、**不参与排期**的 item_id。
    #
    # ⚠️ 它们**不是被删了** —— 行仍在库里、界面上仍看得到（折叠显示）。
    # 排期器只是不把它们当成第二件占时间的事。
    duplicate_item_ids: frozenset[int] = frozenset()
    # ⚠️ 排期时**真实**的空闲块（已扣掉课与已有活动）。
    #
    # 存在的理由：旧版 `describe()` 为了报一个"可用空闲块 N 个"，
    # 用 `free_slots(Timetable(), ...)` 现算了一个 —— 而 `Timetable()`
    # 是**空课表**！那个数把用户所有的课都当成空闲了，是一句印在屏幕上的假话
    # （而且它与旁边"排不下"的结论互相矛盾，读的人只会更糊涂）。
    # 存下来就不会算错：它就是排期器**实际**用的那一份。
    #
    # ⚠️ 名字带 `initial_` 是为了**不与模块级函数 `free_slots` 混淆** ——
    # 存下来的是**排期开始时**的那一份（排完之后会被逐次扣减）。
    initial_slots: list[FreeSlot] = field(default_factory=list)

    @property
    def ok_count(self) -> int:
        return sum(1 for p in self.placements if p.ok)


# ── 占用：课表 → 具体日期的忙碌块 ──────────────────────────────


def _week_parity(d: dt.date, term_start: str) -> int:
    """这个日期是单周还是双周（1 = 单，2 = 双）。

    ⚠️ 学期起点必须是周一（`timetable._check_term_start` 强制），
    所以 `(d - term_start).days // 7` 就是周次，不需要再对齐星期几。
    """
    base = dt.date.fromisoformat(term_start)
    weeks = (d - base).days // 7
    return 1 if weeks % 2 == 0 else 2


def busy_from_timetable(
    tt: Timetable, *, window_from: int, window_to: int
) -> list[Busy]:
    """把**周期性**课表展开成窗口内的**具体**忙碌块。

    ⚠️ 这里是"周课表 → 日期"的转换点，也是最容易错一天的地方：
    `Course.weekday` 是 **0=周一**（Python 的 `date.weekday()` 同口径），
    而 TOML 里用户填的是 **1-7** —— 转换在 `timetable._check_course` 里做掉了，
    本函数全程用 0 基，不再换算。
    """
    if not tt.courses:
        return []

    start_day = dt.datetime.fromtimestamp(window_from).date()
    end_day = dt.datetime.fromtimestamp(window_to).date()
    out: list[Busy] = []
    d = start_day
    while d <= end_day:
        parity = _week_parity(d, tt.term_start) if tt.term_start else 1
        for c in tt.courses:
            if c.weekday != d.weekday():
                continue
            if c.weeks == "odd" and parity != 1:
                continue
            if c.weeks == "even" and parity != 2:
                continue
            lo = int(dt.datetime.combine(d, dt.time.min).timestamp()) + c.start * 60
            hi = int(dt.datetime.combine(d, dt.time.min).timestamp()) + c.end * 60
            out.append(Busy(start=lo, end=hi, label=c.label or str(c)))
        d += dt.timedelta(days=1)
    out.sort(key=lambda b: b.start)
    return out


def free_slots(
    tt: Timetable,
    busy: list[Busy],
    *,
    window_from: int,
    window_to: int,
) -> list[FreeSlot]:
    """窗口内**没被占用**的时段 = 作息范围内减去忙碌块。

    ⚠️ 三类都要减掉，少一类就会排出不可用的时间：
      1. **课**（`busy`）
      2. **作息之外的时段**（凌晨）—— 只减课的话会把任务排到凌晨三点
      3. **已有确定时刻的活动**（调用方把它们并进 `busy`）

    ⚠️ 按**天**切分：作息是"每天 08:00–22:00"这样的日循环，
    跨天的连续空闲块（22:00 到次日 08:00）**不是可用时间**，
    合成一块会让排期器把任务放到夜里。
    """
    slots: list[FreeSlot] = []
    d = dt.datetime.fromtimestamp(window_from).date()
    last = dt.datetime.fromtimestamp(window_to).date()
    while d <= last:
        day0 = int(dt.datetime.combine(d, dt.time.min).timestamp())
        lo = max(day0 + tt.day_start * 60, window_from)
        hi = min(day0 + tt.day_end * 60, window_to)
        if hi > lo:
            # 这一天作息内的忙碌块，裁到 [lo, hi]
            cuts = sorted(
                (max(b.start, lo), min(b.end, hi))
                for b in busy
                if b.end > lo and b.start < hi
            )
            cur = lo
            for bs, be in cuts:
                if bs > cur:
                    slots.append(FreeSlot(start=cur, end=bs))
                cur = max(cur, be)
            if cur < hi:
                slots.append(FreeSlot(start=cur, end=hi))
        d += dt.timedelta(days=1)
    return slots


# ── 排期：区间调度 ─────────────────────────────────────────────


def plan_schedule(
    items: list[SchedulableItem],
    tt: Timetable,
    *,
    busy: list[Busy] | None = None,
    window_from: int,
    window_to: int,
    duplicate_item_ids: "frozenset[int] | set[int] | None" = None,
) -> SchedulePlan:
    """把 `items` 排进空闲块。

    规则（`docs/design/消息整合与排期.md` §三 的朴素版）：
      1. **死线越近，排得越靠前** —— 先排最急的，它对时间最没得挑
      2. 排在**死线之前、离死线最近的那一天**（留出余量，而不是一有空就塞）
      3. **排不进去就说出来**（进 `unscheduled`），不硬塞

    ⚠️ **规则 2 的"离死线最近"是关键**：往前排会让用户在死线前一周就把事情
    做完 —— 那时信息可能还没齐（「材料还没发下来」）。⚠️ 但**不能排到死线之后**。

    ⚠️ **落在空闲块的"哪一头"改过**（用户 2026-10-15 裁定）：
    旧版取块尾（`slot.end - need`），产出 `21:30–22:00` 这种贴著作息末尾的
    区间。现在取块**头**（`slot.start`）—— 因为显示改了：**只报开始时刻**，
    而倒扣出来的时刻是个**推算值**（它依赖"我们假设这件事要 30 分钟"），
    不该看起来像一个确定的开始时间。见 `describe()` 里那段完整论证。

    ⚠️ 排进去的时段仍**按推算的时长从空闲池里扣掉**（同一段时间不能塞两件事）
    —— 那是**内部状态**，与"显示什么"是两件事。
    """
    # ⚠️⚠️ **课表必须并进来**（M7 修的真缺陷，2026-10-15）。
    #
    # 原实现只用了调用方给的 `busy`（那一份装的是**活动**），而 `tt` 只被用来
    # 取作息边界 —— 于是**课根本没参与空闲块计算**，排期器会把任务排进
    # 「你正在上课的时间」。
    #
    # ⚠️ 它**不报错、也不显眼**：产出看起来完全正常（`08:00` 这种齐整的时刻），
    # 只是那些时刻是假的。测试抓到的形状是：
    #   手动调 `free_slots(tt, busy_from_timetable(...))` ⇒ 60 分钟
    #   而 `plan_schedule` 内部算出来的 ⇒ 840 分钟（把课全当成空闲）
    #
    # ⚠️ 修在**这里**而不是"让每个调用方记得传"：那种约定迟早有人漏，
    # 而漏的后果是静默给出错误答案。`tt` 已经是必填参数了 —— 它就该完整生效。
    activity_busy = list(busy or [])
    course_busy = busy_from_timetable(tt, window_from=window_from, window_to=window_to)
    all_busy = activity_busy + course_busy
    slots = free_slots(tt, all_busy, window_from=window_from, window_to=window_to)
    plan = SchedulePlan(
        busy=all_busy, window_from=window_from, window_to=window_to,
        duplicate_item_ids=frozenset(duplicate_item_ids or ()),
        initial_slots=list(slots),
    )

    # ── ② 活动之间的冲突（已有确定时刻的东西撞了）────────────────
    #
    # ⚠️ 这是**排期器管不了**的一类：那些时刻是既成事实，不是它安排的。
    # 但它**必须报出来** —— 用户有两个会撞在一起，这是他最需要知道的事，
    # 而"两个都当障碍物、默默减掉两段时间"会把这件事藏得一干二净。
    #    ⚠️ **只比活动，不比课**（M7）：课与课相撞是**课表自己填错了**
    #    （同一时段排了两门），那是另一件事，且用户在填的时候就该看见。
    #    混进来会让"课与自己"相撞（实测抓到：`大学英语 ⟷ 大学英语`）。
    if activity_busy:
        plan.conflicts = list(
            find_conflicts([Busy.as_entry(b) for b in activity_busy])
        )

    # ── ③ 活动撞课表 ──────────────────────────────────────────────
    if course_busy and activity_busy:
        course_entries = [
            Entry(label=b.label, start=b.start, end=b.end, kind="course")
            for b in course_busy
        ]
        activity_entries = [Busy.as_entry(b) for b in activity_busy]
        for c in find_conflicts(course_entries + activity_entries):
            if {c.a.kind, c.b.kind} == {"course", "activity"}:
                plan.course_conflicts.append(c)

    # 规则 1：死线最近的最先排（给它最大的挑拣余地）
    for item in sorted(items, key=lambda i: (i.deadline_ts, i.item_id)):
        need = item.duration_min * 60
        # 候选 = 整段落在死线之前的空闲块（留一点余量：结束不晚于死线当天开始）
        deadline_day0 = int(
            dt.datetime.combine(
                dt.datetime.fromtimestamp(item.deadline_ts).date(), dt.time.min
            ).timestamp()
        )
        # ⚠️ **已过期的死线要单独说**（实测踩到）：它与"排不下"是两件事 ——
        # 一个是"时间不够"，一个是"**已经错过了**"。混成一句"排不下"会让用户
        # 以为只是安排得紧，而真相是那件事**已经没法按时做了**。
        #
        # ⚠️ 判据是窗口起点而不是 `now`：本模块是**纯函数**（好测），
        # 注入 `now` 会让每次调用多一个必须对齐的时钟。
        if deadline_day0 <= window_from:
            plan.unscheduled.append(
                Placement(item=item, reason="死线已过（不再是「安排」，是「错过」）")
            )
            continue

        candidates = [
            (i, s) for i, s in enumerate(slots)
            if s.end <= deadline_day0 and s.end - s.start >= need
        ]
        if not candidates:
            reason = (
                "死线前没有足够长的空闲块"
                if any(s.end <= deadline_day0 for s in slots)
                else "死线前已经没有任何空闲块"
            )
            plan.unscheduled.append(Placement(item=item, reason=reason))
            continue

        # 规则 2：取**离死线最近的那一天的开始**。
        #
        # ⚠️ **改成从空闲块的头部排**（用户 2026-10-15 裁定"只显示开始时间"）。
        #
        # 旧实现取的是 `slot.end - need`（块尾倒扣），产出 `21:30–22:00` 这种
        # 贴著作息末尾的时段。那时**显示区间**，倒扣还说得通（"在死线前最后一
        # 段空闲里完成"）。
        #
        # 但既然现在只显示**开始时刻**，倒扣就变成坏的了：21:30 是一个
        # **倒推出来的数**，而它看起来像一个确定的开始时间。用户照它安排
        # 别的事时，会以为自己 21:30 才开始忙 —— 而那个数依赖于"我们假设
        # 这件事要 30 分钟"这个前提。
        #
        # ⇒ 从块的**开头**排：开始时刻就是"这段空闲的开始"，是一个**有依据**
        # 的起点（它等于上一个忙碌块的结束、或作息的开始）。
        # 用户看到 `10:00` 就知道"去 10 点那会儿做"，而不是"10:00 做到 10:30"。
        idx, slot = candidates[-1]
        chosen = FreeSlot(start=slot.start, end=slot.start + need)
        plan.placements.append(Placement(item=item, slot=chosen))

        # 从空闲池里扣掉这一段，并把残留（若有）放回去
        #
        # ⚠️ 扣减**仍按推算的时长**（`need`）：同一段时间不能塞两件事，
        # 而排期器必须知道自己占了多久才不会排出重叠。
        # 这是**内部状态**，与"显示什么"是两件事 —— 见上面的说明。
        rest = [s for i, s in enumerate(slots) if i != idx]
        if chosen.end < slot.end:
            rest.append(FreeSlot(start=chosen.end, end=slot.end))
        slots = sorted(rest, key=lambda s: s.start)

    return plan


def describe(plan: SchedulePlan) -> str:
    """给人看的一份排期摘要。

    ⚠️ **"没排进去"那一段不许省**（哪怕只有 0 条也要说"都排下了"）：
    用户最需要知道的正是塞不下的那些。
    """
    lines = [
        f"可用空闲块 {len(plan.initial_slots)} 个"
        f"（总 {sum(s.minutes for s in plan.initial_slots)} 分钟，已扣掉课与已有活动）",
    ]
    if plan.placements:
        lines.append(f"已排 {plan.ok_count} 条：")
        for p in plan.placements:
            s = p.slot
            if s is None:
                continue
            # ⚠️ **只报开始时刻，不显示结束时刻**（用户 2026-10-15 裁定）。
            #
            # 理由是**结束时刻不是抽出来的，是推算的**：
            # 抽取没给"这件事要多久"，`SchedulableItem.duration_min` 是我们
            # 假设的（默认 30 分钟）。把它渲染成 `21:30–22:00` 会让用户以为
            # 那是一个**已知的事实**，而它只是"我们按 30 分钟算的"。
            # 一旦他据此安排别的事，就会踩在一个猜测上 —— 而界面上没有任何
            # 痕迹提示那是猜的。只报开始时刻就没有这个问题。
            a = dt.datetime.fromtimestamp(s.start)
            dl = dt.datetime.fromtimestamp(p.item.deadline_ts)
            lines.append(
                f"  · {a:%m-%d %H:%M}  {p.item.title}（死线 {dl:%m-%d}）"
            )
    if plan.unscheduled:
        lines.append(f"⚠️ 排不下 {len(plan.unscheduled)} 条（**没有硬塞**）：")
        for p in plan.unscheduled:
            dl = dt.datetime.fromtimestamp(p.item.deadline_ts)
            lines.append(f"  · {p.item.title}（死线 {dl:%m-%d}）：{p.reason}")
    else:
        lines.append("全部排下了。")

    # ⚠️ 冲突**单独成段**，不许并进"排不下"：那是**三种不同的东西**
    #   · 「排不下」   = 时间不够（能靠取舍解决）
    #   · 「已过期」   = 已经错过了（解决不了）
    #   · 「撞车」     = 两个都要去（要选一个 / 要请假）
    # 混成一段会让用户分不清该做什么。
    if plan.course_conflicts:
        lines.append("")
        lines.append(f"⚠️ 与你的课冲突 {len(plan.course_conflicts)} 处：")
        for c in plan.course_conflicts:
            lines.append(f"  · {c}")
    if plan.conflicts:
        lines.append("")
        lines.append(f"⚠️ 活动之间撞车 {len(plan.conflicts)} 处（要选一个去）：")
        for c in plan.conflicts:
            lines.append(f"  · {c}")
    return "\n".join(lines)
