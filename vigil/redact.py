"""发送前的脱敏：把能指到具体人的东西挡在本机。

**保留姓名是刻意的**——实测的群昵称直接编码了角色
（「22-1班班主任助理张三13900000000」这一类），这是判断「谁发的、
是不是官方」最可靠的信号，剥掉等于废掉抽取层。

四层遮蔽，各挡一类东西：

1. **URL 段整体豁免**（见 ``redact_text``）——链接里的文档 ID / 分享 token
   是结构化标识符，不是身份号；抹掉会把链接打烂，而 ``links`` 是 item
   schema 的一等字段。**实测依据**：不豁免时全库 34 条链接有 2 条被打烂
   （B 站分享链的 32 位 hex ``vd_source``）。
2. **手机号** —— 两侧带数字边界，不在更长的数字串内部匹配。缺了边界，
   ``1788234567890`` 的前 11 位会被咬掉、留下 ``90`` 的残渣。
3. **长数字串（≥10 位）** —— 学号、订单号一类。
4. **身份标签后的短数字** —— QQ 号常见 8 位、群号 9 位，**位数阈值够不着**，
   只能靠上下文。**实测依据**：不加这条时 8 位 QQ 号、9 位群号会原样出网。

为什么不会误伤日期：``2026-10-08`` 中间的连字符让它不命中 ``\\d{10,}``，
``9月15日`` 同理；身份标签规则要求前面必须有 ``QQ``/``群号`` 之类的词，
所以 ``第 3 教学楼`` 不会被误伤。两条边界都有测试守着。
"""

from __future__ import annotations

import re
from collections.abc import Iterable

PLACEHOLDER = "<号码>"

# 正文里被抹掉的姓名换成的占位。与号码占位分开，是因为两者语义不同：
# 「<号码>」是"这里有个号"，「<姓名>」是"这里有个人"。
_NAME_PLACEHOLDER = "<姓名>"

# 链接：整段豁免。\S+ 是刻意的——QQ 消息里的链接后面常紧跟中文标点。
URL = re.compile(r"https?://\S+|www\.\S+")

# 手机号：11 位、1 开头、第二位 3-9。
# 两侧的数字边界不能省：没有它，13 位的 1788234567890 会被咬掉前 11 位、
# 留下 "90" 这样的残渣（W1 审查实测）。
PHONE = re.compile(r"(?<!\d)1[3-9]\d{9}(?!\d)")

# 学号/订单号一类。10 位是下限：日期与金额远短于此，不会误伤。
LONGNUM = re.compile(r"\d{10,}")

# 身份标签后的短数字。QQ 号/群号常见 5-9 位，位数阈值挡不住，只能靠上下文。
#
# ⚠️ 第一版只枚举了 `QQ|群号|微信` 一类字面标签——**实测几乎没起作用**：
# 真实语料里 5-11 位泄漏只从 107 降到 100。中文写法太散，枚举是打地鼠。
# 关键修法是把 **「群」字本身** 当标签（`交流群900000003`、`QQ通知群：900000002`、
# `加qq群 900000005` 全都以它收口），并加 IGNORECASE 覆盖 qq/Qq/QQ 大小写。
# `(?!\d)` 是防残渣：12 位数字若被 `\d{5,12}` 咬掉前 12 位会留下一位尾巴。
IDENTITY_NUM = re.compile(
    r"((?:qq|Q号|企鹅|群号|学号|工号|微信号|微信|wx|vx|群)\s*[:：]?\s*)"
    r"(\d{5,12})(?!\d)",
    re.IGNORECASE,
)

# ⚠️ `群号` 与 `群` 两个备选**都必须留**，不能只留 `群`：
# 「群号 900000002」里 群 后面跟的是「号」不是数字，光靠 `群` 匹配不上。
# （这条是 controller 誊写计划时删错备选、被自测抓回来的。）


def _redact_segment(text: str) -> str:
    """对**不含 URL** 的片段做遮蔽。"""
    text = IDENTITY_NUM.sub(lambda m: m.group(1) + PLACEHOLDER, text)
    text = PHONE.sub(PLACEHOLDER, text)
    return LONGNUM.sub(PLACEHOLDER, text)


# 角色词表。**刻意与 `prefilter.ROLE_PATTERN` 同源但各留一份**：
#
#   prefilter 依赖 store（它要读群配置判档位），而 redact 是更底层的模块。
#   让 redact import prefilter 会把整条依赖链倒过来。
#
# ⚠️ 两份必须覆盖同一组词。`tests/test_redact.py` 有一条交叉断言钉着它 ——
#    只加一处会让"哪些昵称算官方"在两个地方给出不同答案，而症状很隐蔽：
#    预筛放行了一条消息，脱敏却把它的角色词抹掉，模型看到的是"某人"。
_ROLE_HINT = re.compile(
    r"辅导员|班主任|班长|班助|老师|教师|助理|管理员|部长|主席|团长|队长|负责人|学长|学姐"
)

