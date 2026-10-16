"""日报合成：把某个时间窗内的 items 写成给同学看的一页 Markdown。

三条设计判断，每条都有实测或 spec 支撑：

1. **输入是 items，不是原始消息**（spec §2.4）。筛选在 M1 已经做完，
   日报**不再筛第二遍**——窗口内每条 item 都必须出现在日报里，
   否则就是又一个静默过滤器，而「静默漏掉」正是这个工具最不能犯的错。
2. **不问模型要 item_id**。M1 实测：19 位雪花号命中 0/2、批内序号在真实
   管线里也错。改成让模型**逐字摘录 title**，由 ``match_lines`` 本地定位。
3. **模型只负责措辞与合并，版式由程序确定性渲染**（与 spec §4.6 的偏差 1）。
   自由发挥的 Markdown 无法可靠回连 ``digest_items``，而 spec §2.3 要求
   「日报里说的每一条，必然能在 Web 里找到」。

模型输出契约（``{"lines": [{"quotes": [...], "label": "...", "text": "..."}]}``）
的措辞经真实数据探针验证：9/13 的 10 条并成 8 行、9/8 的 13 条并成 7 行，
两天都是 100% 覆盖、0 处 quote 未命中。
"""

from __future__ import annotations

import datetime as dt
import json
import re
import sqlite3
from dataclasses import dataclass, field
from pathlib import Path

from . import deadline as deadline_mod
from . import prefilter, store
from .categories import load_categories
from .config import Config, REPO_ROOT
from .llm import (
    DEFAULT_BASE,
    DEFAULT_MODEL,
    LLMConfig,
    LLMError,
    chat_json,
    sanitize_for_llm,
)
from .redact import Redactor

# 日报有自己的提示词版本，与 refine.PROMPT_VERSION 各记各的。
#
# ⚠️ 升版范围是「**发往模型的任何文本**」，不只是 build_system_prompt：
# 模型看到的输入 = `build_system_prompt()`（system）**加上**
# `build_user_prompt()` 包裹的 `build_items_payload()`（user）。
# 这三处任何一处的**字段集或措辞**变了，产出就与前版不可比，而
# `digests.prompt_ver` 正是 M3 判断「哪些日报该重跑」的依据——漏升一次，
# 两版产出会被当成一版。**改 payload 字段的人也在范围内。**
PROMPT_VERSION = "v1"

# 系统提示词指纹：`(版本号, sha256(build_system_prompt())[:12])`。
#
# 改成指纹而不是「7 条规则各一个短语」式断言，是因为那种写法**开火方向反了**：
# 它对措辞微调过敏（规则 5「说清」→「讲清」也红），却抓不住真正改行为的
# 三类编辑（前言、末尾的 JSON 形状行、增删规则）。而且它制造反向激励——
# 改措辞必红 → 人养成「顺手改测试里的字符串」的肌肉记忆 → 同一个动作
# 正好绕开 PROMPT_VERSION。
#
# 指纹把两件事绑在一次编辑里：**测试变红时，提示词与版本号两件都要动**。
# ⚠️ 元组里的版本号与 PROMPT_VERSION 是**刻意重复**的，不是笔误：
# 若写成 `(PROMPT_VERSION, sha)`，改版本号时两边一起变、断言恒真，
# 这个守卫就废了（这正是它替换掉的那条断言的老毛病）。
#
# 已知覆盖边界（别把这条当万能）：它只覆盖 `build_system_prompt` 的正文。
# `build_user_prompt` / `build_items_payload` 的改动**没有自动守卫**，
# 只有上面那段注释提醒——改那里时靠人记住升版本。
_PROMPT_FINGERPRINT = ("v1", "a8179068830a")

# 低于这个置信度的行不混进正文，单列到末尾的「🤔 拿不准的」区块。
# 实测全库只有 1 条落在这个区间（M1 误抽的「咨询：四六级报名时间」，0.10），
# 所以它是安全阀而非常态。
LOW_CONFIDENCE = 0.5

# 输出上限。正常一天 20 条 item 的日报约 550 输出 token，2000 有充足余量；
# 但要小到能在模型退化时及时掐断（见 vigil/llm.py 的退化说明）。
MAX_TOKENS = 2000

# 上下文窗口宽度。**必须与 `vigil/refine.py` 的 `context: int = 2` 一致**：
# 空窗日的筛除分布要复现「当时到底发出去了多少条」，而发出去的 = 候选 ±
# 这个宽度的邻居（见 screening_breakdown 的口径边界②）。
REFINE_CONTEXT = 2


def build_system_prompt() -> str:
    """系统提示词。

    ⚠️ 第 6 条（不要写群名）是实测逼出来的：不写这条时模型会把群名当主语
    写进 text（「XX二手交易群发布兼职招聘」），而群名是程序在行尾另外
    追加的，读起来就重复了。但群名**又必须发**——它是模型判断「哪些条目
    属于同一件事」的信号，去掉后 9/13 的合并从 8 行退化回 10 行。
    所以：**发群名，但明令禁止写进输出。**

    ⚠️ 第 2 条（合并）要写在 rules 前部：合并是 spec §4.6 明确要求的
    「合并同类项」，漏掉它日报就退化成 items 的流水账。
    """
    return """你是校园信息日报的编辑。把当天从各个 QQ 群里提炼出的条目，写成一份给同学看的一页日报。

读者的诉求是：**一眼看完，不漏事**。

规则：

1. **每一条 item 都必须出现在日报里**——不能因为"这条不重要"就省略。
2. **讲同一件事的多条 item 合并成一行**：同一个活动、同一个部门、同一件事的
   多条动态，合成一行讲清楚。合并时 quotes 要把涉及的每一条都列上。
3. quotes：逐字摘录你引用的那几条 item 的 title 原文片段。程序拿它回连条目，
   **匹配不上的行会被丢弃**，所以必须逐字照抄，不要改写。
4. label：**不超过 12 个字**的短标签，说明这一行讲的是什么。它会加粗显示在行首，
   要能一眼扫到，**不要写成完整句子**。
5. text：一句话说清细节，让人不看原文就知道该怎么办；没有额外信息时给空串。
6. 输入里的 group 字段只用来帮你判断哪些条目属于同一件事，
   **不要把它写进 label 或 text**——程序会在行尾自动标注来源群。
7. **不要编造 item 里没有的信息**——时间、地点、部门、人名，宁可不写也不能补。

输出必须是 JSON 对象，形如 {"lines": [{"quotes": ["..."], "label": "...", "text": "..."}]}。"""


