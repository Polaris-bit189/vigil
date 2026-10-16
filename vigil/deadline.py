"""截止日核验：这个日期能不能在**源消息正文里逐字找到**。

为什么单独成模块：核验最初长在 M2 的日报渲染层（「别忘」那一节），
而**截止日的可信度是数据属性，不是渲染属性**。M3 的 Web 直接读
`items.deadline_ts`，只在渲染层挡的那道闸门就绕过去了——实测全库 26 条
带截止日的 item 里约半数过不了核验，Web 会把它们原样复活。

所以核验被提到数据层：写入时（refine）挡一次，历史数据用
`vigil deadline-audit --apply` 回填一次。之后**所有读取方自动安全**。
"""

from __future__ import annotations

import datetime as dt
import re


# 汉字写法里各成分之间允许的空白：实测源文有「9 月 16 日」这种写法。
#
# ⚠️ 用 ``[^\S\n]``（横向空白）而**不是** ``\s``：源文是**多条消息用 "\n" 拼起来的**
# （``store.item_sources_text``），容忍换行的话「……9月」结尾的一条消息与
# 「16日开始……」开头的另一条会被粘成一个日期——凭空造出「源文里有」的证据。
# 上一版把整个 haystack 的空白全部删掉（``\s+`` → ``""``），正是这个毛病。
_GAP = r"[^\S\n]*"


def _date_patterns(ts: int) -> tuple[re.Pattern[str], ...]:
    """同一个日期在中文语料里的常见写法（编译后的正则）。**只认带月份的写法。**

    两类，边界规则不同：

    * **数字写法**（``9-16`` / ``9/16`` / ``9.16`` / ``2026-09-16`` …）两侧
      **不能紧挨数字**。审查 F2 在真实库上实测，下面 4 行上一版**全部返回 True**
      （源文里只有 9 月 30 日，却给一条 9 月 3 日的截止日背书；后两行同形）::

          deadline_supported(9-3, "9/30截止")
          deadline_supported(9-3, "9-30截止")
          deadline_supported(9-3, "2026/09/30")
          deadline_supported(9-16, "119-16")

      边界是**纯数字**的（不含 ``-`` / ``/`` / ``.``）：这样「9/3-9/5 报名」这类
      日期区间、以及「2026-09-16」这种更完整的写法都不会被自己的边界误伤。
      注意边界**只挡数字**，所以「9-16 12:00」照旧命中——日期后面跟的是时间。
    * **汉字写法**（``9月16日`` / ``9月16号``）由「月/日」夹住，天然有边界，
      且允许成分间有横向空白（``9 月 16 日``）。
    """
    d = dt.datetime.fromtimestamp(ts)
    y, m, dd = d.year, d.month, d.day
    numeric = (
        f"{y}-{m:02d}-{dd:02d}",
        f"{y}/{m:02d}/{dd:02d}",
        f"{m:02d}-{dd:02d}",
        f"{m}-{dd}",
        f"{m:02d}/{dd:02d}",
        f"{m}/{dd}",
        f"{m:02d}.{dd:02d}",   # 实测源文有「9.11左右截止」（item 258）
        f"{m}.{dd}",           # 实测源文有「9.6上午」（item 84）
    )
    return (
        *(re.compile(rf"(?<!\d){re.escape(f)}(?!\d)") for f in numeric),
        re.compile(rf"{m}{_GAP}月{_GAP}{dd}{_GAP}[日号]"),
    )


def deadline_supported(deadline_ts: int | None, sources: str) -> bool:
    """源文里能不能逐字找到这个日期。**找不到就是没有依据。**

    ⚠️ 判据刻意做成机械可判的（不是再叫一个模型去判）：
    源文里找得到就是找得到，找不到就是没有依据。这样它可以被测试守卫，
    也不会引入第二个模型的判断。

    ⚠️ 为什么要这一步（首次冒烟的真实数据破坏）：M1 的抽取会补出原文没有的
    实体，源文只写「明早7:20集合」「周六下午4.00-8.00」时它照样填了具体日期，
    而「⏰ 别忘」把这个幻觉洗成了机器权威——用户拿去办事的地方在骗人。

    ⚠️ **只认带月份的写法**（``9月16日`` / ``9-16`` / ``9.16`` …）。「26号」「30号」
    这种**裸日不算依据**——修复轮次 1 移除的「裸日回退」在真实库上精度 **0/2**
    （item 88 源文「我有一个朋友26号就开学」被拿去给标着 09-26 的截止日背书、
    item 109 源文「30号就得到学校」给 09-30 背书，两条的语境都指向当月），
    方向同样是"往后飘"。核对月份这一步不能省。
    """
    if not deadline_ts:
        return False
    return any(p.search(sources) for p in _date_patterns(deadline_ts))


