"""截止日核验：源文里能不能逐字找到这个日期。

⚠️ 本文件的用例是**从 tests/test_digest.py 的既有覆盖里长出来的**：
核验函数原在 digest.py，M3 平移到 deadline.py。平移的正确性由
「test_digest.py 的 105 条既有测试全绿」证明，本文件补的是**新增的那部分**：
「哪些该清掉」这个集合计算。

⚠️ 数字是**实测值**（`pytest tests/test_digest.py --co -q`，2026-09-15），不是
计划里的 72——那个数是更早的计数快照，已经过期（审查裁决：本项目对「没人能
验证的数字」有纪律，写小的不如写准的）。
"""

from __future__ import annotations

import datetime as dt

from vigil import deadline


def _ts(y: int, m: int, d: int) -> int:
    return int(dt.datetime(y, m, d, 12, 0).timestamp())


def test_supported_when_source_has_the_date_literally():
    assert deadline.deadline_supported(_ts(2026, 9, 16), "9月16日12:00开始报名缴费")


def test_unsupported_when_source_only_has_relative_words():
    # 四条都是真实库里出现过的原文（见计划「发现 6」）
    assert not deadline.deadline_supported(_ts(2026, 9, 14), "请于今天下午16点之前改备注")
    assert not deadline.deadline_supported(_ts(2026, 9, 15), "明早7：20各班在宿舍楼下集合")
    assert not deadline.deadline_supported(_ts(2026, 9, 19), "周六下午4.00-8.00")
    assert not deadline.deadline_supported(_ts(2026, 8, 31), "八月要出六级成绩了")


def test_no_deadline_is_never_supported():
    assert not deadline.deadline_supported(None, "随便什么 9月16日")


def test_unverified_ids_picks_only_the_unsupported():
    items = [(1, _ts(2026, 9, 16)), (2, _ts(2026, 9, 14)), (3, None)]
    sources = {1: "9月16日截止", 2: "今天下午截止", 3: "无关"}
    assert deadline.unverified_item_ids(items, sources) == frozenset({2})


def test_unverified_ids_treats_missing_source_row_as_unsupported():
    """没有来源行 = 证不出来 = 没有依据。**口径必须与 M2 一致。**"""
    assert deadline.unverified_item_ids([(7, _ts(2026, 9, 16))], {}) == frozenset({7})


def test_unverified_ids_ignores_items_without_deadline():
    assert deadline.unverified_item_ids([(1, None), (2, None)], {}) == frozenset()

# ── Task 2：place 证据闸门 ────────────────────────────────────────

import datetime as _dt

from vigil.deadline import deadline_sane, place_supported


def _ts(y, m, d, h=12):
    return int(_dt.datetime(y, m, d, h).timestamp())


def test_place_supported_by_literal_substring():
    assert place_supported("西太湖校区", "西太湖连隔板") is True


def test_place_supported_by_bigram_overlap():
    """源文只有「西太湖」，place 是「西太湖校区」——2 字子串命中即算有依据。"""
    assert place_supported("科教城宿舍", "科教城的同学宿舍没有校园网") is True


def test_place_without_evidence_is_unsupported():
    """item 249 的真实形状：源文只有时间，没有任何地点。"""
    assert place_supported("立德楼1阶", "周六下午4.00-8.00") is False


def test_place_shorter_than_2_chars_is_not_judged():
    """⚠️ 单字 place（真实数据 item 295 的 `湖`）判据**不适用**——bigram 是空集。

    这是「多分支判据必须多分支守卫」的落点：若把这条分支写成 return False，
    它就会把「湖」误清，而**没有任何测试会因此变红**。
    """
    assert place_supported("湖", "湖收磁吸灯或者小台灯") is True


def test_place_none_is_not_judged():
    """没有 place 就没有可核验的东西——返回 True（不构成"无依据"）。"""
    assert place_supported(None, "任意源文") is True
    assert place_supported("", "任意源文") is True


def test_place_ignores_separators_and_whitespace():
    """place 用了顿号/空格分隔（真实数据 item 271 的形状）。"""
    assert place_supported("8号楼超市、2号楼超市", "8号楼超市今天上新") is True


# ── Task 2：deadline 时序闸门 ─────────────────────────────────────


def test_deadline_before_message_day_is_insane():
    """item 83 的真实形状：死线比消息早 91 天。"""
    assert deadline_sane(_ts(2026, 5, 6), _ts(2026, 8, 5)) is False


