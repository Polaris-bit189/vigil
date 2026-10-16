"""排期：区间调度。

⚠️ 本文件全部用**固定日期**构造，不依赖"跑用例的那一刻是几号"——
排期是纯函数，让它可复现是最基本的要求。
"""

from __future__ import annotations

import datetime as dt

import pytest

from vigil import schedule
from vigil.timetable import Course, Timetable


def _ts(y, m, d, h=0, mi=0) -> int:
    return int(dt.datetime(y, m, d, h, mi).timestamp())


# 2026-09-07 是周一 —— 用它当"学期第一周"，weekday 推算一眼可验。
TERM = "2026-09-07"


def _tt(*courses, **kw) -> Timetable:
    return Timetable(courses=tuple(courses), term_start=kw.pop("term_start", ""), **kw)


# ── 课表 → 忙碌块 ─────────────────────────────────────────────


def test_busy_expands_course_to_every_matching_weekday():
    """⭐ 周三的课在窗口里出现**两次**（两个周三）。"""
    c = Course(weekday=2, start=8 * 60, end=9 * 60 + 40, label="高等数学")
    busy = schedule.busy_from_timetable(
        _tt(c), window_from=_ts(2026, 9, 7), window_to=_ts(2026, 9, 20, 23)
    )
    assert len(busy) == 2, "9/7–9/20 之间有两个周三（9/9、9/16）"
    a = dt.datetime.fromtimestamp(busy[0].start)
    assert (a.month, a.day, a.hour, a.minute) == (9, 9, 8, 0)
    assert busy[0].label == "高等数学"


def test_busy_respects_day_start_and_end():
    """课表作息之外不该产生忙碌块 —— 它是空闲块的边界，不是忙碌块。"""
    c = Course(weekday=2, start=8 * 60, end=9 * 60, label="x")
    busy = schedule.busy_from_timetable(
        _tt(c, day_start=10 * 60, day_end=12 * 60),
        window_from=_ts(2026, 9, 9),
        window_to=_ts(2026, 9, 9, 23),
    )
    # ⚠️ 课在 8-9 点，而作息是 10-12 ⇒ 课落在作息外，但**仍然是一条忙碌块**
    # （它是事实，只是与空闲块不相交）。free_slots 会把它裁掉。
    assert len(busy) == 1


# ── 单双周 ────────────────────────────────────────────────────


def test_odd_week_course_only_appears_on_odd_weeks():
    """⭐ 单周课在单周出现、双周不出现。"""
    c = Course(weekday=2, start=8 * 60, end=9 * 60, label="隔周课", weeks="odd")
    tt = _tt(c, term_start=TERM)  # 9/7 那周 = 第 1 周（单）
    busy = schedule.busy_from_timetable(
        tt, window_from=_ts(2026, 9, 7), window_to=_ts(2026, 9, 20, 23)
    )
    days = [dt.datetime.fromtimestamp(b.start).day for b in busy]
    assert days == [9], f"只该有第 1 周那个周三（9/9），实际 {days}"


def test_even_week_course_only_appears_on_even_weeks():
    c = Course(weekday=2, start=8 * 60, end=9 * 60, label="隔周课", weeks="even")
    tt = _tt(c, term_start=TERM)
    busy = schedule.busy_from_timetable(
        tt, window_from=_ts(2026, 9, 7), window_to=_ts(2026, 9, 20, 23)
    )
    days = [dt.datetime.fromtimestamp(b.start).day for b in busy]
    assert days == [16], f"只该有第 2 周那个周三（9/16），实际 {days}"


# ── 空闲块 ────────────────────────────────────────────────────


def test_free_slots_is_day_scoped_not_a_night_bridge():
    """⭐⭐ 空闲块**按天切分**：22:00 到次日 08:00 **不是**一块可用时间。

    ⚠️ 合成一块的话，排期器会把任务放到"夜里连着的 10 小时"，
    而那段时间用户在睡觉。判据是**没有任何空闲块跨过午夜**。
    """
    tt = _tt()
    slots = schedule.free_slots(
        tt, [], window_from=_ts(2026, 9, 7), window_to=_ts(2026, 9, 9, 23)
    )
    assert len(slots) == 3, "9/7、9/8、9/9 各一段"
    for s in slots:
        a = dt.datetime.fromtimestamp(s.start)
        b = dt.datetime.fromtimestamp(s.end)
        assert a.date() == b.date(), f"空闲块跨天了：{s}"
        assert (a.hour, a.minute) == (8, 0)
        assert (b.hour, b.minute) == (22, 0)


