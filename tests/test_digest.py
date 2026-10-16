"""日报合成的测试。**全部用构造样本，不联网、不碰 data/vigil.db。**"""

from __future__ import annotations

import datetime as dt
import json
import sqlite3
from pathlib import Path

import pytest

from vigil import digest
from vigil.redact import Redactor
from vigil.store import WindowItem


def _item(
    item_id: int = 1,
    *,
    kind: str = "notice",
    title: str = "体检表",
    detail: str | None = "10月8日前交到辅导员处",
    event_ts: int = 1000,
    deadline_ts: int | None = None,
    group_id: int = 100,
    place: str | None = None,
    amount: str | None = None,
    confidence: float = 0.9,
) -> WindowItem:
    return WindowItem(
        item_id=item_id, kind=kind, title=title, detail=detail,
        event_ts=event_ts, deadline_ts=deadline_ts, group_id=group_id,
        place=place, amount=amount, confidence=confidence,
    )


# ── 载荷构造 ────────────────────────────────────────────────


def test_payload_sends_group_name_not_id():
    """⚠️ 探针第一版就是把 group_id 明文发了出去——这条测试守着它。"""
    payload = digest.build_items_payload(
        [_item(group_id=900000001)], {900000001: "新生群"}, Redactor()
    )

    assert payload[0]["group"] == "新生群"
    assert "900000001" not in json.dumps(payload, ensure_ascii=False)


def test_payload_sanitizes_fullwidth_quotes():
    payload = digest.build_items_payload(
        [_item(title="卖笔记", detail="昵称“某某网盘”")], {}, Redactor()
    )

    assert "“" not in payload[0]["detail"]
    assert "「某某网盘」" in payload[0]["detail"]


def test_payload_redacts_phone_numbers():
    """item 是模型从脱敏文本里抽的，但它会补出原文没有的实体——出网前再抹一遍。"""
    payload = digest.build_items_payload(
        [_item(detail="联系 13812345678 报名")], {}, Redactor()
    )

    assert "13812345678" not in payload[0]["detail"]


def test_payload_redacts_numbers_inside_group_name():
    """⚠️ 群名同样是**出网文本**，也要过 ``Redactor``——口径统一，不留例外。

    ``group`` 原先只过 ``sanitize_for_llm``，是「出网字段」里**唯一**的例外，
    而它的 docstring 自称「每个文本字段都过 Redactor」。

    触发路径很短：``config/groups.toml`` 是人工维护的，写一个
    ``XX交流群900000002`` 就明文出网——而 ``Redactor.IDENTITY_NUM`` 的
    ``群`` 分支恰好就是为这种写法设计的。群名只作**合并信号**用（不直接
    展示），抹了号码也无害；「例外需要人记住」才是真正的风险。

    变异反证：把 ``group`` 换回 ``sanitize_for_llm(names.get(...))`` → 本条红。
    """
    payload = digest.build_items_payload(
        [_item(group_id=900000002)], {900000002: "XX交流群900000002"}, Redactor()
    )

    assert "900000002" not in payload[0]["group"]
    # 抹号码不等于把合并信号整段抹掉——群名本身必须还在
    assert "XX交流群" in payload[0]["group"]


def test_payload_formats_time_and_deadline():
    import datetime as dt

    ts = int(dt.datetime(2026, 9, 13, 14, 20).timestamp())
    dl = int(dt.datetime(2026, 10, 8).timestamp())
    # ⚠️ 修复轮次 2：这条的**三条断言一字未改**，只是夹具显式传了 ``trusted``——
    # 自本次起 «截止日出网» 多了一个前提（它得先在源文里有字面依据），
    # 不传就是「都不信任」，deadline 会是 None。这是夹具适配，不是放宽。
    payload = digest.build_items_payload(
        [_item(event_ts=ts, deadline_ts=dl)], {}, Redactor(), trusted={1}
    )

    assert payload[0]["time"] == "14:20"
    assert payload[0]["deadline"] == "2026-10-08"


def test_payload_withholds_deadline_from_untrusted_item():
    """⚠️ 未通过核验的截止日**不许发给模型**——它本来就没有依据（修复轮次 2）。

    Task 7 重跑抓到 Task 6 只堵了一半：程序那个「· 截止 09-15 ·」戳被挡住了，
    可 ``deadline`` 字段照旧出网，模型于是**自己在正文里写了出来**。实测
    9/12 那篇（item 250 已被正确降级、程序戳确实没加）::

        - **西太湖新媒体补录** 针对今天未面试同学，地点待定，明天通知，
          **截止 9 月 15 日**。 · 西太湖新媒体部门招新群

    —— 读者看到的还是同一个无依据的日期。机械核验只堵了程序那个入口，
    模型措辞那个入口是敞着的。模型拿不到这个日期，就不会写出来。
    """
    import datetime as dt

    dl = int(dt.datetime(2026, 10, 8).timestamp())
    items = [
        _item(1, title="没依据", deadline_ts=dl),
        _item(2, title="有依据", deadline_ts=dl, event_ts=2000),
    ]

    payload = digest.build_items_payload(items, {}, Redactor(), trusted={2})

    assert payload[0]["deadline"] is None           # 未受信 → 不发
    assert payload[1]["deadline"] == "2026-10-08"   # 受信 → 照发
    # 字段形状保持稳定：键必须在、值是 None，不是省略键（省略会让模型
    # 在「哪些条目本来有截止日」上失去可比信号）
    assert "deadline" in payload[0]
    assert list(payload[0]) == list(payload[1])


def test_payload_withholds_every_deadline_by_default():
    """⚠️ 默认必须是「都不信任」——与 `build_rows` 的 `trusted=None` 同款理由：
    默认全信任的话，任何忘记传参的调用方都会静默退回危险行为（把模型编的
    日期发出去，再由模型写进正文）。

    变异反证：默认值改成全集，或去掉 `digest()` 里的 `trusted=trusted` → 红。
    """
    import datetime as dt

    dl = int(dt.datetime(2026, 10, 8).timestamp())

    payload = digest.build_items_payload([_item(1, deadline_ts=dl)], {}, Redactor())

    assert payload[0]["deadline"] is None


def test_payload_null_dashes_stay_null():
    payload = digest.build_items_payload(
        [_item(detail=None, place=None, amount=None, deadline_ts=None)], {}, Redactor()
    )

    assert payload[0]["detail"] is None
    assert payload[0]["deadline"] is None


def test_system_prompt_states_the_three_contracts():
    prompt = digest.build_system_prompt()

    assert "quotes" in prompt      # 逐字摘录
    assert "label" in prompt       # 短标签
    assert "不要编造" in prompt     # 不许补原文没有的实体


def test_user_prompt_carries_the_day():
    prompt = digest.build_user_prompt([], day="2026-09-13")

    assert "2026-09-13" in prompt


# ── 守卫补强（审查轮次 1：F1–F4）──────────────────────────────
#
# 上面 7 条是 brief 给的，覆盖的是「已经写好的那几条」。独立变异反证（22 个）
# 抓出它们的覆盖缺口：**载荷有无、提示词规则、别的文本字段、条数守恒**
# 这四类破坏全都 24 passed。下面每条都配了变异证据，见 task-2-report.md。


def test_payload_keeps_every_item():
    """⚠️ 窗口内**每一条** item 都必须出现在载荷里（设计判断第 1 条）。

    日报「不再筛第二遍」是这个工具最不能犯的错——静默漏掉正是它要防的事。
    变异反证：`for item in items:` 改成 `items[:1]`，原先 7 条测试全绿
    （因为它们全都只造 1 条 item，数量根本没被约束）。
    """
    items = [_item(i, title=f"条目{i}", event_ts=1000 + i) for i in range(1, 6)]

    payload = digest.build_items_payload(items, {}, Redactor())

    assert len(payload) == len(items)
    assert [p["title"] for p in payload] == [it.title for it in items]


def test_payload_cleans_every_text_field():
    """⚠️ 三条硬规则要覆盖所有**会出网的文本字段**，不只 detail。

    ``clean()`` 管着 title / detail / place / amount 四个字段。title 尤其危险：
    它是模型被要求「逐字摘录」回连的字段（规则 3），全角引号出现在 title 时
    照旧触发退化。

    变异反证：让 title/place/amount 同时绕过 ``clean()``，原先全绿。
    """
    payload = digest.build_items_payload(
        [
            _item(
                title="昵称“某某网盘”卖笔记",
                detail="联系 13812345678 报名",
                place="“三食堂”门口 13812345678",
                amount="“50 元” 13812345678",
            )
        ],
        {},
        Redactor(),
    )

    for field in ("title", "detail", "place", "amount"):
        assert "“" not in payload[0][field], field
        assert "13812345678" not in payload[0][field], field


def test_payload_unknown_group_id_does_not_leak_the_id():
    """⚠️ 群不在 ``names`` 里时，兜底也绝不能把群号发出去（spec §4.5）。

    真实触发场景：DB 里出现未配置进 ``config/groups.toml`` 的群。
    变异反证：兜底从 ``""`` 改成 ``str(item.group_id)``，原先全绿——
    而这是**群号明文出网**。
    """
    payload = digest.build_items_payload([_item(group_id=900000001)], {}, Redactor())

    assert payload[0]["group"] == ""
    assert "900000001" not in json.dumps(payload, ensure_ascii=False)


def test_system_prompt_fingerprint_matches_version():
    """改了系统提示词就必须同时更新指纹 + 升 ``PROMPT_VERSION``。

    这条**替换**了原先的 ``test_system_prompt_carries_each_rule``（7 条规则
    各一个特征短语，逐条 ``in`` 断言）。换掉的理由是**开火方向反了**，
    实测（隔离副本 + 阳性对照）：

    * 「加一条规则 8」「改前言」这类**真改行为**的编辑 → 216 全绿，抓不住；
    * 规则 5「说清」→「讲清」（**语义不变**）→ 1 failed。

    也就是说它只对最无害的改动类别开火，而最可能改变行为的三类（前言 /
    末尾的 JSON 形状行 / 增删规则）全无守卫；更糟的是它训练人「红灯了就
    改测试里的字符串」，同一个动作正好绕开 ``PROMPT_VERSION``。

    指纹替过来之后：任何提示词正文改动（含前言、形状行）都会红，红的时候
    逼人做一次「升不升版本」的显式决定——这正是 Constraint 11 想要的摩擦。
    代价是措辞微调也会红，而那正是刻意的（见 ``_PROMPT_FINGERPRINT`` 的注释）。

    ⚠️ 断言的两边**必须一个来自运行时代码、一个来自字面量**：
    ``_PROMPT_FINGERPRINT`` 里的版本号是**重复写的字面量**，不是
    ``PROMPT_VERSION`` 的引用。写成 ``(PROMPT_VERSION, sha)`` 的话，
    改版本号时两边一起变、断言恒真——那正是原先
    ``assert cfg.max_tokens == digest.MAX_TOKENS`` 那种「自指」的老毛病。
    """
    import hashlib

    sha = hashlib.sha256(digest.build_system_prompt().encode("utf-8")).hexdigest()[:12]

    assert (digest.PROMPT_VERSION, sha) == digest._PROMPT_FINGERPRINT, (
        "提示词指纹对不上：改了 build_system_prompt 就必须**同时**更新 "
        "_PROMPT_FINGERPRINT 里的 sha 与 PROMPT_VERSION——两件事绑在一次编辑里，"
        "别漏掉任一件（digests.prompt_ver 是 M3 判断「哪些日报该重跑」的依据）"
    )


# ── 常量量级守卫（审查轮次 2：F6）───────────────────────────
#
# 上面那条指纹守住了 PROMPT_VERSION 与提示词的绑定，但常量里还有一个
# **没有取值守卫**的：MAX_TOKENS。原先唯一提到它的断言是
# `test_digest_sends_max_tokens_and_thinking_off` 里的
# `configs[0].max_tokens == digest.MAX_TOKENS`——两边同一个全局，**自指**：
# 审查实测把 `MAX_TOKENS` 改成 `1000000`，216 全绿（SURVIVED）。
# 那条测试守的是「接线」（常量有没有被传下去），守不了「取值」。下面这条补取值。


def test_max_tokens_stays_in_a_sane_range():
    """``MAX_TOKENS`` 的量级要钉住——它的用途是**模型退化时及时掐断**。

    背景（见 ``vigil/llm.py`` 与 ``digest.MAX_TOKENS`` 的注释）：模型遇到
    全角引号会退化成无限空格循环，此时唯一能拦住它的是超时
    ——180s × 3 次重试 ≈ 9 分钟空转；带上 max_tokens 后同样的失控请求
    23s 就停。把上限抬到一百万，等于把这个阀拆了。

    边界：实测正常一天的日报约 550 输出 token（10 条 item 的日子实测
    534），所以下限 500 保证正常日子不会被误截断；上限 8000 保证退化时
    能远早于超时掐断。**不是「越精确越好」**——它守的是量级，不是数字。
    """
    assert 500 <= digest.MAX_TOKENS <= 8000