# 班级/院系前缀：「22-1班」「储运263班」「计算机学院」。
# **保留它**（而不是连同姓名一起抹）是因为它不含个人身份，却能让模型
# 分清"哪个班发的" —— 这对判断"这条通知管不管我"是有效信息。
#
# ⚠️ 只认**有结构**的前缀：前面得有数字（「22-1班」「储运263班」）或至少
#    两个汉字（「计算机学院」）。两条下限都是必需的：
#
#    · 末位 `[班系院部]` 写成可选（`?`）时，`^[一-鿿]{0,8}` 会把整个
#      「同学王五」吃掉 —— 前缀匹配成功、姓名原样留下，遮蔽等于没做。
#    · 汉字下限写成 `{0,8}` 时，「班长小王」的「班」会被当前缀吃进去，
#      出网变成「班班长」。而「班长」是**角色词**、不是班级名。
#
#    两条都是自测抓出来的（见 test_redact.py 的两个对应用例）。
_PREFIX_HINT = re.compile(r"^(?:[一-鿿]{2,8}|\d[\d\-－]*)\d*[班系院部]")

# 一点角色词与前缀都认不出时的兜底称呼。**中性、不泄漏**。
_FALLBACK_NAME = "群成员"


def redact_text(text: str) -> str:
    """抹掉可识别到个人的号码，保留其余一切（含姓名）。

    URL 段**整体豁免**：链接里的文档 ID / 分享 token 是结构化标识符，
    抹掉会把链接打烂，而报名链接正是本项目最想抽出来的东西。
    代价是 URL 里若嵌了手机号会一并放行——这个取舍是刻意的：
    打在链接上的损失是确定的（实测 2/34），而手机号出现在 URL 里极罕见。
    """
    parts: list[str] = []
    last = 0
    for match in URL.finditer(text):
        parts.append(_redact_segment(text[last : match.start()]))
        parts.append(match.group(0))  # URL 原样保留
        last = match.end()
    parts.append(_redact_segment(text[last:]))
    return "".join(parts)


