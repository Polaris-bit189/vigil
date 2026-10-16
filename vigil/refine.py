"""抽取引擎：messages → items。

流水线：
    取待处理 → 规则预筛 → 按群切批 → 展开上下文 → 脱敏 → 批量调 LLM
    → 落 items + item_sources → 记账 refine_runs

设计的两个关键取舍：
  * **丢掉的消息也要记账**。refine_runs 必须覆盖全部消息，
    否则「哪些看过、哪些没看过」分不清，增量就无从谈起。
  * **单批失败不中断整轮**。沿用 export.py 对 senders/media 的既有取舍：
    外部环境的问题降级并记录，不拖垮主流程。
"""

from __future__ import annotations

import datetime as dt
import re
import sqlite3
from dataclasses import dataclass, field, replace
from pathlib import Path

from . import prefilter, store
from .categories import Category, prompt_block
from .config import Config
from .deadline import (
    deadline_sane,
    deadline_supported,
    place_supported,
    start_supported,
)
from .llm import (
    DEFAULT_BASE,
    DEFAULT_MODEL,
    LLMConfig,
    LLMError,
    chat_json,
    sanitize_for_llm,
)
from .redact import Redactor
from .store import PendingMessage

# ⚠️ **v4 起提示词要求 `start`（活动开始时刻）**，见下面的字段说明。
#
# 升版的理由（不是仪式）：`refine_runs` 用 `prompt_ver` 区分产出，而 v3 与 v4
# 的 items **不可比**（v4 多一个字段、且模型看到的要求变了）。
# 不升版的话两版数据会混在同一个 `prompt_ver` 下，事后无法分辨哪些条目
# 是按新要求抽的 —— 而 M1 的量化指标全是 v3 的产物。
PROMPT_VERSION = "v4"

# 输出 token 的上限估计，用于把「输出」也算进预算
_BATCH_TITLE_MAX = 40


@dataclass
class RefineStats:
    """如实报告——失败与跳过都要看得见。"""

    scanned: int = 0
    discarded_local: int = 0
    skipped_deleted: int = 0     # 因软删墓碑而被跳过的消息数（D15）
    candidates: int = 0          # 通过预筛的候选数（= prefilter.ScreenStats.kept）
    sent_messages: int = 0
    batches_planned: int = 0  # 计划要跑多少批
    batches: int = 0  # **实际**跑了几批——预算 break 之后会小于 planned
    items_saved: int = 0
    deadlines_dropped: int = 0   # 源文里找不到依据、被降级的截止日条数
    # ⚠️ 与上一个分开（M6 T10 / M5 遗留 #4）：那个是「模型在编」，
    # 这个是「死线早于消息时间」——运维含义完全不同，合成一个数会让两者
    # 都说不清。它此前**根本没有计数器**，于是「一批 12 条过期死线被全清」
    # 在日志里完全隐形。
    deadlines_backdated: int = 0
    input_tokens: int = 0
    output_tokens: int = 0
    budget_hit: bool = False
    errors: list[str] = field(default_factory=list)

    @property
    def total_tokens(self) -> int:
        return self.input_tokens + self.output_tokens


def build_system_prompt(cats: tuple[Category, ...]) -> str:
    """系统提示词。类目表从配置渲染，改 categories.toml 即改提示词。

    ⚠️ 「以下一律直接不出现在结果里」那三条是用户反馈逼出来的（纯询问价值低、
    多为噪声）。**它一度只写进了计划文档而没进代码**——commit 481dfb9 的
    范围是 `feat(plan)`，改的是 docs/superpowers/plans/…md 里的代码块，
    `vigil/refine.py` 一行没动。后果在 2026-09-14 的模型对照实验里现形：
    模型照样把「怎么向一卡通里充钱啊」「我们不体检吗」抽成条目。

    改这里务必同步 `PROMPT_VERSION`，否则 refine_runs 里新老提示词的产出
    混在一起，谁也说不清哪条是哪个提示词抽的。
    """
    return f"""你是校园 QQ 群的信息提炼助手。从群聊消息里挑出对大学生真正有价值的信息。

类目（只能选这些）：
{prompt_block(cats)}

**以下一律直接不出现在结果里**——不要为它们输出任何占位元素：

* 闲聊、纯表情、无信息量的发言
* **纯询问 / 提问类消息**——「XX 在哪个校区」「能不能带电脑」「什么时候开学」
  这类**提问本身不含信息**，价值低且多为噪声。
  只有当消息**给出了答案或明确信息**时才产出
  （例：「30 号就得到学校」是信息，「30 号开学吗」不是）
* 同学间的感慨、吐槽、附和
* **商业推广 / 代理招募 / 办卡广告**——流量卡、校园卡、宽带、电话卡的办理与
  代理招募、开卡返现、找人办卡、转让卡位等**以推销或拉客为目的**的消息。
  ⚠️ 判据看**这条消息想干什么**，不看它出现了哪个词。同样出现「校园卡」：
  「校园卡办理，需要的联系我」= 推广，丢掉；
  「打电话办卡的都别信哦所有人」= 提醒，「最早9.5」= 信息，
  「住宿费也统一扣1500」= 通知——**这三条都要照常产出**。

输出必须是 JSON 对象，形如 {{"items": [...]}}，其中 items 是数组。
每个元素的结构：
{{"quote": "从原消息里逐字摘录的连续片段",
 "kind": "notice", "title": "一句话摘要（≤{_BATCH_TITLE_MAX}字）",
 "detail": "补充细节或null", "deadline": "YYYY-MM-DD 或 null",
 "start": "YYYY-MM-DD HH:MM 或 null",
 "place": "地点或null", "amount": "金额或null", "confidence": 0.9}}

⚠️ `quote` 必须是原消息里**原样连续出现**的片段（10-30 字）。**不要改写、
不要补全、不要跨消息拼接**——程序会拿它去逐字匹配来源消息。
摘录不准的条目会被直接丢弃，所以**宁可短而准**。

⚠️ `deadline` 与 `start` 是**两件事**，别混：

  `deadline` = **截止日**（「9月30日前交」「周一之前报名」）——**只到日**。
  `start`    = **活动开始的时刻**（「周三 14:00 在体育馆集合」）——**到分钟**。

⚠️ **`start` 只在源文里明确写了时刻时才填。** 源文只写「明早集合」「周六下午」
而没写几点，就填 `null` —— 程序会拿它去源文里逐字核验，**编出来的时刻一律作废**，
那还不如不填（不填的条目照常显示，只是排不进日程表）。

⚠️ 同理，**别把 `deadline` 的时刻抄给 `start`**：死线是「截止到哪天」，
不是「什么时候开始做」。

死线日期必须结合下面提供的「今天」推算正确年份。没有死线的填 null。
`start` 的日期同样要结合「今天」推算，并写成 `YYYY-MM-DD HH:MM`（24 小时制）。"""


