"""摄取：把某个数据源的消息落成三张派生表。

    源（Source）──→ 本模块 ──→ messages / sender_names / msg_media

**本模块只做编排，不做取数**。取数（读 QQ 加密库 / 造合成语料）由 `Source`
实现负责；本模块只管「读到了什么就落什么」，以及把失败如实报出来。

三张派生表的形状是**下游全部代码的唯一契约**（`store.pending_messages`、
`reader.read_messages`、`digest`、`api` 都直接查它们）。所以：

    ⚠️ **改本模块时不要动这三个 schema**。要加字段就加新表，
       否则下游会以一个看不懂的 `no such column` 收场。

设计取舍（沿用早年版 export.py 的结论，均经实测）：

  - **不做增量记账**：每次全量重读已纳群 + `INSERT OR REPLACE`（幂等）。
    实测 4.7 万条几秒 —— 用简单换掉复杂。
  - **失败要如实报**：整群读不到 ⇒ `failed_groups` 非空 ⇒ CLI 退 1。
    这与「这个群本来就没消息」是两件事，绝不能同形（见 `_read_one` 的注释）。
"""

from __future__ import annotations

import pathlib
import sqlite3
from dataclasses import dataclass, field

from . import groups_snapshot
from .config import Config
from .source import GroupRead, Source

# 派生表：正文已抽成文本，查询/检索直接用它。
# 为什么必须有它：原始正文在 QQ 源里是 BLOB，SQLite 的 LIKE 无法在 BLOB 上
# 做文本匹配，不抽出来就搜不了；顺带把解 protobuf 的开销从「每次查询」
# 降到「摄取一次」。
MESSAGE_TABLE = "messages"
_MESSAGE_TABLE_DDL = f"""
CREATE TABLE IF NOT EXISTS {MESSAGE_TABLE} (
    msg_id     INTEGER PRIMARY KEY,
    group_id   INTEGER NOT NULL,
    ts         INTEGER NOT NULL,
    sender_uid TEXT,
    content    TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS messages_group_ts ON {MESSAGE_TABLE}(group_id, ts);
"""

# 发信人姓名表——落库后查询无需再碰源（QQ 源里那是一次解密 + 一次扫表）。
SENDER_TABLE = "sender_names"
_SENDER_TABLE_DDL = f"""
CREATE TABLE IF NOT EXISTS {SENDER_TABLE} (
    group_id   INTEGER NOT NULL,
    uid        TEXT    NOT NULL,
    group_nick TEXT,
    qq_nick    TEXT,
    uin        INTEGER,
    in_group   INTEGER,
    PRIMARY KEY (group_id, uid)
)
"""

# 图片引用表：把「消息里的图」与「本地缓存文件」对应起来。
# path 为 NULL = 这条消息引用了图，但本地没有缓存（QQ 源实测占 93%）。
# 记下未缓存的那些，是为了将来若要联网补全时知道缺哪些。
MEDIA_TABLE = "msg_media"
_MEDIA_TABLE_DDL = f"""
CREATE TABLE IF NOT EXISTS {MEDIA_TABLE} (
    msg_id   INTEGER NOT NULL,
    group_id INTEGER NOT NULL,
    ts       INTEGER NOT NULL,
    md5      TEXT    NOT NULL,
    ext      TEXT,
    path     TEXT,
    PRIMARY KEY (msg_id, md5)
);
CREATE INDEX IF NOT EXISTS msg_media_group ON {MEDIA_TABLE}(group_id, ts);
"""


@dataclass
class IngestStats:
    """摄取结果汇总——失败也要如实报告，不粉饰。

    ⚠️ 字段名与形状是**外部契约**：`daily.py` 与 `cli.py` 逐字读它们。
    （早年叫 `ExportStats`，随数据源层重构改名 —— 语义没变。）
    """

    per_group: list[tuple[int, str, int]] = field(default_factory=list)
    failed_groups: list[tuple[int, str]] = field(default_factory=list)
    skipped_by_group: list[tuple[int, str, int]] = field(default_factory=list)  # (群号, 群名, 条数)
    media_total: int = 0  # 消息里引用的图片数
    media_local: int = 0  # 其中本地有缓存文件的

    @property
    def total_rows(self) -> int:
        return sum(n for _, _, n in self.per_group)

    @property
    def total_skipped(self) -> int:
        return sum(n for _, _, n in self.skipped_by_group)