def build_items_payload(
    items: list[store.WindowItem],
    names: dict[int, str],
    redactor: Redactor,
    *,
    trusted: set[int] | None = None,
) -> list[dict]:
    """把窗口内的 items 整理成发给模型的 JSON。

    ⚠️ 四条硬规则，每条都有实测支撑，少一条都出过事：

    * **只发群名，不发群号**（spec §4.5）。规划期间的探针第一版就是把
      ``group_id`` 明文发了出去——全库 15 个群号本来就不该出网。
    * **出网的每个文本字段都过同一套 ``clean()``，不留例外**——``title`` /
      ``detail`` / ``place`` / ``amount`` / **``group``** 一视同仁。两层理由：
      ① item 是模型从已脱敏文本里抽的，但它会「补出原文没有的实体」，
      所以出网前再抹一遍号码；② 群名来自人工维护的 ``config/groups.toml``，
      出号码的概率极低，但**例外需要人记住，而「记住」正是最容易失效的
      东西**（``group`` 原先只过 ``sanitize_for_llm``，就是这种例外）。
      而 ``Redactor.IDENTITY_NUM`` 的 ``群`` 分支天生就认识
      ``XX交流群123456789`` 这种写法。群名只作**合并信号**用、不直接展示，
      抹了也无害。
    * **过 sanitize_for_llm**：全角引号会让模型退化成无限空格循环。
    * **没通过核验的 ``deadline`` 不发**（``trusted``，Task 6 修复轮次 2）。
      ``trusted`` 是**截止日在源文里有字面依据**的 item_id 集合；不在集合里的
      item，``deadline`` 一律给 ``None``（键照留，字段形状不变）。
      为什么必须卡在这里：Task 6 只挡住了**程序打的戳**（``· 截止 09-15 ·``），
      ``deadline`` 字段照旧出网，于是模型**自己在正文里把它写了出来**——实测
      9/12 的 item 250 已被正确降级、程序戳确实没加，正文里却仍有
      「截止 9 月 15 日」，而「9月15日」在那条源文里毫无依据（源文只说
      「明天下午5点到6点」）。读者看到的还是同一个日期：**机械核验只堵了
      程序那个入口，模型措辞那个入口是敞着的。模型拿不到它，才写不出来。**

    ⚠️ ``trusted`` 默认必须是 ``None`` → 空集 → **全部截止日都不发**。
    默认成全信任的话，任何忘记传参的调用方都会静默退回危险行为
    （与 ``build_rows`` 的 ``trusted=None`` 同款理由）。
    """

    def clean(value: str | None) -> str | None:
        if not value:
            return None
        return sanitize_for_llm(redactor.text(value)) or None

    trusted = set() if trusted is None else trusted
    payload = []
    for item in items:
        payload.append(
            {
                "title": clean(item.title) or "",
                "detail": clean(item.detail),
                "kind": item.kind,
                "time": dt.datetime.fromtimestamp(item.event_ts).strftime("%H:%M"),
                "place": clean(item.place),
                "amount": clean(item.amount),
                "deadline": (
                    dt.datetime.fromtimestamp(item.deadline_ts).strftime("%Y-%m-%d")
                    if item.deadline_ts and item.item_id in trusted
                    else None
                ),
                "group": clean(names.get(item.group_id)) or "",
            }
        )
    return payload


def build_user_prompt(payload: list[dict], *, day: str) -> str:
    """用户提示词。载荷已经是脱敏过的，这里只负责拼装与注入日期。

    注入「今天是哪天」是必须的——M1 实测：不告诉模型今天，它会把
    「9月7号」猜成过去的年份。
    """
    body = json.dumps({"items": payload}, ensure_ascii=False, indent=1)
    return f"今天的日期是 {day}。\n\n以下是 {day} 这一天提炼出的条目：\n{body}"


# 归一化时剥掉的字符：空白与常见中英文标点。
# 目的是容忍「（640）」vs「(640)」这类**格式**差异，不是容忍改写。
#
# ⚠️ 取值比 refine.py 的 `_PUNCT` **多一组引号**（`“”` 与 `"`），这是刻意的：
# 本模块的匹配是拿「**出网后的**引号」去对「**库内原文的**引号」。
# `build_items_payload` 出网前用 `sanitize_for_llm` 把 `“”` 换成 `「」`，
# 而这里的 blob 是 `items` 表里的原文（仍是 `“”`）；模型照规则 3 逐字回抄
# 它**看到的** `「」`。只剥 `「」` 不剥 `“”` 的话两侧对不齐——引号夹在
# 中间时（如 title「“某某网盘”卖笔记」）整行匹配不上、被丢弃，退化成
# 机械补行，而 CLI 退出码仍是 0（静默）。反过来（库里是 `「」`、模型回抄成
# `“”`）同样靠这一条兜住。refine 不需要这组：它的引号两侧都是库内原文，
# 没有「出网时换过一次字符」这层。
_PUNCT = re.compile(r'[\s，。！？、：；「」『』【】（）()\[\]…~～\-—“”"]+')


def _norm(text: str) -> str:
    """归一化。**只动标点与空白，不动汉字与数字。**

    ⚠️ 为什么必须归一化而不是直接 `in`：实测模型会在数字两侧插空格
    （原文「某某网盘310」它写成「某某网盘 310」）。直接子串匹配会漏掉这种
    纯格式差异，而归一化后两侧都无空格、仍逐字对应。
    """
    return _PUNCT.sub("", text)


# 截止日的字面依据核验已提到 `vigil/deadline.py`——数据层（refine 写入、
# deadline-audit 回填）与渲染层（日报「别忘」）共用同一套判据。
# ⚠️ 保留 import 而不是各复制一份：两处判据一旦漂移，就会出现
# 「日报说这条截止日没依据、Web 里却明晃晃显示着」这种自相矛盾。
from .deadline import deadline_supported


def verified_deadlines(
    items: list[store.WindowItem], sources: dict[int, str]
) -> set[int]:
    """哪些 item 的截止日在源文里有字面依据。

    没有来源行的 item（``sources`` 里查不到）一律按**无依据**处理。
    """
    return {
        it.item_id
        for it in items
        if deadline_supported(it.deadline_ts, sources.get(it.item_id, ""))
    }


@dataclass(frozen=True)
class DigestLine:
    """模型返回的一行，已匹配回具体的 items。"""

    label: str
    text: str
    item_ids: tuple[int, ...]