def _time_patterns(ts: int) -> tuple[re.Pattern[str], ...]:
    """同一个时刻在中文语料里的常见写法（编译后的正则）。

    ⚠️ **比日期那套更宽**（`_GAP` 同样禁止跨行）：实测源文里时刻的写法
    远比日期散，而这一道闸门挡的是"模型凭空编一个 08:00"，不是"写法要对上"。
    所以凡是能**唯一确定**该时刻的常见写法都收：

      14:00 / 14：00        数字（半角/全角冒号都认）
      14点 / 14时           数字 + 单位
      下午2点 / 晚上7点半    中文数字 + 时段词
      2:00pm / 14:00        带 pm 的写法

    ⚠️ **「半」要单独认**：实测源文有「晚上7点半」，而只认「7点」的写法会
    把它判成"没有依据"、把一个真时刻丢掉（漏判比误判更常见也更隐蔽）。

    ⚠️ 与 `_date_patterns` 不同，这里**不要求两侧有数字边界**：时刻的边界
    是冒号/单位词本身，而 `(?<!\\d)14:00(?!\\d)` 会把「114:00」这种噪音挡掉
    —— 那是对的，但「9-16 12:00」里 12:00 前面是空格，不受影响。
    """
    d = dt.datetime.fromtimestamp(ts)
    h, m = d.hour, d.minute
    # 24 小时制里 0 点写作「0:00」，但人更常写「凌晨0点」/「晚上12点」；
    # 12 小时制要区分 上午/下午 —— 两种都收。
    h12 = h % 12 or 12
    # 时段词按**自然语感**分档。⚠️ 分档只影响"要不要认这个前缀"，
    # 所以**相邻档都收**（见下面的 `periods`）—— 猜错时段词会漏判，
    # 而漏判的后果是"把真时刻丢掉"，比多认一个前缀糟得多。
    if h < 6:
        period = "凌晨"
    elif h < 12:
        period = "上午"
    elif h < 13:
        period = "中午"
    elif h < 18:
        period = "下午"
    else:
        period = "晚上"
    # ⚠️ 相邻时段词都算依据（「上午11点」与「中午11点」是同一个时刻）。
    # 不这么做的话，一个用词习惯与我们对不上的源文会被判成"没有依据"。
    _NEIGHBORS = {
        "凌晨": ("凌晨", "早上", "上午"),
        "上午": ("上午", "早上", "中午"),
        "中午": ("中午", "上午", "下午"),
        "下午": ("下午", "中午", "晚上"),
        "晚上": ("晚上", "下午", "夜里"),
    }
    periods = _NEIGHBORS.get(period, (period,))
    # 中文数字（只覆盖 1-12，够用了；13 以上中文写作不常见且歧义大）
    _CN = ("", "一", "二", "三", "四", "五", "六", "七", "八", "九", "十",
           "十一", "十二")
    cn_h = _CN[h12] if 0 < h12 <= 12 else ""
    # 「半」只在分钟正好是 30 时才算依据 —— 否则「7点半」不能给「07:00」背书
    half = "半" if m == 30 else ""
    cn_min = f"{m}分" if m else ""

    pats: list[str] = []
    if m == 0:
        # ⚠️ 整点后面**既不能跟数字、也不能跟「半」** —— 实测这条抓出过一个
        # 真缺陷：源文「晚上7点半」里「7点」后面是「半」，只挡数字的话
        # 19:00 会被它背书，判出一个**早半小时**的时刻（不报错，只是排早）。
        pats += [
            rf"(?<!\d){h}{_GAP}[:：]{_GAP}00(?!\d)",
            rf"(?<!\d){h}{_GAP}[点时](?!\d)(?!半)",
        ]
    else:
        pats.append(rf"(?<!\d){h}{_GAP}[:：]{_GAP}{m:02d}(?!\d)")
        # 「14点30」/「14时30分」
        pats.append(rf"(?<!\d){h}{_GAP}[点时]{_GAP}{m}{_GAP}[分]?")
        if half:
            pats.append(rf"(?<!\d){h}{_GAP}[点时]{_GAP}半")
    # ⚠️ **中文数字与阿拉伯数字都要生成**（实测踩到）：「下午2点」里的 `2`
    # 是阿拉伯数字，而 `_CN[2]` 给的是「二」—— 只生成中文写法的话，
    # 这条最普通的源文会被判成「没有依据」。
    #
    # ⚠️ 同理，19:30 的写法可能是「晚上7点半」（12 小时制）或「19:30」（24 小时制），
    # 两条路都要覆盖 —— `h12` 那一路管前者，`h` 那一路（上面的
    # `[点时]` 系列）管后者。
    # ⚠️⚠️ **整点写法后面必须排除「半」**（实测：这条断言抓出过一个真缺陷）。
    # 源文「晚上7点半」里，「7点」后面跟的是「半」而不是数字 —— 一个
    # `(?!\d)` 是挡不住的，于是 19:00 会被「7点半」背书，判出**早半小时**的
    # 时刻。它不会报错，只会让日程把活动排早 30 分钟。
    #
    # ⇒ 判据是 `[点时]` 后面**既不能跟数字、也不能跟「半」**（当分钟为 0 时）。
    _not_half = "(?!半)" if m == 0 else ""
    for w in periods:
        for hh in (str(h12), cn_h):
            if not hh:
                continue
            if m == 0:
                pats.append(rf"{w}{_GAP}{hh}{_GAP}[点时](?!\d){_not_half}")
            else:
                pats.append(rf"{w}{_GAP}{hh}{_GAP}[点时]{_GAP}{m}{_GAP}分?")
                if half:
                    pats.append(rf"{w}{_GAP}{hh}{_GAP}[点时]{_GAP}半")
    # 不带时段词的写法（「8点集合」）也认 —— 源文常常省掉时段。
    for hh in (str(h12), cn_h):
        if not hh:
            continue
        if m == 0:
            pats.append(rf"(?<!\d){hh}{_GAP}[点时](?!\d){_not_half}")
        else:
            pats.append(rf"(?<!\d){hh}{_GAP}[点时]{_GAP}{m}{_GAP}分?")
            if half:
                pats.append(rf"(?<!\d){hh}{_GAP}[点时]{_GAP}半")
    return tuple(re.compile(p) for p in pats)