def build_user_prompt(
    messages: list[PendingMessage], redactor: Redactor, *, today: str
) -> str:
    """用户提示词。**脱敏在这一步完成**——所有离开本机的内容都经过这里。

    形如 ``- U3 昵称: 内容``：代号（姓名保留、号码已抹）让模型能看出
    「同一人说了两次」。**消息本身不带任何编号**——来源定位走 `quote`。

    ⚠️ 为什么不用编号（msg_id 和序号都试过了，都不行）——Task 8 冒烟实测结论：

    * **msg_id 不行**：19 位雪花号，同批 30 条只有末 3 位不同
      （`…673881` / `…673929` / `…673918`）。实测命中 **0/2**，返回的全是
      「在批内但不对应」的 ID——错归因被静默接受。
    * **序号也不行**：换 1-2 位序号后 A/B 测试命中 1/1，但**真实管线里仍然错**
      ——实测一个 4 条消息的批次，真来源在第 3 位，模型读对了内容却返回
      `idx=1`。7B 在「4 条里选 1 个序号」这种任务上也不可靠。

    最终方案是**不问模型编号**：让它逐字摘录，由 `_match_source` 本地定位。
    7B 上实测命中，且**自校验**——匹配不上就丢弃，不可能静默挂错来源。

    ⚠️ 匿名发送者（uid 为空串）**必须逐条区分**。实测全库 1,218 条（2.55%）
    匿名消息；若一律走 ``redactor.actor('')``，它们会全部拿到同一个代号
    （W1 波级审查实测：两个不同群的匿名者都是 ``U1``），模型便会把互不相识
    的人当成同一个人。**匿名者没有身份可言，所以给每条一个互不相同的标记。**
    """
    lines = []
    anonymous = 0
    for m in messages:
        # ⚠️ 出网前**必须**过 sanitize_for_llm（M2-3）。全角引号会让模型退化成
        # 无限空格循环——而 refine 侧的 max_tokens 默认是 None（不发这个字段），
        # 所以唯一的刹车是 180s 超时 × 3 次重试 ≈ 9 分钟空转。
        # 附带收益：`“”` 换成 `「」` 之后 refine 的归一化表认得后者，
        # 模型摘录时自己包一层引号也不会再丢掉来源（计划 §二 发现 9 实测）。
        # ⚠️ 正文要过**两道**：`text()` 抹号码，`scrub_names()` 抹已知人名。
        # 少了第二道，正文里直接写着的人名（「张三说他明天不来」）会照样出网 ——
        # 称呼那一处抹得再干净也没用。这个洞是出口验收的机械判据抓出来的。
        body = sanitize_for_llm(redactor.scrub_names(redactor.text(m.content)))
        if m.sender_uid:
            # ⚠️ 称呼走 `redactor.name()` 而**不是** `redactor.text(m.sender)`：
            # 后者只抹号码、**保留姓名**（见 redact.py 的模块 docstring），
            # 而姓名一旦离开本机就是最直接的个人标识符。`name()` 保留角色词
            # 与班级前缀（那是判断"是不是官方"的信号）、抹掉人名本体。
            #
            # ⚠️ `name()` 的输出是**受控字符串**（只有角色词/前缀/兜底词），
            # 既不含号码也不含全角引号，所以这一处**不需要**再过
            # `sanitize_for_llm`。`body` 那一行的处理原样保留。
            name = redactor.name(m.sender_uid, m.sender) or "未知"
            who = f"{redactor.actor(m.sender_uid)} {name}"
        else:
            # 批内计数器，不用 msg_id——不给模型任何长数字，断掉它"抄数字"的路径
            anonymous += 1
            who = f"匿名{anonymous}"
        lines.append(f"- {who}: {body}")
    return f"今天的日期是 {today}。\n\n消息如下：\n" + "\n".join(lines)


