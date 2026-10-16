"""抽取层的持久化：建表、取待处理消息、写 items、记账。

表建在 data/vigil.db（与 messages 同库），这样「条目 → 源消息」
可以纯 SQL JOIN，不需要跨库。

`digests` / `digest_items` 由 M2 建立——M1 建了也没有写入方。
"""

from __future__ import annotations

import contextlib
import json
import sqlite3
import time
from collections.abc import Iterator, Sequence
from dataclasses import dataclass, replace

STATUS_OK = "ok"
STATUS_DISCARDED = "discarded"
STATUS_ERROR = "error"

# items：结构化条目，日报与 Web 都是它的视图
_ITEMS_DDL = """
CREATE TABLE IF NOT EXISTS items (
    item_id     INTEGER PRIMARY KEY,
    kind        TEXT    NOT NULL,
    title       TEXT    NOT NULL,
    detail      TEXT,
    event_ts    INTEGER NOT NULL,
    deadline_ts INTEGER,
    -- ⚠️ **活动开始时刻**（分钟级），与 `event_ts` 是两件事：
    --   event_ts = **消息发出**的时间（数据层事实，永远有）
    --   start_ts = **活动本身**开始的时间（抽取产物，通常为 NULL）
    --
    -- 存在的理由（`docs/design/时间安排.md` §二）：没有它就只能靠 `deadline_ts`
    -- 判断时间，而死线**只到日** ⇒ 任何"排到几点"的算法都只能凭空发明一个时刻。
    --
    -- ⚠️ **可空是常态，不是异常**：合成语料里带时刻的写法本来就少，
    -- 而「失物招领」这类条目根本没有活动时刻。
    -- 判据是「能不能在源文里逐字找到那个时刻」（`deadline.start_supported`），
    -- 找不到就 NULL —— 与 `deadline_ts` 同一道机器闸门。
    start_ts    INTEGER,
    group_id    INTEGER NOT NULL,
    actor_uid   TEXT,
    place       TEXT,
    links       TEXT,
    amount      TEXT,
    confidence  REAL    NOT NULL,
    model       TEXT    NOT NULL,
    prompt_ver  TEXT    NOT NULL,
    created_at  INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS items_kind_ts  ON items(kind,     event_ts DESC);
CREATE INDEX IF NOT EXISTS items_group_ts ON items(group_id, event_ts DESC);
CREATE INDEX IF NOT EXISTS items_deadline ON items(deadline_ts)
    WHERE deadline_ts IS NOT NULL;
-- 日程表要按**活动时刻**排序（`docs/design/时间安排.md` §四）。
-- 与上面那条同形：部分索引，只覆盖真有 start_ts 的行（那是少数）。
CREATE INDEX IF NOT EXISTS items_start ON items(start_ts)
    WHERE start_ts IS NOT NULL;
"""

# ── 列级迁移（已有的库要能补上新列）────────────────────────────
#
# ⚠️ `CREATE TABLE IF NOT EXISTS` **不会**给已存在的表加列：老库（比如
# `demo/vigil-demo.db` 与用户手上的 `data/vigil.db`）建表时就那么几列，
# 语句整段跳过 ⇒ 查询 `start_ts` 会报 `no such column`。
#
# 范式抄 `overrides._COLUMN_MIGRATIONS`（那里已经有一套跑了很久的）：
# 每条写成 `(表, 列, 语句)` 三元组，收尾拿前两项去 `PRAGMA table_info` **核**——
# 「你以为加上了」与「真的加上了」是两件事，而 SQLite 不会替我们报错。
_COLUMN_MIGRATIONS: tuple[tuple[str, str, str], ...] = (
    (
        "items",
        "start_ts",
        "ALTER TABLE items ADD COLUMN start_ts INTEGER",
    ),
)


def _table_columns(conn: sqlite3.Connection, table: str) -> set[str] | None:
    """表的列名集合。**表不存在时返回 `None`**（与"表在、但没有这一列"区分开）。

    ⚠️ `PRAGMA table_info(<不存在的表>)` **不抛错**，它返回**空集** —— 空集
    同时意味着「表不存在」与「表在、只是这一列没有」。不区分的话，一个全新的
    空库会被当成"有个没有 item_id 的 items 表"。
    """
    exists = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (table,)
    ).fetchone()
    if exists is None:
        return None
    return {str(r[1]) for r in conn.execute(f"PRAGMA table_info({table})")}


def _migrate_columns(conn: sqlite3.Connection) -> None:
    """给**已存在**的表补新列。幂等——每次 `ensure_schema` 都跑。

    ⚠️ **必须在 `executescript(SCHEMA_DDL)` 之前**（实测踩到，2026-10-15）：
    DDL 里有 `CREATE INDEX ... ON items(start_ts)`，而老库的表没有那一列 ——
    先跑 DDL 会炸在 `no such column`。先补列，DDL 就能放心引用新列。

    ⚠️ **表不存在直接跳过**（`_table_columns` 返回 `None`）：那是**全新的库**，
    `executescript` 会用完整 DDL 把表建好、列本来就在。这里 ALTER 反而会炸
    `no such table` —— 实测：空库里 40+ 条用例全红。

    ⚠️ **先查再 ALTER**，不是"直接 ALTER 然后吞异常"：SQLite 没有
    `ADD COLUMN IF NOT EXISTS`，重复 ALTER 抛 `duplicate column name`——
    吞掉它就把「列真的加上了」与「加的时候出了别的错」混为一谈。

    ⚠️ 迁移后要**核**（与 `overrides` 同一纪律）：ALTER 成功不等于列在，
    少了这一步，失败会以「查询时 no such column」的形式在很远的地方冒出来。
    """
    for table, column, ddl in _COLUMN_MIGRATIONS:
        cols = _table_columns(conn, table)
        if cols is None:
            continue  # 表还不存在 —— 全新库，交给下面的 DDL
        if column not in cols:
            conn.execute(ddl)
        # 核：ALTER 之后列必须在（不在就说明这条迁移写错了，当场炸）
        after = _table_columns(conn, table)
        if after is None or column not in after:
            raise RuntimeError(
                f"迁移失败：{table}.{column} 在 ALTER 之后仍然不存在"
                f"（迁移语句：{ddl}）"
            )

# item_sources：可回溯。独立成表而非 JSON 数组，因为要双向查——
# 「这条 item 来自哪几条消息」和「这条消息产出了哪条 item」都要快。
_SOURCES_DDL = """
CREATE TABLE IF NOT EXISTS item_sources (
    item_id INTEGER NOT NULL,
    msg_id  INTEGER NOT NULL,
    PRIMARY KEY (item_id, msg_id)
);
CREATE INDEX IF NOT EXISTS item_sources_msg ON item_sources(msg_id);
"""

# refine_runs：幂等与增量的依据。一条消息一行，重跑跳过已处理的。
_RUNS_DDL = """
CREATE TABLE IF NOT EXISTS refine_runs (
    msg_id     INTEGER PRIMARY KEY,
    refined_at INTEGER NOT NULL,
    status     TEXT    NOT NULL,
    item_count INTEGER NOT NULL DEFAULT 0,
    prompt_ver TEXT    NOT NULL,
    err        TEXT
);
"""

# digests：日报成品。日报是 items 在时间窗内的叙述性渲染，不是第二套抽取管线。
# 唯一索引保证「同一天跑两次」是替换——否则会在库里堆出两篇同日日报。
_DIGESTS_DDL = """
CREATE TABLE IF NOT EXISTS digests (
    digest_id   INTEGER PRIMARY KEY,
    window_from INTEGER NOT NULL,
    window_to   INTEGER NOT NULL,
    body_md     TEXT    NOT NULL,
    model       TEXT    NOT NULL,
    prompt_ver  TEXT    NOT NULL,
    created_at  INTEGER NOT NULL
);
CREATE UNIQUE INDEX IF NOT EXISTS digests_window ON digests(window_from, window_to);
"""

# digest_items：日报 ↔ 条目的互跳。M3 的「点回原文」靠它，
# 也是「日报里说的每一条都能在 Web 里找到」这句承诺的落地。
_DIGEST_ITEMS_DDL = """
CREATE TABLE IF NOT EXISTS digest_items (
    digest_id INTEGER NOT NULL,
    item_id   INTEGER NOT NULL,
    PRIMARY KEY (digest_id, item_id)
);
CREATE INDEX IF NOT EXISTS digest_items_item ON digest_items(item_id);
"""