def test_deadline_same_day_is_sane():
    """同一天不过夜——不判死（当天截止是常见的真实情况）。"""
    assert deadline_sane(_ts(2026, 9, 16, 8), _ts(2026, 9, 16, 23)) is True


def test_deadline_after_message_is_sane():
    assert deadline_sane(_ts(2026, 9, 20), _ts(2026, 9, 16)) is True


def test_deadline_none_is_sane():
    assert deadline_sane(None, _ts(2026, 9, 16)) is True


def test_day_start_survives_pre_1970_event_ts():
    """⚠️ 回退分支的守卫（偏差新增，见报告「偏差」第 3 与第 4 条）。

    触发点不在单元层，而是**整批丢数据**：既有夹具用 ts=1000/1001/1002，
    `_day_start_of` 一抛，`refine()` 每批的 `try/except` 就把那一批的 items
    全吞掉（实测：`test_refine_parses_deadline` 与
    `test_refine_wires_the_deadline_drop_end_to_end` 双双变红）。
    没有这条守卫，把回退分支删掉就只有那两条**别的文件**的用例偶然挡一下。

    ⚠️ **不写死 -28800**：它是 UTC+8 的机器相关值，换时区就不对。这里钉的是
    与机器无关的三条性质：不抛、< ts（不是把 ts 原样返回）、同一天归一。
    """
    ds = deadline._day_start_of(1000)          # 1970-01-01 08:16:40 本地
    assert deadline._day_start_of(4600) == ds, "同一天内的两个时刻必须归到同一个日界"
    assert ds < 1000, "日界必须**早于**当天时刻（不是把 ts 原样返回）"
    assert ds >= 1000 - 86400, "日界不可能比前一天还早"
    # 判据是「早于消息当天」，不是「算不出来就清」——畸形 event_ts 不许把未来死线判死
    assert deadline.deadline_sane(_ts(2026, 9, 7), 1000) is True


# ── `start_supported`：活动时刻的机器闸门（M7）────────────────────


def _at(y, m, d, h, mi=0):
    return int(dt.datetime(y, m, d, h, mi).timestamp())


def test_start_supported_by_numeric_time():
    """源文写「14:00」⇒ 14:00 有依据。"""
    assert deadline.start_supported(_at(2026, 9, 16, 14), "周三14:00在体育馆集合")


def test_start_supported_by_fullwidth_colon():
    """全角冒号也是同一个时刻（源文里两种都常见）。"""
    assert deadline.start_supported(_at(2026, 9, 16, 14), "周三14：00集合")


def test_start_supported_by_chinese_hour():
    assert deadline.start_supported(_at(2026, 9, 16, 14), "下午2点在体育馆")


def test_start_supported_by_chinese_hour_without_period():
    """⚠️ 不带时段词也要认（「8点集合」是极常见的省略写法）。"""
    assert deadline.start_supported(_at(2026, 9, 16, 8), "明早8点集合")


def test_start_supported_accepts_half_past():
    """⚠️ 「7点半」必须认 —— 源文里真出现过，漏判会丢掉一个真时刻。"""
    assert deadline.start_supported(_at(2026, 9, 16, 19, 30), "晚上7点半开会")


def test_start_supported_with_minutes():
    assert deadline.start_supported(_at(2026, 9, 16, 14, 30), "14:30开始")


def test_start_supported_rejects_invented_hour():
    """⭐⭐ **这道闸门存在的全部理由**：源文只写「明早集合」，没说几点。

    模型最想补全的就是这一格 —— 实测源文「明早 7:20 集合」时它会编一个
    具体**日期**；时刻同理，而且更危险（它会按分钟驱动日程表）。
    """
    assert not deadline.start_supported(_at(2026, 9, 16, 8), "明早集合，别迟到")
    assert not deadline.start_supported(_at(2026, 9, 16, 14), "周六下午在体育馆")
    assert not deadline.start_supported(_at(2026, 9, 16, 19, 30), "晚上见")


def test_start_supported_rejects_wrong_hour():
    """⚠️ 源文有 14:00，但模型报的是 15:00 ⇒ **不背书**。

    ⚠️ 这条挡的是"模糊匹配"式实现：一个只匹配「有冒号的数字」的实现
    会在这里绿 —— 那就等于闸门没做。
    """
    assert not deadline.start_supported(_at(2026, 9, 16, 15), "周三14:00集合")