def test_free_slots_subtracts_a_course():
    """⭐ 有课那天，空闲块被切成两段。"""
    c = Course(weekday=0, start=10 * 60, end=12 * 60, label="高数")  # 周一
    tt = _tt(c)
    busy = schedule.busy_from_timetable(
        tt, window_from=_ts(2026, 9, 7), window_to=_ts(2026, 9, 7, 23)
    )
    slots = schedule.free_slots(
        tt, busy, window_from=_ts(2026, 9, 7), window_to=_ts(2026, 9, 7, 23)
    )
    assert len(slots) == 2
    assert slots[0].minutes == 120   # 08:00–10:00
    assert slots[1].minutes == 600   # 12:00–22:00


def test_free_slots_respects_a_custom_day_window():
    """⚠️ 作息是**用户的**信息 —— 改它，空闲块跟着变。"""
    tt = _tt(day_start=9 * 60, day_end=18 * 60)
    slots = schedule.free_slots(
        tt, [], window_from=_ts(2026, 9, 7), window_to=_ts(2026, 9, 7, 23)
    )
    assert len(slots) == 1
    assert slots[0].minutes == 9 * 60  # 09:00–18:00


# ── 排期 ──────────────────────────────────────────────────────


def _item(iid: int, deadline_day: int, title: str = "", dur: int = 30):
    return schedule.SchedulableItem(
        item_id=iid, title=title or f"任务{iid}",
        deadline_ts=_ts(2026, 9, deadline_day), duration_min=dur,
    )


def test_plan_places_an_item_before_its_deadline():
    plan = schedule.plan_schedule(
        [_item(1, 10)], _tt(), window_from=_ts(2026, 9, 7), window_to=_ts(2026, 9, 11, 23)
    )
    assert plan.ok_count == 1
    p = plan.placements[0]
    assert p.slot.end <= _ts(2026, 9, 10), "必须排在死线之前"


def test_plan_uses_the_latest_slot_before_the_deadline():
    """⭐ 规则 2：排在**离死线最近**的那次机会，而不是一有空就塞。

    往前排会让人在死线前一周就把事做完 —— 那时材料可能还没发下来。
    """
    plan = schedule.plan_schedule(
        [_item(1, 11)], _tt(), window_from=_ts(2026, 9, 7), window_to=_ts(2026, 9, 11, 23)
    )
    p = plan.placements[0]
    # 死线是 9/11 的 00:00 ⇒ 最后一段可用是 9/10 那天（22:00 结束）的尾部
    assert dt.datetime.fromtimestamp(p.slot.end).day == 10, (
        "应当排在 9/10（死线前的最后一天），而不是 9/7"
    )


def test_plan_orders_by_deadline_urgency():
    """⭐ 规则 1：死线近的先排（它对时间最没得挑）。"""
    plan = schedule.plan_schedule(
        [_item(1, 10), _item(2, 8)], _tt(),
        window_from=_ts(2026, 9, 7), window_to=_ts(2026, 9, 11, 23),
    )
    assert [p.item.item_id for p in plan.placements] == [2, 1], "急的排前面"


def test_items_never_overlap():
    """⭐⭐ 扣减必须生效：同一段时间不能被两件事占用。

    ⚠️ 不扣减的话，排期器会在同一格排出一堆"重叠的任务" ——
    看起来排了很多，实际一件也做不了。
    """
    plan = schedule.plan_schedule(
        [_item(1, 12, dur=60), _item(2, 12, dur=60), _item(3, 12, dur=60)],
        _tt(), window_from=_ts(2026, 9, 7), window_to=_ts(2026, 9, 11, 23),
    )
    spans = sorted((p.slot.start, p.slot.end) for p in plan.placements if p.slot)
    for (a1, b1), (a2, b2) in zip(spans, spans[1:]):
        assert b1 <= a2, f"排期结果重叠了：{(a1, b1)} 与 {(a2, b2)}"