@dataclass(frozen=True)
class Row:
    """日报里最终要渲染的一行。渲染只认它，不认模型返回的原始结构。"""

    label: str
    text: str
    kind: str
    group_id: int
    deadline_ts: int | None
    low_confidence: bool
    mechanical: bool = False
    # 截止日在**源文里**有没有字面依据（见 ``verified_deadlines``）。
    # 只有 True 的行才进「⏰ 别忘」、才被打「截止 MM-DD」戳。
    # 默认 False 是刻意的：失败方向要选安全的那边（宁可漏催，不可乱催）。
    deadline_trusted: bool = False


def match_lines(
    raw_lines: object, items: list[store.WindowItem]
) -> tuple[list[DigestLine], list[str]]:
    """把模型返回的行匹配回 items，返回 ``(命中行, 未命中的 quotes)``。

    **匹配不上就丢掉那一行，由 build_rows 用机械补行兜住**——宁可读起来
    生硬，也不能把条目静默丢了。未命中的 quotes 会返回给调用方计数上报。

    一个 quote 可能命中**多条** item（实测全库有重复标题，如「校园卡办理」
    系列 5 条）——全都要算覆盖，否则那些条目会被误判成「模型漏了」而
    补出重复的机械行。
    """
    if not isinstance(raw_lines, list):
        return [], []

    normed = [
        (it, _norm(it.title), _norm(f"{it.title} {it.detail or ''}")) for it in items
    ]
    lines: list[DigestLine] = []
    unmatched: list[str] = []

    for raw in raw_lines:
        if not isinstance(raw, dict):
            continue
        quotes = raw.get("quotes")
        if not isinstance(quotes, list):
            continue
        hits: list[store.WindowItem] = []
        for quote in quotes:
            raw_quote = str(quote).strip()
            needle = _norm(raw_quote)
            if not needle:
                continue
            # ⚠️ 先按「整个 title 逐字相等」找。**这一步不能省**：title 本身
            # 可能只有两三个字（「讲座」「体检表」），下面那条子串规则有长度
            # 下限，短标题只能靠这条路命中。
            #
            # 这是规划期实测抓到的缺陷：初版把 refine._match_source 的
            # 「摘录短于 4 字就丢」原样照搬了过来，结果 8 条断言全红
            # ——短标题一条都匹配不上，日报会退化成全机械补行。**那条例外的
            # 适用前提不同**：refine 的摘录是 10-30 字的原文片段，这里的摘录
            # 是整个 title。
            found = [it for it, title, _ in normed if title == needle]
            if not found and len(needle) >= 4:
                # 再按子串找。**下限 4 个字**：摘录太短容易误命中别的条目。
                # ⚠️ 多认领**不只**是「归到某一行、不会丢」这么轻。
                # 规划期审查实测：锚点取的是 `picked[0]`，所以子串多认领一个
                # event_ts 更早的无关条目，会把整行的**分区与署名群**换掉
                # ——实测能把 activity/群200 的事渲染成「· 群100」并归进通知公告。
                # 条目确实没丢，但署错了群、归错了分区。
                # （「锚点优先取整 title 精确命中」的改进已记为延迟项。）
                # 下限取 4 是保守取值，不是硬约束。
                found = [it for it, _, blob in normed if needle in blob]
            if not found:
                unmatched.append(raw_quote)
            for it in found:
                if it not in hits:
                    hits.append(it)
        if not hits:
            continue
        hits.sort(key=lambda it: (it.event_ts, it.item_id))
        label = str(raw.get("label") or "").strip() or hits[0].title
        text = str(raw.get("text") or "").strip()
        lines.append(
            DigestLine(
                label=label,
                text=text,
                item_ids=tuple(it.item_id for it in hits),
            )
        )
    return lines, unmatched


def build_rows(
    lines: list[DigestLine],
    items: list[store.WindowItem],
    *,
    low_confidence: float = LOW_CONFIDENCE,
    trusted: set[int] | None = None,
) -> list[Row]:
    """命中行 + 机械补行 → 最终的渲染行清单。

    **没被任何一行引用的 item 会被机械补一行**（label 用 title、text 用 detail）。
    日报的价值在「不漏事」，所以宁可读起来生硬，也不能因为模型漏写就丢条目
    ——那正是 spec §2.3 要避免的「日报有、列表没有」的反面：列表有、日报没有。

    ``trusted`` 是**截止日在源文里有字面依据**的 item_id 集合（见
    ``verified_deadlines``）。合并行只要有一条的截止日没依据，整行就不算
    可信——行里的日期是 ``min()`` 出来的一个数，说不清它是哪条的依据。

    ⚠️ 默认必须是 ``None`` → 空集 → **全部截止日都不进「别忘」**。
    默认成全信任的话，任何忘记传参的调用方都会静默退回危险行为：
    把模型编出来的日期用机器口吻打出来催人办事。**失败方向选安全的那边。**
    """
    trusted = set() if trusted is None else trusted
    by_id = {it.item_id: it for it in items}
    used: set[int] = set()
    rows: list[Row] = []

    for line in lines:
        picked = [by_id[iid] for iid in line.item_ids if iid in by_id]
        if not picked:
            continue
        used.update(it.item_id for it in picked)
        deadlines = [it.deadline_ts for it in picked if it.deadline_ts]
        rows.append(
            Row(
                label=line.label,
                text=line.text,
                kind=picked[0].kind,
                group_id=picked[0].group_id,
                deadline_ts=min(deadlines) if deadlines else None,
                low_confidence=all(it.confidence < low_confidence for it in picked),
                deadline_trusted=bool(deadlines)
                and all(it.item_id in trusted for it in picked if it.deadline_ts),
            )
        )

    for item in items:
        if item.item_id in used:
            continue
        rows.append(
            Row(
                label=item.title,
                text=item.detail or "",
                kind=item.kind,
                group_id=item.group_id,
                deadline_ts=item.deadline_ts,
                low_confidence=item.confidence < low_confidence,
                mechanical=True,
                deadline_trusted=item.item_id in trusted,
            )
        )
    return rows