def test_user_prompt_embeds_every_item():
    """⚠️ 载荷必须真的进到 user prompt 里，且一条不少。

    原先只断言 ``"2026-09-13" in prompt``，而日期出现在**前缀**里，
    与载荷无关。

    变异反证：①把 ``body`` 整个丢掉只留日期前缀 → 原先全绿，等于拿空
    items 列表去问模型；②``ensure_ascii=False`` 改成 ``True`` → 原先全绿，
    中文全变 ``\\uXXXX``（模型看不到原文，输出 token 暴涨）。
    """
    payload = digest.build_items_payload(
        [_item(1, title="体检表"), _item(2, title="讲座", event_ts=2000)], {}, Redactor()
    )

    prompt = digest.build_user_prompt(payload, day="2026-09-13")

    assert "体检表" in prompt
    assert "讲座" in prompt
    # ensure_ascii=True 会把中文写成 \uXXXX——见 docstring 变异反证 ②
    assert "\\u" not in prompt

# ── quote 匹配 ──────────────────────────────────────────────


def test_match_lines_finds_item_by_quote():
    items = [_item(1, title="体检表"), _item(2, title="讲座", event_ts=2000)]

    lines, unmatched = digest.match_lines(
        [{"quotes": ["讲座"], "label": "学术讲座", "text": "周五晚 7 点"}], items
    )

    assert unmatched == []
    assert len(lines) == 1
    assert lines[0].label == "学术讲座"
    assert lines[0].item_ids == (2,)


def test_match_lines_tolerates_inserted_spaces():
    """⚠️ 实测：模型会在数字两侧插空格（原文「某某网盘310」它写成「某某网盘 310」）。

    直接子串匹配会漏掉这种**格式**差异，但归一化后两侧都无空格、仍逐字对应。
    """
    items = [_item(1, detail="百度网盘搜索昵称“某某网盘310”")]

    lines, unmatched = digest.match_lines(
        [{"quotes": ["某某网盘 310"], "label": "卖笔记", "text": ""}], items
    )

    assert unmatched == []
    assert lines[0].item_ids == (1,)


@pytest.mark.parametrize(
    "db_title, quoted",
    [
        # 库内原文是弯引号，模型照规则 3 回抄它**看到的**「」（sanitize 换过）
        ("“某某网盘”卖笔记", "「某某网盘」卖笔记"),
        # 反向：库内原文本就是「」（中文语料里会出现），模型回抄成弯引号
        ("「某某网盘」卖笔记", "“某某网盘”卖笔记"),
    ],
)
def test_match_lines_tolerates_quote_style_swapped_by_sanitize(db_title, quoted):
    """⚠️ 引号**不能夹在中间**——夹了就整行匹配不上，静默退化成机械补行。

    出网前 ``sanitize_for_llm`` 把 ``“”`` 换成 ``「」``（防模型退化成无限空格
    循环），而匹配用的 blob 是**库内原文**。若 ``_norm`` 只剥 ``「」`` 不剥
    ``“”``，两侧的引号字符就对不齐：模型逐字回抄它看到的 ``「某某网盘」卖笔记``，
    归一化后是 ``某某网盘卖笔记``，而库内原文归一化后仍是
    ``“某某网盘”卖笔记``——**引号把中间那段切断了**，子串匹配失败。

    后果不是「少一条」，是**整行被丢 → 退化成机械补行**，而 CLI 退出码仍是 0
    （同 F-A/F-E 的静默形状）。原先的 ``_PUNCT`` 里没有引号，这个 case 就是红的。

    变异反证：把 ``“”`` 从 ``_PUNCT`` 里去掉 → **两条都红**（实测 2 failed）——
    上面那两个方向都要靠它兜住。
    """
    items = [_item(1, title=db_title, detail=None)]

    lines, unmatched = digest.match_lines(
        [{"quotes": [quoted], "label": "卖笔记", "text": ""}], items
    )

    assert unmatched == []
    assert lines[0].item_ids == (1,)


def test_match_lines_merges_multiple_quotes():
    items = [_item(1, title="面试地点变更"), _item(2, title="录取名单公布", event_ts=2000)]

    lines, _ = digest.match_lines(
        [{"quotes": ["面试地点变更", "录取名单公布"], "label": "新媒体", "text": "…"}],
        items,
    )

    assert lines[0].item_ids == (1, 2)


def test_match_lines_reports_unmatched_quotes():
    items = [_item(1, title="体检表")]

    lines, unmatched = digest.match_lines(
        [{"quotes": ["根本不存在的标题"], "label": "x", "text": ""}], items
    )

    assert lines == []
    assert unmatched == ["根本不存在的标题"]


def test_match_lines_drops_too_short_quote():
    """太短的摘录容易误命中，宁可丢——沿用 refine._match_source 的判断。"""
    items = [_item(1, title="体检表")]

    lines, unmatched = digest.match_lines(
        [{"quotes": ["表"], "label": "x", "text": ""}], items
    )

    assert lines == []
    assert unmatched == ["表"]


def test_match_lines_ignores_malformed_entries():
    """模型偶尔会返回字符串或 null 元素，不能让它们崩掉整轮。"""
    lines, unmatched = digest.match_lines(
        ["不是字典", None, {"quotes": "不是数组"}, {"quotes": ["体检表"], "label": "a"}],
        [_item(1, title="体检表")],
    )

    assert len(lines) == 1
    assert lines[0].label == "a"


def test_match_lines_falls_back_to_title_when_label_missing():
    lines, _ = digest.match_lines(
        [{"quotes": ["体检表"], "text": ""}], [_item(1, title="体检表")]
    )

    assert lines[0].label == "体检表"


def test_match_lines_one_quote_matching_two_items_marks_both():
    """标题重复的两条（实测全库有，如「校园卡办理」系列）必须都算覆盖。"""
    items = [_item(1, title="校园卡办理"), _item(2, title="校园卡办理", event_ts=2000)]

    lines, _ = digest.match_lines(
        [{"quotes": ["校园卡办理"], "label": "校园卡", "text": ""}], items
    )

    assert lines[0].item_ids == (1, 2)


# ── 机械补行 ────────────────────────────────────────────────


def test_build_rows_appends_mechanical_row_for_uncovered_item():
    items = [_item(1, title="体检表"), _item(2, title="讲座", event_ts=2000)]
    lines, _ = digest.match_lines(
        [{"quotes": ["体检表"], "label": "体检", "text": "10月8日前交"}], items
    )

    rows = digest.build_rows(lines, items)

    assert [r.mechanical for r in rows] == [False, True]
    assert rows[1].label == "讲座"


def test_build_rows_marks_low_confidence_only_when_all_items_low():
    items = [
        _item(1, title="存疑", confidence=0.1),
        _item(2, title="确定的", confidence=0.9, event_ts=2000),
    ]
    lines, _ = digest.match_lines(
        [
            {"quotes": ["存疑"], "label": "a", "text": ""},
            {"quotes": ["确定的"], "label": "b", "text": ""},
        ],
        items,
    )

    rows = digest.build_rows(lines, items)

    assert rows[0].low_confidence is True
    assert rows[1].low_confidence is False


def test_build_rows_merged_row_is_confident_when_any_item_is():
    items = [_item(1, title="存疑", confidence=0.1), _item(2, title="确定的", confidence=0.9, event_ts=2000)]
    lines, _ = digest.match_lines(
        [{"quotes": ["存疑", "确定的"], "label": "a", "text": ""}], items
    )

    rows = digest.build_rows(lines, items)

    assert rows[0].low_confidence is False
    assert rows[0].deadline_ts is None


def test_build_rows_takes_earliest_deadline_of_merged_items():
    items = [
        _item(1, title="甲", deadline_ts=5000),
        _item(2, title="乙", deadline_ts=4000, event_ts=2000),
    ]
    lines, _ = digest.match_lines(
        [{"quotes": ["甲", "乙"], "label": "合并", "text": ""}], items
    )

    assert digest.build_rows(lines, items)[0].deadline_ts == 4000


def test_build_rows_mechanical_row_keeps_own_confidence():
    items = [_item(1, title="存疑的", confidence=0.1)]

    rows = digest.build_rows([], items)

    assert rows[0].mechanical is True
    assert rows[0].low_confidence is True


# ── 渲染 ────────────────────────────────────────────────────


@pytest.fixture
def cats():
    from vigil.categories import Category

    return (
        Category("notice", "通知公告", "正式通知", "📋"),
        Category("activity", "活动", "讲座与社团", "🎤"),
        Category("academic", "学业", "作业与考试", "📚"),
    )


def _render(rows, cats, *, window_from=0, names=None, groups=3, messages=412,
            now_ts=None):
    return digest.render_markdown(
        day="2026-09-13",
        stat_line=digest.stat_line(groups=groups, messages=messages,
                                   items=sum(1 for _ in rows) or 0),
        rows=rows, cats=cats, names=names or {}, window_from=window_from,
        # ⚠️ 缺省 `None` 与生产路径一致地"不显示相对天数"——测试要可控，
        # 不能依赖"跑用例的那一刻是几号"。
        now_ts=now_ts,
    )


def _row(label, text="", kind="notice", group_id=100, deadline_ts=None,
         low_confidence=False, mechanical=False, deadline_trusted=False):
    """构造 ``Row`` 的助手——避免每处都写一串位置参数（Task 6 加了第 8 个字段）。"""
    return digest.Row(label, text, kind, group_id, deadline_ts, low_confidence,
                      mechanical, deadline_trusted)


def test_render_header_and_stat_line(cats):
    out = _render([digest.Row("体检表", "10月8日前交", "notice", 100, None, False)], cats)

    assert out.startswith("# 守夜人日报 · 2026-09-13")
    assert "3 个群 412 条消息" in out
    assert "## 📋 通知公告" in out
    assert "- **体检表** 10月8日前交" in out


# ── 标题带星期（`docs/design/时间注入.md` §三）─────────────────────


def test_render_title_includes_chinese_weekday(cats):
    """⭐ 「周三截止」这种写法在群消息里到处都是，标题带星期才能一眼对上。

    ⚠️ 2026-09-13 是**星期日**。这个日子是查表的边界（`weekday()` 的
    6 对应「日」，写成 7 会 IndexError）——不是随手挑的。
    """
    out = _render([_row("体检表")], cats)
    assert out.startswith("# 守夜人日报 · 2026-09-13 · 星期日")


def test_render_weekday_for_a_midweek_day(cats):
    """2026-09-16 是星期三——钉住表中段，防止整表移位。"""
    out = digest.render_markdown(
        day="2026-09-16", stat_line="x", rows=[_row("体检表")], cats=cats, names={},
    )
    assert out.startswith("# 守夜人日报 · 2026-09-16 · 星期三")


def test_render_weekday_survives_an_unparseable_day(cats):
    """⚠️ 日期解析不了 ⇒ **不加星期**，而不是抛。

    渲染标题不该是参数校验的地方——真正的校验在 `day_window`（它给可读的
    参数错误）。这里再抛一次只会让错误更难懂。
    """
    out = digest.render_markdown(
        day="不是日期", stat_line="x", rows=[_row("体检表")], cats=cats, names={},
    )
    assert out.startswith("# 守夜人日报 · 不是日期")


# ── 「别忘」的临近提醒（相对天数）────────────────────────────────


def _now(y, m, d, h=10):
    return int(dt.datetime(y, m, d, h).timestamp())


def _deadline(y, m, d, h=12):
    return int(dt.datetime(y, m, d, h).timestamp())


def test_alert_shows_relative_countdown(cats):
    """⭐ 3 天后 ⇒「还有 3 天」出现在「别忘」行里。"""
    out = _render(
        [_row("体检表", deadline_ts=_deadline(2026, 9, 16), deadline_trusted=True)],
        cats,
        window_from=0,
        now_ts=_now(2026, 9, 13),
    )
    assert "还有 3 天" in out


def test_alert_shows_today_and_tomorrow(cats):
    """⭐ 边界：0 天与 1 天都给催办文案。"""
    today = _render(
        [_row("体检表", deadline_ts=_deadline(2026, 9, 13), deadline_trusted=True)],
        cats, window_from=0, now_ts=_now(2026, 9, 13),
    )
    assert "今天截止" in today
    tomorrow = _render(
        [_row("体检表", deadline_ts=_deadline(2026, 9, 14), deadline_trusted=True)],
        cats, window_from=0, now_ts=_now(2026, 9, 13),
    )
    assert "明天截止" in tomorrow


def test_alert_omits_countdown_beyond_a_week(cats):
    """⚠️ 超过一周（`normal` 档）**不加**相对天数——只是把日期又说了一遍。

    ⚠️ 变异反证：把 `if level != "normal"` 去掉 ⇒ 本条必红。
    """
    out = _render(
        [_row("体检表", deadline_ts=_deadline(2026, 10, 20), deadline_trusted=True)],
        cats, window_from=0, now_ts=_now(2026, 9, 13),
    )
    assert "还有" not in out
    assert "截止 10-20" in out