def test_item_that_does_not_fit_goes_to_unscheduled():
    """⭐⭐ **规则 3**：排不下就说出来，**不许硬塞**。

    ⚠️ 一个"总能排进去"的排期器是假的 —— 它只是在骗用户以为时间够用。
    判据三半：进 unscheduled、**不在 placements 里**、有可读的理由。
    """
    # ⚠️ 单天作息是 08:00–22:00（14 小时），所以"10 小时的任务"其实**装得下**
    # —— 要造一个**真的**装不下的：比任何单日空闲块都长。
    # （实测踩到：一开始用 600 分钟，结果排进去了，是我的测试假设错了。）
    plan = schedule.plan_schedule(
        [_item(1, 8, dur=15 * 60)], _tt(),  # 15 小时 > 单日 14 小时
        window_from=_ts(2026, 9, 7), window_to=_ts(2026, 9, 11, 23),
    )
    assert plan.ok_count == 0
    assert len(plan.unscheduled) == 1
    assert plan.unscheduled[0].reason, "必须给出理由，不能是空的"
    assert plan.unscheduled[0].slot is None


def test_no_slot_after_the_deadline_is_used():
    """⚠️ 死线之后的空闲再多也不排 —— 那已经不是"安排"，是"错过"。"""
    plan = schedule.plan_schedule(
        # 死线是 9/7 当天 00:00（窗口一开始就过了），窗口里有大把空闲
        [_item(1, 7, dur=30)], _tt(),
        window_from=_ts(2026, 9, 7), window_to=_ts(2026, 9, 11, 23),
    )
    assert plan.ok_count == 0, "死线前没有任何可用时间 ⇒ 不该硬塞到死线之后"
    assert plan.unscheduled[0].slot is None


def test_expired_deadline_is_reported_differently_from_no_room():
    """⭐⭐ **「已错过」与「排不下」是两件事**，不许混成一句。

    ⚠️ 实测踩到：合成语料里的死线全是过去的（9 月初，而"今天"是 10 月），
    于是一整个窗口都在死线之后 —— 排期器当时把它们**一律**报成
    「死线前已经没有任何空闲块」，那听起来像"安排得紧"，而真相是
    **那些事已经没法按时做了**。用户据此做的决定完全不同。
    """
    plan = schedule.plan_schedule(
        [_item(1, 1, title="上个月的事")], _tt(),
        window_from=_ts(2026, 9, 7), window_to=_ts(2026, 9, 11, 23),
    )
    assert plan.ok_count == 0
    assert "已过" in plan.unscheduled[0].reason, (
        f"过期死线必须单独说明，实际理由：{plan.unscheduled[0].reason!r}"
    )


def test_future_deadline_with_no_room_says_no_room_not_expired():
    """⚠️ 阴性对照：死线**还没到**但排不下 ⇒ 说"没位置"，**不许说"已过"**。

    没有这条对照的话，一个把**所有**排不下都写成"已过"的实现照样绿。
    """
    plan = schedule.plan_schedule(
        [_item(1, 9, title="塞不下", dur=15 * 60)], _tt(),
        window_from=_ts(2026, 9, 7), window_to=_ts(2026, 9, 11, 23),
    )
    assert "已过" not in plan.unscheduled[0].reason
    assert "空闲" in plan.unscheduled[0].reason


def test_existing_activity_blocks_scheduling():
    """⭐ 有 `start_ts` 的活动**占用**时间，是障碍物而不是待排任务。"""
    # 9/8 全天被占（08:00–22:00）
    occupied = schedule.Busy(
        start=_ts(2026, 9, 8, 8), end=_ts(2026, 9, 8, 22), label="已有活动"
    )
    plan = schedule.plan_schedule(
        [_item(1, 9)], _tt(), busy=[occupied],
        window_from=_ts(2026, 9, 7), window_to=_ts(2026, 9, 8, 23),
    )
    # 只剩 9/7 那天可用 ⇒ 应当排到 9/7，而不是 9/8
    assert plan.ok_count == 1
    assert dt.datetime.fromtimestamp(plan.placements[0].slot.start).day == 7