def stat_line(
    *, groups: int, messages: int, items: int, refined: int | None = None
) -> str:
    """日报开头那句话。数字全部由程序算，不问模型——它编过。

    ⚠️ **覆盖不全时必须加限定**：非空日若只抽取了一部分（如 `vigil refine` 撞了
    预算护栏中途停），不加限定的话「当天 N 条消息，提炼出 M 条」**读起来像读遍了
    全部 N 条**。默认 ``refined=None`` 表示「不加限定」，与既有调用点兼容；
    覆盖完整时输出一字不变（用户已验收的三篇走的正是这条）。
    """
    base = f"当天 {groups} 个群 {messages:,} 条消息"
    if refined is not None and refined < messages:
        base += f"（仅抽取了 {refined:,}/{messages:,}）"
    return f"{base}，提炼出 {items} 条。" if items else f"{base}。"


# 星期查表。**刻意不用 `strftime("%A")`**：它在无 locale 的环境下返回英文
# （`Wednesday`），而 `locale.setlocale(locale.LC_TIME, "zh_CN")` 是**全局副作用**
# ——服务跑在线程池里（见 `api.py` 的模块 docstring），改全局 locale 会影响
# 同一个进程里所有线程的时间格式化。查表没有这个代价。
#
# ⚠️ `weekday()` 是 **0=周一**，与直觉的"0=周日"差一天。
_WEEKDAY_CN = ("一", "二", "三", "四", "五", "六", "日")


def _weekday_suffix(day: str) -> str:
    """`"2026-10-14"` → `" · 星期三"`。解析不了就返回空串（**不抛**）。

    ⚠️ 为什么值得加（不是装饰）：「**周三截止**」这种写法在群消息里到处都是。
    标题带上星期，用户看死线时能**一眼对上**群里的说法，不必在脑子里换算。

    ⚠️ 解析失败返回空串而不是抛：`day` 由调用方给（可能来自 `--date` 参数），
    而渲染标题**不该是**参数校验的地方——真正的日期校验在 `day_window` 里，
    那里失败会给出可读的参数错误。这里再抛一次只会让错误信息更难懂。
    """
    try:
        d = dt.date.fromisoformat(day)
    except ValueError:
        return ""
    return f" · 星期{_WEEKDAY_CN[d.weekday()]}"


def _render_row(
    row: Row, names: dict[int, str], *, with_deadline: bool, now_ts: int | None = None
) -> str:
    parts = [f"- **{row.label}**"]
    if row.text:
        parts.append(row.text)
    line = " ".join(parts)
    tail = names.get(row.group_id, "")
    if with_deadline and row.deadline_ts:
        stamp = dt.datetime.fromtimestamp(row.deadline_ts).strftime("%m-%d")
        tail = f"截止 {stamp} · {tail}" if tail else f"截止 {stamp}"
        # ⭐ 临近提醒（`docs/design/时间注入.md` §二）：在绝对日期之外补一句
        # 「还有 N 天 / 今天截止」。**只有真的临近才加**（`normal` 档不加）——
        # 20 天后的死线写「还有 20 天」只是把日期又说了一遍。
        #
        # ⚠️ **分级必须复用 `deadline.py` 的那一份**，不在这里重写阈值：
        # 前端 `format.ts::relDeadline` 是第二份（它必须独立，因为日历要跟
        # 「当前时刻」变），第三份就该被禁止了——三份阈值 = 必漂移。
        if now_ts is not None:
            level = deadline_mod.deadline_level(row.deadline_ts, now_ts)
            if level != "normal":
                tail = f"{deadline_mod.rel_deadline_text(row.deadline_ts, now_ts)} · {tail}"
    # 群名查不到就什么都不缀——**不要退回群号**，那是本不该出现在成品里的数字
    return f"{line} · {tail}" if tail else line


def render_markdown(
    *,
    day: str,
    stat_line: str,
    rows: list[Row],
    cats: tuple,
    names: dict[int, str],
    window_from: int = 0,
    now_ts: int | None = None,
) -> str:
    """确定性渲染。模型只提供 label 与 text，版式全在这里。

    ⚠️ `now_ts` 缺省是 ``None`` ⇒ **不渲染相对天数**（只写「截止 MM-DD」）。
    这是刻意的：相对天数要一个「现在」，而**测试与历史重渲染都不该依赖
    运行时刻**——带着当前时间渲染一份 9 月的日报，会写出「还有 30 天」这种
    对那个日期毫无意义的话。真正跑管线时（`digest()`）才传入真实的 now。

    分区顺序：**别忘 → 各类目 → 拿不准的**。

    * 「别忘」= 有 deadline 且 **deadline 不早于窗口起点** 的行。加下界是真实
      数据逼出来的：全库有 1 条 item 的截止日早于事件日（8/5 的条目挂 5/6），
      不过滤就会在八月日报里冒出「别忘 5 月 6 日」。
    * **截止日还必须在源文里有字面依据**（``deadline_trusted``，Task 6）。
      实测 4 条里 3 条是模型编的，而「别忘」正是用户拿去办事的地方。
      没有依据的行**照常出现在它的类目区块里**（不漏事），只是不再以权威口吻催办。
    * 进了「别忘」的行**不再出现在类目区块**——同一件事读两遍是负担。
    * 低置信度的行只进「拿不准的」，即使它有截止日：存疑的信息不该催人去办。
    """
    head = f"# 守夜人日报 · {day}{_weekday_suffix(day)}\n\n{stat_line}\n"
    if not rows:
        return head + "\n没有值得一提的信息。\n"

    alert_idx = {
        i
        for i, r in enumerate(rows)
        if r.deadline_trusted
        and r.deadline_ts
        and r.deadline_ts >= window_from
        and not r.low_confidence
    }
    unsure_idx = {i for i, r in enumerate(rows) if r.low_confidence}

    icons = {c.slug: (c.icon, c.label) for c in cats}
    order = [c.slug for c in cats]

    buckets: dict[str, list[int]] = {}
    for i, row in enumerate(rows):
        if i in alert_idx or i in unsure_idx:
            continue
        buckets.setdefault(row.kind, []).append(i)
    # 类目表里没有的 kind 排到最后——**不静默丢**
    for kind in buckets:
        if kind not in icons:
            order.append(kind)

    out = [head]
    if alert_idx:
        out.append("## ⏰ 别忘\n")
        # ⚠️ `now_ts` **只传给「别忘」**：那一节是催办的地方，相对天数在这里
        # 有用。类目区块不催办，加「还有 3 天」只是噪声。
        out += [_render_row(rows[i], names, with_deadline=True, now_ts=now_ts)
                for i in sorted(alert_idx)]
        out.append("")
    for slug in order:
        idx = buckets.get(slug)
        if not idx:
            continue
        icon, label = icons.get(slug, ("", slug))
        title = f"{icon} {label}".strip() if icon else label
        out.append(f"## {title}\n")
        out += [_render_row(rows[i], names, with_deadline=False) for i in idx]
        out.append("")
    if unsure_idx:
        out.append("## 🤔 拿不准的\n")
        out += [_render_row(rows[i], names, with_deadline=False)
                for i in sorted(unsure_idx)]
        out.append("")

    return "\n".join(out).rstrip() + "\n"