def _record_error(
    conn: sqlite3.Connection, msg_ids: list[int], *, prompt_ver: str, err: object
) -> str | None:
    """把一批消息记成 error 状态。**自己失败不再抛**。

    ⚠️ 存在的理由（M3-2）：三处 `store.record_run` 原先都**裸奔在 `except`
    体里**。异常处理路径上再抛异常，`except` 是接不住自己的——它会穿过整个
    批次循环把整轮 refine 掀掉，**而那时恰恰是"库出了问题"的时候**（锁死、
    只读、磁盘满），也就是最可能连续失败的时候。

    ⚠️ 本函数**不吞**异常：它把失败**返回**给调用方，由调用方照常写进
    `stats.errors`——账目仍然可见，只是不再掀桌子。M1/M3 的教训是
    「静默吞异常不可接受」，这条守住了。

    返回值：记账也失败时的描述；成功返回 `None`。
    """
    try:
        with store.transaction(conn):
            store.record_run(
                conn, msg_ids, status=store.STATUS_ERROR,
                prompt_ver=prompt_ver, err=str(err)[:200], commit=False,
            )
    except Exception as inner:  # noqa: BLE001 — 见 docstring：返回而非抛出
        return f"error 记账也失败了: {inner}"
    return None


def _tombstoned_msg_ids() -> frozenset[int]:
    """读人工干预层的墓碑（软删事件对应的源消息 id）。

    ⚠️ 读不到一律返回空集——overlay 缺失**不该**让 refine 跑不起来。
    最坏后果只是"这次重抽把某条复活了"，用户再删一次即可；
    而在这里抛异常会让整条每日管线停摆，代价大得多。
    """
    from . import overrides

    try:
        conn = overrides.connect()
        try:
            return overrides.deleted_msg_ids(conn)
        finally:
            conn.close()
    except sqlite3.Error:
        return frozenset()


def _parse_deadline(value: object) -> int | None:
    """把 'YYYY-MM-DD' 转成 unix 秒。解析不了就当没有——不猜。

    ⚠️ 只捕 ``ValueError`` 是不够的：``"1970-01-01"`` 是**合法日期**，却在
    ``timestamp()`` 上抛 ``OSError``（实测本机：1970-01-03 之前的日期、
    以及 4000 年以后的日期都落不进去）。而它恰恰是模型表达「没有截止日」
    最经典的哨兵——只挡格式不挡范围，等于把最常见的那个值留成了地雷。
    捕获范围与 ``api._day_start`` 对齐：**格式错的、范围错的都当"没有"处理**。

    ⚠️ 返回值保持"没把握就不猜"：抛异常和返回 None 对调用方是同一件事，
    但没有第二个人知道要在这里接异常——M3 的接线点（`_to_item`）就在
    每批 try/except 的**外面**，一个日期串足以掀掉整轮 refine。
    """
    if not isinstance(value, str) or not value.strip():
        return None
    try:
        return int(dt.datetime.strptime(value.strip(), "%Y-%m-%d").timestamp())
    except (ValueError, OSError, OverflowError):
        return None


def _parse_start(value: object) -> int | None:
    """把 ``'YYYY-MM-DD HH:MM'`` 转成 unix 秒。解析不了就当没有——不猜。

    ⚠️ **刻意与 `_parse_deadline` 分成两个函数**，不合并
    （`docs/design/时间安排.md` §二 的裁决）：
    死线**本来就只到日**（「9月30日前完成」），强制它带时刻会让模型编一个
    时间出来。两个字段的**精度要求不同**，共用一个解析器迟早把其中一边的
    语义弄坏 —— 而症状很隐蔽：死线悄悄变成「当天 00:00」。

    ⚠️ 分钟缺失算不算？**不算**。`"%Y-%m-%d %H:%M"` 对 ``'2026-09-16 14'``
    会抛 ValueError ⇒ 返回 None。这是刻意的：源文里没写分钟时，模型补出来的
    那一半同样是编的。宁可不要，也不能拿一个半真半假的时刻去排日程。

    ⚠️ 捕获范围与 `_parse_deadline` 逐条对齐（ValueError / OSError /
    OverflowError）：1970-01-03 之前的日期在 `timestamp()` 上抛 OSError，
    而 4000 年以后抛 OverflowError——只挡格式不挡范围，等于留了个地雷在
    调用方的 `try` 外面（它掀掉的是一整批，不是一个字段）。
    """
    if not isinstance(value, str) or not value.strip():
        return None
    try:
        return int(dt.datetime.strptime(value.strip(), "%Y-%m-%d %H:%M").timestamp())
    except (ValueError, OSError, OverflowError):
        return None


