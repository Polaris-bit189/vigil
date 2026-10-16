"""规则预筛：先在本机零成本地砍掉大半消息，再交给 LLM。

这是省钱的地方——全量直喂 LLM 按探针实测约 3.7M 输入 token，
预筛能再降一个数量级。

判定顺序是**先丢弃后保留**：硬丢弃规则优先，命中即丢，
不给保留规则翻案的机会。理由——「已完成」这类消息在班级群里
是 tier=high，若让保留规则先跑就会被全部喂给模型（探针实测
15 条里 8 条是「已完成」）。
"""

from __future__ import annotations

import re
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field

from .store import PendingMessage
from .source import NON_TEXT

# 短于这个字节数的消息没有信息量。用字节而非字符：
# 「好的」是 6 字节、单字「好」是 3 字节，按字符算会全放进来。
MIN_BYTES = 4

# 关键词表。刻意**不用**「时间」「地点」「收」「出」这类高频词——
# 它们几乎命中一切，等于没筛。
DEFAULT_KEYWORDS = frozenset(
    """
    通知 报名 截止 考试 作业 选课 讲座 招新 面试 成绩 体检 缴费 办理 领取
    登记 补考 四六级 重修 报到 注册 提交 填写 会议 集合 培训 公示 名单
    转让 出售 求购 拼车 租房 兼职 招聘 实习 校招 家教 勤工 日结
    丢失 拾到 捡到 失物 认领 招领 寻物 换 退 补
    """.split()
)

# 群昵称里编码了角色——这是判断发送者是否"官方"的可用信号。
# 实测：'22-1班班主任助理张三'、'某某社团李四' 这类写法。
ROLE_PATTERN = re.compile(
    r"辅导员|班主任|班长|班助|老师|教师|助理|管理员|部长|主席|团长|队长|负责人|学长|学姐"
)

# 整条消息就是一句应答，没有信息量。
# 必须是**全匹配**——「收到，明天9点集合」不能被丢。
BARE_ACK = re.compile(
    r"^(已(完成|收到|填写|填好|交|办|知)|收到|好(的)?|嗯+|哦+|谢谢?|ok|OK|"
    r"知道(了)?|行|1|✓|👌)[!！。.~～、\s]*$"
)


@dataclass(frozen=True)
class Candidate:
    """一条被判定为值得抽取的消息。reason 用于观测与调参。"""

    msg_id: int
    group_id: int
    ts: int
    sender: str
    content: str
    reason: str


@dataclass
class ScreenStats:
    total: int = 0
    kept: int = 0
    dropped_empty: int = 0
    dropped_short: int = 0
    dropped_bad_time: int = 0
    dropped_ack: int = 0
    dropped_flood: int = 0
    kept_high_tier: int = 0
    kept_keyword: int = 0
    kept_role: int = 0

    @property
    def dropped(self) -> int:
        return (
            self.dropped_empty
            + self.dropped_short
            + self.dropped_bad_time
            + self.dropped_ack
            + self.dropped_flood
        )


def _normalized(text: str) -> str:
    return re.sub(r"\s+", "", text).lower()


def _flood_ids(
    messages: Sequence[PendingMessage], *, window: int = 60, threshold: int = 3
) -> set[int]:
    """找出刷屏消息：**同一群内**同一 uid 在 window 秒内发出 threshold 条以上相同内容。

    ⚠️ 两个约束都不能少——W1 波级审查实测抓出的缺陷，改前先读懂为什么：

    * **必须按群分桶**。只按 (uid, 内容) 分桶会让全部 15 个群的匿名者
      共用同一个桶：三个群的匿名者 60 秒内转发同一条通知，就会被误判成
      「刷屏」整批丢掉——而**被转发的通知恰恰是本项目最想保住的东西**。
    * **匿名者（uid 为空串）整体跳过**。实测全库 1,218 条（2.55%）匿名消息；
      `pending_messages` 把 NULL uid 规范化成 `''`，而**空串不是身份**——
      拿它当桶键等于把互不相识的人当成同一个人。
    """
    buckets: dict[tuple[int, str, str], list[PendingMessage]] = {}
    for m in messages:
        if not m.sender_uid:  # 匿名者无法归属身份，不参与刷屏判定
            continue
        body = _normalized(m.content)
        if not body:
            continue
        buckets.setdefault((m.group_id, m.sender_uid, body), []).append(m)

    out: set[int] = set()
    for group in buckets.values():
        if len(group) < threshold:
            continue
        group.sort(key=lambda m: m.ts)
        for i in range(len(group) - threshold + 1):
            if group[i + threshold - 1].ts - group[i].ts <= window:
                out.update(m.msg_id for m in group[i : i + threshold])
    return out