def yesterday(today: dt.date | None = None) -> str:
    """默认日报日期 = 昨天。日报是「回看昨天」，不是「看今天」。"""
    base = today or dt.date.today()
    return (base - dt.timedelta(days=1)).isoformat()


def day_window(date_str: str) -> tuple[int, int]:
    """把 ``YYYY-MM-DD`` 变成 ``[当天 00:00, 次日 00:00)`` 的 unix 秒区间。

    ⚠️ 用 datetime + timedelta 而不是 ``since + 86400``：后者在有夏令时的
    时区会差一小时。国内用不着，但这是免费的正确答案。
    """
    try:
        start = dt.datetime.strptime(date_str, "%Y-%m-%d")
    except (TypeError, ValueError) as exc:
        raise ValueError(
            f"日期格式应为 YYYY-MM-DD，实际是 {date_str!r}"
        ) from exc
    return int(start.timestamp()), int((start + dt.timedelta(days=1)).timestamp())


# 日报落盘目录。相对仓库根，与 spec §4.6 的约定一致。
DEFAULT_OUTPUT_DIR = REPO_ROOT / "docs" / "digests"


@dataclass
class DigestStats:
    """如实报告——模型漏写的、匹配失败的、程序补的，都要看得见。"""

    day: str = ""
    window_from: int = 0
    window_to: int = 0
    items: int = 0
    lines: int = 0          # 模型返回且匹配成功的行数
    mechanical: int = 0     # 程序补的行数——**不为 0 就说明模型漏写了**
    # 截止日核验（Task 6）：源文里有字面依据的 / 找不到依据被降级的。
    # 静默丢弃截止日是另一种失败，所以两个数都要如实上报、由 CLI 打出来。
    deadlines_kept: int = 0
    deadlines_dropped: int = 0
    unmatched_quotes: int = 0
    messages: int = 0
    groups: int = 0
    input_tokens: int = 0
    output_tokens: int = 0
    digest_id: int | None = None
    output_path: str = ""
    errors: list[str] = field(default_factory=list)


def digest(
    config: Config,
    *,
    api_key: str,
    db_path: Path | None = None,
    conn: sqlite3.Connection | None = None,
    since: int,
    until: int,
    day_label: str,
    model: str = DEFAULT_MODEL,
    base_url: str = DEFAULT_BASE,
    enable_thinking: bool | None = False,
    output_dir: Path | None = None,
    write_file: bool = True,
    dry_run: bool = False,
    prompt_ver: str = PROMPT_VERSION,
    on_progress=print,
) -> DigestStats:
    """合成一天的日报并落库（可选落文件）。

    连接由调用方持有：CLI 传 db_path，测试传内存 conn（与 refine 同款约定）。
    """
    if until <= since:
        raise ValueError(f"结束时间必须晚于开始时间：{since} → {until}")

    stats = DigestStats(
        day=day_label, window_from=since, window_to=until,
    )
    names = {g.id: g.name for g in config.groups}
    cats = load_categories()
    out_dir = output_dir or DEFAULT_OUTPUT_DIR

    owns_conn = conn is None
    if conn is None:
        # ⚠️ `uri=True` 是 Task 5 加的（裁决 R4）：下面 `store.window_items` 的入口会
        # ATTACH overlay（`file:...?mode=ro`），而 URI 形式只在连接带
        # `SQLITE_OPEN_URI` 时才被解析。URI 的 `mode=ro` 是读路径的机械保证，
        # 不退化成非 URI 挂载。路径不以 `file:` 开头时这个标志不影响行为。
        conn = sqlite3.connect(str(db_path or config.output_db), uri=True)
    try:
        store.ensure_schema(conn)
        items = store.window_items(conn, since=since, until=until)
        # 截止日核验：只有能在**该条自己的源消息**里逐字找到的日期才进「别忘」。
        # 数据是 M1 抽的（它会补出原文没有的实体），核验放在 M2 是因为
        # 「催办」这个动作发生在 M2——本设计裁决原意是可靠，实测却把幻觉
        # 洗成了机器权威。降级的条数照实上报（见 CLI），不静默丢。
        sources = store.item_sources_text(conn, [it.item_id for it in items])
        trusted = verified_deadlines(items, sources)
        stats.deadlines_kept = len(trusted)
        stats.deadlines_dropped = sum(
            1 for it in items if it.deadline_ts and it.item_id not in trusted
        )
        messages, groups = store.window_stats(conn, since=since, until=until)
        stats.items = len(items)
        stats.messages = messages
        stats.groups = groups

        # ⚠️ Task 12：覆盖率检查原先**只存在于空窗分支**。非空日若只抽取了一部分
        # （`vigil refine` 撞预算护栏 `--budget` 会中途停），那句「当天 N 条消息，
        # 提炼出 M 条」读起来就像读遍了全部 N 条——同一句假话的第四个变体，
        # 也是 M4 自动化（实跑 `refine → digest`）最容易撞上的一个。
        # 覆盖完整时 `stat_line` 的输出**一字不变**。
        _, refined = store.window_refine_coverage(conn, since=since, until=until)
        summary = stat_line(
            groups=groups, messages=messages, items=len(items), refined=refined
        )

        # ⚠️ --dry-run 必须排在「空窗」分支**前面**。
        #
        # 规划期的端到端实测抓到过这个顺序错误：原本空窗分支在前且无条件
        # ``_persist``，于是 `vigil digest --date <空窗日> --dry-run` **照样写库
        # 落文件**。单元测试没抓到，是因为它用的样本有 item、走的是另一条分支
        # ——空窗 + dry-run 这个组合当时根本没被测过（「空守卫」那一类）。
        if dry_run:
            if items:
                on_progress(
                    f"[digest] --dry-run：窗口内 {len(items)} 条 item，"
                    f"将调用 1 次模型，不写库、不落文件"
                )
            else:
                # ⚠️ **预览不许猜正文**（Task 11 修复轮次 2 / 审查 F1）：
                # 空窗路径的正文不调模型、是免费的，所以直接**算出来再引用**。
                # 曾经这里硬编码「将写一篇『没有值得一提的信息』的日报」——
                # Task 11 改正文时没人改这一行，同一句「关于完整性的话」
                # 就在预览里变成了假话（文件 / 进度行 / CLI 汇总行之后的第五个变体）。
                # 现在预览引用 `_empty_day_case` 的产物，与真实路径**同一段代码**。
                body, _ = _empty_day_case(
                    conn, config, day_label=day_label, groups=groups,
                    messages=messages, since=since, until=until,
                )
                on_progress(
                    f"[digest] --dry-run：{day_label} 窗口内没有条目，"
                    f"不调用模型、不写库。\n"
                    f"[digest] --dry-run：将写出的正文（不含标题）如下：\n"
                    f"{_body_without_title(body)}"
                )
            return stats

        # 空窗日**不调模型**：既不该花钱，也不该给模型机会编出点什么。
        # 但照样出文件——spec §4.6 要求「明确输出没有值得一提的信息，不假装有事」。
        #
        # ⚠️ 判定（哪一支、说什么）全在 `_empty_day_case` 里，与 `--dry-run`
        # 预览共用同一段代码——**判据只有一份**，见它的 docstring。
        if not items:
            body, progress = _empty_day_case(
                conn, config, day_label=day_label, groups=groups,
                messages=messages, since=since, until=until,
            )
            on_progress(progress)
            return _persist(stats, conn, body, model, prompt_ver, [], write_file,
                            out_dir, day_label)

        # ⚠️ 必须把 `trusted` 传进去（修复轮次 2）：没通过核验的截止日**不出网**。
        # 否则模型会在正文里自己写出那个日期，把程序戳那条漏路原样搬到散文里
        # ——实测 9/12 就是这么漏的。`trusted` 在上面（`window_items` 之后）
        # 就算好了，这里只是接线；本函数的调用方只有这一处。
        payload = build_items_payload(items, names, Redactor(), trusted=trusted)
        try:
            result = chat_json(
                LLMConfig(
                    api_key=api_key, model=model, base_url=base_url,
                    enable_thinking=enable_thinking, max_tokens=MAX_TOKENS,
                ),
                system=build_system_prompt(),
                user=build_user_prompt(payload, day=day_label),
            )
        except LLMError as exc:
            # ⚠️ 不降级。拼一篇只有机械行的「日报」写进 docs/digests/ 的话，
            # 那篇文件与正常日报长得一模一样——正是不许出现的假绿。
            stats.errors.append(f"模型调用失败: {exc}")
            on_progress(f"[digest] {stats.errors[-1]}")
            return stats

        stats.input_tokens = result.input_tokens
        stats.output_tokens = result.output_tokens

        lines, unmatched = match_lines(result.payload.get("lines"), items)
        rows = build_rows(lines, items, trusted=trusted)
        stats.lines = len(lines)
        stats.unmatched_quotes = len(unmatched)
        stats.mechanical = sum(1 for r in rows if r.mechanical)

        body = render_markdown(
            day=day_label, stat_line=summary, rows=rows, cats=cats, names=names,
            window_from=since,
            # ⚠️ 传**真实的现在**：相对天数（「还有 3 天」）只在真跑管线时才有
            # 意义。手工重渲染一份历史日报时不传，就不会写出对那个日期无意义的
            # 「还有 30 天」。
            now_ts=int(dt.datetime.now().timestamp()),
        )
        # 窗口内**每条** item 都挂上——行里有的是模型写的，有的是机械补的，
        # 但两种都对应真实条目，所以覆盖率必然是 100%。
        return _persist(
            stats, conn, body, model, prompt_ver,
            [it.item_id for it in items], write_file, out_dir, day_label,
        )
    finally:
        if owns_conn and conn is not None:
            conn.close()