# 归一化时剥掉的字符：空白与常见中英文标点。
# 目的是容忍「（640）」vs「(640)」这类**格式**差异，而不是容忍改写。
#
# ⚠️ **本字符集必须与 `vigil/digest.py` 的同名表（`digest.py:197`）逐字一致。**
# 两处的理由是同一个：一侧的引号在**出网时被换过**，另一侧没有。
# `sanitize_for_llm` 把 `“”` 换成 `「」` 送出去，模型照抄回来的是 `「」`，
# 而**源文里躺着的是 `“”`**——只剥 `「」` 不剥 `“”` 的话，两侧归一化不对称，
# 摘录整句时子串对不上，**那条 item 直接丢掉来源**（实测 ~25/47,719）。
#
# ⚠️ 这是 **M2-3 修复引入的净回归**，不是历史遗留：修之前这些消息两侧都是
# `“”`、匹配得上；修之后提示词变 `「」`、源文仍是 `“”`，反而匹配不上。
# 也就是说 —— `digest.py` 那句「refine 不需要这组：它的引号两侧都是库内原文」
# 自 M2-3 起**已作废**（refine 的提示词现在也过 `sanitize_for_llm` 了）。
# 改任何一边、忘了另一边，就是同一类缺陷被逐字照抄第二次：
# 一边记得、一边不记得，而**没有任何东西会报错**。
#
# ⚠️⚠️ **但别把「为什么它安全」写成「两侧对称剥离，所以不会误归属」——那句话是错的**
# （独立审查从逻辑上推翻、本机复现了反例）。两侧同时剥字符，**仍然**会让
# 「只差被剥字符」的两条消息**互相顶替**：摘录挂到另一个人头上。
# 反例（批序 [msg2 李四「他说“明天”交作业了」, msg1 小王「他说“明天交作业”了」]，
# 模型照抄 msg1 的净化形）：改前 → None（fail-safe 丢来源）、**改后 → msg2（挂错发布人）**；
# 拿 ASCII `"` 那组更干净：**改前正确、改后错**。
#
# 真正让这次改动站得住的是**实测**，不是对称性：
# 全库 47,719 条消息，按群、按 **message-id 簇**比较（多剥字符只会合并旧簇，不会拆）——
# 旧表 5,328 个**歧义簇** / 新表 5,328 个**歧义簇**（全部簇是 24,406 个），
# **由 ≥2 个旧簇合并而来的新增歧义簇 = 0**。
# ⇒ 日后要再扩大这个字符集，**必须重跑同一测量**才可以放行，而且三条都得满足：
#   ① **必须按 message-id 簇比**，不能拿「归一化后的键字符串」直接比——
#      那样会得出假的新增/消失（同一个簇的键串换了个样子而已）。
#      实测两个错误读法给的数：比**歧义簇的键串** → 「1 新增 / 1 消失」，
#      按字面比**全部键** → 「25 新增 / 25 消失」。两个都不作数。
#   ② **必须按群比**：不按群读出来的歧义簇数不一样（实测 5,301 vs 5,328），
#      两个数都对，但口径必须固定，否则两次测量没法对比。
#   ③ ⚠️ **相等不碰撞 ≠ 包含不碰撞**。`_match_source` 走的是**子串包含**，不是相等——
#      本次扩表实测新造出 6 对包含关系（`“水牛学长”` ⊆ `一个水牛学长` 之类，
#      2 条 needle 侧消息），所幸涉及消息全是终态（discarded / ok）、活的 0 条。
#      **放行前必须专门查包含关系**，只查相等会整类漏掉。
_PUNCT = re.compile(r'[\s，。！？、：；「」『』【】（）()\[\]…~～\-—“”"]+')


def _normalize(text: str) -> str:
    return _PUNCT.sub("", text)


def _match_source(quote: str, batch: list[PendingMessage]) -> PendingMessage | None:
    """按【逐字摘录】在批次里定位来源消息。

    ⚠️ 这是 Task 8 冒烟实测逼出来的设计——**不问模型要编号**：

    * **msg_id 不行**：19 位雪花号，同批 30 条只有末 3 位不同（`…673881` /
      `…673929`）。实测命中 0/2，返回的全是「在批内但不对应」的 ID。
    * **批内序号也不行**：实测一个 4 条消息的批次，真来源在第 3 位，
      模型读对了内容却返回 `idx=1`。7B 在「4 条里选 1 个序号」上也不可靠。

    改成让模型逐字摘录、由本地匹配后，**7B 上一次命中**。

    **匹配不上就返回 None，调用方丢弃该条目。** 宁可少一条，也不能把来源挂错
    ——M1 出口标准要求能跳回原文**且内容对得上**，挂错的来源比没有来源更糟。
    """
    needle = _normalize(quote)
    if len(needle) < 4:  # 太短的摘录容易误命中，宁可丢
        return None
    for m in batch:
        if needle in _normalize(m.content):
            return m
    return None


def _to_item(
    raw: dict, batch: list[PendingMessage], *, known_kinds: frozenset[str]
) -> store.ExtractedItem | None:
    """把模型返回的一条 JSON 转成 ExtractedItem。

    任何不合法（摘录匹配不上、类目不在表里、缺 title）都返回 None——
    模型会编，宁可不入库也不入脏数据。
    """
    quote = str(raw.get("quote") or "").strip()
    source = _match_source(quote, batch)
    if source is None:
        return None

    # ⚠️ 依据只许来自**这一条**消息（`src_msg_ids` 恒为 `(source.msg_id,)`）。
    # 拿整个 batch 的正文去拼会重演 `deadline.py` 里 `_GAP` 注释记的那个坑：
    # 跨消息拼接**凭空造出证据**。
    source_ids = frozenset({source.msg_id})

    kind = str(raw.get("kind", "")).strip()
    if kind not in known_kinds:
        return None

    title = str(raw.get("title") or "").strip()
    if not title:
        return None

    links_raw = raw.get("links") or []
    links = tuple(str(x) for x in links_raw) if isinstance(links_raw, list) else ()

    try:
        confidence = float(raw.get("confidence") or 0.0)
    except (TypeError, ValueError):
        confidence = 0.0

    def _text(key: str) -> str | None:
        value = raw.get(key)
        if value is None:
            return None
        text = str(value).strip()
        return text or None

    # ⚠️ 两道要素闸门都在**写入边界**上做，理由与 _verify_deadlines
    # 的 docstring 完全一样：数据一旦入库就到处流，每个读取方各挡一次迟早有人忘。
    sources_text = "\n".join((m.content or "") for m in batch if m.msg_id in source_ids)
    raw_place = _text("place")
    place = raw_place if place_supported(raw_place, sources_text) else None

    deadline_ts = _parse_deadline(raw.get("deadline"))
    # ⚠️ `start` 走**和 `deadline` 同一道机器闸门**，但用**它自己的**判据
    # （`start_supported` 认时刻、`deadline_supported` 认日期）。
    #
    # 不加这道闸的后果比日期那次更重：时刻驱动的是**按分钟排期**，
    # 一个编出来的「08:00」会让用户提前两小时去集合，而界面上它看起来
    # 和真的一模一样。判据与处置都照抄 deadline：**找不到依据 ⇒ NULL**
    # （条目照常显示，只是排不进日程表）。
    raw_start = raw.get("start")
    start_ts = _parse_start(raw_start)
    if start_ts is not None and not start_supported(start_ts, sources_text):
        start_ts = None
    # ⚠️ **时序闸门（`deadline_sane`）不在这里**，见 `_verify_deadlines`。
    # 挪走的理由不是洁癖：闸门留在 `_to_item` 里时，它清掉的值**没有任何
    # 计数器**——`stats.deadlines_dropped` 只数 `deadline_supported` 那道闸。
    # 后果是「一批 12 条过期死线被全清」在运维侧**零告警**（M5 遗留 #4）。
    # 两道闸现在并排站在写入边界上，谁都能一眼看全。

    return store.ExtractedItem(
        kind=kind,
        title=title,
        detail=_text("detail"),
        event_ts=source.ts,
        deadline_ts=deadline_ts,
        group_id=source.group_id,
        actor_uid=source.sender_uid or None,
        place=place,
        links=links,
        amount=_text("amount"),
        confidence=max(0.0, min(1.0, confidence)),
        src_msg_ids=(source.msg_id,),
        start_ts=start_ts,
    )


