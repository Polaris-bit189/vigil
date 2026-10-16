"""规则预筛的测试。

用例尽量取自探针实跑到的真实消息——尤其那 8 条「已完成」，
它们正是「纯应答」规则的由来。
"""

from __future__ import annotations

import pytest

from vigil import prefilter
from vigil.prefilter import Candidate, screen, expand_context, make_batches

HIGH = lambda gid: "high"    # noqa: E731
NORMAL = lambda gid: "normal"  # noqa: E731


def _keep_ids(cands):
    return {c.msg_id for c in cands}


# ── 硬丢弃 ────────────────────────────────────────────


def test_drops_non_text(msg_factory):
    msgs = [msg_factory(1, "[非文本]")]
    kept, stats = screen(msgs, tier_of=HIGH)
    assert kept == []
    assert stats.dropped_empty == 1


def test_drops_too_short(msg_factory):
    msgs = [msg_factory(1, "1"), msg_factory(2, "dd"), msg_factory(3, "好")]
    kept, stats = screen(msgs, tier_of=HIGH)
    assert kept == []
    assert stats.dropped_short == 3


def test_drops_bad_timestamp(msg_factory):
    """1970-01-01 的脏数据（实测存在于多个群）。"""
    msgs = [msg_factory(1, "这里有通知请查收", ts=0)]
    kept, stats = screen(msgs, tier_of=HIGH)
    assert kept == []
    assert stats.dropped_bad_time == 1


@pytest.mark.parametrize("text", ["已完成", "收到", "好的", "好的！", "嗯嗯", "OK", "知道了"])
def test_drops_bare_acknowledgements(msg_factory, text):
    """探针实测：班级群 15 条里 8 条是「已完成」，纯噪声。"""
    kept, stats = screen([msg_factory(1, text)], tier_of=HIGH)
    assert kept == []
    assert stats.dropped_ack == 1


def test_keeps_ack_with_substance(msg_factory):
    """「收到」后面跟真东西的不能丢。"""
    kept, _ = screen([msg_factory(1, "收到，明天9点在西太湖集合")], tier_of=HIGH)
    assert _keep_ids(kept) == {1}


def test_drops_flood(msg_factory):
    """同一人 60 秒内连发 3 条一样的——广告号刷屏的形态。"""
    msgs = [
        msg_factory(i, "校园卡办理 联系QQ123", ts=1000 + i, uid="spammer")
        for i in range(3)
    ]
    kept, stats = screen(msgs, tier_of=NORMAL)
    assert kept == []
    assert stats.dropped_flood == 3


def test_flood_does_not_catch_different_people(msg_factory):
    """不同人各说一次「已完成」不是刷屏（实测的班级群就是这形态）。"""
    msgs = [
        msg_factory(i, "已完成", ts=1000 + i, uid=f"u{i}")
        for i in range(8)
    ]
    kept, stats = screen(msgs, tier_of=HIGH)
    # 被「纯应答」挡掉，而不是被刷屏挡掉
    assert stats.dropped_ack == 8
    assert stats.dropped_flood == 0


def test_flood_does_not_cross_groups(msg_factory):
    """**同一个人**在不同群转发同一条通知，不该被判刷屏。

    ⚠️ uid 必须是**非空**的。写成匿名者的话，消息会被 `_flood_ids` 开头的
    「跳过匿名者」分支先拦下，测试就退化成**空守卫**——把 `group_id` 从桶键里
    删掉也照样绿（W2 审查用变异测试实测抓出：29 条测试全绿）。

    这条守的是「**桶键必须含 group_id**」。
    """
    msgs = [
        msg_factory(
            i, "转发通知：明天停课", ts=1000 + i, group_id=gid, uid="u_forwarder"
        )
        for i, gid in enumerate([100, 200, 300], start=1)
    ]
    kept, stats = screen(msgs, tier_of=NORMAL)
    assert stats.dropped_flood == 0, "同人跨群的同内容不该判为刷屏"
    assert _keep_ids(kept) == {1, 2, 3}


def test_flood_does_not_catch_anonymous_across_groups(msg_factory):
    """匿名者在不同群发同样的话，不该被判刷屏。

    ⚠️ 这条与上一条守的是**不同**的规则：这条守「**跳过匿名者**」，
    上一条守「桶键含 group_id」。

    两者**必须分开写**。合成一条的话，「跳过匿名者」分支会先命中，
    从而把 `group_id` 的缺失整个掩盖掉——这正是第一条测试最初的写法错误。
    """
    msgs = [
        msg_factory(i, "转发通知：明天停课", ts=1000 + i, group_id=gid, uid="")
        for i, gid in enumerate([100, 200, 300], start=1)
    ]
    kept, stats = screen(msgs, tier_of=NORMAL)
    assert stats.dropped_flood == 0, "匿名者的同内容不该判为刷屏"
    assert _keep_ids(kept) == {1, 2, 3}


def test_flood_ignores_anonymous_senders(msg_factory):
    """匿名者（uid 为空串）不参与刷屏判定——**空串不是身份**。

    实测全库 1,218 条（2.55%）匿名消息；把空串当桶键等于把互不相识的人
    当成同一个人。
    """
    msgs = [
        msg_factory(i, "校园卡办理联系我", ts=1000 + i, group_id=100, uid="")
        for i in range(3)
    ]
    _, stats = screen(msgs, tier_of=NORMAL)
    assert stats.dropped_flood == 0