def test_no_countdown_without_now_ts(cats):
    """⚠️ 不给 `now_ts` ⇒ **只写绝对日期**（手工重渲染历史日报走这条路）。

    没有它，重渲染一份 9 月的日报会写出「还有 30 天」——那对那个日期无意义。
    """
    out = _render(
        [_row("体检表", deadline_ts=_deadline(2026, 9, 16), deadline_trusted=True)],
        cats, window_from=0,
    )
    assert "截止 09-16" in out
    assert "还有" not in out


def test_render_appends_group_name(cats):
    out = _render(
        [digest.Row("体检表", "", "notice", 100, None, False)],
        cats, names={100: "示例班级群"},
    )

    assert out.rstrip().endswith("· 示例班级群")


def test_render_omits_tail_when_group_unknown(cats):
    """群名查不到时不要退回群号——那是本不该出现在成品里的数字。"""
    out = _render([digest.Row("体检表", "", "notice", 999, None, False)], cats)

    assert "999" not in out


def test_render_alerts_section_for_future_deadline(cats):
    """⚠️ Task 6 后「进别忘」多了一个前提：截止日必须在源文里**有依据**。

    这条守的是**版式**（截至日在窗口内就进「别忘」、进了就不再在类目区块重复），
    所以夹具给它 ``deadline_trusted=True``——三条断言一条没动，**不是弱化**。
    「没依据就不进别忘」由 `test_render_excludes_untrusted_deadline_from_alerts` 守。
    """
    rows = [_row("体检表", "交到辅导员处", "notice", 100, 5000, False,
                 deadline_trusted=True)]

    out = _render(rows, cats, window_from=1000)

    assert "## ⏰ 别忘" in out
    assert "截止 " in out
    # 进了「别忘」就不该在类目区块里再出现一次
    assert out.count("**体检表**") == 1


def test_render_excludes_already_past_deadline_from_alerts(cats):
    """⚠️ 实测数据里有 deadline 早于事件日的条目（8/5 的条目挂 5/6 的截止）。

    不过滤的话，八月日报里会冒出「别忘 5 月 6 日」这种不可能执行的事。
    """
    rows = [digest.Row("档案袋封口", "", "notice", 100, 500, False)]

    out = _render(rows, cats, window_from=1000)

    assert "## ⏰ 别忘" not in out
    assert "- **档案袋封口**" in out        # 仍然要出现在类目区块里，不丢


def test_render_sections_follow_category_order(cats):
    rows = [
        digest.Row("a", "", "academic", 100, None, False),
        digest.Row("b", "", "activity", 100, None, False),
        digest.Row("c", "", "notice", 100, None, False),
    ]

    out = _render(rows, cats)

    assert out.index("## 📋 通知公告") < out.index("## 🎤 活动") < out.index("## 📚 学业")


def test_render_unknown_kind_is_kept_not_dropped(cats):
    """类目表里没有的 kind（理论上不该有）排到最后，但**不能静默丢**。"""
    out = _render([digest.Row("x", "", "没见过的类目", 100, None, False)], cats)

    assert "- **x**" in out


def test_render_unsure_section_comes_last(cats):
    rows = [
        digest.Row("确定", "", "notice", 100, None, False),
        digest.Row("存疑", "", "academic", 100, None, True),
    ]

    out = _render(rows, cats)

    assert "## 🤔 拿不准的" in out
    assert out.index("## 🤔 拿不准的") > out.index("## 📋 通知公告")
    assert out.index("**存疑**") > out.index("## 🤔 拿不准的")


def test_render_low_confidence_beats_deadline(cats):
    """既低置信度又有截止日的行归「拿不准」——存疑的信息不该催人去办。"""
    rows = [digest.Row("存疑", "", "notice", 100, 5000, True)]

    out = _render(rows, cats, window_from=1000)

    assert "## ⏰ 别忘" not in out
    assert "## 🤔 拿不准的" in out


def test_render_empty_window_says_no_news(cats):
    out = digest.render_markdown(
        day="2026-08-08", stat_line=digest.stat_line(groups=2, messages=12, items=0),
        rows=[], cats=cats, names={},
    )

    assert "没有值得一提的信息" in out
    assert "# 守夜人日报 · 2026-08-08" in out


def test_stat_line_mentions_items_when_present(cats):
    assert "提炼出 7 条" in digest.stat_line(groups=3, messages=412, items=7)


def test_stat_line_omits_items_when_zero():
    line = digest.stat_line(groups=2, messages=12, items=0)

    assert "提炼出" not in line
    assert "12" in line


# ── 窗口换算 ────────────────────────────────────────────────


def test_day_window_covers_exactly_one_day():
    since, until = digest.day_window("2026-09-13")

    assert until - since == 86400
    import datetime as dt

    assert dt.datetime.fromtimestamp(since).strftime("%H:%M") == "00:00"


def test_day_window_rejects_garbage():
    with pytest.raises(ValueError):
        digest.day_window("昨天")


def test_yesterday_defaults_to_the_day_before_today():
    import datetime as dt

    assert digest.yesterday(dt.date(2026, 9, 15)) == "2026-09-14"


# ── 锚点不变量（审查轮次 1：I1）─────────────────────────────
#
# 锚点 = `hits.sort(key=(event_ts, item_id))` 之后的**第一个** item，它决定整行的
# 分区与署名群。审查实测：删掉那行 `hits.sort`、把 `picked[0]` 改成 `picked[-1]`、
# 或把排序键退化成只按 `item_id`，**当时的 200 条测试全绿**——三条都是静默错配。
# 根因是所有多命中夹具的「命中序」恰好等于「排序序」。下面两条刻意让两者**错开**。


def test_anchor_is_earliest_item_regardless_of_input_order():
    """items 逆序传入 + quotes 逆序列出，锚点仍须是 event_ts 最小的那条。

    红线：模型对 `quotes` 的顺序没有任何约束（prompt 也没规定），
    所以「按 quotes 命中序取锚点」= 让模型的书写顺序决定群名与分区。
    """
    items = [
        _item(9, title="晚的", event_ts=2000, kind="activity", group_id=200),
        _item(1, title="早的", event_ts=1000, kind="notice", group_id=100),
    ]

    lines, _ = digest.match_lines(
        [{"quotes": ["晚的", "早的"], "label": "L", "text": ""}], items
    )

    assert lines[0].item_ids == (1, 9)
    row = digest.build_rows(lines, items)[0]
    assert (row.kind, row.group_id) == ("notice", 100)


def test_anchor_ranks_by_event_ts_before_item_id():
    """排序键是 ``(event_ts, item_id)``——**先比 event_ts**。

    这条夹具刻意让 `item_id` 的顺序与 `event_ts` 的顺序**相反**（早的那条
    item_id 更大）：只按 `item_id` 排的实现会挑错锚点，而 `item_ids` 集合、
    `label` / `text` 全都照旧——**唯独分区与署名群错**。
    """
    items = [
        _item(2, title="晚的", event_ts=2000, kind="activity", group_id=200),
        _item(9, title="早的", event_ts=1000, kind="notice", group_id=100),
    ]

    lines, _ = digest.match_lines(
        [{"quotes": ["晚的", "早的"], "label": "L", "text": ""}], items
    )

    assert lines[0].item_ids == (9, 2)
    row = digest.build_rows(lines, items)[0]
    assert (row.kind, row.group_id) == ("notice", 100)


# ── 编排 ────────────────────────────────────────────────────


class _FakeLLM:
    """假 LLM：记录调用次数，返回预设载荷。绝不联网。"""

    def __init__(self, lines):
        self.lines = lines
        self.calls = 0
        self.configs = []
        self.users = []          # 每次调用收到的 user prompt（Task 6 修复轮次 2 起）

    def __call__(self, cfg, *, system, user, sleep=None):
        from vigil.llm import LLMResult

        self.calls += 1
        self.configs.append(cfg)
        self.users.append(user)
        return LLMResult(payload={"lines": self.lines}, input_tokens=100,
                         output_tokens=50)


@pytest.fixture
def seeded(memdb):
    """两条 item + 一条窗口外 + 完整 messages。

    ⚠️ `sender_names` 是 Task 9 补的（形状与 `vigil/export.py` 的
    ``_SENDER_TABLE_DDL``、`tests/test_store.py::_seed_messages` 一致）：
    空窗日的筛除分布要**把本地预筛重跑一遍**，而它走的是
    ``store.pending_messages``——那条 SQL 会 LEFT JOIN `sender_names`
    （预筛的「角色昵称」保留规则要用发信人）。缺这张表时该 SQL 直接
    ``no such table: sender_names``。造真库里本来就有的表，不是放宽断言。
    """
    from vigil import store

    memdb.executescript(
        """
        CREATE TABLE messages (
            msg_id INTEGER PRIMARY KEY, group_id INTEGER NOT NULL,
            ts INTEGER NOT NULL, sender_uid TEXT, content TEXT NOT NULL
        );
        CREATE TABLE sender_names (
            group_id   INTEGER NOT NULL,
            uid        TEXT    NOT NULL,
            group_nick TEXT,
            qq_nick    TEXT,
            uin        INTEGER,
            in_group   INTEGER,
            PRIMARY KEY (group_id, uid)
        );
        """
    )
    since, until = digest.day_window("2026-09-13")
    memdb.executemany(
        "INSERT INTO messages VALUES (?,?,?,?,?)",
        [
            (1, 100, since + 60, "u_a", "通知：明天交体检表"),
            (2, 100, since + 120, "u_b", "收到"),
            (3, 200, since + 180, "u_c", "有讲座"),
        ],
    )
    store.ensure_schema(memdb)
    memdb.executemany(
        "INSERT INTO items (item_id, kind, title, detail, event_ts, deadline_ts,"
        " group_id, actor_uid, place, links, amount, confidence, model,"
        " prompt_ver, created_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        [
            (1, "notice", "体检表", "10月8日前交", since + 60, None, 100, None,
             None, "[]", None, 0.9, "m", "v2", 1),
            (2, "activity", "讲座", None, since + 180, None, 200, None,
             None, "[]", None, 0.9, "m", "v2", 1),
        ],
    )
    memdb.commit()
    return memdb, since, until


class _Config:
    """最小 Config 替身——只用到 groups 与 tier_of。

    ``tier_of`` 是 Task 9 补的：空窗日的筛除分布要把本地预筛重跑一遍
    （``digest.screening_breakdown``），而预筛的保留规则要按群档位判定。
    **刻意转调真 ``Config.tier_of``**，不在这里重抄一遍遍历——替身抄一份
    实现就会与真身漂移，而漂移方向恰好是「测试以为的口径 ≠ 运行时的口径」。
    """

    def __init__(self, groups):
        from vigil.config import Config, Group

        self.groups = tuple(Group(id=g, name=n) for g, n in groups.items())
        self._real = Config(
            source_dir=Path(), output_db=Path(), groups=self.groups
        )

    def tier_of(self, gid: int) -> str:
        return self._real.tier_of(gid)


def test_digest_writes_body_and_links_every_item(seeded, monkeypatch, tmp_path):
    from vigil import store

    conn, since, until = seeded
    fake = _FakeLLM([
        {"quotes": ["体检表"], "label": "体检", "text": "10月8日前交"},
        {"quotes": ["讲座"], "label": "讲座", "text": "周五晚"},
    ])
    monkeypatch.setattr("vigil.digest.chat_json", fake)

    stats = digest.digest(
        _Config({100: "班级群", 200: "新生群"}), api_key="k", conn=conn,
        since=since, until=until, day_label="2026-09-13", output_dir=tmp_path,
    )

    assert fake.calls == 1
    assert stats.items == 2
    assert stats.mechanical == 0
    assert stats.errors == []
    body = (tmp_path / "2026-09-13.md").read_text(encoding="utf-8")
    assert "- **体检** 10月8日前交 · 班级群" in body
    # 窗口内**每条** item 都必须挂上——这是「日报里的每一条都能在 Web 找到」的前提
    linked = conn.execute(
        "SELECT item_id FROM digest_items WHERE digest_id=? ORDER BY item_id",
        (stats.digest_id,),
    ).fetchall()
    assert [r[0] for r in linked] == [1, 2]

    # ⚠️ **「库副本 == 文件副本」**：同一篇日报有两个副本（`digests.body_md` 是
    # M3 Web 与所有下游读到的那个，`.md` 是磁盘上那个），**必须逐字节相等**。
    #
    # 这条不变量原先在编排层**零断言**（`grep body_md tests/` 只命中 test_store.py）：
    # 变异 N15「落库正文 = 文件正文 + 前缀」曾 211 全绿存活。
    # 按**字节**比而不是按文本比：`read_text()` 会做 universal-newline 归一化，
    # CRLF 的差异会被吃掉——那正是 `_persist` 里 `newline="\n"` 的守卫。
    db_body = conn.execute(
        "SELECT body_md FROM digests WHERE digest_id=?", (stats.digest_id,)
    ).fetchone()[0]
    assert db_body == body
    assert (tmp_path / "2026-09-13.md").read_bytes() == db_body.encode("utf-8")