def _verify_deadlines(
    produced: list[store.ExtractedItem], batch: list[store.PendingMessage]
) -> tuple[list[store.ExtractedItem], int, int]:
    """写入前核验：两道闸，各自记数。

    ⚠️ 核验放在**写入边界**而不是渲染层：数据一旦入库就到处流，每个读取方
    各挡一次，迟早有人忘——M2 就只在日报挡过，M3 的 Web 差一点把那 13 条
    幻觉日期原样复活（「今天下午16点之前」被填成 9-14、「明早7:20」被填成 9-15）。

    ⚠️ 只降级 `deadline_ts` 这一个字段：条目的标题、正文、来源一概不动。
    用户仍然看得到这条信息，只是不再被告知一个编出来的日期。

    **两道闸的区别**（M6 T10 把它们并排放在这里）：
      * `deadline_supported` —— 源文里**找不到字面依据**（模型编的）
      * `deadline_sane`      —— 字面有依据，但**死线早于消息时间**（已过期三个月的「⏰ 别忘」）

    ⚠️ 分开计数是**承重的**：两者的运维含义完全不同——前者说明模型在编，
    后者说明**提示词或数据源有问题**（为什么会有三个月前的死线被当成新的？）。

    **返回值**：`(核验后的条目, 无依据的条数, 时序倒挂的条数)`。
    """
    texts = {m.msg_id: (m.content or "") for m in batch}
    ts_by_msg = {m.msg_id: m.ts for m in batch}
    out: list[store.ExtractedItem] = []
    unsupported = 0
    backdated = 0
    for it in produced:
        if it.deadline_ts and not deadline_supported(
            it.deadline_ts, "\n".join(texts.get(mid, "") for mid in it.src_msg_ids)
        ):
            unsupported += 1
            it = replace(it, deadline_ts=None)
        elif it.deadline_ts:
            # ⚠️ `elif` 而不是第二个 `if`：一条已经被判「无依据」的截止日
            # **不该再计一次**时序倒挂——那会让两个数加起来大于被清的总数，
            # 而运维看到「2 条被清」+「2 条被清」却只有 2 条条目时会怀疑账目。
            # ⚠️ 源消息的 ts 取**最早**的那条（一条 item 可以有多条源消息）：
            # 死线必须晚于**它出现的最早时刻**才算合理，取最晚的会放过一批。
            srcs = [ts_by_msg[mid] for mid in it.src_msg_ids if mid in ts_by_msg]
            # ⚠️ **`srcs` 为空 ⇒ 这一支"放弃核验"（fail-open），而它今天不可达**
            # （M6 终审 #26）。不可达的理由是**实测的**、不是"看着不可能"：
            # 源消息不在本批里 ⇒ 上面第一条闸门的 `texts.get(mid, "")` 拿到空串
            # ⇒ `deadline_supported` 判**假** ⇒ 这条 `elif` **根本走不到**
            # ——那种条目进的是 `unsupported` 计数。
            # `test_an_item_whose_source_is_not_in_the_batch_is_caught_by_the_first_gate`
            # 钉着这条推理（删掉那条用例的理由就只剩一句散文了）。
            # ⇒ 留着 `if srcs` 是**防御性**的；代价如实记：真到了这里，时序核验
            # 会被**静默**放弃（不计数、不打日志）。要把它变成信号得加一个新计数，
            # 而那个计数在"不可达"这件事上恒为 0——**今天加了也看不见**
            # （这是"注释点明"而不是"加计数"的唯一理由，不是省事）。
            if srcs and not deadline_sane(it.deadline_ts, min(srcs)):
                backdated += 1
                it = replace(it, deadline_ts=None)
        out.append(it)
    return out, unsupported, backdated