# items_fts：搜索索引。**外部内容表**（content='items'）而不是另存一份正文——
# 正文只存 items 一处，索引只存分词位置，省空间也不会两份数据说了不一样的话。
#
# ⚠️ tokenize='trigram' 的理由：中文没有词边界，trigram 按 3 字符滑窗建索引，
# 于是「子串匹配」天然可用，不需要分词器。但它有两条实测出来的脾气：
#   1. **短于 3 字符的查询静默返回 0 条**（不报错）→ 见 search_items 的兜底分支
#   2. 裸查询串会被当 FTS5 语法解析，`NOT`/`(`/`a"b` 直接抛错 → 见 _fts_phrase
_SEARCH_DDL = """
CREATE VIRTUAL TABLE IF NOT EXISTS items_fts USING fts5(
    title, detail,
    content='items', content_rowid='item_id',
    tokenize='trigram'
);
CREATE TRIGGER IF NOT EXISTS items_fts_ai AFTER INSERT ON items BEGIN
  INSERT INTO items_fts(rowid, title, detail)
  VALUES (new.item_id, new.title, new.detail);
END;
CREATE TRIGGER IF NOT EXISTS items_fts_ad AFTER DELETE ON items BEGIN
  INSERT INTO items_fts(items_fts, rowid, title, detail)
  VALUES ('delete', old.item_id, old.title, old.detail);
END;
CREATE TRIGGER IF NOT EXISTS items_fts_au AFTER UPDATE ON items BEGIN
  INSERT INTO items_fts(items_fts, rowid, title, detail)
  VALUES ('delete', old.item_id, old.title, old.detail);
  INSERT INTO items_fts(rowid, title, detail)
  VALUES (new.item_id, new.title, new.detail);
END;
"""

SCHEMA_DDL = (
    _ITEMS_DDL
    + _SOURCES_DDL
    + _RUNS_DDL
    + _DIGESTS_DDL
    + _DIGEST_ITEMS_DDL
    + _SEARCH_DDL
)


@dataclass(frozen=True)
class PendingMessage:
    """待抽取的一条消息，姓名已解析好。"""

    msg_id: int
    group_id: int
    ts: int
    sender_uid: str
    sender: str
    content: str


@dataclass(frozen=True)
class ExtractedItem:
    """一条结构化条目。src_msg_ids 让它可回溯到原文。"""

    kind: str
    title: str
    detail: str | None
    event_ts: int
    deadline_ts: int | None
    group_id: int
    actor_uid: str | None
    place: str | None
    links: tuple[str, ...]
    amount: str | None
    confidence: float
    src_msg_ids: tuple[int, ...]
    # ⚠️ **有默认值且排在最后**：`start_ts` 是后加的字段（M7），
    # 而既有测试与调用方都在用位置参数构造它——插在中间会让它们**静默错位**
    # （Python 会把 `group_id` 当成 `start_ts`，类型都是 int，一个都不报错）。
    # 放末尾 + 默认 None ⇒ 老调用点行为逐字不变。
    start_ts: int | None = None


@dataclass(frozen=True)
class WindowItem:
    """日报要用到的一条条目。只含渲染与匹配需要的列。"""

    item_id: int
    kind: str
    title: str
    detail: str | None
    event_ts: int
    deadline_ts: int | None
    group_id: int
    place: str | None
    amount: str | None
    confidence: float


def ensure_schema(conn: sqlite3.Connection) -> None:
    """建表。幂等——每次 refine 都调，不靠外部迁移工具。

    顺带重建搜索索引：触发器只管建好之后的新数据，**存量行**得靠 rebuild。
    跑在写路径上（refine / digest），代价是 O(条目数)，换来「索引不会
    长期停在过期状态」——搜索返回空结果时，那个空结果才真是「库里没有」。
    """
    # 人工干预层（D14）的文件也在这里就位：`refine` / `digest` 跑过之后，
    # 服务启动时就不必再建它。⚠️ **不能**放在 `api.py` 的 `connect()` 里——
    # 那是只读路径。
    from . import overrides

    overrides.ensure_schema()
    # ⚠️⚠️ **必须在 `executescript` 之前**（实测踩到，2026-10-15）：
    # `SCHEMA_DDL` 里有 `CREATE INDEX ... ON items(start_ts)`，而老库的
    # `items` 表**没有那一列** —— `CREATE TABLE IF NOT EXISTS` 会静默跳过
    # 整个建表，于是建索引那一步炸在 `no such column: start_ts`。
    #
    # ⚠️ 这个顺序**测试抓不到**：测试用的都是新建库（DDL 一步到位，列本来就在）。
    # 只有「拿一个迁移前建的库跑一次」才现形 —— 手工验过老 `data/vigil.db`。
    # 判据是**先补列、再走 DDL**：DDL 里可以放心引用新列。
    _migrate_columns(conn)
    conn.executescript(SCHEMA_DDL)
    rebuild_search_index(conn)
    conn.commit()


def pending_messages(
    conn: sqlite3.Connection,
    *,
    since: int | None = None,
    until: int | None = None,
    limit: int | None = None,
    redo: bool = False,
) -> list[PendingMessage]:
    """取待抽取的消息，按时间升序（时间序是上下文与切批的前提）。

    不在这里过滤 `[非文本]`——硬丢弃是预筛层的职责，
    这样 refine_runs 才能覆盖全部消息（M1 出口标准要求无遗漏）。
    """
    sql = """
        SELECT m.msg_id, m.group_id, m.ts,
               COALESCE(m.sender_uid, '') AS uid,
               COALESCE(NULLIF(s.group_nick, ''), NULLIF(s.qq_nick, ''), '') AS sender,
               m.content
        FROM messages m
        LEFT JOIN sender_names s
               ON s.group_id = m.group_id AND s.uid = m.sender_uid
    """
    where: list[str] = []
    params: list[object] = []

    if not redo:
        # ⚠️ 必须排除 error 行重试，不能把它们当成「已处理」。
        # 写成 `NOT IN (SELECT msg_id FROM refine_runs)` 的话，一次网络抖动
        # （429/超时）就会把那批消息**永久跳过**，且没有任何自动重试机制——
        # 静默的永久数据丢失。error 行是「试过但没成功」，不是「已处理」。
        where.append(
            "m.msg_id NOT IN (SELECT msg_id FROM refine_runs WHERE status != ?)"
        )
        params.append(STATUS_ERROR)
    if since is not None:
        where.append("m.ts >= ?")
        params.append(since)
    if until is not None:
        where.append("m.ts < ?")
        params.append(until)

    if where:
        sql += " WHERE " + " AND ".join(where)
    sql += " ORDER BY m.ts ASC, m.msg_id ASC"
    if limit is not None:
        sql += " LIMIT ?"
        params.append(limit)

    return [PendingMessage(*row) for row in conn.execute(sql, params)]


@contextlib.contextmanager
def transaction(conn: sqlite3.Connection) -> Iterator[None]:
    """把一段写操作圈成一个事务：正常退出 commit，出异常 rollback 后原样抛出。

    ⚠️ 存在的唯一理由是**关掉一个窗口**：`save_items` 与 `record_run` 各自
    commit 一次，两步之间崩掉（断电/重启/任务被杀）会留下「items 落库了、
    消息没标记」的状态——下次 `pending_messages` 把同一批消息再抽一遍，
    **产出重复 items 且全程无声**（`items` 表没有任何唯一键，唯一防重复的
    机制就是 `refine_runs` 的记账跟得上）。写进一个事务后，这一批对库的改动
    是**全落盘或全丢弃**，重抽是干净的。

    ⚠️ 捕 `BaseException` 而不是 `Exception`：`KeyboardInterrupt` 与
    `SystemExit` 也必须 rollback，否则 Ctrl-C 会留下半个批次。

    ⚠️ **`commit()` 必须在保护圈里**。它自己也会失败（磁盘满、库被锁死），
    而 `else: conn.commit()` 那种写法**抛了没人 rollback**——悬挂的写会留在连接上，
    被**下一次** commit（比如 `_record_error` 记账那一次）顺手刷出去，于是
    「items 落库了、消息却记成 error」⇒ 下轮重抽 ⇒ **重复 items 且无声**。
    `rollback()` 在 commit 失败后也是必要的：sqlite3 的失败 commit 不保证已清事务。
    （判据：`test_transaction_rolls_back_when_commit_itself_fails`。）

    ⚠️ **不可重入**：不要在 `with transaction(conn)` 里面再开一层——
    sqlite3 的嵌套提交会让内层先落盘，窗口就回来了。
    """
    try:
        yield
        conn.commit()
    except BaseException:
        conn.rollback()
        raise