def test_digest_sends_max_tokens_and_thinking_off(seeded, monkeypatch, tmp_path):
    conn, since, until = seeded
    fake = _FakeLLM([{"quotes": ["体检表"], "label": "a", "text": ""}])
    monkeypatch.setattr("vigil.digest.chat_json", fake)

    digest.digest(_Config({100: "班级群"}), api_key="k", conn=conn, since=since,
                  until=until, day_label="2026-09-13", output_dir=tmp_path)

    # ⚠️ 这条守的是**接线**（常量有没有被传下去：把 `max_tokens=MAX_TOKENS`
    # 删掉它就会变 None → 红），守不了**取值**——两边引用的是同一个全局，
    # 一起改就全绿。取值由 `test_max_tokens_stays_in_a_sane_range` 守。
    assert fake.configs[0].max_tokens == digest.MAX_TOKENS
    assert fake.configs[0].enable_thinking is False


def test_digest_never_sends_group_ids(seeded, monkeypatch, tmp_path):
    """⚠️ 守着「群号不出网」这条硬约束——探针第一版就是在这里翻车的。"""
    conn, since, until = seeded
    seen = {}

    def spy(cfg, *, system, user, sleep=None):
        from vigil.llm import LLMResult

        seen["user"] = user
        seen["system"] = system
        return LLMResult(payload={"lines": []}, input_tokens=1, output_tokens=1)

    monkeypatch.setattr("vigil.digest.chat_json", spy)

    digest.digest(_Config({100: "班级群", 200: "新生群"}), api_key="k", conn=conn,
                  since=since, until=until, day_label="2026-09-13", output_dir=tmp_path)

    assert "班级群" in seen["user"]
    assert "100" not in seen["user"].replace("2026-09-13", "").replace("10月8日", "")


def test_digest_uncovered_item_gets_mechanical_row(seeded, monkeypatch, tmp_path):
    conn, since, until = seeded
    fake = _FakeLLM([{"quotes": ["体检表"], "label": "体检", "text": ""}])
    monkeypatch.setattr("vigil.digest.chat_json", fake)

    stats = digest.digest(_Config({100: "班级群", 200: "新生群"}), api_key="k",
                          conn=conn, since=since, until=until,
                          day_label="2026-09-13", output_dir=tmp_path)

    assert stats.mechanical == 1
    body = (tmp_path / "2026-09-13.md").read_text(encoding="utf-8")
    assert "- **讲座**" in body


def test_digest_empty_window_does_not_call_model(seeded, monkeypatch, tmp_path):
    """空窗日：不花 token、不留空白文件，但**明确说没有信息**（spec §4.6）。

    ⚠️ Task 9 起「说没有信息」多了一个**前提**：窗口内消息必须**全部有
    ``refine_runs`` 记账**。所以这里补了那三行记账——**只动了 setup，
    断言一字未改**。

    不改 setup 的话，本条的断言恰好就是 Task 9 要消灭的那句**假话**：
    窗口里 3 条消息一条都没抽取过，日报却说「没有值得一提的信息」。
    补记账后本条的语义反而更强：它走的正是「已抽取 + 确实没事」那条
    分支（会连带跑到 ``screening_breakdown``），而不只是「空窗口」。
    """
    from vigil import store

    conn, since, until = seeded
    conn.execute("DELETE FROM items")
    store.record_run(conn, [1, 2, 3], status=store.STATUS_DISCARDED, prompt_ver="v2")
    conn.commit()
    fake = _FakeLLM([])
    monkeypatch.setattr("vigil.digest.chat_json", fake)

    stats = digest.digest(_Config({100: "班级群"}), api_key="k", conn=conn,
                          since=since, until=until, day_label="2026-09-13",
                          output_dir=tmp_path)

    assert fake.calls == 0
    assert stats.items == 0
    body = (tmp_path / "2026-09-13.md").read_text(encoding="utf-8")
    assert "没有值得一提的信息" in body


def test_digest_llm_failure_writes_nothing_and_reports(seeded, monkeypatch, tmp_path):
    """失败不许降级成「机械拼盘」——那种文件看起来和正常日报一样。"""
    from vigil.llm import LLMError

    conn, since, until = seeded

    def boom(cfg, *, system, user, sleep=None):
        raise LLMError("HTTP 503")

    monkeypatch.setattr("vigil.digest.chat_json", boom)

    stats = digest.digest(_Config({100: "班级群"}), api_key="k", conn=conn,
                          since=since, until=until, day_label="2026-09-13",
                          output_dir=tmp_path)

    assert stats.errors and "503" in stats.errors[0]
    assert stats.digest_id is None
    assert not (tmp_path / "2026-09-13.md").exists()
    assert conn.execute("SELECT COUNT(*) FROM digests").fetchone()[0] == 0


def test_digest_rerun_replaces_instead_of_stacking(seeded, monkeypatch, tmp_path):
    """⚠️ 「替换」是**两件事**：不堆叠（行数）+ 正文真的被换成新版。

    原先只断言了 `COUNT(*)==1` 与 `calls==2`，把「替换正文」那半边丢了——
    变异「窗口已有 digest 就跳过 save_digest、只刷新文件」曾 **211 全绿存活**。
    后果是静默的：磁盘 `.md` 是新版、`digests.body_md`（Web/下游读的那个）
    是旧版，两个副本分歧而没有任何报错。

    ⚠️ 两次调用**必须喂不同的 lines**。用同一个 `fake` 时两版正文一模一样，
    「库里是第二版」这条断言恒真、什么也守不住——这是补这条守卫的必要条件。
    """
    conn, since, until = seeded
    cfg = _Config({100: "班级群", 200: "新生群"})
    first = _FakeLLM([{"quotes": ["体检表", "讲座"], "label": "第一版", "text": ""}])
    monkeypatch.setattr("vigil.digest.chat_json", first)

    digest.digest(cfg, api_key="k", conn=conn, since=since, until=until,
                  day_label="2026-09-13", output_dir=tmp_path)

    second = _FakeLLM([{"quotes": ["体检表", "讲座"], "label": "第二版", "text": ""}])
    monkeypatch.setattr("vigil.digest.chat_json", second)

    digest.digest(cfg, api_key="k", conn=conn, since=since, until=until,
                  day_label="2026-09-13", output_dir=tmp_path)

    assert conn.execute("SELECT COUNT(*) FROM digests").fetchone()[0] == 1
    assert first.calls == 1 and second.calls == 1

    db_body = conn.execute(
        "SELECT body_md FROM digests WHERE window_from=? AND window_to=?",
        (since, until),
    ).fetchone()[0]
    assert "第二版" in db_body, "库里的正文没有被换成第二版"
    assert "第一版" not in db_body, "第一版正文残留在库里"
    assert (tmp_path / "2026-09-13.md").read_bytes() == db_body.encode("utf-8")


def test_digest_dry_run_does_not_call_model_or_write(seeded, monkeypatch, tmp_path):
    """⚠️ 有 item 的 dry-run 分支也要守「不写库」。

    原先这条只断言了「不落文件」，「不写库」只有**空窗**那条测试守着——
    与 M6（空窗分支偷偷写库）是**同形镜像洞**。变异 N6b「只在有 item 的
    dry-run 分支里插一句 store.save_digest」曾 211 全绿存活，后果是 dry-run
    期间库里被塞进一篇日报，而用户以为什么都没动（M3 的 Web 会展示它）。
    两个分支都要守。
    """
    conn, since, until = seeded
    fake = _FakeLLM([])
    monkeypatch.setattr("vigil.digest.chat_json", fake)

    stats = digest.digest(_Config({100: "班级群"}), api_key="k", conn=conn,
                          since=since, until=until, day_label="2026-09-13",
                          output_dir=tmp_path, dry_run=True)

    assert fake.calls == 0
    assert stats.items == 2
    assert stats.digest_id is None
    assert not (tmp_path / "2026-09-13.md").exists()
    assert conn.execute("SELECT COUNT(*) FROM digests").fetchone()[0] == 0


def test_digest_dry_run_on_empty_window_still_writes_nothing(seeded, monkeypatch, tmp_path):
    """⚠️ 空窗 + dry-run 的组合——端到端实测抓到过：当时空窗分支排在 dry_run
    检查前面且无条件落库，于是 `--dry-run` 照样写了库、落了文件。
    单测原本用的样本有 item，走的是另一条分支，所以没守住这个组合。"""
    conn, since, until = seeded
    conn.execute("DELETE FROM items")
    conn.commit()
    fake = _FakeLLM([])
    monkeypatch.setattr("vigil.digest.chat_json", fake)

    stats = digest.digest(_Config({100: "班级群"}), api_key="k", conn=conn,
                          since=since, until=until, day_label="2026-09-13",
                          output_dir=tmp_path, dry_run=True)

    assert fake.calls == 0
    assert stats.digest_id is None
    assert not (tmp_path / "2026-09-13.md").exists()
    assert conn.execute("SELECT COUNT(*) FROM digests").fetchone()[0] == 0


def test_digest_no_write_skips_file_but_still_saves(seeded, monkeypatch, tmp_path):
    conn, since, until = seeded
    fake = _FakeLLM([{"quotes": ["体检表", "讲座"], "label": "全部", "text": ""}])
    monkeypatch.setattr("vigil.digest.chat_json", fake)

    stats = digest.digest(_Config({100: "班级群", 200: "新生群"}), api_key="k",
                          conn=conn, since=since, until=until,
                          day_label="2026-09-13", output_dir=tmp_path,
                          write_file=False)

    assert stats.digest_id is not None
    assert stats.output_path == ""
    assert not (tmp_path / "2026-09-13.md").exists()


def test_digest_rejects_reversed_window(seeded):
    conn, _, _ = seeded

    with pytest.raises(ValueError):
        digest.digest(_Config({100: "班级群"}), api_key="k", conn=conn,
                      since=5000, until=1000, day_label="2026-09-13")


# ── 编排：CLI 层 ────────────────────────────────────────────
#
# 这一段守的是 brief 正文点名承重、但出口自查里**一度没人守**的三件事：
# 失败返回非零（「M4 自动化的报警信号——别吞掉」）、`--no-write` 真的不落文件、
# 密钥真的接到了 LLMConfig 上。三者原先各有一个变异体全绿存活：
# `return 1`→`return 0`、`write_file=not args.no_write`→`True`、`api_key=""`。
#
# 范式抄 `tests/test_refine.py::test_cli_refine_model_falls_back_to_default`：
# 用 `cli.main([...])` 驱动，把 `_load_config_only` / `_require_export_db`
# 换成替身。⚠️ 这里多一步——`DEFAULT_OUTPUT_DIR` 也**必须**换掉。


@pytest.fixture
def cli_env(seeded, monkeypatch, tmp_path):
    """把 `cmd_digest` 的外部依赖全接上，一件都不许落到真实环境。

    ⚠️ `DEFAULT_OUTPUT_DIR` 必须换成 tmp_path：`cmd_digest` **不传**
    `output_dir`，不换的话这里每一次成功路径都会往仓库的 `docs/digests/`
    里写文件——测试污染工作区。

    ⚠️ **`load_llm_key` 两处都要接**（与 `tests/test_cli.py::cli_env` 同）：
    `cli.py` 用 `from .config import load_llm_key` 在**模块级**绑定了自己的
    那份引用，而 `cmd_digest` 以**裸名**调它 ⇒ 只打 `vigil.config.load_llm_key`
    够不着它，密钥判空后会 `sys.exit`。打 `cli.load_llm_key` 才是真正生效的
    那一处；再打一份 `vigil.config` 是为了不依赖别的夹具恰好还在。
    """
    from vigil import cli

    conn, _, _ = seeded
    db = tmp_path / "export.db"
    disk = sqlite3.connect(str(db))
    conn.backup(disk)
    disk.commit()
    disk.close()

    monkeypatch.setattr(
        cli, "_load_config_only",
        lambda: _Config({100: "班级群", 200: "新生群"}),
    )
    monkeypatch.setattr(cli, "_require_export_db", lambda config: db)
    monkeypatch.setattr(cli, "load_llm_key", lambda *a, **k: "k-cli")
    monkeypatch.setattr("vigil.config.load_llm_key", lambda *a, **k: "k-cli")
    monkeypatch.setattr("vigil.digest.DEFAULT_OUTPUT_DIR", tmp_path / "digests")
    return db


def _count_digests(db) -> int:
    conn = sqlite3.connect(str(db))
    try:
        return conn.execute("SELECT COUNT(*) FROM digests").fetchone()[0]
    finally:
        conn.close()


def test_cli_digest_llm_failure_exits_nonzero(cli_env, monkeypatch, tmp_path):
    """⚠️ **非零退出码是 M4 自动化的报警信号。**

    变异「失败分支 `return 1` → `return 0`」曾 211 全绿存活：日报失败会静默，
    自动化收不到任何信号——正是 brief 里点名的「别吞掉」。
    """
    from vigil import cli
    from vigil.llm import LLMError

    def boom(cfg, *, system, user, sleep=None):
        raise LLMError("HTTP 503")

    monkeypatch.setattr("vigil.digest.chat_json", boom)

    rc = cli.main(["digest", "--date", "2026-09-13"])

    assert rc == 1
    assert _count_digests(cli_env) == 0
    assert not (tmp_path / "digests" / "2026-09-13.md").exists()