def screening_breakdown(
    conn: sqlite3.Connection, config: Config, *, since: int, until: int
) -> tuple[int, int, int]:
    """窗口内的 (消息总数, 本地筛掉, 送模型)。

    ⚠️ **为什么要重算而不是查 `refine_runs`**：那张表只记
    ``discarded`` / ``ok``，而「本地规则筛掉」与「送模型后判无价值」
    **都记 ``discarded``**——查不出来。所以按 refine 的管线形状重跑一遍
    （``pending_messages`` → ``prefilter.screen`` → ``expand_context``，纯本地、零 API）。

    ⚠️ **``sent`` 是 ``expand_context`` 之后的条数**——候选**加上前后各
    ``REFINE_CONTEXT`` 条上下文邻居**才是真的会出网的那些，不是候选数。
    首版写成 ``总数 - screen_stats.dropped``，把 967 条「没命中保留规则」
    （同样一步都没出本机）算进了「送模型」：8/8 实测把 **45** 报成了 **976**
    （夸大约 20 倍），而这句错话恰好出在「专门用来防止假数字」的那段文案里
    （修复轮次 1）。**`screen_stats.dropped` 只是「硬丢弃」，不是「没出网」。**

    ⚠️ **为什么不报「硬丢弃」计数**（修复轮次 2）：``screen_stats.dropped`` 只算硬丢弃，
    而它与 ``local`` **不是子集关系**——``expand_context`` 取 ±2 邻居时**不看那条消息
    有没有被丢**，所以被硬规则判死的消息照样可能作为上下文出网。于是「含 N 条硬丢弃」
    里的 N 至少有三种都讲得通的读法（规则层面丢的 83 / 真没出网的 81 / …），
    **读者无从判断该信哪个，也没法验证**。一个没人能验证的数字不如不给：文案只留
    ``1,014 / 45 / 1,059`` 三个**含义确定、且彼此自洽**（前两者之和 == 第三者）的数，
    括注回到定性描述。（首版为此借过 ``prefilter._flood_ids`` / ``_drop_reason``
    来算那个交集，已随计数一并删掉——没有消费方的私有借用就是纯异味。）

    ⚠️ **已知边界（固有代价，不是 bug）**：
    ① ``config/groups.toml`` 的 ``tier`` 与 ``prefilter`` 规则**与 refine 当时一致**；
    ② refine 当时用的 ``context`` 也是默认值（有人 `--context N` 跑过就对不上）；
    ③ ``refine_runs`` 的 ``error`` 行按「没抽取完」算（见 ``window_refine_coverage``）。
    任一者变了，这里的拆分就会与当时的实际不一致。
    """
    msgs = store.pending_messages(conn, since=since, until=until, redo=True)
    candidates, _ = prefilter.screen(msgs, tier_of=config.tier_of)
    in_scope = prefilter.expand_context(msgs, candidates, context=REFINE_CONTEXT)
    sent = len(in_scope)
    return len(msgs), len(msgs) - sent, sent