def _dedupe_batch(items: list[ExtractedItem]) -> list[ExtractedItem]:
    """批内去重：``(title, event_ts, group_id)`` 相同的只留一条。

    ⚠️ 存在的理由是一个真实缺陷（M4 实测）：模型在**一次响应**里把同一条消息
    抽了两遍，而 `items` 表没有唯一键，于是库里出现两条一模一样的条目
    （真库 item 290 / 296，来源同为 msg 7685735400902931272）。
    `trans._run` 的记账防不住它——**同一次 `save_items` 内部**的重复，
    没有任何一层会拦。

    ⚠️ **来源取并集而不是丢弃**：两条重复项的 `src_msg_ids` 可能不同
    （同秒的两条不同消息），直接丢一条会让 `item_sources` 少一行。
    "可回溯到原文"是 M1 的出口标准，不能在这里静默失守。

    ⚠️ 键里**不带** `actor_uid` / `kind`：同标题同秒同群的条目，即便模型给了
    不同的类目，也是同一条信息被抽了两遍——按最早的那条留下。
    """
    seen: dict[tuple[str, int, int], int] = {}
    out: list[ExtractedItem] = []
    for it in items:
        key = (it.title, it.event_ts, it.group_id)
        idx = seen.get(key)
        if idx is None:
            seen[key] = len(out)
            out.append(it)
            continue
        merged = tuple(dict.fromkeys((*out[idx].src_msg_ids, *it.src_msg_ids)))
        out[idx] = replace(out[idx], src_msg_ids=merged)
    return out


# ⚠️ 公开别名（M5 Task 9）：`repass._produce` 必须在**算产出计数之前**走同一套
# 批内去重。写成别名而不是第二份实现——两份实现会漂移，而漂移的方向正是
# 「存量重判说该留 2 条、refine 只写 1 行」这种两条路径互相矛盾的形态
# （计划勘误 E4；真库 item 290/296 就是模型对同一条消息重抽两次）。
# `save_items` 内部走的是同一个函数对象，不是"相似的一段代码"。
dedupe_batch = _dedupe_batch


def save_items(
    conn: sqlite3.Connection,
    items: list[ExtractedItem],
    *,
    model: str,
    prompt_ver: str,
    now: int | None = None,
    commit: bool = True,
) -> int:
    """写 items 与它们的来源，返回写入条数。

    ⚠️ `commit=False` 用于把本函数并入外层 `transaction()` —— 调用方有责任
    用 `with transaction(conn):` 圈住，否则写的东西永远不会落盘。
    """
    items = _dedupe_batch(items)
    stamp = int(time.time()) if now is None else now
    for item in items:
        cur = conn.execute(
            "INSERT INTO items (kind, title, detail, event_ts, deadline_ts,"
            " start_ts, group_id, actor_uid, place, links, amount, confidence,"
            " model, prompt_ver, created_at)"
            " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                item.kind,
                item.title,
                item.detail,
                item.event_ts,
                item.deadline_ts,
                item.start_ts,
                item.group_id,
                item.actor_uid,
                item.place,
                json.dumps(list(item.links), ensure_ascii=False),
                item.amount,
                item.confidence,
                model,
                prompt_ver,
                stamp,
            ),
        )
        conn.executemany(
            "INSERT OR IGNORE INTO item_sources (item_id, msg_id) VALUES (?, ?)",
            [(cur.lastrowid, mid) for mid in item.src_msg_ids],
        )
    if commit:
        conn.commit()
    return len(items)


def record_run(
    conn: sqlite3.Connection,
    msg_ids: list[int] | tuple[int, ...],
    *,
    status: str,
    prompt_ver: str,
    item_count: int = 0,
    err: str | None = None,
    now: int | None = None,
    commit: bool = True,
) -> None:
    """记账。用 REPLACE 保证重跑时是更新而非重复插入。

    ⚠️ `commit=False` 用于把本函数并入外层 `transaction()` —— 调用方有责任
    用 `with transaction(conn):` 圈住，否则写的东西永远不会落盘。
    """
    if not msg_ids:
        return
    stamp = int(time.time()) if now is None else now
    conn.executemany(
        "INSERT OR REPLACE INTO refine_runs"
        " (msg_id, refined_at, status, item_count, prompt_ver, err)"
        " VALUES (?,?,?,?,?,?)",
        [(mid, stamp, status, item_count, prompt_ver, err) for mid in msg_ids],
    )
    if commit:
        conn.commit()


def window_items(
    conn: sqlite3.Connection, *, since: int, until: int
) -> list[WindowItem]:
    """取时间窗内的条目，按 ``(event_ts, item_id)`` 升序。窗口是 ``[since, until)``。

    排序必须**确定**：未命中条目的机械补行按本函数返回序追加，出网 payload
    也按它排列——顺序变了，用户看到的行序就会变。

    ⚠️ 但**分区与署名群不依赖这个顺序**：``build_rows`` 的锚点是 ``picked[0]``，
    而 ``picked`` 的顺序由 ``line.item_ids`` 决定，``line.item_ids`` 来自
    ``digest.match_lines``——后者自己做了
    ``hits.sort(key=lambda it: (it.event_ts, it.item_id))``。锚点顺序由那个
    排序独立保证，与本函数的 ORDER BY 无关。

    （旧注释曾称锚点「隐式依赖」本函数顺序，那是**错的因果**：结论「顺序不能改」
    成立，但理由不是锚点，而是机械补行序与出网 payload 序。已修正。）

    ⚠️ **人工干预层必须在这里生效**：被软删的条目不进日报，被改过类目的条目
    按新类目进日报。`digest.py` 不直接读 `items`——它走本函数，
    所以**这一处就是日报侧的全部接入点**（spec §3.3 实测）。
    """
    _ensure_overlay(conn)
    rows = conn.execute(
        f"SELECT i.item_id, {_EFFECTIVE_KIND}, i.title, i.detail, i.event_ts,"
        " i.deadline_ts, i.group_id, i.place, i.amount, i.confidence"
        " FROM items i"
        " LEFT JOIN ov.item_state ost ON ost.item_id = i.item_id"
        # ⚠️ 这一处**曾经是手抄的**（T5 之前它自己写 `COALESCE(ost.kind, i.kind)`，
        # 而且没有 `ka`），漏掉它的后果是「界面与日报对同一个条目的类目说法不同」
        # ——R13 的典型形状。现在它用同一个常量。
        " LEFT JOIN ov.kind_alias ka ON ka.old_kind = i.kind"
        " LEFT JOIN ov.kind_alias ka2 ON ka2.old_kind = ost.kind"
        f" WHERE i.event_ts >= ? AND i.event_ts < ? AND {_NOT_DELETED}"
        " ORDER BY i.event_ts ASC, i.item_id ASC",
        (since, until),
    ).fetchall()
    return [WindowItem(*row) for row in rows]


def window_stats(
    conn: sqlite3.Connection, *, since: int, until: int
) -> tuple[int, int]:
    """窗口内的 (消息数, 群数)。日报开头那句「N 个群 M 条消息」用它。

    按 ts 区间取，1970-01-01 那批脏数据天然落在窗口外——不需要额外过滤，
    也就不会误把它们算进消息数。
    """
    return conn.execute(
        "SELECT COUNT(*), COUNT(DISTINCT group_id) FROM messages"
        " WHERE ts >= ? AND ts < ?",
        (since, until),
    ).fetchone()


def window_refine_coverage(
    conn: sqlite3.Connection, *, since: int, until: int
) -> tuple[int, int]:
    """窗口内的 (消息总数, **已抽取**的条数)。

    空窗日的日报靠它自证「**是安静，不是管线没跑**」——覆盖率不足时
    绝不能说「没有值得一提的信息」，那是句可能为假的话。

    ⚠️ ``status='error'`` 的行**不算已抽取**（修复轮次 1）：批次失败时 refine
    会写 error 行，而 ``pending_messages(redo=False)`` 的语义是「**试过但没成功
    ≠ 已处理**」——那些消息下次还会被重新取出来跑。两边若不同调，日报就会
    在「其实还有一批没抽」时说出「没有值得一提的信息」：又一个假话入口。
    （首版只查「有没有 refine_runs 行」，与 refine 的重试口径不一致。）
    """
    total = conn.execute(
        "SELECT COUNT(*) FROM messages WHERE ts >= ? AND ts < ?", (since, until)
    ).fetchone()[0]
    refined = conn.execute(
        "SELECT COUNT(*) FROM messages m WHERE m.ts >= ? AND m.ts < ?"
        " AND m.msg_id IN (SELECT msg_id FROM refine_runs WHERE status != ?)",
        (since, until, STATUS_ERROR),
    ).fetchone()[0]
    return total, refined