def test_cli_digest_no_write_skips_file_but_still_saves(cli_env, monkeypatch, tmp_path):
    """`--no-write` 必须一路传到 `digest(write_file=False)`，不能被写死成 True。"""
    from vigil import cli

    fake = _FakeLLM([{"quotes": ["体检表", "讲座"], "label": "全部", "text": ""}])
    monkeypatch.setattr("vigil.digest.chat_json", fake)

    rc = cli.main(["digest", "--date", "2026-09-13", "--no-write"])

    assert rc == 0
    assert _count_digests(cli_env) == 1
    # 目录整个都不该被建出来——不是「建了但没写这个文件」
    assert not (tmp_path / "digests").exists()


def test_cli_digest_passes_api_key_to_llm(cli_env, monkeypatch, tmp_path):
    """密钥没接到 `LLMConfig` 上就会每次 401，而测试全绿。

    变异 `api_key=""` 曾 211 全绿存活。
    """
    from vigil import cli

    fake = _FakeLLM([{"quotes": ["体检表", "讲座"], "label": "全部", "text": ""}])
    monkeypatch.setattr("vigil.digest.chat_json", fake)

    assert cli.main(["digest", "--date", "2026-09-13"]) == 0

    assert fake.configs[0].api_key == "k-cli"


# ── 截止日核验（Task 6，冒烟抓到的真实数据破坏）────────────────


def test_deadline_supported_finds_chinese_date():
    import datetime as dt

    ts = int(dt.datetime(2026, 9, 16, 12, 0).timestamp())

    assert digest.deadline_supported(ts, "9月16日12:00开始报名缴费")
    assert digest.deadline_supported(ts, "9月16日截止")


def test_deadline_supported_tolerates_spaces_in_source():
    """实测源文里有「9 月 16 日」这种带空格的写法。"""
    import datetime as dt

    ts = int(dt.datetime(2026, 9, 16).timestamp())

    assert digest.deadline_supported(ts, "9 月 16 日 截 止")


def test_deadline_supported_rejects_relative_time_words():
    """⚠️ 冒烟实测：源文只写「明早」「明天」「周六下午」时，M1 却把 deadline 填成了具体日期。

    这类**没有字面依据**的日期必须判为不支持——否则「别忘」会把已经发生的事
    说成「别忘了」。
    """
    import datetime as dt

    ts = int(dt.datetime(2026, 9, 15).timestamp())

    assert not digest.deadline_supported(ts, "各位小班：明早7:20在宿舍楼下集合")
    assert not digest.deadline_supported(ts, "周六下午4.00-8.00")
    assert not digest.deadline_supported(ts, "明天下午5点到6点有补录的机会")


def test_deadline_supported_false_when_no_deadline():
    assert not digest.deadline_supported(None, "随便什么 9月16日")


def test_deadline_supported_requires_a_month():
    """⚠️ 只有裸日、没有月份 → **一律不算依据**（真实反例 item 88 / item 109）。

    修复轮次 1（审查 F1，Important）：上一版为了满足 brief 自带的一条测试
    （源文写的是裸日「16日12:00…」）加了「裸日回退」——只核对日、不核对月。
    审查在真实库上实测这条路径的精度是 **0/2**，两条都错，而且错的方向正是
    本任务要消灭的「往后飘」：

    | item | 事件日 | 程序标 | 源文 |
    |---|---|---|---|
    | 88  | 08-06 | **09-26** | 「31号也不算早了，我有一个朋友**26号**就开学」 |
    | 109 | 08-17 | **09-30** | 「**30号**就得到学校」 |

    源文里的「26号」说的是 8 月 26（同消息语境），却被拿去给一条标着 9-26 的
    截止日背书，再被机器口吻打进「别忘」。**核对月份这一步不能省**——找不到
    月份就是没有依据，条目照常出现在类目区块（降级不丢事）。
    """
    import datetime as dt

    ts_26 = int(dt.datetime(2026, 9, 26).timestamp())
    ts_30 = int(dt.datetime(2026, 9, 30).timestamp())
    ts_3 = int(dt.datetime(2026, 9, 3).timestamp())

    assert not digest.deadline_supported(ts_26, "31号也不算早了，我有一个朋友26号就开学")
    assert not digest.deadline_supported(ts_30, "30号就得到学校")
    # 裸日连「落在更长数字里」这一层都无从谈起：没有月份就是没有依据
    assert not digest.deadline_supported(ts_3, "3日截止")
    assert not digest.deadline_supported(ts_3, "13日交表")
    # 带上月份才认（裸日那条路去掉后，这是唯一的口径）
    assert digest.deadline_supported(ts_26, "9月26号开学")
    assert digest.deadline_supported(ts_3, "9月3日截止")


def test_deadline_supported_rejects_numeric_form_inside_a_longer_number():
    """⚠️ 审查 §2 / F2 实测：下面 4 行**全部返回 True**（短写法落在更长数字里）。

    `9/3`、`9-3` 这类短数字写法上一版是**纯子串**比较，于是源文里只有 9 月 30 日
    （「9/30截止」）时，一条 **9 月 3 日**的（可能同样是模型编的）截止日也会被判成
    有依据——与裸日那条是同一个洞的另一支，同样是"月份/日号看错一位就放行"。
    """
    import datetime as dt

    ts_3 = int(dt.datetime(2026, 9, 3).timestamp())
    ts_16 = int(dt.datetime(2026, 9, 16).timestamp())

    assert not digest.deadline_supported(ts_3, "9/30截止")
    assert not digest.deadline_supported(ts_3, "9-30截止")
    assert not digest.deadline_supported(ts_3, "2026/09/30")
    assert not digest.deadline_supported(ts_16, "119-16")
    # 短写法本身仍要认，且**不因为后面跟着时间就丢**（「9-16 12:00」里的空格不是数字）
    assert digest.deadline_supported(ts_3, "9/3截止")
    assert digest.deadline_supported(ts_3, "9-3 18:00截止")
    assert digest.deadline_supported(ts_16, "2026-09-16 12:00开始报名")


def test_deadline_supported_accepts_dotted_form():
    """点号写法（修复轮次 1：审查 F3，真实源文 item 84「9.6上午」、item 258「9.11左右截止」）。

    这两条**源文里确实有**那个日期，上一版判据不认（写法没覆盖）→ 被降级，
    属**假阴性**（方向安全，但「别忘」的召回白打折）。点号与 `M-D`/`M/D` 同族，
    都带月份，精度风险同级。
    """
    import datetime as dt

    ts_6 = int(dt.datetime(2026, 9, 6).timestamp())
    ts_11 = int(dt.datetime(2026, 9, 11).timestamp())
    ts_1 = int(dt.datetime(2026, 9, 1).timestamp())

    assert digest.deadline_supported(ts_6, "9.6上午")
    assert digest.deadline_supported(ts_11, "9.11左右截止")
    assert digest.deadline_supported(ts_6, "09.06 上午")
    # 边界：小数点另一侧的日号不能顶替（「9.11」不是 9 月 1 日）
    assert not digest.deadline_supported(ts_1, "9.11左右截止")


def test_deadline_supported_does_not_bridge_messages():
    """⚠️ 源文是**多条消息用 ``"\\n"`` 拼起来的**（``store.item_sources_text``）——
    日期不能跨消息拼出来。

    上一版把整个 haystack 的空白**全部**去掉（`\\s+` → ""），于是「……9月」
    结尾的一条消息与「16日开始……」开头的另一条消息会被粘成「9月16日」，
    凭空造出一个「源文里有」的日期。现在只在**横向**空白处放宽（`[^\\S\\n]`），
    换行两侧不认。
    """
    import datetime as dt

    ts = int(dt.datetime(2026, 9, 16).timestamp())

    assert not digest.deadline_supported(ts, "报名从9月\n16日开始")
    assert not digest.deadline_supported(ts, "截止9-\n16")
    # 同一行内的空格照旧容忍（实测源文有「9 月 16 日」）
    assert digest.deadline_supported(ts, "9 月 16 日 截 止")


def test_verified_deadlines_returns_only_supported_ids():
    import datetime as dt

    ts = int(dt.datetime(2026, 9, 16).timestamp())
    items = [
        _item(1, title="有依据", deadline_ts=ts),
        _item(2, title="没依据", deadline_ts=ts, event_ts=2000),
    ]
    sources = {1: "9月16日12:00开始报名", 2: "周六下午4.00-8.00"}

    assert digest.verified_deadlines(items, sources) == {1}


def test_build_rows_defaults_to_not_trusting_any_deadline():
    """⚠️ 默认必须是「全不信任」——默认全信任的话，忘记传参的调用方会静默退回危险行为。"""
    items = [_item(1, title="甲", deadline_ts=5000)]

    rows = digest.build_rows([], items)

    assert rows[0].deadline_trusted is False


def test_build_rows_marks_trusted_deadline():
    items = [_item(1, title="甲", deadline_ts=5000)]

    rows = digest.build_rows([], items, trusted={1})

    assert rows[0].deadline_trusted is True


def test_build_rows_untrusted_when_any_merged_deadline_is_untrusted():
    """合并行里只要有一条截止日没依据，整行就不进「别忘」。"""
    items = [
        _item(1, title="甲", deadline_ts=5000),
        _item(2, title="乙", deadline_ts=6000, event_ts=2000),
    ]
    lines, _ = digest.match_lines(
        [{"quotes": ["甲", "乙"], "label": "合并", "text": ""}], items
    )

    rows = digest.build_rows(lines, items, trusted={1})

    assert rows[0].deadline_trusted is False


def test_render_excludes_untrusted_deadline_from_alerts(cats):
    """没依据的截止日不许进「别忘」，也不许被打上「截止 MM-DD」戳。"""
    rows = [_row("存疑日期", "明天下午", "notice", 100, 5000, False)]

    out = _render(rows, cats, window_from=1000)

    assert "## ⏰ 别忘" not in out
    assert "截止" not in out
    assert "- **存疑日期**" in out        # 条目本身照常出现，不漏事


def test_render_includes_trusted_deadline_in_alerts(cats):
    rows = [
        _row("有依据", "16日12:00开始", "notice", 100, 5000, False,
             deadline_trusted=True)
    ]

    out = _render(rows, cats, window_from=1000)

    assert "## ⏰ 别忘" in out
    assert "截止 " in out


def test_row_deadline_trusted_defaults_to_false(cats):
    """⚠️ ``Row.deadline_trusted`` 的**字段默认值**也必须是不信任。

    变异反证（实测）：把 ``Row`` 的 `deadline_trusted: bool = False` 改成
    ``= True`` → **230 全绿存活**。原因是别处带截止日的夹具都显式传了这个参数，
    而其余 Row 根本没有截止日（``alert_idx`` 要求 ``r.deadline_ts``），
    所以默认值那条路径没人走过。

    这个默认值兜的是**将来**的调用方：谁忘了传，谁就该得到「不催办」这个
    安全方向（与 ``build_rows`` 的 ``trusted=None`` 同一个道理）。
    """
    row = digest.Row("体检表", "交到辅导员处", "notice", 100, 5000, False)

    out = _render([row], cats, window_from=1000)

    assert row.deadline_trusted is False
    assert "## ⏰ 别忘" not in out


def test_digest_wires_deadline_verification(seeded, monkeypatch, tmp_path):
    """接线的端到端：源文有字面日期的进「别忘」，没有的不进，且计数如实上报。"""
    import datetime as dt
    from vigil import store

    conn, since, until = seeded
    conn.execute("DELETE FROM items")
    conn.execute("DELETE FROM messages")
    ts_ok = int(dt.datetime(2026, 9, 16, 12, 0).timestamp())
    # 两条的截止日**取不同的值**（修复轮次 2）——同值的话「item 2 的日期有没有
    # 出网」这条断言恒真（item 1 的同一个日期会照样出现在 payload 里），什么也守不住。
    ts_nobasis = int(dt.datetime(2026, 9, 20).timestamp())
    conn.execute(
        "INSERT INTO messages VALUES (1, 100, ?, 'u_a', '9月16日12:00开始报名缴费')",
        (since + 60,),
    )
    conn.execute(
        "INSERT INTO messages VALUES (2, 100, ?, 'u_b', '明天下午5点有补录机会')",
        (since + 120,),
    )
    conn.executemany(
        "INSERT INTO items (item_id, kind, title, detail, event_ts, deadline_ts,"
        " group_id, actor_uid, place, links, amount, confidence, model,"
        " prompt_ver, created_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        [
            (1, "academic", "四六级报名", None, since + 60, ts_ok, 100, None,
             None, "[]", None, 0.9, "m", "v2", 1),
            (2, "activity", "补录面试", None, since + 120, ts_nobasis, 100, None,
             None, "[]", None, 0.9, "m", "v2", 1),
        ],
    )
    conn.executemany(
        "INSERT INTO item_sources (item_id, msg_id) VALUES (?,?)", [(1, 1), (2, 2)]
    )
    conn.commit()
    fake = _FakeLLM([{"quotes": ["四六级报名", "补录面试"], "label": "两件事", "text": ""}])
    monkeypatch.setattr("vigil.digest.chat_json", fake)

    stats = digest.digest(_Config({100: "班级群"}), api_key="k", conn=conn,
                          since=since, until=until, day_label="2026-09-13",
                          output_dir=tmp_path)

    assert stats.deadlines_kept == 1
    assert stats.deadlines_dropped == 1
    body = (tmp_path / "2026-09-13.md").read_text(encoding="utf-8")
    # 合并行的截止日不可信 → 不该进「别忘」
    assert "## ⏰ 别忘" not in body

    # ⚠️ 修复轮次 2：未核验的截止日**也不许出网**——这是「模型措辞」那条漏路的入口。
    # 变异反证：去掉 `digest()` 里的 `trusted=trusted` → 本条红。
    sent = fake.users[0]
    assert "2026-09-16" in sent       # item 1 的源文里有「9月16日」→ 照发
    assert "2026-09-20" not in sent   # item 2 的源文里没有 → 扣住（模型写不出来）


