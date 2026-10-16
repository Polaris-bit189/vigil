"""时间冲突：三种语义各自的判据。

⚠️ 本文件里的**重复样本取自真库实测**（2026-10-15，item 7 / 35）——
不是编的。判据要挡住的正是真实出现过的那两条。
"""

from __future__ import annotations

import datetime as dt

from vigil import conflicts
from vigil.conflicts import Entry


def _ts(y, m, d, h=0, mi=0) -> int:
    return int(dt.datetime(y, m, d, h, mi).timestamp())


# ── 相交判据（三个层次共用这一份）───────────────────────────────


def test_overlap_detects_a_real_overlap():
    assert conflicts.overlaps(0, 100, 50, 150) is True


def test_touching_endpoints_are_not_an_overlap():
    """⭐⭐ 端点相接**不算**冲突。

    ⚠️ 写成 `<=` 会把每一个"严丝合缝排下去"的日程都报成冲突 ——
    而那恰恰是**排得好**的一种形状（上一件事刚结束、下一件紧接着开始）。
    判据是半开区间 `[start, end)`，与 `schedule.free_slots` 同一套约定。

    ⚠️ 变异反证：`a_start < b_end and b_start < a_end` 改成 `<=` ⇒ 本条必红。
    """
    assert conflicts.overlaps(0, 100, 100, 200) is False


def test_overlap_minutes_is_zero_when_disjoint():
    assert conflicts.overlap_minutes(0, 100, 200, 300) == 0


def test_overlap_minutes_counts_the_intersection():
    assert conflicts.overlap_minutes(0, 600, 300, 900) == 300 // 60


def test_containment_is_an_overlap():
    """一段完全包住另一段也是冲突。"""
    assert conflicts.overlaps(0, 1000, 100, 200) is True


# ── ① 同一件事的重复 ──────────────────────────────────────────


def test_titles_match_by_substring():
    """⭐ 真库的形态：同一件事的详略两种写法。"""
    assert conflicts.titles_match(
        "今晚七点老地方集合", "今晚19:00老地方集合，请准时参加"
    )


def test_titles_match_rejects_short_titles():
    """⚠️ 太短的标题不参与：「集合」「提醒」互为子串的概率极高。

    拿它们当依据等于**没有依据**。判据是长度下限，不是相似度。
    """
    assert conflicts.titles_match("集合", "集合通知") is False
    assert conflicts.titles_match("提醒", "提醒一下") is False


def test_titles_match_rejects_different_events():
    assert conflicts.titles_match("高等数学考试", "大学英语讲座") is False


def test_find_duplicate_groups_matches_the_real_pair():
    """⭐⭐ **真库实测的那一对**必须被识别为同一件事。

    item 7「今晚七点老地方集合」/ item 35「今晚19:00老地方集合，请准时参加」
    start_ts 完全相同（10-15 19:00），来自两条不同消息。
    """
    t = _ts(2026, 10, 15, 19)
    groups = conflicts.find_duplicate_groups([
        (7, "今晚七点老地方集合", t),
        (35, "今晚19:00老地方集合，请准时参加", t),
    ])
    assert len(groups) == 1
    assert groups[0].item_ids == (7, 35)
    assert groups[0].representative == 7, "代表取最小的 item_id（最早抽出的那条）"
    assert groups[0].extra_count == 1


def test_find_duplicate_groups_requires_identical_start_ts():
    """⭐⭐ **秒级完全相同**才合并 —— 不设容差。

    ⚠️ 19:00 与 19:30 可能是同一场（有人写集合时间、有人写开始时间），
    也**可能真的是两场**。猜错的代价是**漏掉一整个活动**，而漏掉的活动
    在界面上**不留痕迹** —— 所以宁可分开列。
    """
    groups = conflicts.find_duplicate_groups([
        (1, "今晚老地方集合", _ts(2026, 10, 15, 19, 0)),
        (2, "今晚老地方集合", _ts(2026, 10, 15, 19, 30)),
    ])
    assert groups == (), "时间不同 ⇒ 不合并（哪怕标题一模一样）"


def test_find_duplicate_groups_needs_similar_titles():
    """⚠️ 同一时刻的**两件不同的事**不许合并。

    这是误合并的正面守卫：晚上七点既可能有讲座、也可能有班会。
    """
    t = _ts(2026, 10, 15, 19)
    groups = conflicts.find_duplicate_groups([
        (1, "期末讲座", t),
        (2, "班级班会", t),
    ])
    assert groups == ()


def test_find_duplicate_groups_handles_three_way_duplicates():
    """三条重复也要归成一组（连通分量，不是两两配对）。"""
    t = _ts(2026, 10, 15, 19)
    groups = conflicts.find_duplicate_groups([
        (1, "今晚七点老地方集合", t),
        (2, "今晚19:00老地方集合，请准时参加", t),
        (3, "今晚七点老地方集合，别迟到", t),
    ])
    assert len(groups) == 1
    assert groups[0].item_ids == (1, 2, 3)