def message_span(conn: sqlite3.Connection) -> tuple[int, int] | None:
    """全库消息的 ``(最早 ts, 最晚 ts)``；一条都没有时 None。

    空窗日靠它分清「**这天没数据**」与「**这天真的没人说话**」——
    两者在旧版里都渲染成「没有值得一提的信息」，而前者是句假话。

    ⚠️ **为什么要看全库的最值、而不是「窗口前面有没有消息」**：窗口落在
    跨度之外时，库里的消息一条都不可能与这天相邻；而只要窗口夹在
    ``(MIN(ts), MAX(ts))`` 之间，就必然**前后都有数据**——后者正是
    「这天真的没人说话」这句话成立的依据。只看单侧（比如「窗口之前有没有
    消息」）会在「库里的数据全在这天之后」时说不出口径相同的结论。

    ⚠️ **必须过滤 ``ts > 0``，否则「窗口早于全部数据」那一侧永远判不出来**
    （修复轮次 1）。本库有 14 条 ``ts=0`` 的脏数据（1970-01-01），它们会把
    ``MIN(ts)`` 死死按在 0：查一个**早于所有数据**的日期（如 ``2026-06-01``）时
    ``window[1] <= span[0]`` 即 ``<= 0`` → False、``window[0] > span[1]`` → False，
    于是 ``outside`` 算成 False，日报说「这天是真的没人说话」——**可那天压根
    不在数据覆盖范围内**。同一句假话，换了第三个入口。
    实测（过滤后）：``span = (1784553735, 1789308554)`` = 2026-07-20 .. 2026-09-13。

    ⚠️ **``window_stats`` 不要跟着一起改**——两处**看起来**口径不一致，实则
    不是同一类问题，强行「统一」会把对的改错：

    * ``window_stats`` 是**按窗口**统计（``WHERE ts >= ? AND ts < ?``）。脏行的
      ``ts=0`` 天然落在任何一个真实日期的窗口之外，**毒不到**它——它的口径
      本来就对，改它没有收益、只有风险（``ts > 0`` 那种过滤在窗口统计里是
      多余条件，且会让人以为「窗口统计也依赖脏数据」）。
    * ``message_span`` 是**全库 MIN/MAX**，没有任何窗口把脏行挡在外面，
      一条脏行就能把整个下界拉到 0——**它才是会被毒化的那个**。

    一句话：**窗口统计天然免疫脏行，全局最值不免疫**；所以过滤只加在这里。
    """
    row = conn.execute(
        "SELECT MIN(ts), MAX(ts) FROM messages WHERE ts > 0"
    ).fetchone()
    if not row or row[0] is None:
        return None
    return int(row[0]), int(row[1])


def save_digest(
    conn: sqlite3.Connection,
    *,
    window_from: int,
    window_to: int,
    body_md: str,
    model: str,
    prompt_ver: str,
    item_ids: list[int],
    now: int | None = None,
    commit: bool = True,
) -> int:
    """写一篇日报，返回 digest_id。同一个窗口**替换**而不是追加。

    ⚠️ 三步（删 digest_items → 删 digests → 插新行）**必须在同一个事务里**。
    分开提交的话，崩在中间会让当天日报凭空消失——而 `docs/digests/` 的
    文件还在，归档不变量（文件集合 == 库里的窗口日期集合）当场破掉，
    且没有任何东西会报错。

    ⚠️ `commit=False` 时**不自己开事务**，由调用方用 `transaction()` 圈住——
    这样「写库」与「落文件」能进同一个事务（见 `digest._persist`）。
    """
    stamp = int(time.time()) if now is None else now

    def _write() -> int:
        # 不删关联行的话，重跑一次就会在 digest_items 里留下指向前一版日报的孤儿行
        conn.execute(
            "DELETE FROM digest_items WHERE digest_id IN"
            " (SELECT digest_id FROM digests WHERE window_from=? AND window_to=?)",
            (window_from, window_to),
        )
        conn.execute(
            "DELETE FROM digests WHERE window_from=? AND window_to=?",
            (window_from, window_to),
        )
        cur = conn.execute(
            "INSERT INTO digests (window_from, window_to, body_md, model,"
            " prompt_ver, created_at) VALUES (?,?,?,?,?,?)",
            (window_from, window_to, body_md, model, prompt_ver, stamp),
        )
        new_id = int(cur.lastrowid or 0)
        conn.executemany(
            "INSERT OR IGNORE INTO digest_items (digest_id, item_id) VALUES (?,?)",
            [(new_id, iid) for iid in item_ids],
        )
        return new_id

    if commit:
        with transaction(conn):
            return _write()
    return _write()


def item_sources_text(
    conn: sqlite3.Connection, item_ids: list[int]
) -> dict[int, str]:
    """把每条 item 的**源消息正文**按 item_id 拼起来。

    用途：核验「截止日是不是源文里真有的」（``digest.deadline_supported``）。

    ⚠️ 为什么需要这么做：M1 的抽取**会补出原文没有的实体**，截止日是重灾区。
    首次冒烟实测：窗口内 4 条 deadline 有 3 条在源文里毫无依据，且方向一致
    地往后飘（源文「明早7:20集合」被填成 9-15、「周六下午4.00-8.00」被填成
    9-19，而那场面试 9/12 就面完了）。

    没有来源行的 item 不出现在返回里——调用方按「无依据」处理。
    """
    if not item_ids:
        return {}
    marks = ",".join("?" * len(item_ids))
    rows = conn.execute(
        f"SELECT s.item_id, m.content FROM item_sources s"
        f" JOIN messages m ON m.msg_id = s.msg_id"
        f" WHERE s.item_id IN ({marks})",
        list(item_ids),
    ).fetchall()
    grouped: dict[int, list[str]] = {}
    for item_id, content in rows:
        grouped.setdefault(item_id, []).append(content or "")
    return {k: "\n".join(v) for k, v in grouped.items()}


def items_with_start(
    conn: sqlite3.Connection, *, since: int, until: int
) -> list[tuple[int, str, int]]:
    """窗口内**有确定时刻**的条目：`(item_id, title, start_ts)`。

    ⚠️ 与 `upcoming_activities` 是**两个不同的用途**，别合并：
      · `upcoming_activities` 给排期器当**障碍物**（要 overlay 过滤软删）
      · 本函数给**去重/冲突检测**用（也要过滤，但语义是"界面上会显示哪些"）
    今天两者的 SQL 恰好一样，但**用途不同** ⇒ 合成一个会让下次改其中一处
    （比如排期要排除取消的活动）时误伤另一边。
    """
    rows = conn.execute(
        "SELECT i.item_id, i.title, i.start_ts FROM items i"
        " WHERE i.start_ts IS NOT NULL AND i.start_ts >= ? AND i.start_ts < ?"
        " ORDER BY i.start_ts, i.item_id",
        (since, until),
    ).fetchall()
    alive = _drop_soft_deleted(conn, [(int(r[0]),) for r in rows])
    keep = {r[0] for r in alive}
    return [(int(a), str(b), int(c)) for a, b, c in rows if int(a) in keep]


def schedulable_items(
    conn: sqlite3.Connection, *, since: int, until: int
) -> list[tuple[int, str, int]]:
    """窗口内**待排**的条目：有死线、还没被排过的 `(item_id, title, deadline_ts)`。

    ⚠️ **只取有 `deadline_ts` 的**：排期回答的是"这件事什么时候做"，
    而一条连截止日都没有的条目**没有排的依据** —— 硬排等于凭空发明一个紧迫性。
    （与 `start_ts` 的区别见 `schedule.py` 的模块 docstring：
    有 start 的是**已定时刻**，是障碍物；有 deadline 的才是待排的。）

    ⚠️ 必须接 overlay（跳过软删条目）：用户软删掉的东西**不该出现在日程里**
    —— 那是他自己说"这条我不关心"的方式。
    """
    from . import overrides

    rows = conn.execute(
        "SELECT i.item_id, i.title, i.deadline_ts FROM items i"
        " WHERE i.deadline_ts IS NOT NULL AND i.deadline_ts >= ? AND i.deadline_ts < ?"
        " ORDER BY i.deadline_ts, i.item_id",
        (since, until),
    ).fetchall()
    alive = _drop_soft_deleted(conn, [(int(r[0]),) for r in rows])
    keep = {r[0] for r in alive}
    return [(int(a), str(b), int(c)) for a, b, c in rows if int(a) in keep]