def test_cli_digest_reports_dropped_deadlines(cli_env, monkeypatch, capsys):
    """⚠️ 降级必须**看得见**：静默丢弃截止日是另一种失败。

    变异反证：删掉 ``cmd_digest`` 里那段 ``if stats.deadlines_dropped:`` 的打印
    → 本条红（brief 那 10 条全都守不住「打印出来」这一半——`DigestStats` 上的
    计数有测试，`cli.py` 的可见性没有）。
    """
    from vigil import cli

    # item 1「体检表」的源消息是「通知：明天交体检表」——挂任何具体日期都没有字面依据
    conn = sqlite3.connect(str(cli_env))
    conn.execute("UPDATE items SET deadline_ts=? WHERE item_id=1", (5000,))
    conn.execute("INSERT INTO item_sources (item_id, msg_id) VALUES (1, 1)")
    conn.commit()
    conn.close()
    fake = _FakeLLM([{"quotes": ["体检表", "讲座"], "label": "全部", "text": ""}])
    monkeypatch.setattr("vigil.digest.chat_json", fake)

    rc = cli.main(["digest", "--date", "2026-09-13"])

    out = capsys.readouterr().out
    assert rc == 0
    assert "1 条截止日在源消息里找不到字面依据" in out
    assert "已保留 0 条" in out


# ── 空窗日的自证与筛除分布（Task 9，用户验收后追加）────────────


def test_render_empty_day_not_yet_refined_does_not_claim_nothing_happened():
    """⚠️ 覆盖不全时**不许**说「没有值得一提的信息」——那是句可能为假的话。

    这条是 Task 9 真正的价值：把一句可能为假的话换成一句一定为真的话。
    """
    out = digest.render_empty_day(
        day="2026-08-08", groups=1, messages=1059, refined=0,
        local_dropped=0, sent=0,
    )

    assert "没有值得一提的信息" not in out
    assert "尚未抽取" in out
    assert "vigil refine" in out
    assert "0/1,059" in out


def test_render_empty_day_shows_screening_breakdown():
    """⚠️ 数字取的是 8/8 的**真实**构成（修复轮次 1 的口径）。

    旧 fixture 用的 812/247（用户举的形态）在真实管线下是不成立的形状：
    247 条「送模型」意味着绝大多数消息都出网了，而实际出网的只有 45 条。
    """
    out = digest.render_empty_day(
        day="2026-08-08", groups=1, messages=1059, refined=1059,
        local_dropped=1014, sent=45,
    )

    assert "没有值得一提的信息" in out
    assert "1,014" in out and "45" in out
    assert "1,059" in out
    assert "送到模型" in out
    # ⚠️ 整句钉死（不是只查「1,014」在不在）：括注**只定性**，曾经带的
    # 「含 N 条硬丢弃」已删（那个 N 没有唯一含义、读者无法验证）——
    # 钉整句能抓住「顺手把计数加回来」这类回归（修复轮次 2）。
    assert "其中 1,014 条被本地规则筛掉（纯应答 / 过短 / 刷屏广告一类）" in out
    assert "硬丢弃" not in out


def test_render_empty_day_partial_coverage_is_treated_as_not_refined():
    out = digest.render_empty_day(
        day="2026-08-08", groups=1, messages=1059, refined=1058,
        local_dropped=812, sent=246,
    )

    assert "尚未抽取" in out
    assert "1,058/1,059" in out


def test_screening_breakdown_splits_local_from_model(seeded):
    """本地筛掉与送模型后无产出必须分得开（refine_runs 分不开，只能重算）。

    ⚠️ **修复轮次 1 起期望值变了**：`sent` 改成按 refine 的真实管线
    （`expand_context` 之后）算，于是这个 3 条消息的小窗口里，
    唯一的候选（msg 3）前后各 2 条邻居把 msg 1/2 **全拉进了 in_scope**
    ——三条都出网，本地筛掉 0 条。旧期望 `(3, 2, 1)` 是「候选数当送模型数」
    那版口径的产物。

    真正演示「本地筛掉 vs 送模型」的拆分在
    ``test_screening_breakdown_sent_counts_context_expansion_not_candidates``
    （窗口里留了够远的消息，上下文拉不到它）。
    """
    conn, since, until = seeded
    conn.execute("DELETE FROM messages")
    conn.executemany(
        "INSERT INTO messages VALUES (?,?,?,?,?)",
        [
            (1, 100, since + 10, "u_a", "收到"),                       # 纯应答 → 本地丢
            (2, 100, since + 20, "u_b", "dd"),                         # 过短 → 本地丢
            (3, 100, since + 30, "u_c", "明天记得带体检表到辅导员处"),   # 送模型
        ],
    )
    conn.commit()

    total, local, sent = digest.screening_breakdown(
        conn, _Config({100: "班级群"}), since=since, until=until
    )

    assert (total, local, sent) == (3, 0, 3)
    assert local + sent == total       # ④ 文案里两个数字之和必须等于消息总数


def test_screening_breakdown_sent_counts_context_expansion_not_candidates(seeded):
    """⚠️ 「送模型」= `expand_context` 之后的条数，不是候选数（修复轮次 1）。

    旧公式 `总数 - screen_stats.dropped` 把「没命中保留规则」的也算成送模型：
    `screen_stats.dropped` **只算硬丢弃**，而 8/8 实测 1,059 条里硬丢弃只有 83，
    于是 45 被报成了 976（夸大约 20 倍）——错话出在专门防假数字的那段文案里。

    ⚠️ `expand_context` 拉的是**位置**上前后各 2 条邻居（`by_group` 的有序
    下标），**不是时间窗口**——所以「离得远」必须按**下标**隔开 3 条以上，
    光把时间拉开没用（第一版就写成 `since+300`，结果下标只差 1、照样被拉走，
    实测红）。msg 6 与候选隔了 3 个下标，`context=2` 够不着它。

    变异反证：
    * `sent` 改回 `len(msgs) - screen_stats.dropped` → 本条红（`sent` = 3 ≠ 5）
    * `sent` 改成候选数 `len(candidates)` → 本条红（`sent` = 1 ≠ 5）
    """
    conn, since, until = seeded
    conn.execute("DELETE FROM messages")
    conn.executemany(
        "INSERT INTO messages VALUES (?,?,?,?,?)",
        [
            (1, 100, since + 10, "u_a", "收到"),                       # 硬丢弃，但被上下文拉走
            (2, 100, since + 20, "u_b", "dd"),                         # 硬丢弃，但被上下文拉走
            (3, 100, since + 30, "u_c", "明天记得带体检表到辅导员处"),   # 候选（下标 2）
            (4, 100, since + 40, "u_d", "今天天气不错啊"),               # 软丢弃，但被上下文拉走
            (5, 100, since + 50, "u_e", "食堂今天人真多"),               # 软丢弃，但被上下文拉走
            (6, 100, since + 60, "u_f", "收到"),                        # 下标 5：够不着 → 没出网
        ],
    )
    conn.commit()

    total, local, sent = digest.screening_breakdown(
        conn, _Config({100: "班级群"}), since=since, until=until
    )

    assert (total, local, sent) == (6, 1, 5)
    assert local + sent == total
    assert sent < total            # 至少有一条真的没出网，否则这条测不出「本地筛掉」


def test_digest_empty_window_reports_not_refined(seeded, monkeypatch, tmp_path):
    """端到端：窗口没抽取过时，日报里必须是「尚未抽取」而不是「没事」。"""
    conn, since, until = seeded
    conn.execute("DELETE FROM items")
    conn.commit()
    fake = _FakeLLM([])
    monkeypatch.setattr("vigil.digest.chat_json", fake)

    digest.digest(_Config({100: "班级群"}), api_key="k", conn=conn,
                  since=since, until=until, day_label="2026-09-13",
                  output_dir=tmp_path)

    body = (tmp_path / "2026-09-13.md").read_text(encoding="utf-8")
    assert "尚未抽取" in body
    assert "没有值得一提的信息" not in body


def test_digest_empty_window_breakdown_end_to_end(seeded, monkeypatch, tmp_path):
    """端到端：抽取过且确实没事时，报出筛除分布。"""
    from vigil import store

    conn, since, until = seeded
    conn.execute("DELETE FROM items")
    conn.execute("DELETE FROM messages")
    conn.executemany(
        "INSERT INTO messages VALUES (?,?,?,?,?)",
        [(1, 100, since + 10, "u_a", "收到"), (2, 100, since + 20, "u_b", "dd"),
         (3, 100, since + 30, "u_c", "随便聊聊天气不错啊今天挺热的")],
    )
    store.record_run(conn, [1, 2, 3], status=store.STATUS_DISCARDED, prompt_ver="v2")
    conn.commit()
    fake = _FakeLLM([])
    monkeypatch.setattr("vigil.digest.chat_json", fake)

    digest.digest(_Config({100: "班级群"}), api_key="k", conn=conn,
                  since=since, until=until, day_label="2026-09-13",
                  output_dir=tmp_path)

    body = (tmp_path / "2026-09-13.md").read_text(encoding="utf-8")
    assert "没有值得一提的信息" in body
    assert "被本地规则筛掉" in body
    assert "3/3" not in body          # 覆盖信息只在未抽取分支里出现
    # 三条都没命中保留规则 → 一条都没出网；括注只定性、不带任何计数
    assert "其中 3 条被本地规则筛掉（纯应答 / 过短 / 刷屏广告一类）" in body
    # ⚠️ Task 11：`sent == 0` 时不许渲染「0 条送到模型后判为无价值」——
    # 「没送模型」与「送了但判为无价值」是两回事，前者不能装成后者。
    assert "没有送模型" in body
    assert "0 条送到模型" not in body
    assert "硬丢弃" not in body


# ── 空窗日的两个退化输入（Task 11）─────────────────────────────


def test_render_empty_day_no_messages_at_all_outside_span():
    """⚠️ F1：窗口落在数据库时间范围之外时，说「没事」是假话——那是**没数据**。"""
    out = digest.render_empty_day(
        day="2026-09-14", groups=0, messages=0, refined=0,
        local_dropped=0, sent=0, span=(1000, 3000),
    )

    assert "没有值得一提的信息" not in out
    assert "vigil export" in out
    assert "0 条被本地规则筛掉" not in out        # 退化句子不许出现


def test_render_empty_day_no_messages_but_inside_span():
    """窗口落在范围之内却 0 条 → 是真的没人说话。"""
    out = digest.render_empty_day(
        day="2026-09-14", groups=0, messages=0, refined=0,
        local_dropped=0, sent=0, span=(1000, 3000),
        window=(2000, 2999),
    )

    assert "没有值得一提的信息" in out
    assert "真的没人说话" in out


def test_render_empty_day_window_before_all_data():
    """⚠️ 窗口**早于全部数据** → 是「没数据」，不是「没人说话」（修复轮次 1）。

    旧判据 `window[1] <= span[0] or window[0] > span[1]` 在 span 的下界被
    `ts=0` 脏行拉到 0 时会**两边都判不出来**，于是这里会说出「真的没人说话」
    ——一句关于「没人说话」的断言，而那天压根不在数据覆盖范围内。
    """
    out = digest.render_empty_day(
        day="2026-06-01", groups=0, messages=0, refined=0,
        local_dropped=0, sent=0, span=(1000, 3000),
        window=(100, 999),                     # 整个窗口都在 span[0] 之前
    )

    assert "vigil export" in out
    assert "真的没人说话" not in out
    assert "没有值得一提的信息" not in out


def test_render_empty_day_zero_sent_does_not_say_zero():
    """⚠️ sent == 0 时不许渲染「0 条送到模型后判为无价值」。"""
    out = digest.render_empty_day(
        day="2026-08-08", groups=1, messages=20, refined=20,
        local_dropped=20, sent=0, span=(1, 10**12),
    )

    assert "0 条送到模型" not in out
    assert "没有送模型" in out
    assert "没有值得一提的信息" in out