def _window_outside_span(
    window: tuple[int, int] | None, span: tuple[int, int] | None
) -> bool:
    """窗口是否落在全库消息时间跨度**之外**（库为空 / 判不了时算「之外」）。

    「判不了算之外」是**保守的一侧**：宁可说「这天没数据、先跑 vigil export」
    （一句用户能验证、也能行动的话），也不许在没把握时说「这天没事」
    ——后者正是 Task 9 审查抓到的 F1：**同一句假话换了个入口**。

    提取成函数是为了让 ``digest()`` 的进度行与日报正文**共用同一个判据**：
    两处各写一份的话，改了一处就会出现「屏幕说没数据、文件说没人说话」。
    """
    if span is None or window is None:
        return True
    return window[1] <= span[0] or window[0] > span[1]


def render_empty_day(
    *,
    day: str,
    groups: int,
    messages: int,
    refined: int,
    local_dropped: int,
    sent: int,
    span: tuple[int, int] | None = None,
    window: tuple[int, int] | None = None,
) -> str:
    """空窗日的日报正文。

    **三条判据**：

    1. 窗口内**一条消息都没有** → 分两种（Task 11）：
       ① 窗口在数据库跨度之外 → 「这天没数据，先跑 ``vigil export``」
       ② 在跨度之内 → 「前后都有数据，这天是真的没人说话」
    2. 窗口内消息**全部有 refine_runs 记账** → 出筛除分布 +「没有值得一提的信息」
    3. 覆盖不全 → **不许说「没有值得一提的信息」**，改说「尚未抽取」并提示先跑 refine

    第 1、3 条是同一件事的三次打磨：把一句**可能为假**的话，换成一句**一定为真**
    的话。在此之前，「这天确实没事」「这天压根没抽取过」「这天压根没导出过」
    在日报里长得一模一样——而只有第一种是真的没事。

    ⚠️ **判据 1 为什么必须排在 2 前面**：``refined < messages`` 在
    ``messages == 0`` 时是 ``0 < 0``，**为假**——不先拦下的话，一个从没导出过
    的日子会径直走进「已覆盖」分支，说出「没有值得一提的信息」，还附上
    「其中 0 条被本地规则筛掉…0 条送到模型…」两行退化句子（F1）。
    这不是新引入的，但 M4 自动化会真撞上：``vigil export`` 失败那天，
    日报会说「今天很安静」而不是「今天没数据」。

    ⚠️ **判据 1 的两个分支里，只有「之内」那支带「没有值得一提的信息」**：
    「没数据」不是「没事」，这两句话不能互相顶替——这是 F1 的全部要点。

    ⚠️ 筛除分布只给三个数（``local_dropped`` / ``sent`` / ``messages``），
    三个都有确定含义、且前两者之和恒等于第三者。**括注只定性**（「纯应答 / 过短 /
    刷屏广告一类」）——曾经这里还带一个「含 N 条硬丢弃」的计数，已删：那个 N
    没有唯一含义（规则层面丢的 / 真没出网的 是两回事），**读者无法验证**，
    而「一个没人能验证的数字」正是这段文案存在的意义所要防的东西（修复轮次 2）。
    """
    # ① 窗口内一条消息都没有——「没事」还是「没数据」，必须说清是哪一种。
    if messages == 0:
        if _window_outside_span(window, span):
            return (
                f"# 守夜人日报 · {day}\n\n"
                f"当天没有任何消息记录——数据库里也没有覆盖这一天的数据。\n\n"
                f"先跑 `vigil export`。\n"
            )
        return (
            f"# 守夜人日报 · {day}\n\n"
            f"当天没有任何消息记录。\n\n"
            f"数据库里前后都有数据，所以这天是真的没人说话。\n"
            f"\n没有值得一提的信息。\n"
        )
    head = f"# 守夜人日报 · {day}\n\n当天 {groups} 个群 {messages:,} 条消息。\n"
    # ② 覆盖不全（原判据，不变）
    if refined < messages:
        return (
            f"# 守夜人日报 · {day}\n\n"
            f"当天 {groups} 个群 {messages:,} 条消息，"
            f"**本窗口尚未抽取**（{refined:,}/{messages:,}）。\n\n"
            f"先跑 `vigil refine` 再生成日报。\n"
        )
    # ③ 已覆盖：出筛除分布。⚠️ `sent == 0` 与「送了但判为无价值」是两回事，
    # 不许把前者渲染成「0 条送到模型后判为无价值」——那是一句**没发生的事**。
    if sent == 0:
        tail = (
            f"其中 {local_dropped:,} 条被本地规则筛掉（纯应答 / 过短 / 刷屏广告一类），"
            f"没有送模型。\n"
        )
    else:
        tail = (
            f"其中 {local_dropped:,} 条被本地规则筛掉（纯应答 / 过短 / 刷屏广告一类），"
            f"\n{sent:,} 条送到模型后判为无价值。\n"
        )
    return head + "\n" + tail + "\n没有值得一提的信息。\n"


def _body_without_title(body: str) -> str:
    """正文去掉第一行的 ``# 守夜人日报 · 日期`` 标题（预览里不复述标题，只引正文行）。"""
    lines = body.split("\n")
    if lines and lines[0].startswith("# "):
        lines = lines[1:]
    return "\n".join(lines).strip("\n")