def _drop_reason(m: PendingMessage, *, flood: set[int]) -> str | None:
    """硬丢弃。返回原因码，None 表示没被丢弃。"""
    body = m.content.strip()
    if not body or body == NON_TEXT:
        return "empty"
    # 单字符一律算「太短」——「好」「1」没有信息量。
    # 但**多字符的纯应答不算太短**：「OK」只有 2 字节，若在这里被挡掉，
    # 就会被记成 dropped_short 而不是 dropped_ack——两者都丢，但计数要
    # 反映「应答噪声」的真实规模（探针实测班级群 15 条里 8 条是「已完成」）。
    # 它们照样会丢，只是由下面 BARE_ACK 那一关来丢。
    if len(body.encode("utf-8")) < MIN_BYTES and not (
        len(body) > 1 and BARE_ACK.match(body)
    ):
        return "short"
    if m.ts <= 0:
        return "bad_time"
    if m.msg_id in flood:
        return "flood"
    if BARE_ACK.match(body):
        return "ack"
    return None


def _keep_reason(m: PendingMessage, *, tier: str, keywords: frozenset[str]) -> str:
    """保留判定。返回原因，空串表示不保留。"""
    if tier == "high":
        return "high_tier"
    if ROLE_PATTERN.search(m.sender):
        return "role"
    for kw in keywords:
        if kw in m.content:
            return "keyword"
    return ""


def screen(
    messages: Sequence[PendingMessage],
    *,
    tier_of: Callable[[int], str],
    keywords: frozenset[str] = DEFAULT_KEYWORDS,
) -> tuple[list[Candidate], ScreenStats]:
    """把消息分成「值得抽取」与「丢弃」两堆。"""
    stats = ScreenStats(total=len(messages))
    flood = _flood_ids(messages)
    kept: list[Candidate] = []

    for m in messages:
        dropped = _drop_reason(m, flood=flood)
        if dropped is not None:
            setattr(stats, f"dropped_{dropped}", getattr(stats, f"dropped_{dropped}") + 1)
            continue

        reason = _keep_reason(m, tier=tier_of(m.group_id), keywords=keywords)
        if not reason:
            # 既不硬丢弃也没命中保留规则——算作「软丢弃」，不计入硬丢弃计数
            continue

        kept.append(
            Candidate(
                msg_id=m.msg_id,
                group_id=m.group_id,
                ts=m.ts,
                sender=m.sender,
                content=m.content,
                reason=reason,
            )
        )
        stats.kept += 1
        setattr(stats, f"kept_{reason}", getattr(stats, f"kept_{reason}") + 1)

    return kept, stats


def expand_context(
    messages: Sequence[PendingMessage],
    candidates: Sequence[Candidate],
    *,
    context: int = 2,
) -> list[PendingMessage]:
    """把候选连同它们**在同一群内**前后各 context 条邻居一并取回。

    为什么需要：单条消息常常没头没尾（「明天记得带」），
    邻居能救回这类。跨群的「邻居」是毫无意义的，所以按群分组。

    参数 messages 必须已按时间升序（pending_messages 保证了这点）。
    """
    if context <= 0:
        keep = {c.msg_id for c in candidates}
        return [m for m in messages if m.msg_id in keep]

    by_group: dict[int, list[PendingMessage]] = {}
    for m in messages:
        by_group.setdefault(m.group_id, []).append(m)

    positions = {
        gid: {m.msg_id: i for i, m in enumerate(msgs)}
        for gid, msgs in by_group.items()
    }

    in_scope: dict[int, set[int]] = {}
    for c in candidates:
        index = positions.get(c.group_id)
        if not index or c.msg_id not in index:
            continue
        i = index[c.msg_id]
        lo = max(0, i - context)
        hi = min(len(by_group[c.group_id]), i + context + 1)
        in_scope.setdefault(c.group_id, set()).update(range(lo, hi))

    out: list[PendingMessage] = []
    for gid, indexes in in_scope.items():
        msgs = by_group[gid]
        out.extend(msgs[i] for i in sorted(indexes))
    return out


def make_batches(
    messages: Sequence[PendingMessage], *, max_batch: int = 30
) -> list[list[PendingMessage]]:
    """按（群，时间）连续性切批。

    不跨群混批是刻意的：不同群的对话毫无关系，混在一起会让模型
    把 A 群的上下文套到 B 群的消息上。
    """
    out: list[list[PendingMessage]] = []
    current: list[PendingMessage] = []
    last_group: int | None = None

    for m in messages:
        if last_group is not None and (m.group_id != last_group or len(current) >= max_batch):
            out.append(current)
            current = []
        current.append(m)
        last_group = m.group_id

    if current:
        out.append(current)
    return out