def refine(
    config: Config,
    *,
    api_key: str,
    db_path: Path | None = None,
    conn: sqlite3.Connection | None = None,
    since: int | None = None,
    until: int | None = None,
    limit: int | None = None,
    redo: bool = False,
    batch_size: int = 30,
    context: int = 2,
    budget_tokens: int | None = None,
    model: str = DEFAULT_MODEL,
    base_url: str = DEFAULT_BASE,
    enable_thinking: bool | None = False,
    prompt_ver: str = PROMPT_VERSION,
    dry_run: bool = False,
    on_progress=print,
) -> RefineStats:
    """跑一轮抽取。幂等——已处理的消息会被跳过，除非 redo=True。

    连接由调用方持有：CLI 传 db_path，测试传内存 conn。
    """
    from .categories import load_categories

    cats = load_categories()
    known_kinds = frozenset(c.slug for c in cats)
    stats = RefineStats()

    # redo + limit 叠加是有害组合（W1 Task 4 审查实测）：
    # redo 把已抽过的消息重新抽一遍，limit 又只取最早的 N 条——
    # 结果是重复产出 items、白烧 token。快速失败挡住它，别让它静默发生。
    if redo and limit is not None:
        raise ValueError(
            "redo 与 limit 不能同时用：redo 会重抽已处理的消息，"
            "limit 又只取最早的 N 条，两者叠加会重复产出 items 并白烧 token。"
            "要限量重抽，请用 since/until 圈定时间窗。"
        )

    owns_conn = conn is None
    if conn is None:
        # ⚠️ **两个都给 None 是调用方的错，必须响亮失败。**
        #
        # 不加这一条的话，`str(None)` 是**字面量 `"None"`**，于是一个拼错的
        # 调用会在**当前工作目录**静默建出一个叫 `None` 的库文件——满是表结构、
        # 一行数据都没有，而调用方以为自己在往真库里写。
        # 实测发生过（2026-10-15，`content/` 根目录留下一份 73KB 的空库）：
        # ⚠️ 而且它**不在 `.gitignore` 的保护范围内**——`*.db` 匹配不到一个
        # 叫 `None` 的文件，所以它会随开源仓库发出去。
        if db_path is None:
            raise ValueError(
                "refine() 必须给 db_path 或 conn 之一："
                "两个都是 None 时，`str(None)` 会在当前目录建出一个"
                "名为 None 的空库文件（而不是报错）。"
            )
        # ⚠️ `uri=True` 是 Task 5 加的（裁决 R4）：本连接**既读又写**，其中读的那部分
        # 会走 `store` 的查询层（入口 ATTACH overlay，URI 形式需要 `SQLITE_OPEN_URI`）。
        # 不改用 `PRAGMA query_only` 兜底——那是**连接级**的，会把这里的写路径冻住。
        conn = sqlite3.connect(str(db_path), uri=True)
    try:
        store.ensure_schema(conn)

        messages = store.pending_messages(
            conn, since=since, until=until, limit=limit, redo=redo
        )

        # D15：被软删条目的源消息**永不被重抽复活**。
        #
        # ⚠️ **非 redo 与 redo 都会经过这道闸门**——过滤是**无条件**的，
        # 不要把它挪进 `redo` 分支。机理（实测，见 task-6-report.md「勘误」）：
        # `refine_runs` 按 msg_id 做 `INSERT OR REPLACE`（`store.py:379-384`），
        # 而 `_record_error` 会把一次成功的 ok 记账**覆写成 error**；`pending_messages`
        # 的既有语义是「error 行要重试、不算已处理」⇒ 一次「抽过 → 用户软删 →
        # 后来某轮 `--redo` 失败」之后，**下一轮非 redo 的 refine 照样会把这条消息
        # 取出来重抽**，被删的条目以**新的 item_id** 复活（overlay 挡的是**旧**
        # item_id）⇒ D15 被静默重开。`test_refine_skips_tombstoned_even_without_redo`
        # 钉着这条：把它收窄成 `if redo and tombstones:` 那条测试立刻变红。
        #
        # ⚠️ **刻意不碰 `refine_runs`**：那些行的 ok 记录是**既有事实**，
        # 把它改写成 discarded 会篡改 M1 出口标准赖以成立的账目。
        tombstones = _tombstoned_msg_ids()
        if tombstones:
            kept = [m for m in messages if m.msg_id not in tombstones]
            stats.skipped_deleted = len(messages) - len(kept)
            messages = kept

        stats.scanned = len(messages)
        # ⚠️ **必须排在下面那个早退之前**（M5 遗留 F1）：排在后面的话，
        # 「一整窗消息全被墓碑」会走到 `if not messages: return`，
        # 于是那行日志**永远不会执行**——而它本该说出的正是这件事。
        # 用户看到的是「[refine] 没有待处理的消息」，与「这个窗口本来就空」
        # **一字不差**：一句无法区分两种事实的日志，等于没有日志。
        if stats.skipped_deleted:
            # ⚠️ **句首的总数必须自己带上**（M6 终审 #23）：下面那行
            # 「扫描 N 条 → …」在**早退之后**，而这两行是**互斥**的（整窗被墓碑时
            # 走的是 `if not messages: return`，那行永远不打印）⇒ 只写"其中"
            # 就是指向一个**还没打印、也可能永远不打印**的数。
            # ⚠️ 但**不许**为此去改 `stats.scanned` 本身：它的语义是"过滤之后
            # 真正要处理多少条"（CLI 拿 `scanned - sent_messages` 报"本地筛掉"），
            # 把墓碑算进去会让那句话把"被墓碑挡下的"错报成"本地筛掉的"
            # （`test_refine_skipped_deleted_counts_only_what_it_actually_skipped`
            # 钉着 `stats.scanned == 2` / `== 0` 这两个值）。
            # ⇒ 只在这一行里现算总数。
            on_progress(
                f"[refine] 扫描 {stats.scanned + stats.skipped_deleted:,} 条，"
                f"其中 {stats.skipped_deleted:,} 条已软删，跳过重抽（D15 墓碑）"
            )

        if not messages:
            on_progress("[refine] 没有待处理的消息")
            return stats

        candidates, screen_stats = prefilter.screen(
            messages, tier_of=config.tier_of
        )
        stats.candidates = screen_stats.kept
        stats.discarded_local = screen_stats.dropped
        # ⚠️ `expand_context` **提前到这里**：进度行要报「送模型」就必须先算出来。
        # 它是纯函数、不碰 I/O，提前没有副作用；下面那个 `if not candidates`
        # 分支在空候选时 expand_context 返回空表，行为一字不变。
        in_scope = prefilter.expand_context(messages, candidates, context=context)
        stats.sent_messages = len(in_scope)

        # ⚠️ 这三个数**自洽**：本地筛掉 + 送模型 == 扫描。这是 M2-4 的修法。
        # 旧版写「扫描 N 条 → 规则保留 M 条（本地丢弃 D 条）」——43,439 条
        # 消息同时不属于「保留」也不属于「丢弃」（`prefilter.py:177-179` 的
        # 「软丢弃」分支），三个数加起来对不上，而 D 又与日报里的「本地筛掉」
        # 差 7.6 倍。**不要**改回「其中」：规则硬丢弃与本地筛掉**不是**包含
        # 关系（`expand_context` 不看消息有没有被硬规则判死，被判死的照样
        # 作为上下文出网）。
        on_progress(
            f"[refine] 扫描 {stats.scanned:,} 条 → 本地筛掉 "
            f"{stats.scanned - stats.sent_messages:,} 条"
            f" → 送模型 {stats.sent_messages:,} 条（候选 {stats.candidates:,} 条）"
        )

        # 没进候选的一律记账为 discarded，其中既含硬丢弃也含「没命中保留规则」。
        # 记账必须做全：refine_runs 覆盖不到的消息会让增量同步永远重扫它们。
        # 集合先建好——47k 条消息下逐条重建集合是 O(n²)。
        candidate_ids = {c.msg_id for c in candidates}
        not_candidate_ids = [m.msg_id for m in messages if m.msg_id not in candidate_ids]
        if not dry_run and not_candidate_ids:
            try:
                with store.transaction(conn):
                    store.record_run(
                        conn, not_candidate_ids, status=store.STATUS_DISCARDED,
                        prompt_ver=prompt_ver, commit=False,
                    )
            except Exception as exc:  # noqa: BLE001
                # ⚠️ 偏差（逃逸舱，见报告「偏差」第 2 条）：brief 的 Step 7(g) 只把
                # 这里**圈进事务**、没给保护圈——而它在**主流程**上（并不在任何
                # `except` 体里），记账一失败就会穿过整个 refine 掀掉整轮：
                # `stats.batches` 停在 0、一条消息都不抽。brief 自己的 Step 5 用例
                # （`test_record_error_failure_does_not_kill_the_run`）要的正是
                # 「记账全炸也不许掀掉整轮」——两条要求只有补上这个保护圈才同时
                # 成立，所以按同一取舍补上：**返回**失败而不是抛出，账目照旧可见。
                # 这批消息会保持未记账，下次 refine 重新本地预筛（不烧 token、
                # 也**不会**重复产出 items——它们本来就没进候选），比整轮不跑好。
                stats.errors.append(f"未进候选的消息记账失败: {exc}")

        if not candidates:
            on_progress("[refine] 预筛后没有候选，结束")
            return stats

        batches = prefilter.make_batches(in_scope, max_batch=batch_size)
        stats.batches_planned = len(batches)

        if dry_run:
            on_progress(
                f"[refine] --dry-run：将发送 {len(in_scope):,} 条、分 {len(batches)} 批，"
                f"不调用模型、不写库"
            )
            return stats

        system = build_system_prompt(cats)
        today = dt.date.today().isoformat()
        llm_cfg = LLMConfig(
            api_key=api_key, model=model, base_url=base_url,
            enable_thinking=enable_thinking,
        )

        for index, batch in enumerate(batches, start=1):
            if budget_tokens is not None and stats.total_tokens >= budget_tokens:
                stats.budget_hit = True
                on_progress(
                    f"[refine] 已达预算上限 {budget_tokens:,} token，提前停止"
                    f"（已完成 {index - 1}/{len(batches)} 批）"
                )
                break

            stats.batches += 1  # 实际跑了——放在预算闸之后，break 的那批不算跑过
            # ⚠️ 把**本批出现的昵称**喂给 Redactor，用于正文里的姓名遮蔽。
            #
            # 名单只能来自本批：`Redactor` 是 per-batch 新建的（代号在批内稳定、
            # 跨批重编），而人名表若跨批共享就等于把别的批的人名也抹掉 ——
            # 那会把"某人在别的群里说的话"误判成本批的。
            #
            # 用昵称而不是 uid：`_NAME_EXPR` 那条 SQL 已经把 uid 解析成了
            # 优选昵称（群昵称 > QQ 昵称 > 号），而**正文里出现的是昵称**。
            redactor = Redactor(
                n for m in batch if m.sender for n in Redactor.person_names(m.sender)
            )
            user = build_user_prompt(batch, redactor, today=today)
            batch_ids = [m.msg_id for m in batch]

            try:
                result = chat_json(llm_cfg, system=system, user=user)
            except LLMError as exc:
                msg = f"第 {index} 批失败: {exc}"
                stats.errors.append(msg)
                on_progress(f"[refine] {msg}")
                if extra := _record_error(
                    conn, batch_ids, prompt_ver=prompt_ver, err=exc
                ):
                    stats.errors.append(f"第 {index} 批 {extra}")
                continue

            # ⚠️ token 累加**留在保护圈外**：token 是真花掉的，本地后处理炸了
            # 不代表模型没被调用——圈进去会让账目说谎（成功路径行为不变）。
            stats.input_tokens += result.input_tokens
            stats.output_tokens += result.output_tokens

            # ⚠️ 保护圈**从这一行起，到本批结束**：解析、核验、落库、记账、
            # 报进度。设计里那句「单批失败不中断整轮」以前只对**网络**失败
            # 成立——这里的每一步都在上一版 try/except 的圈外，于是一条 item
            # 带个 ``deadline: "1970-01-01"`` 就能让整轮 refine 抛异常终止：
            # 前面跑完的批次白跑，剩下的批次永远不跑。
            # 这是 M1 留下的保护圈缺口，M3 只是又往缺口里塞了一个新调用点
            # （核验接线），所以修法是**把整段本地后处理挪进圈里**，
            # 而不是逐个去堵已知的异常类型。
            try:
                raw_items = result.payload.get("items")
                if not isinstance(raw_items, list):
                    raw_items = []

                produced: list[store.ExtractedItem] = []
                hit_ids: set[int] = set()
                for raw in raw_items:
                    if not isinstance(raw, dict):
                        continue
                    item = _to_item(raw, batch, known_kinds=known_kinds)
                    if item is None:
                        continue
                    produced.append(item)
                    hit_ids.update(item.src_msg_ids)

                # 截止日降级是**纯本地**判定，放事务外——事务里只留 DB 写。
                dropped = 0
                backdated = 0
                if produced:
                    produced, dropped, backdated = _verify_deadlines(produced, batch)

                # ⚠️ 落库 + 记账必须在**同一个事务**里（见 store.transaction 的
                # docstring）：分开提交时，两步之间崩掉会让同一批消息下次被
                # 重抽，产出重复 items 且无声。`commit=False` 是关键——漏一个
                # 就白搭，而且**不会报错**，只会静默地退回旧行为。
                with store.transaction(conn):
                    saved = 0
                    if produced:
                        saved = store.save_items(
                            conn, produced, model=model, prompt_ver=prompt_ver,
                            commit=False,
                        )
                    store.record_run(
                        conn, batch_ids, status=store.STATUS_DISCARDED,
                        prompt_ver=prompt_ver, commit=False,
                    )
                    if hit_ids:
                        store.record_run(
                            conn, sorted(hit_ids), status=store.STATUS_OK,
                            prompt_ver=prompt_ver, item_count=saved,
                            commit=False,
                        )

                # ⚠️ 只有事务真的提交了才动 stats——回滚了还记账，账目会说谎
                stats.deadlines_dropped += dropped
                stats.deadlines_backdated += backdated
                stats.items_saved += saved
            except Exception as exc:
                # 捕 `Exception` 而不是某个具体类型：设计意图是「单批失败不中断
                # 整轮」，捕窄一类，下一个未知异常类型会**再次**掀掉整轮——
                # 那正是这次在修的模式（同一类缺陷在本项目已是第三次出现）。
                # 捕宽的前提是**可见**：异常照样进 stats.errors、照样
                # on_progress 出来、照样记进 refine_runs；静默吞异常不可接受。
                msg = f"第 {index} 批本地后处理失败: {exc}"
                stats.errors.append(msg)
                on_progress(f"[refine] {msg}")
                if extra := _record_error(
                    conn, batch_ids, prompt_ver=prompt_ver, err=exc
                ):
                    stats.errors.append(f"第 {index} 批 {extra}")
                continue

            # ⚠️ 成功话术**必须放在 try/except 之外**（计划 §8.5）：
            # 它炸了要**冒出去**，不许被上面那个 `except Exception` 接住——
            # 接住的话 `_record_error` 会把**刚提交成功**的这批消息
            # `INSERT OR REPLACE` 改记成 `error` ⇒ 下轮 `pending_messages`
            # 重抽它们 ⇒ **重复 items 且无声**。这正是 M4 要消灭的形状，
            # 而它只在「事务已提交、报进度失败」这个夹缝里触发：
            # 提交**之前**炸会被 rollback（安全），提交**之后**炸才毒化账目。
            # 541/542 是纯内存累加、不会抛，所以移走这一句就关掉了整条链。
            # `produced` 在这里一定已绑定：try 里它先于任何可能抛的语句赋值，
            # 而 try 失败会 `continue`、根本到不了这行。
            on_progress(
                f"[refine] 批次 {index}/{len(batches)}：{len(batch)} 条 → "
                # ⚠️ 用 `saved`（`save_items` 的**返回值**，去重后的落库条数）
                # 而不是 `len(produced)`（去重**前**）。`store._dedupe_batch` 会把
                # 同 (title, event_ts, group_id) 的条目并成一条，所以两个数**会**
                # 不同——而报去重前的数就是报了一个没落库的条数（M5 遗留 #7）。
                f"{saved} 条 item"
                + (f"（另有 {len(produced) - saved} 条与批次内重复，已合并）"
                   if len(produced) > saved else "")
            )

        return stats
    finally:
        if owns_conn and conn is not None:
            conn.close()