def upcoming_activities(
    conn: sqlite3.Connection, *, since: int, until: int
) -> list[tuple[int, str, int]]:
    """窗口内**已有确定时刻**的活动：`(item_id, title, start_ts)`。

    ⚠️ 这些是排期时的**障碍物**，不是待排项 —— 见 `schedule.py` 顶部那段。
    """
    rows = conn.execute(
        "SELECT item_id, title, start_ts FROM items"
        " WHERE start_ts IS NOT NULL AND start_ts >= ? AND start_ts < ?"
        " ORDER BY start_ts, item_id",
        (since, until),
    ).fetchall()
    alive = _drop_soft_deleted(conn, [(int(r[0]),) for r in rows])
    keep = {r[0] for r in alive}
    return [(int(a), str(b), int(c)) for a, b, c in rows if int(a) in keep]


def _drop_soft_deleted(
    conn: sqlite3.Connection, ids: list[tuple[int, ...]]
) -> list[tuple[int, ...]]:
    """丢掉被用户软删的条目。

    ⚠️ 软删状态在 **overlay** 里，不是主库的列 —— 「删除是软的」那条不变量
    要求行留在主库原处。所以"这条还在不在"必须问 overlay。
    复用既有的 `soft_deleted_item_ids`（它已经处理了 overlay 的 attach 与
    缺表兜底），不在这里重写一遍判断 —— 两份实现迟早漂移。
    """
    if not ids:
        return []
    flat = [int(r[0]) for r in ids]
    soft = soft_deleted_item_ids(conn, flat)
    return [r for r in ids if int(r[0]) not in soft]


def items_with_deadline(conn: sqlite3.Connection) -> list[tuple[int, int]]:
    """所有**带截止日**的 ``(item_id, deadline_ts)``。回填核验用。"""
    return [
        (int(a), int(b))
        for a, b in conn.execute(
            "SELECT item_id, deadline_ts FROM items"
            " WHERE deadline_ts IS NOT NULL ORDER BY item_id"
        )
    ]


def clear_deadlines(conn: sqlite3.Connection, item_ids: list[int]) -> int:
    """把给定条目的 ``deadline_ts`` 置 NULL，返回实际改动行数。

    只动这一列：条目的标题、正文、来源一概不碰——**降级的是那一个字段，
    不是整条信息**。用户仍然看得到这条，只是不再被告知一个编出来的日期。
    """
    if not item_ids:
        return 0
    marks = ",".join("?" * len(item_ids))
    cur = conn.execute(
        f"UPDATE items SET deadline_ts = NULL WHERE item_id IN ({marks})",
        list(item_ids),
    )
    conn.commit()
    return int(cur.rowcount)


# ── 存量重判（M5 Task 9 / D10）的四个读写 helper ──────────────────
#
# ⚠️ 这四个函数**不 commit**（`delete_items` / `downgrade_item_fields`）：
# 存量重判是一次不可逆的批量改动，事务边界由调用方（`repass.apply_plan`）
# 决定——「要么全成要么全废」不能拆成几十次各自落盘。
# `clear_deadlines` 那种自带 commit 的写法在这里是**错**的：它中途崩掉会
# 留下「删了一半」的库，而清单已经打印出去了。


def items_used_in_digests(
    conn: sqlite3.Connection, item_ids: Sequence[int]
) -> frozenset[int]:
    """这些 item 里，哪些被**任何**一篇日报引用过。

    ⚠️ 存量重判靠它决定"哪些不能删"——删掉被引用的条目会让
    `digest_items` 留下悬空行，而日报正文里那句话还印着（spec R9 实测：
    `digest_items` 66 行指向 66 条，整批替换会让它们全部悬空）。
    """
    if not item_ids:
        return frozenset()
    marks = ",".join("?" * len(item_ids))
    return frozenset(
        int(r[0])
        for r in conn.execute(
            f"SELECT DISTINCT item_id FROM digest_items WHERE item_id IN ({marks})",
            list(item_ids),
        )
    )


def existing_items_for_messages(
    conn: sqlite3.Connection, msg_ids: Sequence[int]
) -> dict[int, list[int]]:
    """源消息 → 它产出过的 item_id 列表（按 item_id 升序）。

    存量重判的第一步：找出"已产出条目的那批源消息"及其产物。

    ⚠️ 一个 item 可以挂在**多条**源消息上（`_dedupe_batch` 的来源取并集），
    所以同一个 item_id 在返回值里可能出现两次——调用方自己按 item 去重。
    """
    if not msg_ids:
        return {}
    marks = ",".join("?" * len(msg_ids))
    out: dict[int, list[int]] = {}
    for mid, iid in conn.execute(
        f"SELECT msg_id, item_id FROM item_sources WHERE msg_id IN ({marks})"
        " ORDER BY msg_id, item_id",
        list(msg_ids),
    ):
        out.setdefault(int(mid), []).append(int(iid))
    return out


def soft_deleted_item_ids(
    conn: sqlite3.Connection, item_ids: Sequence[int]
) -> frozenset[int]:
    """这些 item 里，哪些被用户**软删**过（D15：行仍在、只是不再显示）。

    ⚠️ 存量重判必须问这一句（计划勘误 E6）：软删是「视图层拿掉了」，
    而 `repass` 做的是 `DELETE FROM items`——**物理删**。删掉之后
    `item_edits` 里那条 delete 事件还在，`undo` 也照样返回成功，
    但**没有任何行可以恢复**：用户的"撤销"变成了静默失败。

    ⚠️ 走 `_ensure_overlay` 而不是直接 JOIN——那条路径会**自动挂载** overlay，
    漏挂时是响亮的 `OverlayError` 而不是「查不到软删 ⇒ 全部照删」的静默错误。
    """
    if not item_ids:
        return frozenset()
    _ensure_overlay(conn)
    marks = ",".join("?" * len(item_ids))
    return frozenset(
        int(r[0])
        for r in conn.execute(
            "SELECT item_id FROM ov.item_state"
            f" WHERE deleted = 1 AND item_id IN ({marks})",
            list(item_ids),
        )
    )


def delete_items(conn: sqlite3.Connection, item_ids: Sequence[int]) -> int:
    """删条目及其来源行。返回删除条数。

    ⚠️ 必须先删 `item_sources`：`items` 的 FTS 触发器（`items_fts_ad`）挂在
    `items` 的 DELETE 上，删主行会顺带清索引；而 `item_sources` 没有触发器，
    不显式删就会留下**指向不存在条目的来源行**。

    ⚠️ **不 commit**——调用方决定事务边界（存量重判要整批要么全成要么全废）。
    """
    if not item_ids:
        return 0
    marks = ",".join("?" * len(item_ids))
    conn.execute(f"DELETE FROM item_sources WHERE item_id IN ({marks})", list(item_ids))
    cur = conn.execute(f"DELETE FROM items WHERE item_id IN ({marks})", list(item_ids))
    return int(cur.rowcount)


def downgrade_item_fields(
    conn: sqlite3.Connection,
    updates: Sequence[tuple[int, str | None, int | None]],
) -> int:
    """把存量条目的 `place` / `deadline_ts` 改成给定值（通常是把不合格的降为 NULL）。

    `updates` 是 ``(item_id, place, deadline_ts)`` 三元组。

    ⚠️ 这是 UPDATE 不是重建 ⇒ `item_id` 不变 ⇒ 不打断 `digest_items`（D16 的同一条理由）。
    ⚠️ **不 commit**，理由同 `delete_items`。
    """
    n = 0
    for item_id, place, deadline_ts in updates:
        cur = conn.execute(
            "UPDATE items SET place = ?, deadline_ts = ? WHERE item_id = ?",
            (place, deadline_ts, item_id),
        )
        n += int(cur.rowcount)
    return n


# ── 只读查询层：Web 的读侧（M3 Task 2）────────────────────────
#
# 全是只读的 SELECT（`rebuild_search_index` 是唯一例外，它写索引）。
# 列名与 API 契约逐字对应，`:mod:`vigil.api`（T3）直接把它序列化出去。

# FTS5 trigram 的最小可查长度。短于它的查询**不会报错，只会返回空**。
MIN_TRIGRAM = 3


def rebuild_search_index(conn: sqlite3.Connection) -> None:
    """把搜索索引与 items 重建一致。

    触发器建好之后新数据会自动同步；这个函数管的是**存量行**与
    「索引曾经与 items 脱节」的情况。
    """
    conn.execute("INSERT INTO items_fts(items_fts) VALUES ('rebuild')")
    conn.commit()