# ── ② / ③ 冲突检测 ────────────────────────────────────────────


def test_activities_that_collide_are_reported():
    """⭐⭐ **两个活动撞在一起必须报出来**，不许默默减掉两段时间。

    ⚠️ 这是排期器**管不了**的一类（那些时刻是既成事实，不是它安排的），
    但正是用户最需要知道的事 —— 而"两个都当障碍物"会把这件事藏得
    一干二净：空闲块少了，用户只看到"排不下"，看不到真正的原因。
    """
    a = schedule.Busy(start=_ts(2026, 9, 7, 14), end=_ts(2026, 9, 7, 16), label="期末讲座")
    b = schedule.Busy(start=_ts(2026, 9, 7, 15), end=_ts(2026, 9, 7, 17), label="班级班会")
    plan = schedule.plan_schedule(
        [], _tt(), busy=[a, b],
        window_from=_ts(2026, 9, 7), window_to=_ts(2026, 9, 7, 23),
    )
    assert len(plan.conflicts) == 1
    assert plan.conflicts[0].minutes == 60
    labels = {plan.conflicts[0].a.label, plan.conflicts[0].b.label}
    assert labels == {"期末讲座", "班级班会"}


def test_activities_back_to_back_are_not_a_conflict():
    """⚠️ 阴性对照：紧挨着的两个活动**不是**冲突。"""
    a = schedule.Busy(start=_ts(2026, 9, 7, 14), end=_ts(2026, 9, 7, 15), label="A")
    b = schedule.Busy(start=_ts(2026, 9, 7, 15), end=_ts(2026, 9, 7, 16), label="B")
    plan = schedule.plan_schedule(
        [], _tt(), busy=[a, b],
        window_from=_ts(2026, 9, 7), window_to=_ts(2026, 9, 7, 23),
    )
    assert plan.conflicts == []


def test_activity_colliding_with_a_course_is_reported_separately():
    """⭐⭐ ③：活动撞课表 ⇒ 进 `course_conflicts`，**不是** `conflicts`。

    ⚠️ 两类必须分开：活动之间撞车要用户**选一个**，而撞课是**提醒**
    （课改不了）。混成一个列表用户就不知道该怎么处置。
    """
    c = Course(weekday=0, start=14 * 60, end=15 * 60 + 40, label="大学英语")  # 周一
    activity = schedule.Busy(
        start=_ts(2026, 9, 7, 14), end=_ts(2026, 9, 7, 15, 30), label="期末讲座"
    )
    plan = schedule.plan_schedule(
        [], _tt(c), busy=[activity],
        window_from=_ts(2026, 9, 7), window_to=_ts(2026, 9, 7, 23),
    )
    assert plan.conflicts == [], "活动与课不算「活动之间撞车」"
    assert len(plan.course_conflicts) == 1
    got = plan.course_conflicts[0]
    kinds = {got.a.kind, got.b.kind}
    assert kinds == {"course", "activity"}, "两侧的 kind 必须能分清是课还是活动"


def test_activity_outside_class_hours_has_no_course_conflict():
    """⚠️ 阴性对照：活动在课之外 ⇒ 不报课冲突。"""
    c = Course(weekday=0, start=14 * 60, end=16 * 60, label="大学英语")
    plan = schedule.plan_schedule(
        [], _tt(c),
        busy=[schedule.Busy(start=_ts(2026, 9, 7, 9), end=_ts(2026, 9, 7, 10), label="早课冲突")],
        window_from=_ts(2026, 9, 7), window_to=_ts(2026, 9, 7, 23),
    )
    assert plan.course_conflicts == []


def test_no_timetable_means_no_course_conflicts():
    """⚠️ 没填课表 ⇒ 判不了课冲突，**不许凭空报**。"""
    plan = schedule.plan_schedule(
        [], _tt(),
        busy=[schedule.Busy(start=_ts(2026, 9, 7, 14), end=_ts(2026, 9, 7, 15), label="讲座")],
        window_from=_ts(2026, 9, 7), window_to=_ts(2026, 9, 7, 23),
    )
    assert plan.course_conflicts == []