def start_supported(start_ts: int | None, sources: str) -> bool:
    """源文里能不能逐字找到这个**时刻**。找不到就是编的。

    ⚠️ 与 `deadline_supported` 同一道闸门、同一条理由：模型会编。
    实测（README 记着的那条）：源文只写「明早 7:20 集合」，模型照样填了一个
    具体日期 —— 而**时刻**比日期更容易被编，因为"几点开始"正是模型最想
    补全的那一格。没有这道闸，`start_ts` 会成为一个**看起来精确的幻觉**，
    而它会驱动日程表按分钟排期 —— 比编一个日期危险得多。

    ⚠️ 返回 False 的处置是**降级成 NULL**（不是丢条目）：条目照常显示，
    只是"排不进日程表"。与 `deadline_ts` 的处置逐字一致。
    """
    if not start_ts:
        return False
    return any(p.search(sources) for p in _time_patterns(start_ts))


def unverified_item_ids(
    items: list[tuple[int, int | None]], sources: dict[int, str]
) -> frozenset[int]:
    """哪些 item 的截止日**在源文里没有依据**（只统计真有截止日的那些）。

    没有来源行的 item（``sources`` 里查不到）一律按**无依据**处理——
    与 ``digest.verified_deadlines`` 同一口径：**证不出来就是没有**。

    ⚠️ 这里返回的是「该清掉」的集合，而不是「该保留」的集合。命名方向
    故意与 ``verified_deadlines`` 相反，因为它们各自的调用点是反的：
    渲染层是「挑出可信的去显示」，数据层是「挑出不可信的来清掉」。
    """
    out: list[int] = []
    for item_id, deadline_ts in items:
        if deadline_ts and not deadline_supported(deadline_ts, sources.get(item_id, "")):
            out.append(item_id)
    return frozenset(out)


def deadline_level(deadline_ts: int, now_ts: int) -> str:
    """距死线的分级：`expired` / `urgent` / `soon` / `normal`。

    ⚠️ **这份阈值与前端 `web/src/format.ts::relDeadline` 必须逐字一致。**
    两处各写一份是**知情取舍**，不是疏忽：

      · 日报要落盘成 `.md`（后端渲染），前端拿不到 ⇒ 后端必须有一份
      · 日历要跟随「当前时刻」变化（前端渲染），后端算完会过期 ⇒ 前端必须有一份

    ⚠️ 症状提醒：阈值一旦漂移，表现是「**日历上标红了、日报里却没提**」——
    查起来毫无头绪，因为两边单看都正常。所以两处各钉同一条边界用例
    （0 / 1 / 2 / 3 / 7 / 8 天）。

    ⚠️ 按**本地日**对齐，不是按秒差：死线是「日」级语义（`_parse_deadline`
    只解析到日，见 `refine.py`）。按秒差算的话，「明天 09:00 截止」在今晚
    23:00 会算出 0.4 天 ⇒ 取整成 0 ⇒ 显示「今天截止」，**差了一天**。
    """
    days = _day_diff(deadline_ts, now_ts)
    if days < 0:
        return "expired"
    if days <= 1:
        return "urgent"
    if days <= 3:
        return "soon"
    return "normal"