def _fts_phrase(q: str) -> str:
    """把用户输入包成 FTS5 **短语**查询。

    ⚠️ 不包就会炸（实测）：`NOT`、`a"b`、`(`、`补退选 -卡` 全都让 MATCH 抛
    ``OperationalError``——用户在搜索框里打个引号就是 500。包成短语后，
    这些输入退化成「按字面找这个短语」，语义仍然正确。
    """
    return '"' + q.replace('"', '""') + '"'


def _escape_like(q: str) -> str:
    """LIKE 的通配符转义。不转义的话用户输入的 % 会变成「匹配一切」。"""
    return q.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


# C0 控制字符（U+0000–U+001F），含 NUL。它们不承担任何搜索语义。
_CONTROL_TRANS = str.maketrans("", "", "".join(chr(c) for c in range(0x20)))


def _strip_controls(q: str) -> str:
    """剥掉 C0 控制字符（含 NUL）。**必须在 trigram / LIKE 分岔之前调用。**

    ⚠️ 一个 NUL 能让**两条路径各出一个假结果**，而且都能从 HTTP 触发
    （``q`` 是用户直接给的查询串）。实测（内存库 2 行）：

    - ``len(q) >= 3`` → FTS5 解析器在 NUL 处**截断**，``_fts_phrase`` 包出来的
      引号因此不闭合 → 未捕获的 ``OperationalError: unterminated string``
      → T3 上线后就是一个 **500**。
      实测：``q='\\x00选课通知'`` / ``q='选\\x00课'`` 都抛这个错。
    - ``len(q) <= 2`` → SQLite 的 LIKE 把 pattern 在 NUL 处**截断成 ``%``**
      → **返回全表**。实测：``q='\\x00课'`` 在 2 行的库里返回 ``total=2``，
      而正确答案是 1（只有「选课通知」含「课」）。
      这比报错危险得多——它是个**假数字**：页面会说「共 2 条」，
      用户以为搜到了，其实一行都没筛。本项目最忌的就是这类错。

    控制字符本来就不承担搜索语义，剥掉不会让哪个正常查询失去结果。
    剥完为空串时按「不筛选」处理——与既有的「空 ``q`` 等于不筛选」契约一致。
    """
    return q.translate(_CONTROL_TRANS)


@dataclass(frozen=True)
class ApiItem:
    """喂给 Web 的一条条目。列名与 API 契约逐字对应。

    ``actor`` 已在 SQL 里解析成姓名（同 ``pending_messages`` 的口径）；
    ``group_name`` 不在这里解析——群名来自 ``config/groups.toml`` 而不是库，
    由 API 层补上。库返回 id、配置层补名字，这个分工与既有代码一致。
    """

    item_id: int
    kind: str
    title: str
    detail: str | None
    event_ts: int
    deadline_ts: int | None
    group_id: int
    actor: str | None
    actor_uin: int | None
    place: str | None
    amount: str | None
    links: tuple[str, ...]
    source_count: int
    # ⚠️ **有默认值且排在最后**（与 `ExtractedItem.start_ts` 同一条理由）：
    # 既有测试用位置参数构造它，插在中间会让它们**静默错位**
    # （全是 int/str，一个都不会报错）。
    start_ts: int | None = None


@dataclass(frozen=True)
class SourceRow:
    """一条源消息的原文。Web 的「点回原文」靠它。"""

    msg_id: int
    ts: int
    sender: str
    group_id: int
    content: str


@dataclass(frozen=True)
class DigestSummaryRow:
    digest_id: int
    window_from: int
    window_to: int
    created_at: int
    item_count: int


@dataclass(frozen=True)
class DigestRow:
    digest_id: int
    window_from: int
    window_to: int
    body_md: str
    created_at: int
    item_count: int


def _ensure_overlay(conn: sqlite3.Connection) -> None:
    """确保人工干预层已挂成 `ov`。**每个走 `_ITEM_COLS` 的查询入口都要调。**

    ⚠️ 做成"入口自 attach"而不是"调用方先 attach"，是**结构性**的防线：
    SQL 里写了 `ov.item_state` 而没人挂载时，报的是运行时的
    `no such table`——一个漏掉的调用方就是一个 500。
    自 attach 之后，「忘记」在结构上不可能发生（R13 的落点）。

    ⚠️ 局部 import：`store` 与 `overrides` 谁先被 import 都不该出问题。
    （Task 4 实测：其实**不存在**模块级环，`overrides` 只 import 标准库与 `config`；
     局部 import 无害、又省掉每次 import 的代价——留着，但别照抄"会成环"这个错论证。）
    """
    from . import overrides

    overrides.attach_readonly(conn)
    # ⚠️ **对账**（Fix loop 2 / FIX 2）：`categories.toml` 是可以**手改**的
    # （本里程碑明确邀请），而手改不经过任何写端点 ⇒ 剪枝跑不到 ⇒
    # 「别名行的 old_kind 不许是现存 slug」这条不变量会破
    # （实测：手改把 life 加回来之后，手工选的 life 会被旧别名顶成 life_v2）。
    # ⚠️ 它是**幂等**的，且自带 `stat` 闸门（文件没变 ⇒ 立刻返回、连库都不连）
    # ——这是它能挂在每一次查询入口上的前提。
    overrides.reconcile_if_stale()


# ⚠️ **「有效类目」的唯一表达式**（M6 T5）。四条来源的优先级是**承重的**：
#
#     ka2.new_kind  `ost.kind` **自己**是个废 slug 时的改名映射 —— 跟着改名走
#     ost.kind      人工在界面上改过的分类 —— 它**是现存 slug 时**不能被改名覆盖
#     ka.new_kind   `i.kind`（库里的原值）的改名映射
#     i.kind        库里的原值
#
# ⚠️ `ka2` 与 `ka` 的分工（Fix loop 1 / FIX 2）：`ka` 管**库里的原值**，
# `ka2` 管**人工改过的那个值**。没有 `ka2` 的话：「把条目手工改成 life」
# 之后「把 life 改名成 life_v2」，那条条目会**永远停在 life**——一个已经不存在的
# slug（spec §4.2 ④ 点名要禁的状态）。
#
# ⭐ `ka2` 之所以**安全**（不会顶掉用户的合法选择），靠的是一条由剪枝维持的
# **不变量**：`kind_alias` 里不许有 `old_kind` ∈ 现存 slug 的行
# （`overrides.prune_kind_aliases`，每次写 `categories.toml` 之前都会剪）。
# 于是「`ost.kind` 是现存 slug」⇒ `ka2` 匹配不到行 ⇒ NULL ⇒ 落回 `ost.kind`。
# 反过来说：**谁要拿掉那次剪枝，`ka2` 就会变成 bug**（见 `prune_kind_aliases` 的反例）。
#
# ⚠️ 抽成常量**不是为了好看**：在 T5 之前，同一个表达式在**四个地方**各抄了一遍
# （`_ITEM_COLS` / `_item_filters` / `kind_counts` 两处 / `window_items`），
# 而 `window_items` 那份是手抄的、形状还不同（它没有 `ost` 的别名）。
# 「改三处漏一处」在这个形状下是**必然**会发生的，而漏掉 `window_items`
# 的后果是「界面与日报对同一个条目的类目说法不同」——正是 R13。
# 现在只有一处可改，与 `_NOT_DELETED` 完全同形。
#
# ⚠️ 用的地方**必须同时挂 `ka` 与 `ka2` 两个 LEFT JOIN**（见 `_ITEM_JOINS`）。
# 不挂的话 SQLite 报 `no such column: ka.new_kind` / `ka2.new_kind`——**响亮**的
# 失败，不是静默的错分类。这是刻意的：静默的那一半（忘了改某个消费点）才是真正
# 危险的，而"少挂 JOIN"这类错误一跑就现形。
_EFFECTIVE_KIND = "COALESCE(ka2.new_kind, ost.kind, ka.new_kind, i.kind)"

_ITEM_COLS = (
    f"i.item_id, {_EFFECTIVE_KIND} AS kind, i.title, i.detail, i.event_ts,"
    " i.deadline_ts, i.start_ts, i.group_id,"
    " COALESCE(NULLIF(sn.group_nick, ''), NULLIF(sn.qq_nick, ''), '') AS actor,"
    " sn.uin AS actor_uin,"
    " i.place, i.amount, i.links,"
    " (SELECT COUNT(*) FROM item_sources src WHERE src.item_id = i.item_id)"
    "   AS source_count"
)