def test_find_duplicate_groups_output_is_deterministic():
    """⚠️ 输出顺序必须确定 —— 否则前端按组折叠时顺序会抖。"""
    t = _ts(2026, 10, 15, 19)
    rows = [
        (5, "今晚七点老地方集合", t),
        (3, "今晚七点老地方集合，别迟到", t),
    ]
    g1 = conflicts.find_duplicate_groups(rows)
    g2 = conflicts.find_duplicate_groups(list(reversed(rows)))
    assert g1 == g2
    assert g1[0].item_ids == (3, 5), "组内也必须升序"


def test_core_is_computed_once_per_entry(monkeypatch):
    """⭐⭐ **性能守卫**：每个条目的 core 只能算一次（M7 修复的回归锁）。

    ⚠️ 原实现在桶内两两循环里调 `titles_match`，而后者每次重算两个 core ——
    实测（250 条同一时刻）`_core` 被调用 **62,250 次**，而按条目数只需 250 次，
    **重复 249 倍**，那占了 76ms 里的绝大部分。

    ⚠️ 判据是**调用次数**，不是耗时：耗时随机器负载波动，在 CI 上会假红假绿；
    而调用次数是确定的 —— 它精确地钉住"有没有重复计算"这件事本身。

    ⚠️ 变异反证：把 `find_duplicate_groups` 改回循环里调 `titles_match`
    ⇒ 本条必红（调用次数变成 O(n²)）。
    """
    calls = {"n": 0}
    real = conflicts._core

    def counting(title: str) -> str:
        calls["n"] += 1
        return real(title)

    monkeypatch.setattr(conflicts, "_core", counting)

    base = _ts(2026, 10, 15, 19)
    n = 60  # 全落在同一时刻 ⇒ 最坏情况（桶里有 n 条）
    conflicts.find_duplicate_groups([(i, f"今晚七点老地方集合{i % 3}", base) for i in range(n)])

    assert calls["n"] == n, (
        f"每个条目的 core 只该算一次（应 {n} 次），实际 {calls['n']} 次"
        f"——这是 O(n²) 重复计算，见 M7 的性能修复"
    )


def test_singleton_entries_produce_no_group():
    groups = conflicts.find_duplicate_groups([(1, "随便一件事", _ts(2026, 10, 15, 9))])
    assert groups == ()


# ── ② / ③ 时段冲突 ────────────────────────────────────────────


def _e(label, h1, m1, h2, m2, **kw) -> Entry:
    return Entry(
        label=label,
        start=_ts(2026, 10, 15, h1, m1),
        end=_ts(2026, 10, 15, h2, m2),
        **kw,
    )


def test_find_conflicts_reports_an_overlap():
    cs = conflicts.find_conflicts([
        _e("实验报告", 15, 0, 16, 30, kind="task"),
        _e("体测培训", 15, 30, 17, 0, kind="activity"),
    ])
    assert len(cs) == 1
    assert cs[0].minutes == 60


def test_find_conflicts_ignores_back_to_back_entries():
    """⚠️ 紧挨着不算冲突（同 `overlaps` 的端点判据）。"""
    cs = conflicts.find_conflicts([
        _e("A", 15, 0, 16, 0, kind="task"),
        _e("B", 16, 0, 17, 0, kind="task"),
    ])
    assert cs == ()


def test_conflict_carries_both_sides_for_the_ui():
    """⚠️ 冲突必须**两边都带上**：只说"冲突了"等于把排查丢回给用户。
    （与 `configpack` 那条"两边的映射都要印出来"同一条纪律。）"""
    cs = conflicts.find_conflicts([
        _e("讲座", 14, 0, 16, 0, item_id=1, kind="activity"),
        _e("大学英语", 15, 0, 17, 0, kind="course"),
    ])
    labels = {cs[0].a.label, cs[0].b.label}
    assert labels == {"讲座", "大学英语"}
    # course 那侧没有 item_id —— 前端据此渲染成"你的课"而不是"群里的活动"
    course = cs[0].a if cs[0].a.kind == "course" else cs[0].b
    assert course.item_id is None


def test_course_conflicting_with_an_activity_is_reported():
    """⭐ ③ 的核心场景：群里的讲座正好落在你的课上。"""
    cs = conflicts.find_conflicts([
        _e("期末讲座", 14, 0, 15, 30, item_id=9, kind="activity"),
        _e("大学英语", 14, 0, 15, 40, kind="course"),
    ])
    assert len(cs) == 1
    assert cs[0].minutes == 90


def test_find_conflicts_is_empty_for_a_clean_day():
    cs = conflicts.find_conflicts([
        _e("A", 8, 0, 9, 0),
        _e("B", 10, 0, 11, 0),
        _e("C", 14, 0, 15, 0),
    ])
    assert cs == ()