def _open_output_db(path: pathlib.Path) -> sqlite3.Connection:
    """打开（或新建）明文输出库，只建三张派生表。

    ⚠️ 早年这里还建「源库的完整空表结构」（好让 qqcli 认），那是 QQ 专有的
    兼容需求。开源版没有 qqcli，只建下游真正要的三张表。
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(path))
    # ⚠️ **不用 WAL**（早年用的）。理由是**确定性**：WAL 下新数据先进 `-wal`
    # 侧车，主库 `.db` 的内容取决于事务边界 —— 于是「同一 seed 连跑两次」
    # 得到两个不同字节的 `.db`，而"可复现"本身就不成立了
    # （评委拿到的库与材料里的不是一个东西）。
    #
    # 代价是并发写会 `database is locked`。对本项目无影响：写路径
    # （`vigil ingest` / `vigil demo`）本来就上了 `SingleInstance` 文件锁，
    # 同一时刻只有一个写者 —— WAL 想解决的问题在这里不存在。
    conn.execute("PRAGMA journal_mode = DELETE;")
    conn.execute("PRAGMA synchronous = NORMAL;")
    conn.executescript(_MESSAGE_TABLE_DDL)  # executescript 会隐式提交
    conn.executescript(_MEDIA_TABLE_DDL)
    conn.commit()
    return conn


def _groups_to_ingest(
    config, source, on_progress
) -> list[tuple[int, str]]:
    """要摄取的群 = **配置里启用的** ∪ **源自己报告的**。

    ⚠️ 为什么需要"源自己报告的"那一半（2026-10-16，实现网页源时踩到）：
    `config/groups.toml` 里的群号是**人写死的**（QQ 群号、合成语料的
    9000000xx）。而网页源的 group_id 是 `md5(url)` **算出来的** ——
    用户不可能先算出 md5 再写进配置。

    实测后果：只按配置选群时，网页源摄取出 **0 个群 0 条消息**，
    而且**不报错**（`enabled_groups` 里那些 id 恰好都不在源里，
    每个都被 `failed_groups` 记下，但那次调用方没看）。

    ⚠️ 取**并集**而不是替换成 `list_groups()`：QQ / 合成源的群清单
    可能比配置大得多（那是全库所有群，不只是要监听的）。
    用并集 ⇒ **既有两个源的行为一字不变**，网页源也能被发现。
    """
    configured = {g.id: g.name for g in config.enabled_groups}
    try:
        available = dict(_names_of(source))
    except Exception as exc:  # noqa: BLE001 — 源不报清单不该让摄取整个失败
        # ⚠️ 只警告：`list_groups()` 是**可选能力**（协议不强制它返回全部），
        # 而按配置摄取是老路径，必须照常走。
        on_progress(f"[注意] 源没有报告群清单（{str(exc)[:60]}），只按配置摄取")
        available = {}

    # ⚠️⚠️ **只取交并时过滤掉源里没有的**（实测踩到，2026-10-16）：
    #
    # 两份清单的来源不同，**它们对不上是常态而不是错误**：
    #   · `configured` 是**人写的**（QQ 群号 / 合成语料的 9000000xx）
    #   · `available` 是**源算出来的**（网页源用 md5(url)）
    # 换源时（比如从 synthetic 切到 web），旧配置里的群号在新源里
    # **理所当然不存在**。
    #
    # ⚠️ 原实现把它们全塞进 `failed_groups`，产出 4 条
    # 「没有这个通知源」—— 而那**不是失败**，是"这个源跟那份配置无关"。
    # 报成失败会让人以为采集坏了，而真相是配置没跟着换。
    #
    # ⇒ 判据：**配置里的群若源不认识，就不摄取它**（跳过），
    #   而源自己报的群照常摄取。两个源的行为都因此变得合理。
    out: dict[int, str] = {}
    for gid, gname in available.items():
        out[gid] = gname
    # 源报了清单时，配置里的群只保留"源确实有"的那些；
    # 源**没**报清单时（`available` 为空）保持老行为：按配置摄取
    # —— 那种源的 `read_group` 自己会抛，错误信息比这里编的准确。
    if available:
        for gid, gname in configured.items():
            if gid in available:
                out[gid] = configured[gid] or available[gid]
    else:
        out = configured
    return sorted(out.items())


def _names_of(source) -> list[tuple[int, str]]:
    """`source.list_groups()` + `group_names()` 合成 `[(id, 名字)]`。

    ⚠️ `group_names()` 是锦上添花（协议允许它返回空），所以它失败时
    退化成"名字就是群号"，而不是让整件事失败。
    """
    names: dict[str, str] = {}
    try:
        names = source.group_names()
    except Exception:  # noqa: BLE001
        names = {}
    return [(gid, names.get(str(gid), str(gid))) for gid, _n, _t in source.list_groups()]


def _write_messages(out: sqlite3.Connection, group_id: int, read: GroupRead) -> None:
    """把整群消息写入派生表（表由 _open_output_db 建好）。"""
    out.executemany(
        f"INSERT OR REPLACE INTO {MESSAGE_TABLE} VALUES (?,?,?,?,?)",
        [
            (m.msg_id, group_id, m.ts, m.sender_uid, m.content)
            for m in read.messages
        ],
    )


def _write_senders(
    out: sqlite3.Connection, group_ids: set[int], sender_map: dict
) -> int:
    """把关注群的成员姓名写入，让查询不必再碰源。"""
    out.execute(_SENDER_TABLE_DDL)
    rows = [
        (gid, uid, s.group_nick, s.qq_nick, s.uin, 1 if s.in_group else 0)
        for (gid, uid), s in sender_map.items()
        if gid in group_ids
    ]
    out.executemany(f"INSERT OR REPLACE INTO {SENDER_TABLE} VALUES (?,?,?,?,?,?)", rows)
    out.commit()
    return len(rows)


def _write_media(
    out: sqlite3.Connection, group_id: int, read: GroupRead, cache: dict
) -> tuple[int, int]:
    """把消息里的图片引用写入映射表，返回 (引用总数, 本地有文件的数)。

    同一消息可能引用多张图（转发/相册），全部记录。
    """
    records = []
    for m in read.messages:
        for md5, ext in m.media:
            found = cache.get(md5)
            records.append(
                (
                    m.msg_id,
                    group_id,
                    m.ts,
                    md5,
                    ext,
                    str(found) if found else None,
                )
            )
    if records:
        out.executemany(
            f"INSERT OR REPLACE INTO {MEDIA_TABLE} VALUES (?,?,?,?,?,?)", records
        )
    return len(records), sum(1 for r in records if r[5])


def ingest(config: Config, source: Source, *, on_progress=print) -> IngestStats:
    """把配置里所有 enabled 的群从 `source` 摄取进 `config.output_db`。

    幂等：重跑得到同一状态（`INSERT OR REPLACE`）。

    ⚠️ `source` 是**必填位置参数**而不是可选关键字 —— 早年 `export(config, key)`
       的密钥参数在这里消失了（开源版没有需要密钥的源），而"用哪个源"必须
       显式决定。给一个默认值会让"忘了传"变成一个静默的默认源。
    """
    stats = IngestStats()
    out = _open_output_db(config.output_db)

    # 发信人姓名。取不到就退化成空——姓名是增强项，不该拖垮摄取。
    try:
        sender_map = source.sender_names()
    except Exception:  # noqa: BLE001
        sender_map = {}
    n_senders = _write_senders(out, {g.id for g in config.enabled_groups}, sender_map)
    on_progress(f"发信人姓名：{n_senders:,} 人")

    # ⚠️ 顺手刷新群清单快照。**必须查全源**而不是只列已纳群：
    # 那个页面的用途正是「从源里挑一个还没纳的群加进来」，只列已纳的
    # 等于把功能砍掉。
    # ⚠️ 失败只警告不中断：快照是增强项，摄取的主业是把消息落下来。
    snap_note = ""
    try:
        counts = source.list_groups()
        names: dict[str, str] = {}
        try:
            names = source.group_names()
        except Exception:  # noqa: BLE001 — 群名是锦上添花
            names = {}
        groups_snapshot.write(
            groups_snapshot.collect(counts, names),
            groups_snapshot.default_path(config),
        )
        snap_note = f"群清单快照：{len(counts)} 个群"
    except Exception as exc:  # noqa: BLE001 — 增强项，不该拖垮摄取
        snap_note = f"[警告] 群清单快照写不出去：{str(exc)[:80]}"
    # ⚠️ **报进度必须留在 try 外面**（M6 T4 复审 Minor-3）：写在 `try` 里的话，
    # 「日志写不进去」会被上面那句 `except Exception` 吞成一句
    # 「群清单快照写不出去」——**一个指向错误方向的诊断**。能吞的只有
    # 「打扫类」失败；**报进度是本次运行的记录**。
    on_progress(snap_note)

    # 源侧图片索引：只取一次，所有群共用。图片是增强项，取不到不拖垮摄取。
    #
    # ⚠️ `on_progress` **必须留在 try 外面**（同上的完整论证）：它原先和
    # `media_index()` 挤在同一个 try 里，于是「报进度失败」（比如日志写不进去）
    # 会被 `except Exception` 吞成 `cache = {}` —— 后果不只是那句话丢了，
    # 而是 `stats.media_local` 变成 0，汇总行会报出「本地可取 0 张」这个**假数字**。
    try:
        cache = source.media_index()
    except Exception:  # noqa: BLE001 — 图片是增强项
        cache = {}
    on_progress(f"本地图片缓存：{len(cache):,} 个")

    try:
        for gid, gname in _groups_to_ingest(config, source, on_progress):
            try:
                read = source.read_group(gid)
            except Exception as exc:  # noqa: BLE001 — 外部环境问题，降级而非中断
                # ⚠️ **整群读不到 = 失败**，不是"这个群没消息"。
                # 详见 source.Source.read_group 的契约注释。
                stats.failed_groups.append((gid, str(exc)[:80]))
                on_progress(f"  ✗ {gid} {gname}：{str(exc)[:60]}")
                continue

            _write_messages(out, gid, read)
            n_media, n_local = _write_media(out, gid, read, cache)
            stats.media_total += n_media
            stats.media_local += n_local
            out.commit()
            stats.per_group.append((gid, gname, len(read.messages)))

            note = ""
            if read.skipped_count:
                stats.skipped_by_group.append(
                    (group.id, group.name, read.skipped_count)
                )
                note = f"  [降级：跳过 {read.skipped_count} 条]"
            on_progress(f"  ✓ {gid} {gname}：{len(read.messages):,} 条{note}")
    finally:
        out.close()

    return stats