class Redactor:
    """一次 run 内保持代号稳定的映射器。

    映射只活在内存里：`items` 表存的是原始 `group_id` 与 `sender_uid`，
    回填时不需要从代号反解，所以这套映射不落盘——落盘反而是多余的泄漏面。

    ⚠️ ``actor('')`` 会抛 ``ValueError``，这是刻意的。匿名发送者
    （实测全库 1,218 条 = 2.55%）**没有身份可言**：给它们一个共享代号，
    模型就会把互不相识的人当成同一个人。调用方必须对空 uid 特判、
    逐条区分（见 ``refine.build_user_prompt``）。

    至于为什么用 fail-fast 而不是静默返回一个占位符：静默的话，
    一次误调用就会把后续**所有真实 uid 的代号整体错位**（W1 审查实测），
    而错误要到产出质量变差时才被发现——那时已经烧掉一整轮 token 了。
    """

    def __init__(self, known_names: Iterable[str] | None = None) -> None:
        self._groups: dict[int, str] = {}
        self._actors: dict[str, str] = {}
        # 已知人名表 —— 用于**正文**里的姓名遮蔽（`scrub_names`）。
        #
        # ⚠️ 为什么正文也需要它：称呼那一处走 `name()` 抹得很干净，
        # 但**消息正文里可能直接写着人名**（「张三说他明天不来」「李四的材料
        # 交了吗」）。只抹称呼而不抹正文 = 姓名照样出网。
        # 这个洞是出口验收的机械判据抓出来的（见 tests/test_refine.py 的
        # `test_prompt_contains_no_real_name`）。
        #
        # 为什么是"传入一个已知名单"而不是"正则识别中文姓名"：
        # 中文姓名没有可靠的字面特征（「明天」也像名字），
        # 靠正则识别会大量误伤正文（"班主任说明天" → "班主任说<姓名>"）。
        # 而**我们本来就知道**这个群里有哪些昵称 —— `sender_names` 表里全是。
        # 用已知名单做**精确**替换，零误伤。
        self._names: list[str] = sorted(
            {n for n in (known_names or ()) if n},
            key=len,
            reverse=True,  # 长名优先：「张三丰」不该被「张三」切掉一半
        )

    def scrub_names(self, text: str) -> str:
        """把**已知人名**从正文里抹掉，换成占位。

        与 `name()` 的分工：`name()` 处理"谁说的"（结构化字段），
        本方法处理"说了什么"（自由文本）。两者都要过，姓名才真的不出网。

        用**精确子串**替换而非正则识别——原因见 `__init__` 里那段注释。
        """
        for n in self._names:
            if n in text:
                text = text.replace(n, _NAME_PLACEHOLDER)
        return text

    @staticmethod
    def person_names(nick: str) -> set[str]:
        """从一个群昵称里剥出**人名本体**。

        为什么需要它：`scrub_names` 的名单来自 `sender_names` 表，而那里存的
        是**完整昵称**（`22-1班班主任助理张三`）；正文里出现的却是**裸名**
        （「张三明天交材料」）。直接拿完整昵称去匹配正文，一个字都命中不了 ——
        这个洞是验收时实测抓出来的（正文里的姓名照样出网）。

        做法：把角色词与班级前缀去掉，剩下的那段就是人名候选。
        「22-1班班主任助理张三」→ 剥掉前缀「22-1班」与角色「班主任」「助理」
        → 「张三」。

        ⚠️ 剩下的**必须够短**（≤4 字）才算人名。不设上限的话，
        一个没有角色词的长昵称（比如「求索技术部的小窝」）会被整段当成"人名"，
        而它极可能是常用短语 —— 那会把正文里正常的词也抹掉。
        中文姓名通常 2-3 字、复姓 4 字，4 是安全上限。
        """
        if not nick:
            return set()
        rest = nick
        prefix_m = _PREFIX_HINT.match(rest)
        if prefix_m:
            rest = rest[prefix_m.end():]
        rest = _ROLE_HINT.sub("", rest)
        # 组织名前缀（「社团负责人王五」里剩下的「社团」）。**必须在角色词之后剥**：
        # 「负责人」是角色、它前面才是组织名。
        rest = re.sub(r"^(?:社团|部门|学院|学校|协会|学生会|团委|班委)+", "", rest)
        # 剥完还剩人名（去掉空白与常见符号）
        rest = re.sub(r"[\s·・．.、,，-]+", "", rest)
        return {rest} if 0 < len(rest) <= 4 else set()

    def group(self, gid: int) -> str:
        if gid not in self._groups:
            self._groups[gid] = f"G{len(self._groups) + 1}"
        return self._groups[gid]

    def actor(self, uid: str) -> str:
        if not uid:
            raise ValueError(
                "actor() 不接受空 uid——匿名发送者没有身份，"
                "必须由调用方逐条区分（见 refine.build_user_prompt）"
            )
        if uid not in self._actors:
            self._actors[uid] = f"U{len(self._actors) + 1}"
        return self._actors[uid]

    def name(self, uid: str, raw_name: str) -> str:
        """出网用的称呼：**保留角色词与班级前缀，抹掉人名本体**。

        这个方法存在的理由是「本机内部」与「出网边界」是两件事：

            模块 docstring 说"保留姓名是刻意的" —— 那个理由在**本机内部**成立。
            但姓名一旦离开本机就是最直接的个人标识符，而赛题第⑧条要求
            「不得侵犯他人隐私」。所以出网处必须抹。

        为什么不整体替换成 ``<姓名>``：

            文档开头那句"角色词是判断是否官方的信号"——那个信号在
            「22-1班班主任助理」里，**不在「张三」里**。整体替换会把信号
            一起抹掉，等于废掉抽取层。所以只抹人名本体、留下角色与前缀。

        为什么键是 ``uid`` 而不是昵称：

            昵称会变（同一人今天叫 A、明天叫 B），uid 不会。用昵称做键会在
            一个 run 内把同一个人算成两个——破坏"同一人说了两次"的可见性。
            所以这个方法的签名与 ``actor()`` 一样以 uid 为首参，
            哪怕实现暂时用不到它。（保留参数是为将来要按 uid 缓存时不必改签名。）

        匿名者由调用方特判、**不调用本方法** —— 与 ``actor('')`` 抛
        ``ValueError`` 是同一条规矩（匿名者本来就没有姓名可抹）。
        """
        _ = uid
        raw = raw_name or ""

        # 前缀只取**开头的班级/院系标记**（「22-1班」「储运263班」「计算机学院」）。
        # ⚠️ 用 `finditer` 取**第一个**匹配，且它必须紧贴串首 —— 这样
        #    「班长小王」里的「班」不会被当成前缀（`_PREFIX_HINT` 要求
        #    分类字前面得有数字或 ≥2 个汉字，「班长」不满足）。
        prefix_m = _PREFIX_HINT.match(raw)
        prefix = prefix_m.group(0) if prefix_m else ""

        # 角色词：从**前缀之后**的那一段里找，避免把前缀里的分类字又算一遍。
        tail = raw[len(prefix):]
        roles = list(dict.fromkeys(_ROLE_HINT.findall(tail)))  # 去重但保序

        out = f"{prefix}{''.join(roles)}"
        return out or _FALLBACK_NAME

    def text(self, s: str) -> str:
        return redact_text(s)