def test_start_supported_rejects_half_from_a_different_minute():
    """⚠️ 「7点半」不能给「07:00」背书：半 ≠ 整点。"""
    assert not deadline.start_supported(_at(2026, 9, 16, 19), "晚上7点半开会")


def test_start_supported_rejects_across_a_newline():
    """⚠️ 与日期同一条判据：源文是**多条消息用 "\\n" 拼的**，不许跨行粘。

    不挡的话，「……14点」结尾的一条消息与「00 开始……」开头的另一条会被
    粘成一个 `14:00` —— **凭空造出「源文里有」的证据**。
    """
    assert not deadline.start_supported(_at(2026, 9, 16, 14), "下午的事\n00 开始")


def test_start_none_is_never_supported():
    """没有 start 就没有可核验的东西。"""
    assert not deadline.start_supported(None, "14:00 集合")
    assert not deadline.start_supported(0, "14:00 集合")


# ── 死线倒数分级（`docs/design/时间注入.md` §二）────────────────────
#
# ⚠️ **这组用例与前端 `web/src/__tests__/format.test.ts::relDeadline` 逐条对应。**
# 两边各写一份是知情取舍（日报落盘要后端算、日历要跟当前时刻要前端算），
# 代价是阈值可能漂移 —— 而漂移的症状是「日历上标红了、日报里却没提」，
# 单看任何一边都正常。所以**边界值必须两边都钉**。
#
# ⚠️ 别用 `_ts()` 造「今天」：它是固定日期，而 `deadline_level` 要一个显式的
# `now_ts`。用一个固定的 now 让用例**不依赖当前时刻**（否则今天绿明天红）。

_NOW = _ts(2026, 9, 16, 10)  # 2026-09-16 10:00 本地


def test_deadline_level_expired():
    assert deadline.deadline_level(_ts(2026, 9, 15), _NOW) == "expired"
    assert deadline.rel_deadline_text(_ts(2026, 9, 14), _NOW) == "已过期 2 天"


def test_deadline_level_urgent_boundary_0_and_1():
    """⭐ 边界：0 天与 1 天都算 urgent。"""
    assert deadline.deadline_level(_ts(2026, 9, 16), _NOW) == "urgent"
    assert deadline.deadline_level(_ts(2026, 9, 17), _NOW) == "urgent"
    assert deadline.rel_deadline_text(_ts(2026, 9, 16), _NOW) == "今天截止"
    assert deadline.rel_deadline_text(_ts(2026, 9, 17), _NOW) == "明天截止"


def test_deadline_level_urgent_within_the_same_local_day():
    """⚠️ 按**本地日**对齐，不是按秒差。

    现在是 09-16 10:00，死线是 09-17 09:00（差 23 小时）。按秒差取整会得到
    0 天 ⇒「今天截止」——**差了一天**。按本地日算才是「明天截止」。
    """
    deadline_ts = _ts(2026, 9, 17, 9)
    assert deadline.deadline_level(deadline_ts, _NOW) == "urgent"
    assert deadline.rel_deadline_text(deadline_ts, _NOW) == "明天截止"


def test_deadline_level_boundary_between_urgent_and_soon():
    """2 天起离开 urgent（`<= 1` 与 `<= 3` 的分界）。"""
    assert deadline.deadline_level(_ts(2026, 9, 18), _NOW) == "soon"


def test_deadline_level_boundary_between_soon_and_normal():
    """4 天起进入 normal（`<= 3` 的分界）。"""
    assert deadline.deadline_level(_ts(2026, 9, 20), _NOW) == "normal"


def test_deadline_level_boundary_at_seven_days():
    """⭐ 边界：7 天仍是「还有 N 天」的文案，8 天起回落成绝对日期。"""
    assert deadline.rel_deadline_text(_ts(2026, 9, 23), _NOW) == "还有 7 天"
    assert deadline.rel_deadline_text(_ts(2026, 9, 24), _NOW) == "9月24日"
    # 但分级不变——两者都是 normal（8 天不该因为文案变了就换档）
    assert deadline.deadline_level(_ts(2026, 9, 23), _NOW) == "normal"
    assert deadline.deadline_level(_ts(2026, 9, 24), _NOW) == "normal"