def test_digest_zero_message_window_outside_span_says_export(seeded, monkeypatch,
                                                             tmp_path):
    """端到端接线：窗口落在数据库跨度之外时，说「先跑 vigil export」。

    ⚠️ 光测 `render_empty_day` 的参数不够——`digest()` 忘传 `span`/`window`
    时那几条单测照样绿（默认 `None` 走「没数据」分支），而真实入口
    （M4 自动跑 `vigil digest`，`vigil export` 失败那天）仍是老样子。
    这条钉死接线。
    """
    conn, since, until = seeded
    conn.execute("DELETE FROM items")
    conn.commit()
    fake = _FakeLLM([])
    monkeypatch.setattr("vigil.digest.chat_json", fake)

    out_since, out_until = digest.day_window("2026-09-20")
    digest.digest(_Config({100: "班级群"}), api_key="k", conn=conn,
                  since=out_since, until=out_until, day_label="2026-09-20",
                  output_dir=tmp_path)

    body = (tmp_path / "2026-09-20.md").read_text(encoding="utf-8")
    assert "vigil export" in body
    assert "没有值得一提的信息" not in body
    assert "被本地规则筛掉" not in body


def test_digest_zero_message_window_inside_span_says_really_quiet(seeded, monkeypatch,
                                                                 tmp_path):
    """端到端接线：前后都有数据、窗口内确实 0 条 → 「真的没人说话」。"""
    conn, since, until = seeded
    conn.execute("DELETE FROM items")
    conn.execute("DELETE FROM messages")
    conn.executemany(
        "INSERT INTO messages VALUES (?,?,?,?,?)",
        [(1, 100, since - 1000, "u_a", "前一天在聊"),
         (2, 100, until + 1000, "u_b", "后一天在聊")],
    )
    conn.commit()
    fake = _FakeLLM([])
    monkeypatch.setattr("vigil.digest.chat_json", fake)

    digest.digest(_Config({100: "班级群"}), api_key="k", conn=conn,
                  since=since, until=until, day_label="2026-09-13",
                  output_dir=tmp_path)

    body = (tmp_path / "2026-09-13.md").read_text(encoding="utf-8")
    assert "真的没人说话" in body
    assert "vigil export" not in body
    assert "没有值得一提的信息" in body


def test_digest_window_before_all_data_with_zero_ts_dirty_row(seeded, monkeypatch,
                                                              tmp_path):
    """端到端：库里的 ``ts=0`` 脏行不许把「早于全部数据的一天」说成「没人说话」。

    ⚠️ 这条是**真库形状的复现**：真库有 14 条 1970 脏行 + 数据只到 9/13。
    不过滤 `ts > 0` 时 `span[0] = 0`，`outside` 两边都判不出来 → 日报说
    「这天真的没人说话」——**同一句假话的第三个入口**。
    """
    conn, since, until = seeded
    conn.execute("DELETE FROM items")
    conn.execute(
        "INSERT INTO messages VALUES (?,?,?,?,?)", (99, 100, 0, "u_x", "1970 脏行")
    )
    conn.commit()
    monkeypatch.setattr("vigil.digest.chat_json", _FakeLLM([]))

    early_since, early_until = digest.day_window("2026-06-01")
    digest.digest(_Config({100: "班级群"}), api_key="k", conn=conn,
                  since=early_since, until=early_until, day_label="2026-06-01",
                  output_dir=tmp_path)

    body = (tmp_path / "2026-06-01.md").read_text(encoding="utf-8")
    assert "vigil export" in body
    assert "真的没人说话" not in body


def test_digest_no_data_progress_line_has_no_zero_counts(seeded, monkeypatch,
                                                         capsys, tmp_path):
    """⚠️ 屏幕上的进度行与日报正文同标准：退化输入下不许打印「0 条」句子。

    旧代码在「已覆盖」分支无条件打印「（本地筛掉 0 条，送到模型 0 条）」——
    正文修好了、屏幕上还在说同一句退化的话（同一个假话的第三个出口：
    文件、进度行、CLI 汇总行）。
    """
    conn, since, until = seeded
    conn.execute("DELETE FROM items")
    conn.commit()
    monkeypatch.setattr("vigil.digest.chat_json", _FakeLLM([]))

    out_since, out_until = digest.day_window("2026-09-20")
    digest.digest(_Config({100: "班级群"}), api_key="k", conn=conn,
                  since=out_since, until=out_until, day_label="2026-09-20",
                  output_dir=tmp_path)

    out = capsys.readouterr().out
    assert "0 条" not in out
    assert "先跑 vigil export" in out


def test_digest_zero_sent_progress_line_has_no_zero_counts(seeded, monkeypatch,
                                                           capsys, tmp_path):
    """`sent == 0` 时进度行同样不许说「送到模型 0 条」（退化输入 ②）。"""
    from vigil import store

    conn, since, until = seeded
    conn.execute("DELETE FROM items")
    conn.execute("DELETE FROM messages")
    conn.executemany(
        "INSERT INTO messages VALUES (?,?,?,?,?)",
        [(1, 100, since + 10, "u_a", "收到"), (2, 100, since + 20, "u_b", "dd"),
         (3, 100, since + 30, "u_c", "随便聊聊天气不错啊今天挺热的")],
    )
    store.record_run(conn, [1, 2, 3], status=store.STATUS_DISCARDED, prompt_ver="v2")
    conn.commit()
    monkeypatch.setattr("vigil.digest.chat_json", _FakeLLM([]))

    digest.digest(_Config({100: "班级群"}), api_key="k", conn=conn,
                  since=since, until=until, day_label="2026-09-13",
                  output_dir=tmp_path)

    out = capsys.readouterr().out
    assert "0 条" not in out
    assert "一条也没送模型" in out


# ── 非空日的覆盖率（Task 12，用户 2026-09-15 裁定追加）──────────
#
# 覆盖率检查原先**只存在于空窗分支**：非空日若只抽取了一部分（`vigil refine`
# 撞预算护栏 `--budget` 会中途停），日报照常写「当天 9 个群 514 条消息，提炼出
# 6 条」——**读起来像读遍了 514 条，实际只看了一部分**。这是同一句「可能的假话」
# 的第四个变体，也是 M4 自动化（实跑 `refine → digest`）最容易撞上的一个。
#
# 判据：**只在覆盖不全时**加限定；覆盖完整时输出**一字不变**（用户已验收的
# 9/11、9/12、9/13 三篇覆盖率实测都是 100%）。


def test_stat_line_unchanged_when_fully_refined():
    """覆盖完整时一字不变——用户已验收的三篇走的正是这条。"""
    assert (
        digest.stat_line(groups=9, messages=514, items=6, refined=514)
        == "当天 9 个群 514 条消息，提炼出 6 条。"
    )


def test_stat_line_defaults_to_no_qualifier():
    """默认不加限定，既有调用点行为不变。"""
    assert "仅抽取" not in digest.stat_line(groups=9, messages=514, items=6)


def test_stat_line_shows_partial_coverage():
    """⚠️ 覆盖不全时必须说清楚，否则日报读起来像读遍了全部消息。"""
    line = digest.stat_line(groups=9, messages=514, items=4, refined=300)

    assert "仅抽取了 300/514" in line
    assert "提炼出 4 条" in line


def test_digest_nonempty_window_flags_partial_coverage(seeded, monkeypatch, tmp_path):
    """端到端：非空日只抽取了一部分时，正文必须带限定。"""
    from vigil import store

    conn, since, until = seeded
    conn.execute("DELETE FROM messages")
    conn.executemany(
        "INSERT INTO messages VALUES (?,?,?,?,?)",
        [(1, 100, since + 10, "u_a", "通知：明天交体检表"),
         (2, 100, since + 20, "u_b", "收到"),
         (3, 100, since + 30, "u_c", "有讲座")],
    )
    # 只记账前两条 → 覆盖 2/3
    store.record_run(conn, [1, 2], status=store.STATUS_DISCARDED, prompt_ver="v2")
    conn.commit()
    fake = _FakeLLM([{"quotes": ["体检表"], "label": "体检", "text": ""}])
    monkeypatch.setattr("vigil.digest.chat_json", fake)

    digest.digest(_Config({100: "班级群"}), api_key="k", conn=conn,
                  since=since, until=until, day_label="2026-09-13",
                  output_dir=tmp_path)

    body = (tmp_path / "2026-09-13.md").read_text(encoding="utf-8")
    assert "仅抽取了 2/3" in body


# ── `--dry-run` 预览不许猜正文（Task 11 修复轮次 2 / 审查 F1）──────────
#
# 预览是「关于日报会说什么的一句话」——**它和正文一样要核资格**。
# 这六条测试钉住「预览引用实际会写的正文」这个结构性保证。


def _all_dropped_window(conn, since, until):
    """造一个「有消息、全抽取过、一条都没出网」的窗口（覆盖完整 + sent==0）。"""
    from vigil import store

    conn.execute("DELETE FROM items")
    conn.execute("DELETE FROM messages")
    conn.executemany(
        "INSERT INTO messages VALUES (?,?,?,?,?)",
        [(1, 100, since + 10, "u_a", "收到"), (2, 100, since + 20, "u_b", "dd"),
         (3, 100, since + 30, "u_c", "随便聊聊天气不错啊今天挺热的")],
    )
    store.record_run(conn, [1, 2, 3], status=store.STATUS_DISCARDED, prompt_ver="v2")
    conn.commit()


def test_dry_run_preview_on_no_data_day_says_export(seeded, monkeypatch, capsys,
                                                    tmp_path):
    """⚠️ F1：无数据日的预览不许说「没有值得一提的信息」。

    Task 11 之前预览说「将写一篇『没有值得一提的信息』的日报」**是真话**
    （旧正文确实含该句）；Task 11 把正文改成「先跑 `vigil export`」之后
    同一行没人改，**预览就变成了假话**——同一个结构的第五个变体。
    """
    conn, since, until = seeded
    monkeypatch.setattr("vigil.digest.chat_json", _FakeLLM([]))

    out_since, out_until = digest.day_window("2026-09-20")      # 库里没有这一天
    digest.digest(_Config({100: "班级群"}), api_key="k", conn=conn,
                  since=out_since, until=out_until, day_label="2026-09-20",
                  output_dir=tmp_path, dry_run=True)

    out = capsys.readouterr().out
    assert "先跑 `vigil export`" in out
    assert "没有值得一提的信息" not in out
    assert "# 守夜人日报" not in out            # 只引正文行，不复述标题


def test_dry_run_preview_on_really_quiet_day_says_nobody_talked(seeded, monkeypatch,
                                                                capsys, tmp_path):
    """窗口在数据范围**之内**却 0 条 → 预览说「真的没人说话」。"""
    conn, since, until = seeded
    conn.execute("DELETE FROM items")
    conn.execute("DELETE FROM messages")
    conn.executemany(
        "INSERT INTO messages VALUES (?,?,?,?,?)",
        [(1, 100, since - 1000, "u_a", "前一天在聊"),
         (2, 100, until + 1000, "u_b", "后一天在聊")],
    )
    conn.commit()
    monkeypatch.setattr("vigil.digest.chat_json", _FakeLLM([]))

    digest.digest(_Config({100: "班级群"}), api_key="k", conn=conn,
                  since=since, until=until, day_label="2026-09-13",
                  output_dir=tmp_path, dry_run=True)

    out = capsys.readouterr().out
    assert "真的没人说话" in out
    assert "vigil export" not in out


def test_dry_run_preview_on_not_yet_refined_window_says_refine(seeded, monkeypatch,
                                                              capsys, tmp_path):
    """覆盖不全的空窗 → 预览说「尚未抽取」，不说「没有值得一提的信息」。

    ⚠️ 这一句是 **Task 9 遗留**的同因假话：Task 9 把正文改成「尚未抽取」时，
    预览那行同样没人改（F1 报告里点名的「同一行的第二个假话」）。
    """
    conn, since, until = seeded
    conn.execute("DELETE FROM items")
    conn.commit()
    monkeypatch.setattr("vigil.digest.chat_json", _FakeLLM([]))

    digest.digest(_Config({100: "班级群"}), api_key="k", conn=conn,
                  since=since, until=until, day_label="2026-09-13",
                  output_dir=tmp_path, dry_run=True)

    out = capsys.readouterr().out
    assert "尚未抽取" in out
    assert "vigil refine" in out
    assert "没有值得一提的信息" not in out