def _empty_day_case(
    conn: sqlite3.Connection,
    config: Config,
    *,
    day_label: str,
    groups: int,
    messages: int,
    since: int,
    until: int,
) -> tuple[str, str]:
    """空窗日：算出 ``(正文, 进度行)``。**真实路径与 ``--dry-run`` 预览共用这一段。**

    ⚠️ **为什么必须共用**（Task 11 修复轮次 2 / 审查 F1）：``--dry-run`` 的预览是
    「关于日报会说什么的一句话」——**它和日报正文一样要核资格**。原先预览是硬编码的
    「将写一篇『没有值得一提的信息』的日报」：Task 11 之前那是真话，Task 11 把正文
    改成「先跑 ``vigil export``」之后**同一行没人改，于是变成了假话**
    ——同一个结构的第五个变体（文件 / 进度行 / CLI 汇总行 / 预览行…）。

    修法是**结构性的，不是再改一次文案**：空窗路径的正文不需要调模型、是免费的，
    那就**直接算出来再引用**。判据没有第二份，**预览说会写什么**与**实际写了什么**
    是同一段代码的产物，结构上不可能漂移。非空日的预览（「将调用 1 次模型」）
    是结构性陈述、不涉及正文内容，不在本函数的职责内。

    三个分支的**判据与顺序**与真实路径完全一致，进度行文案逐字保留：

    1. ``messages == 0`` → 再分「范围之外（没数据）」/「范围之内（真没人说话）」。
       ⚠️ 必须排在最前：``refined < total`` 在这里是 ``0 < 0``，**为假**，
       不先拦下就会走进「已覆盖」说「没有值得一提的信息」（F1 本体）。
       此时也不该调 ``screening_breakdown``（只会返回 ``(0,0,0)``，
       那句「本地筛掉 0 条，送到模型 0 条」是同一个退化句子换了块屏幕）。
    2. ``refined < total`` → 覆盖不全，说「尚未抽取」，不下「没事」的结论（Task 9）。
    3. 其余 → 覆盖完整，出筛除分布；``sent == 0`` 时说「没有送模型」而非「0 条送到模型」。
    """
    total, refined = store.window_refine_coverage(conn, since=since, until=until)
    if messages == 0:
        span = store.message_span(conn)
        outside = _window_outside_span((since, until), span)
        body = render_empty_day(
            day=day_label, groups=groups, messages=messages,
            refined=refined, local_dropped=0, sent=0,
            span=span, window=(since, until),
        )
        progress = (
            f"[digest] {day_label}：窗口内一条消息记录也没有——"
            + (
                "数据库里也没有覆盖这一天的数据，日报照实说「先跑 vigil export」"
                if outside
                else "但前后都有数据，日报照实说「这天真的没人说话」"
            )
            + "，不调用模型"
        )
        return body, progress
    if refined < total:
        body = render_empty_day(
            day=day_label, groups=groups, messages=messages,
            refined=refined, local_dropped=0, sent=0,
        )
        return body, (
            f"[digest] {day_label}：窗口内 {refined:,}/{total:,} 条已抽取，"
            f"**尚未抽取完**——日报只说明这个，不下「没事」的结论"
        )
    _, local, sent = screening_breakdown(conn, config, since=since, until=until)
    body = render_empty_day(
        day=day_label, groups=groups, messages=messages,
        refined=refined, local_dropped=local, sent=sent,
    )
    # ⚠️ `sent == 0` 时进度行同样不许说「送到模型 0 条」（退化输入 ②）：
    # 这里是 `local == messages > 0`，所以「本地筛掉 N 条」本身不退化。
    progress = (
        f"[digest] {day_label}：窗口内没有条目（本地筛掉 {local:,} 条，"
        + ("一条也没送模型）" if sent == 0 else f"送到模型 {sent:,} 条）")
    )
    return body, progress


def _persist(
    stats: DigestStats,
    conn: sqlite3.Connection,
    body: str,
    model: str,
    prompt_ver: str,
    item_ids: list[int],
    write_file: bool,
    out_dir: Path,
    day_label: str,
) -> DigestStats:
    """落库 + 落文件。两件都做，或（write_file=False）只落库。

    ⚠️ **落文件必须显式 ``newline="\\n"``。** `Path.write_text` 的
    ``newline=None`` 会把 ``\\n`` 翻译成 ``os.linesep``，于是 Windows 上
    落出来的 ``.md`` 是 CRLF、而库里 ``body_md`` 是 LF——**同一篇日报的两个
    副本在字节层就不相等**（审查实测：文件 204 字节 / 库 106 字节）。
    后果不是「多了几个 \\r」这么轻：任何按字节对账的下游（M3 的 Web 与文件
    比对、hash 校验、把 .md 提交进 git 后被 autocrlf 反复改写）都会看到
    无意义 diff，而且「库副本 == 文件副本」这条不变量**没法用最自然的写法
    断言**——只能打折扣按文本比，那种折扣正是将来出事的入口。
    """
    # ⚠️ 写库与落文件必须在**同一个事务**里，且顺序是「先写临时文件 → 原子改名
    # → 再提交库」。理由与两种旧写法的坏处：
    #
    #   · 旧版先 `save_digest`（自己 commit）再 `write_text`：落盘失败会留下
    #     一个没有 .md 的库行，归档不变量（文件集合 == 窗口日期集合）当场
    #     破掉，而没有任何东西会报错。
    #   · 直接 `write_text` 是「截断原文件再写」：写到一半失败会让**上一版**
    #     日报变成半截文件——而 docs/digests/ 是**归档**，它唯一的价值就是完整。
    #     所以先写 `.md.tmp` 再用 `Path.replace` 原子改名（同目录内改名，
    #     Windows 上也是原子的）。
    #
    # 落盘失败 ⇒ 事务回滚 ⇒ 库行与文件都没有，且异常照常抛给调用方
    # （`vigil daily` 记进 LAST-ERROR.txt 并返回非零；`vigil digest` 直接
    # 冒泡成 traceback、退出码非零——两条都**不是**静默），重跑
    # `vigil digest --date <day>` 即可。
    with store.transaction(conn):
        stats.digest_id = store.save_digest(
            conn,
            window_from=stats.window_from,
            window_to=stats.window_to,
            body_md=body,
            model=model,
            prompt_ver=prompt_ver,
            item_ids=item_ids,
            commit=False,
        )
        if write_file:
            path = out_dir / f"{day_label}.md"
            path.parent.mkdir(parents=True, exist_ok=True)
            tmp = path.with_name(path.name + ".tmp")
            try:
                tmp.write_text(body, encoding="utf-8", newline="\n")
                tmp.replace(path)
            except BaseException:
                # ⚠️ 失败不许把残骸留在归档目录里。不变量按原文是
                # 「`docs/digests/` 的**文件集合** == 库里的窗口日期集合」，
                # 一个没人引用的 `.md.tmp` 是同一个洞的镜像破法——而且是
                # 本写法自己引入的（旧的直接 `write_text` 从不产生 .tmp）。
                # 清理失败不遮住原异常：用户该看到的是「落盘为什么失败」。
                try:
                    tmp.unlink(missing_ok=True)
                except OSError:
                    pass
                raise
            stats.output_path = str(path)
    return stats