def test_flood_still_catches_same_group_spammer(msg_factory):
    """收紧之后，真正的同群刷屏仍必须被抓住——别把修复做成功能阉割。"""
    msgs = [
        msg_factory(i, "校园卡办理 联系QQ123", ts=1000 + i, group_id=100, uid="spammer")
        for i in range(3)
    ]
    kept, stats = screen(msgs, tier_of=NORMAL)
    assert kept == []
    assert stats.dropped_flood == 3


# ── 保留 ──────────────────────────────────────────────


def test_high_tier_keeps_non_keyword_messages(msg_factory):
    """班级群里「看着像闲聊」的往往是真通知，不敢用关键词过滤。"""
    kept, _ = screen([msg_factory(1, "大家记得把那个表弄一下")], tier_of=HIGH)
    assert _keep_ids(kept) == {1}
    assert kept[0].reason == "high_tier"


def test_normal_tier_needs_keyword(msg_factory):
    kept, _ = screen([msg_factory(1, "大家记得把那个表弄一下")], tier_of=NORMAL)
    assert kept == []


def test_normal_tier_keeps_keyword_hit(msg_factory):
    kept, _ = screen([msg_factory(1, "明天讲座地点改到报告厅")], tier_of=NORMAL)
    assert _keep_ids(kept) == {1}
    assert kept[0].reason == "keyword"


def test_normal_tier_keeps_role_sender(msg_factory):
    """辅导员/班助发的，即使不含关键词也要留。"""
    msgs = [msg_factory(1, "大家把那个弄一下", sender="22-1班班主任助理张三")]
    kept, _ = screen(msgs, tier_of=NORMAL)
    assert _keep_ids(kept) == {1}
    assert kept[0].reason == "role"


def test_stats_counts_everything(msg_factory):
    msgs = [
        msg_factory(1, "[非文本]"),
        msg_factory(2, "1"),
        msg_factory(3, "明天讲座在报告厅"),
    ]
    _, stats = screen(msgs, tier_of=NORMAL)
    assert stats.total == 3
    assert stats.kept == 1
    assert stats.dropped_empty == 1
    assert stats.dropped_short == 1


# ── 上下文展开 ────────────────────────────────────────


def test_expand_context_includes_neighbours(msg_factory):
    """单条消息常常没头没尾（「明天记得带」），邻居能救回这类。"""
    msgs = [msg_factory(i, f"第{i}条", ts=1000 + i) for i in range(1, 8)]
    cands = [Candidate(msg_id=4, group_id=100, ts=1004, sender="x",
                       content="第4条", reason="keyword")]
    out = expand_context(msgs, cands, context=2)
    assert [m.msg_id for m in out] == [2, 3, 4, 5, 6]


def test_expand_context_clamps_at_edges(msg_factory):
    msgs = [msg_factory(i, f"第{i}条", ts=1000 + i) for i in range(1, 4)]
    cands = [Candidate(msg_id=1, group_id=100, ts=1001, sender="x",
                       content="第1条", reason="keyword")]
    out = expand_context(msgs, cands, context=2)
    assert [m.msg_id for m in out] == [1, 2, 3]


def test_expand_context_does_not_cross_groups(msg_factory):
    """邻居必须是同群的——跨群拼接会让上下文完全错位。"""
    msgs = [
        msg_factory(1, "群A第1条", group_id=100, ts=1000),
        msg_factory(2, "群A第2条", group_id=100, ts=1001),
        msg_factory(3, "群B第1条", group_id=200, ts=1002),
        msg_factory(4, "群B第2条", group_id=200, ts=1003),
    ]
    cands = [Candidate(msg_id=2, group_id=100, ts=1001, sender="x",
                       content="群A第2条", reason="keyword")]
    out = expand_context(msgs, cands, context=5)
    assert [m.msg_id for m in out] == [1, 2]


def test_expand_context_zero_is_passthrough(msg_factory):
    msgs = [msg_factory(i, f"第{i}条", ts=1000 + i) for i in range(1, 4)]
    cands = [Candidate(msg_id=2, group_id=100, ts=1002, sender="x",
                       content="第2条", reason="keyword")]
    assert [m.msg_id for m in expand_context(msgs, cands, context=0)] == [2]


def test_expand_context_dedupes_overlapping_windows(msg_factory):
    msgs = [msg_factory(i, f"第{i}条", ts=1000 + i) for i in range(1, 6)]
    cands = [
        Candidate(msg_id=2, group_id=100, ts=1002, sender="x", content="第2条", reason="k"),
        Candidate(msg_id=3, group_id=100, ts=1003, sender="x", content="第3条", reason="k"),
    ]
    out = expand_context(msgs, cands, context=1)
    assert [m.msg_id for m in out] == [1, 2, 3, 4]


# ── 切批 ──────────────────────────────────────────────


def test_make_batches_splits_by_group(msg_factory):
    msgs = [
        msg_factory(1, "a", group_id=100),
        msg_factory(2, "b", group_id=100),
        msg_factory(3, "c", group_id=200),
    ]
    batches = make_batches(msgs, max_batch=30)
    assert [[m.msg_id for m in b] for b in batches] == [[1, 2], [3]]


def test_make_batches_respects_max(msg_factory):
    msgs = [msg_factory(i, f"第{i}条", group_id=100) for i in range(1, 8)]
    batches = make_batches(msgs, max_batch=3)
    assert [len(b) for b in batches] == [3, 3, 1]


def test_make_batches_empty():
    assert make_batches([]) == []