def test_conflicts_are_printed_in_their_own_section():
    """⚠️ 冲突**单独成段**，不许并进"排不下" —— 三种东西处置不同。"""
    a = schedule.Busy(start=_ts(2026, 9, 7, 14), end=_ts(2026, 9, 7, 16), label="讲座")
    b = schedule.Busy(start=_ts(2026, 9, 7, 15), end=_ts(2026, 9, 7, 17), label="班会")
    plan = schedule.plan_schedule(
        [], _tt(), busy=[a, b],
        window_from=_ts(2026, 9, 7), window_to=_ts(2026, 9, 7, 23),
    )
    text = schedule.describe(plan)
    assert "活动之间撞车" in text
    assert "讲座" in text and "班会" in text


def test_describe_counts_slots_from_the_real_timetable():
    """⭐⭐ **不许报假数**：「可用空闲块」必须来自**真实课表**那条路径。

    ⚠️ 原实现用 `free_slots(Timetable(), ...)` 现算 —— 而 `Timetable()`
    是**空课表**！那个数把用户所有的课都当成空闲了，是一句印在屏幕上的假话，
    而且与旁边"排不下"的结论**自相矛盾**（读的人只会更糊涂）。

    ⚠️ 判据是**两个课表的空闲块数必须不同**：如果实现还在用空课表，
    有课的那份算出来会与它一样多 ⇒ 变红。
    """
    from vigil.timetable import Course

    # 一份"满课"的作息：只有 1 小时空（09:00–10:00）
    busy_tt = _tt(
        Course(weekday=0, start=8 * 60, end=9 * 60, label="早课"),
        Course(weekday=0, start=10 * 60, end=22 * 60, label="满课"),
        day_start=8 * 60, day_end=22 * 60,
    )
    plan = schedule.plan_schedule(
        [], busy_tt, window_from=_ts(2026, 9, 7), window_to=_ts(2026, 9, 7, 23)
    )
    text = schedule.describe(plan)
    # ⚠️ 作息是 08:00–22:00 = **840 分钟**（14 小时），而课把中间切掉了，
    # 所以只有 1 个空闲块（但它是从 09:00 到 10:00 的那一小时）。
    # ⚠️ 实测踩到：我最初把这里的期望写成 60 —— 那是把"空闲块数"与
    # "该块的长度"搞混了（`describe` 报的是**所有空闲块的总分钟数**）。
    assert "1 个" in text, f"满课那天该只有 1 个空闲块，实际：{text!r}"
    # 判据：**必须等于课把 840 切出来的那两段之和**，而不是别的数
    from vigil import schedule as sm
    busy = sm.busy_from_timetable(
        busy_tt, window_from=_ts(2026, 9, 7), window_to=_ts(2026, 9, 7, 23)
    )
    slots = sm.free_slots(
        busy_tt, busy, window_from=_ts(2026, 9, 7), window_to=_ts(2026, 9, 7, 23)
    )
    expected = sum(s.minutes for s in slots)
    assert f"{expected} 分钟" in text, f"该报 {expected} 分钟，实际：{text!r}"
    # ⚠️ 对照：空课表那天的总分钟数**必须不同** —— 否则实现还在用空课表
    empty = schedule.plan_schedule(
        [], _tt(), window_from=_ts(2026, 9, 7), window_to=_ts(2026, 9, 7, 23)
    )
    assert "840 分钟" in schedule.describe(empty), "空课表那天是 840 分钟"
    assert expected != 840, (
        "⚠️ 有课与没课算出同一个数 ⇒ 实现还在用空课表（`Timetable()`）算空闲块"
    )


def test_empty_timetable_is_detectable():
    """⚠️ 空课表要**可判** —— 排期器据此说「你还没填课表」。

    不区分的话会排出一份"全是空闲"的结果，那看起来像"排成功了"。
    """
    assert Timetable().is_empty
    assert not Timetable(courses=(Course(weekday=0, start=480, end=540),)).is_empty