def test_dry_run_preview_matches_render_empty_day_verbatim(seeded, monkeypatch,
                                                           capsys, tmp_path):
    """覆盖完整的空窗：预览引用的正文与 `render_empty_day` 的输出**逐字一致**。"""
    conn, since, until = seeded
    _all_dropped_window(conn, since, until)
    monkeypatch.setattr("vigil.digest.chat_json", _FakeLLM([]))

    digest.digest(_Config({100: "班级群"}), api_key="k", conn=conn,
                  since=since, until=until, day_label="2026-09-13",
                  output_dir=tmp_path, dry_run=True)

    out = capsys.readouterr().out
    expected = digest.render_empty_day(
        day="2026-09-13", groups=1, messages=3, refined=3,
        local_dropped=3, sent=0,
    )
    title, _, rest = expected.partition("\n")
    assert title.startswith("# 守夜人日报")
    assert rest.strip("\n") in out            # 正文行逐字被引用
    assert title not in out                   # 标题不重复出现


def test_dry_run_preview_equals_body_actually_written(seeded, monkeypatch, capsys,
                                                      tmp_path):
    """⚠️ 端到端钉死「不漂移」：预览引用的正文 == **真跑写出来的**正文（去标题）。

    这条不依赖 `render_empty_day` 的参数（不硬编码 `local_dropped` / `sent`），
    也就不用 `_body_without_title`——期望值直接从产物文件里切出来。
    预览若用错了参数（比如漏传 `span`）或写死了文案，这条就红。
    """
    conn, since, until = seeded
    _all_dropped_window(conn, since, until)
    monkeypatch.setattr("vigil.digest.chat_json", _FakeLLM([]))

    digest.digest(_Config({100: "班级群"}), api_key="k", conn=conn,
                  since=since, until=until, day_label="2026-09-13",
                  output_dir=tmp_path, dry_run=True)
    preview = capsys.readouterr().out

    digest.digest(_Config({100: "班级群"}), api_key="k", conn=conn,
                  since=since, until=until, day_label="2026-09-13",
                  output_dir=tmp_path, dry_run=False)
    written = (tmp_path / "2026-09-13.md").read_text(encoding="utf-8")

    assert written.split("\n", 1)[1].strip("\n") in preview


def test_dry_run_preview_equals_written_body_on_no_data_day(seeded, monkeypatch,
                                                            capsys, tmp_path):
    """同上，但走 F1 的那个入口（无数据日）——两个分支都要钉。"""
    conn, since, until = seeded
    monkeypatch.setattr("vigil.digest.chat_json", _FakeLLM([]))
    out_since, out_until = digest.day_window("2026-09-20")

    digest.digest(_Config({100: "班级群"}), api_key="k", conn=conn,
                  since=out_since, until=out_until, day_label="2026-09-20",
                  output_dir=tmp_path, dry_run=True)
    preview = capsys.readouterr().out

    digest.digest(_Config({100: "班级群"}), api_key="k", conn=conn,
                  since=out_since, until=out_until, day_label="2026-09-20",
                  output_dir=tmp_path, dry_run=False)
    written = (tmp_path / "2026-09-20.md").read_text(encoding="utf-8")

    assert written.split("\n", 1)[1].strip("\n") in preview


# ── 截止日判据只有一份（M3 Task 1 平移）────────────────────────


def test_deadline_supported_is_the_shared_module_not_a_local_copy():
    """守住「判据只有一份」。

    若有人日后又在 digest.py 里写回一个本地 `deadline_supported`，
    Web 侧（读 deadline.py）与日报侧（读本地副本）就会各判各的——
    同一条截止日可能在日报里被隐去、在 Web 上照常显示。
    """
    from vigil import deadline

    assert digest.deadline_supported is deadline.deadline_supported


# ── M4 T6：落盘失败不许留下"库里有、文件没有"的日报 ────────────────


def _break_digest_file_write(monkeypatch, *, partial=False):
    """把「日报落盘」这一步变成必炸。

    ⚠️ **两种目标都要炸**：旧写法直接写 `YYYY-MM-DD.md`，新写法先写
    `.md.tmp` 再原子改名。注入若只认其中一种，M2（把 `tmp.replace(path)`
    换回 `path.write_text`）就会让测试「因为换了写法」而变红/变绿——那样
    守的成了实现细节，而不是「库什么时候提交」。两种都炸，M1/M2 的结论
    才只取决于提交顺序。

    `partial=False`：一个字节都没写就抛（权限拒绝、目录不存在那种）。
    `partial=True`：**先写一半再抛**——磁盘满的真实形状正是「文件已经被
    截断成半截」。

    注入点选 `Path.write_text` 是因为它是 `_persist` 落盘分支里**唯一**
    写字节的那一步（`tmp.replace` 只是改名，不写内容）；按后缀放行其它
    写入，避免误伤测试自身的文件操作。
    """
    real_write_text = Path.write_text

    def fake_write_text(self, data, *args, **kwargs):
        if self.name.endswith((".md", ".md.tmp")):
            if partial:
                self.write_bytes(data[: max(1, len(data) // 2)].encode("utf-8"))
            raise OSError(28, "No space left on device", str(self))
        return real_write_text(self, data, *args, **kwargs)

    monkeypatch.setattr(Path, "write_text", fake_write_text)


def test_persist_rolls_back_db_when_file_write_fails(seeded, monkeypatch, tmp_path):
    """⭐ 归档不变量的守护。

    不变量（接力文件原文）：`docs/digests/` 的文件集合 == 库里 `digests` 的
    窗口日期集合。旧 `_persist` **先写库、后写文件**，文件那步失败就留下
    一个没有 `.md` 的库行——不变量当场破掉，而没有任何东西会报错。
    修好后两者在同一事务里：文件写失败 ⇒ 库行一并回滚，两者都没有。

    变异反证：M1（删掉 `commit=False`）→ 本条红（`count(*)` 变成 1）。
    """
    conn, since, until = seeded
    monkeypatch.setattr(
        "vigil.digest.chat_json",
        _FakeLLM([{"quotes": ["体检表", "讲座"], "label": "全部", "text": ""}]),
    )
    _break_digest_file_write(monkeypatch)

    with pytest.raises(OSError) as excinfo:
        digest.digest(_Config({100: "班级群", 200: "新生群"}), api_key="k",
                      conn=conn, since=since, until=until,
                      day_label="2026-09-13", output_dir=tmp_path)

    # 失败必须**看得见**：异常照常抛给调用方（CLI/daily 转成非零退出）。
    # 断言错误原文而不只断言类型——别的 OSError 也满足类型，只有这句是
    # 「落盘那一步炸的」的证据（注入没生效时这条先红，不会假绿）。
    assert "No space left on device" in str(excinfo.value)
    assert conn.execute("SELECT COUNT(*) FROM digests").fetchone()[0] == 0
    assert not (tmp_path / "2026-09-13.md").exists()


def test_persist_failed_rerun_changes_neither_file_nor_db(seeded, monkeypatch,
                                                          tmp_path):
    """⚠️ 失败的重跑必须**什么都没改**：归档与库都停在上一版。

    三件都要钉，缺哪件就有变异存活：

    · **文件**：直接 `write_text` 是「截断原文件再写」，写到一半失败会把
      **上一版**日报砸成半截——而归档唯一的价值就是完整。所以落盘走
      `.md.tmp` → `Path.replace`（同目录改名，Windows 上也是原子的）。
    · **库**：`save_digest(commit=False)` 的 DELETE+INSERT 回滚后，库里必须
      还是**上一版**那一行。只在文件那半边回滚是不够的：不变量是「文件 ==
      库」，两边都得停在原地。
    · **目录**：失败不许在归档目录里留下 `.md.tmp`。不变量按原文是
      「`docs/digests/` 的**文件集合** == 窗口日期集合」——一个没人引用的
      残留文件正是同一个洞的镜像（而且这种写法是 T6 自己引入的）。

    变异反证：删掉失败分支的 `tmp.unlink` → 目录那条红；M2（`tmp.replace` →
    `path.write_text`）→ 文件那条红。
    """
    conn, since, until = seeded
    cfg = _Config({100: "班级群", 200: "新生群"})
    monkeypatch.setattr("vigil.digest.chat_json", _FakeLLM([
        {"quotes": ["体检表", "讲座"], "label": "第一版", "text": ""}]))
    digest.digest(cfg, api_key="k", conn=conn, since=since, until=until,
                  day_label="2026-09-13", output_dir=tmp_path)

    path = tmp_path / "2026-09-13.md"
    file_before = path.read_bytes()
    db_before = conn.execute(
        "SELECT body_md FROM digests WHERE window_from=? AND window_to=?",
        (since, until),
    ).fetchone()[0]
    # 前提：第一版真的落了盘、入了库（否则下面的断言在空集上恒真）
    assert file_before and "第一版" in db_before

    # 第二次：正文换成第二版，但落盘写到一半就炸
    monkeypatch.setattr("vigil.digest.chat_json", _FakeLLM([
        {"quotes": ["体检表", "讲座"], "label": "第二版", "text": ""}]))
    _break_digest_file_write(monkeypatch, partial=True)

    with pytest.raises(OSError):
        digest.digest(cfg, api_key="k", conn=conn, since=since, until=until,
                      day_label="2026-09-13", output_dir=tmp_path)

    assert [p.name for p in tmp_path.iterdir()] == ["2026-09-13.md"], \
        "归档目录里留下了残骸（半截 .md.tmp）"
    assert path.read_bytes() == file_before, "上一版日报被砸烂了"
    assert conn.execute(
        "SELECT body_md FROM digests WHERE window_from=? AND window_to=?",
        (since, until),
    ).fetchone()[0] == db_before, "库里被换成了第二版"


def test_persist_leaves_no_tmp_file_behind(seeded, monkeypatch, tmp_path):
    """成功的运行也不许在归档目录里留下 `.md.tmp`。

    不变量是按**文件集合**说的（`docs/digests/` 的文件集合 == 库里的窗口
    日期集合），所以临时文件只许活在「写 → 改名」之间那一瞬。变异 M2
    （`tmp.replace(path)` → `path.write_text(body, ...)`）在**成功路径**上
    每次都会留一个 `.md.tmp`：库里有一行、目录里多一个没人引用的文件——
    正是这条不变量要防的那个洞的镜像。`assert` 用集合相等而不是
    「`.tmp` 不存在」，这样「多出别的残骸」也会被抓住。
    """
    conn, since, until = seeded
    monkeypatch.setattr("vigil.digest.chat_json", _FakeLLM([
        {"quotes": ["体检表", "讲座"], "label": "全部", "text": ""}]))
    digest.digest(_Config({100: "班级群", 200: "新生群"}), api_key="k", conn=conn,
                  since=since, until=until, day_label="2026-09-13",
                  output_dir=tmp_path)

    assert sorted(p.name for p in tmp_path.iterdir()) == ["2026-09-13.md"]



# ── overlay 接入日报：**日报侧的语义继承**守卫（T5 Fix loop 第 1 轮 / FIX 1）──────


def _section_of(body: str, heading: str) -> str | None:
    """取 ``## <heading>`` 到下一个 ``## `` 之间的正文；没有这一节返回 None。"""
    marker = f"## {heading}\n"
    if marker not in body:
        return None
    return body.split(marker, 1)[1].split("\n## ", 1)[0]


def test_digest_buckets_by_the_overridden_kind(seeded, monkeypatch, tmp_path):
    """⭐ **日报必须按「覆盖后的类目」分桶**——这是 R13 在日报侧的落点。

    ⚠️ 变异反证（T5 审查实测 W3）：把 `store.window_items` 里的
    `COALESCE(ost.kind, i.kind)` 写回 `i.kind`（JOIN 与 attach 都留着、
    SQL 完全合法）⇒ **全量 478 条一条都不红**。
    后果正是「界面上改了分类、**日报仍按旧类目分桶出正文**」。

    为什么此前零守护：6 条 overlay 测试**全在 `tests/test_store.py`**，
    日报侧一条都没有——而 spec §3.3 说 `digest.py` 走 `window_items`、
    「一处生效，所有消费方自动继承」；**那句继承此前是无人守的断言**。
    """
    from vigil import overrides

    conn, since, until = seeded
    # item 1 原本是 notice（📋 通知公告），人工改成 academic（📚 学业）
    overrides.ensure_schema()          # autouse 夹具已把 OVERRIDES_DB 挪到 tmp
    w = overrides.connect()
    try:
        overrides.set_kind(w, item_id=1, kind="academic")
    finally:
        w.close()

    fake = _FakeLLM([
        {"quotes": ["体检表"], "label": "体检", "text": "10月8日前交"},
        {"quotes": ["讲座"], "label": "讲座", "text": "周五晚"},
    ])
    monkeypatch.setattr("vigil.digest.chat_json", fake)

    digest.digest(
        _Config({100: "班级群", 200: "新生群"}), api_key="k", conn=conn,
        since=since, until=until, day_label="2026-09-13", output_dir=tmp_path,
    )
    body = (tmp_path / "2026-09-13.md").read_text(encoding="utf-8")

    academic = _section_of(body, "📚 学业")
    assert academic is not None, "改了类目之后，日报里必须出现新类目那一节"
    assert "体检" in academic, "被改过类目的条目必须出现在**新**类目下"
    notice = _section_of(body, "📋 通知公告")
    assert notice is None or "体检" not in notice, (
        "同一条不许**同时**留在旧类目下（那是「改了分类、日报照旧」）"
    )