_ITEM_JOINS = (
    " FROM items i"
    " LEFT JOIN sender_names sn ON sn.group_id = i.group_id AND sn.uid = i.actor_uid"
    " LEFT JOIN ov.item_state ost ON ost.item_id = i.item_id"
    # slug 改名映射（M6 T5）。键取**库里的原值** `i.kind`。
    #
    # ⚠️ 写成 `ON ka.old_kind = COALESCE(ost.kind, i.kind)` **也一样对**——实测：
    # 四处一起换掉，全量 **704 passed / 0 red**。两个键在本表达式下可证明等价：
    # `ost.kind` 非空时它自己就占了 `COALESCE` 的最左位，`ka.new_kind` 根本没机会
    # 露头（匹配到哪一行都改不了答案）；`ost.kind` 为空时两个键是同一个表达式。
    # 选 `i.kind` 只是因为它短、且不必让读的人先在脑子里展开一遍 COALESCE
    # ——**不是**因为另一个写法会错。（勘误 E7：没有实测支撑的理由不许写成结论。）
    " LEFT JOIN ov.kind_alias ka ON ka.old_kind = i.kind"
    # ⚠️ 第二条别名 JOIN：键是**人工改过的那个值**（`ost.kind`），Fix loop 1 加。
    # 与上一条的区别就是 `_EFFECTIVE_KIND` 顶上那四行的分工——`ka` 管库里的原值、
    # `ka2` 管人工值。两条都是 LEFT JOIN ⇒ 匹配不到就是 NULL，不改变行数。
    " LEFT JOIN ov.kind_alias ka2 ON ka2.old_kind = ost.kind"
)

# ⚠️ 软删的条目在**所有**读取路径上都必须消失（D15）。拼进 `_ITEM_JOINS` 的
# `WHERE` 片段单独定义，是为了让"哪几处用了它"一眼可数——
# 漏掉任何一处的后果是「界面上删了、日报里还在」（R13）。
_NOT_DELETED = "IFNULL(ost.deleted, 0) = 0"

# ⚠️ 日报的**计数**（`item_count`）必须与 `digest_items` 用**同一套过滤**
# （裁决 R9 / T5 修复轮 1）。此前这里是未过滤的 `COUNT(*) FROM digest_items`，
# 而 `digest_items` 已过滤 ⇒ 同一响应里「列表说 3 条、items 只有 2 条」，自相矛盾
# （`web/src/types.ts` 直接用这个字段）。抽成常量是为了**只剩一处可改**。
#
# ⚠️ **它是 D10 同形的「手抄 FROM」**（Fix loop 1 / FIX 7）：这份子查询自己拼了
# `LEFT JOIN ov.item_state ost`，而不是复用 `_ITEM_JOINS`——所以哪天它需要
# `_EFFECTIVE_KIND`（今天**不需要**：它只数条数、不回答「什么类目」），
# 就必须**同时**把 `ka` / `ka2` 两个 JOIN 抄进来。今天**良性**，别改行为。
_DIGEST_ITEM_COUNT = (
    " (SELECT COUNT(*) FROM digest_items di"
    " JOIN items i ON i.item_id = di.item_id"
    " LEFT JOIN ov.item_state ost ON ost.item_id = i.item_id"
    f" WHERE di.digest_id = d.digest_id AND {_NOT_DELETED})"
)


def _parse_links(raw: object) -> tuple[str, ...]:
    """``items.links`` 存的是 JSON 数组文本。

    ⚠️ 解析失败**不抛异常**：这一列是 M1 写的，坏一条不该让整页 500。
    解析不出来就当没有链接（视图少一栏，不是白屏）。
    """
    if not raw:
        return ()
    try:
        data = json.loads(raw)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return ()
    if not isinstance(data, list):
        return ()
    return tuple(str(x) for x in data)


def _to_api_item(row: tuple) -> ApiItem:
    # ⚠️ **解包顺序必须与 `_ITEM_COLS` 逐字对应**：这里全是 int/str，
    # 顺序错一个位置**不会报错**，只会让 `place` 显示成 `amount`。
    # 加列时两处要同改（`tests/test_store.py` 有用例钉住往返）。
    (item_id, kind, title, detail, event_ts, deadline_ts, start_ts, group_id,
     actor, actor_uin, place, amount, links, source_count) = row
    return ApiItem(
        item_id=item_id,
        kind=kind,
        title=title,
        detail=detail,
        event_ts=event_ts,
        deadline_ts=deadline_ts,
        group_id=group_id,
        actor=actor or None,
        actor_uin=int(actor_uin) if actor_uin is not None else None,
        place=place,
        amount=amount,
        links=_parse_links(links),
        source_count=int(source_count),
        start_ts=int(start_ts) if start_ts is not None else None,
    )


def _item_filters(
    *,
    kinds: Sequence[str] | None,
    actor_uins: Sequence[int] | None,
    since: int | None,
    until: int | None,
    group: int | None,
    q: str | None,
) -> tuple[list[str], list[object]]:
    """拼 WHERE 片段。

    ⚠️ **维度内并集、维度间交集**（spec §四 #4）：
    `kinds` 之间是 OR、`actor_uins` 之间是 OR，而两组之间是 AND。
    写成"全部 OR 到一起"是最容易犯的错，也是这个函数唯一的语义要点。
    """
    where: list[str] = [_NOT_DELETED]
    params: list[object] = []

    if kinds:
        # ⚠️ 按**覆盖后**的类目筛——否则界面上改了分类却筛不出来。
        marks = ",".join("?" * len(kinds))
        where.append(f"{_EFFECTIVE_KIND} IN ({marks})")
        params.extend(kinds)
    if actor_uins:
        # ⚠️ `uin` 只在 `sender_names` 里（`items` 存的是 `actor_uid`）。
        # 这也是 `search_items` 的 COUNT **必须**带上同一个 LEFT JOIN 的原因：
        # 少一个 `sn` 就是 `no such column: sn.uin`（两处并排写着就是为了让人看见）。
        marks = ",".join("?" * len(actor_uins))
        where.append(f"sn.uin IN ({marks})")
        params.extend(actor_uins)
    if since is not None:
        where.append("i.event_ts >= ?")
        params.append(since)
    if until is not None:
        where.append("i.event_ts < ?")
        params.append(until)
    if group is not None:
        where.append("i.group_id = ?")
        params.append(group)
    if q is not None:
        # ⚠️ 净化必须在这里、**分岔之前**——两条路径各有各的坏法（见 _strip_controls）。
        # 剥完为空串就与「没传 q」同义：沿用「空 q 等于不筛选」的既有契约。
        q = _strip_controls(q)
    if q:
        if len(q) >= MIN_TRIGRAM:
            where.append(
                "i.item_id IN (SELECT rowid FROM items_fts WHERE items_fts MATCH ?)"
            )
            params.append(_fts_phrase(q))
        else:
            # ⚠️ trigram tokenizer **对短于 3 字符的查询静默返回 0 条**（实测，
            # 不报错）——而中文双字词（「选课」「讲座」）恰恰是最常见的查询。
            # 没有这一支的话，「搜不到」会被读成「库里没有」，是句假话。
            # items 目前 270 行、年增约两千行，LIKE 全表扫的代价可以忽略。
            like = f"%{_escape_like(q)}%"
            where.append(
                "(i.title LIKE ? ESCAPE '\\'"
                " OR IFNULL(i.detail, '') LIKE ? ESCAPE '\\')"
            )
            params += [like, like]
    return where, params