def rel_deadline_text(deadline_ts: int, now_ts: int) -> str:
    """距死线的人读文案。与 `deadline_level` 共用同一套日差。

    ⚠️ 超过 7 天回落到**绝对日期**而不是「还有 12 天」——那时人已经在看日历，
    相对天数反而要心算。（前端 `relDeadline` 同此处置。）
    """
    days = _day_diff(deadline_ts, now_ts)
    if days < 0:
        return f"已过期 {-days} 天"
    if days == 0:
        return "今天截止"
    if days == 1:
        return "明天截止"
    if days <= 7:
        return f"还有 {days} 天"
    d = dt.datetime.fromtimestamp(deadline_ts)
    return f"{d.month}月{d.day}日"


def _day_diff(a: int, b: int) -> int:
    """两个时间戳相差几个**本地日**（正数 = 前者在未来）。

    忽略当日已过的时分秒——死线是「日」级语义，差 23 小时与差 1 小时
    在「还有几天」这个问题上应当等价（都是「明天/今天」的分界）。
    """
    return round((_day_start_of(a) - _day_start_of(b)) / 86400)


def _bigrams(text: str) -> frozenset[str]:
    """去掉分隔符与空白后的全部 2 字子串。

    分隔符按实测数据里真出现过的集合取（item 271 用顿号、item 137 用全角竖线）。
    """
    cleaned = _PLACE_SEP.sub("", text)
    return frozenset(cleaned[i : i + 2] for i in range(len(cleaned) - 1))


_PLACE_SEP = re.compile(r"[\s、,，/｜|·]+")


def place_supported(place: str | None, sources: str) -> bool:
    """源文里能不能找到这个地点的依据。

    ⚠️ **判据刻意做得宽**（任一 2 字子串命中即算有依据），因为 place 与 deadline
    的性质不同：deadline 驱动「⏰ 别忘」，错了会让人误事；place 只是显示提示。
    实测：整串匹配太严会误杀「西太湖校区 ← 源文『西太湖连隔板』」这类。

    ⚠️ **短于 2 字的 place 判据不适用 ⇒ 返回 True**。真实数据里 item 295 的
    place 是单字「湖」，bigram 对它恒为空集——写成 False 会静默误清，
    且没有任何测试会因此变红（M2「多分支判据必须多分支守卫」）。
    """
    if not place:
        return True
    grams = _bigrams(place)
    if not grams:
        return True
    return bool(grams & _bigrams(sources))


def deadline_sane(deadline_ts: int | None, event_ts: int) -> bool:
    """截止日不早于消息当天——`deadline` 字段的语义是**需要行动的截止日**。

    ⚠️ 这不是"幻觉检测"（那由 `deadline_supported` 负责）。item 83 的
    `档案袋封口时间：5 月 6 日` 在源文里**找得到**、抽取也没错，它是**过去的既成事实**——
    但它不满足「需要行动」这个语义，所以不该占着「⏰ 别忘」那一节。

    判据按**本地日**比较（与 `_day_start` 的口径一致）：同一天不过夜 ⇒ 放行。
    """
    if deadline_ts is None:
        return True
    day_start = _day_start_of(event_ts)
    return deadline_ts >= day_start


def _day_start_of(ts: int) -> int:
    """某个 epoch 秒**所在的本地日**的 00:00（epoch 秒）。

    ⚠️ **偏差（逃逸舱第 1 种形状，见报告「偏差」第 3 条）**：brief 只有前两行，
    而它在 Windows 上会**抛**——``datetime(1970,1,1).timestamp()`` 是负 epoch，
    本机抛 ``OSError [Errno 22]``（与 `_parse_deadline` 的 docstring 同一条限制：
    1970-01-03 之前的本地时间转不成 unix 秒，本机实测 ``fromtimestamp(-28800)``
    同样抛）。后果**不是"测试红"而是静默丢数据**：调用点在 `_to_item` 里，而它
    的外层是 `refine()` 每批的 ``try/except``——一个超前/畸形的 `event_ts` 会让
    **整批**的 items 一条都不落库，只留一行 error。既有夹具 ts=1000（1970）正好
    命中它，实测把 `test_refine_parses_deadline` 与
    `test_refine_wires_the_deadline_drop_end_to_end` 打红，这条路才被发现。

    ⇒ 正常路径**逐字保留** brief 的写法（结果与 `api._day_start` 同口径），
    只在它抛的那一格补一个**纯算术**回退：`fromtimestamp` 拿到的本地墙钟减去
    「当日已过的秒数」就是本地午夜，全程不再对负 epoch 调 `timestamp()`。
    中国无夏令时，本机两条路径逐秒一致；跨 DST 时区时回退路径的口径要重新裁决。
    """
    d = dt.datetime.fromtimestamp(ts)
    try:
        return int(dt.datetime.combine(d.date(), dt.time.min).timestamp())
    except (OSError, OverflowError):  # Windows：1970-01-03 之前的本地时间
        return ts - (d.hour * 3600 + d.minute * 60 + d.second)