def search_items(
    conn: sqlite3.Connection,
    *,
    kinds: Sequence[str] | None = None,
    actor_uins: Sequence[int] | None = None,
    since: int | None = None,
    until: int | None = None,
    group: int | None = None,
    q: str | None = None,
    limit: int = 50,
    offset: int = 0,
) -> tuple[list[ApiItem], int]:
    """查条目，返回 ``(当页, 符合条件的总数)``。

    排序是 ``event_ts DESC, item_id DESC``（新的在前）——信息流的默认读法。
    ⚠️ 与 ``window_items`` 的 ASC 不同是**故意的**：日报按时间顺着读，
    信息流按时间倒着看。两处都不许「顺手改成一致」。
    """
    _ensure_overlay(conn)
    where, params = _item_filters(
        kinds=kinds, actor_uins=actor_uins,
        since=since, until=until, group=group, q=q,
    )
    clause = (" WHERE " + " AND ".join(where)) if where else ""
    # ⚠️ 总数也要走同一套 JOIN/过滤——否则分页与「共 N 条」会说谎。
    # ⚠️ 这里的 FROM 必须与 `_ITEM_JOINS` **逐字一致**（人物筛选用到 `sn.uin`）：
    # 两处并排写着，就是为了让「改了一处忘了另一处」一眼可见。
    # ⚠️ M6 T5 起还多一处：`_item_filters` 产出的 `kinds` 片段引用
    # `{_EFFECTIVE_KIND}`（它含 `ka`）⇒ **这里的 FROM 也必须挂 `ka`**，
    # 否则「按类目筛」这条路径直接 `no such column: ka.new_kind`。
    # 它是 `_EFFECTIVE_KIND` 的**第五个消费点**，而计划正文只数了四处
    # ——因为这一处消费的是**筛选片段**而不是那个表达式本身。
    total = int(
        conn.execute(
            "SELECT COUNT(*) FROM items i"
            " LEFT JOIN sender_names sn"
            "   ON sn.group_id = i.group_id AND sn.uid = i.actor_uid"
            " LEFT JOIN ov.item_state ost ON ost.item_id = i.item_id"
            " LEFT JOIN ov.kind_alias ka ON ka.old_kind = i.kind"
            " LEFT JOIN ov.kind_alias ka2 ON ka2.old_kind = ost.kind"
            f"{clause}",
            params,
        ).fetchone()[0]
    )
    rows = conn.execute(
        f"SELECT {_ITEM_COLS}{_ITEM_JOINS}{clause}"
        " ORDER BY i.event_ts DESC, i.item_id DESC LIMIT ? OFFSET ?",
        [*params, limit, offset],
    ).fetchall()
    return [_to_api_item(r) for r in rows], total


def deadlines_in_range(
    conn: sqlite3.Connection,
    *,
    since: int | None = None,
    until: int | None = None,
) -> list[ApiItem]:
    """带截止日的条目，按 ``(deadline_ts, event_ts, item_id)`` **升序**。

    日历用：从早到晚读，与信息流的 `event_ts DESC` 相反。

    ⚠️ 三处复用既有常量（`_ITEM_COLS` / `_ITEM_JOINS` / `_NOT_DELETED`），
    理由与 `search_items` 逐字相同：软删必须在这里也生效、类目别名也必须在这里
    也解析——漏任何一处的后果是「日历与信息流对同一个条目的说法不同」（R13）。

    ⚠️ 排序的第三键是 `item_id`：`deadline_ts` 与 `event_ts` 都可能相同，只有
    加它才是**确定**序（同 `window_items` 对确定序的要求）。

    ⚠️ **不许**在这里写 `date(deadline_ts,'unixepoch','+8 hours')`：本地日口径的
    唯一定义处是 `api._day_start` / `api._day_of`（E5）。这里只比原始 epoch 秒。
    """
    _ensure_overlay(conn)
    where = ["i.deadline_ts IS NOT NULL", _NOT_DELETED]
    params: list[int] = []
    if since is not None:
        where.append("i.deadline_ts >= ?")
        params.append(since)
    if until is not None:
        where.append("i.deadline_ts < ?")
        params.append(until)
    clause = " WHERE " + " AND ".join(where)
    rows = conn.execute(
        f"SELECT {_ITEM_COLS}{_ITEM_JOINS}{clause}"
        " ORDER BY i.deadline_ts ASC, i.event_ts ASC, i.item_id ASC",
        params,
    ).fetchall()
    return [_to_api_item(r) for r in rows]


def get_item(conn: sqlite3.Connection, item_id: int) -> ApiItem | None:
    """单条条目；不存在返回 None（由调用方决定 404 的形状）。"""
    _ensure_overlay(conn)
    row = conn.execute(
        f"SELECT {_ITEM_COLS}{_ITEM_JOINS} WHERE i.item_id = ? AND {_NOT_DELETED}",
        (item_id,),
    ).fetchone()
    return _to_api_item(row) if row else None


def source_messages(conn: sqlite3.Connection, item_id: int) -> list[SourceRow]:
    """这条 item 来源消息的**原文**，按 ``(ts, msg_id)`` 升序。

    顺序是当初喂给模型的顺序——回溯时读起来才顺。

    名字不叫 ``item_sources``——那会与**表名**撞车，读代码时分不清
    说的是表还是函数（这里返回的是「消息」，不是「来源行」）。
    """
    rows = conn.execute(
        "SELECT m.msg_id, m.ts,"
        " COALESCE(NULLIF(sn.group_nick, ''), NULLIF(sn.qq_nick, ''), '') AS sender,"
        " m.group_id, IFNULL(m.content, '')"
        " FROM item_sources src"
        " JOIN messages m ON m.msg_id = src.msg_id"
        " LEFT JOIN sender_names sn"
        "        ON sn.group_id = m.group_id AND sn.uid = m.sender_uid"
        " WHERE src.item_id = ?"
        " ORDER BY m.ts ASC, m.msg_id ASC",
        (item_id,),
    ).fetchall()
    return [SourceRow(*r) for r in rows]


def kind_counts(conn: sqlite3.Connection) -> dict[str, int]:
    """每个类目有多少条（**按覆盖后的类目**，且不含软删）。类目视图的角标用它。"""
    _ensure_overlay(conn)
    # ⚠️ 这里**不能省 JOIN**：它不走 `_ITEM_JOINS`（那是 SELECT 列与 FROM 的配对），
    # 所以加 `ka` 时必须**同一个函数里两处一起加**（FROM 与 GROUP BY 表达式
    # 都依赖它）。缺 FROM 那半 ⇒ `no such column: ka.new_kind`。
    return {
        str(k): int(n)
        for k, n in conn.execute(
            f"SELECT {_EFFECTIVE_KIND}, COUNT(*) FROM items i"
            " LEFT JOIN ov.item_state ost ON ost.item_id = i.item_id"
            " LEFT JOIN ov.kind_alias ka ON ka.old_kind = i.kind"
            " LEFT JOIN ov.kind_alias ka2 ON ka2.old_kind = ost.kind"
            f" WHERE {_NOT_DELETED} GROUP BY {_EFFECTIVE_KIND}"
        )
    }


def list_digests(
    conn: sqlite3.Connection, *, limit: int = 30
) -> list[DigestSummaryRow]:
    """日报列表，最近的窗口在前。**不带正文**——列表页用不上，白白撑大响应。

    ⚠️ `item_count` 与 `digest_items` 同源（裁決 R9）：软删的条目**不计入**。
    """
    _ensure_overlay(conn)
    rows = conn.execute(
        "SELECT d.digest_id, d.window_from, d.window_to, d.created_at,"
        f"{_DIGEST_ITEM_COUNT}"
        " FROM digests d ORDER BY d.window_from DESC LIMIT ?",
        (limit,),
    ).fetchall()
    return [DigestSummaryRow(*r) for r in rows]


def get_digest(conn: sqlite3.Connection, digest_id: int) -> DigestRow | None:
    """一篇日报（含正文）。⚠️ `item_count` 与 `digest_items` 同源（裁決 R9）。"""
    _ensure_overlay(conn)
    row = conn.execute(
        "SELECT d.digest_id, d.window_from, d.window_to, d.body_md, d.created_at,"
        f"{_DIGEST_ITEM_COUNT}"
        " FROM digests d WHERE d.digest_id = ?",
        (digest_id,),
    ).fetchone()
    return DigestRow(*row) if row else None


def digest_items(conn: sqlite3.Connection, digest_id: int) -> list[ApiItem]:
    """一篇日报引用的条目，按 ``(event_ts, item_id)`` 升序——与日报正文同序。

    ⚠️ 软删的条目在这里必须消失（D15）。理由**不是**「与 `body_md` 保持一致」
    ——`digests.body_md` 是**快照**（生成当时的正文，不随干预层变化），
    过滤与不过滤都会与它不同。真理由是两条：
    ① D15 要求被删条目从界面**与日报**一起消失；
    ② `/api/digests/{id}` 里 `item_count` 与 `items` 是**同一个响应**，
       两者必须自洽（裁決 R9——`item_count` 与这里同源）。
    """
    _ensure_overlay(conn)
    rows = conn.execute(
        f"SELECT {_ITEM_COLS}{_ITEM_JOINS}"
        " JOIN digest_items di ON di.item_id = i.item_id"
        f" WHERE di.digest_id = ? AND {_NOT_DELETED}"
        " ORDER BY i.event_ts ASC, i.item_id ASC",
        (digest_id,),
    ).fetchall()
    return [_to_api_item(r) for r in rows]
