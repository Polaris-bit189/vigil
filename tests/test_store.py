"""持久化层的测试。全部用内存库，不碰 data/vigil.db。

⚠️ Task 5 起，本文件里**每个** `sqlite3.connect(":memory:", ...)` 都带 `uri=True`：
查询层入口会 `ATTACH 'file:...?mode=ro'`，而 URI 形式只在连接带
`SQLITE_OPEN_URI` 时才被解析（Task 4 实测、T5 复核）。对 `:memory:` 而言这个标志
不影响任何行为，与生产侧三处连接的补齐（裁决 R4）是同一件事。
"""

from __future__ import annotations

import sqlite3

import pytest

from vigil import overrides
from vigil import store
from vigil.store import ExtractedItem, PendingMessage


# messages + sender_names 的形状，与 export.py 产出的完全一致。
# 这两张表**不由 store.ensure_schema 建**（它们是 export.py 的产物、真库里一直在），
# 但查询层会 JOIN 它们——所以测试里凡是要跑查询层的插桩，都得先把它们立起来。
# ⚠️ 用 IF NOT EXISTS：M3 Task 2 的 `_insert_item` 会在 `_seed_messages` 之前
# 先立一次表，两者叠加时不能炸。
_MESSAGE_TABLES_DDL = """
CREATE TABLE IF NOT EXISTS messages (
    msg_id     INTEGER PRIMARY KEY,
    group_id   INTEGER NOT NULL,
    ts         INTEGER NOT NULL,
    sender_uid TEXT,
    content    TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS sender_names (
    group_id   INTEGER NOT NULL,
    uid        TEXT    NOT NULL,
    group_nick TEXT,
    qq_nick    TEXT,
    uin        INTEGER,
    in_group   INTEGER,
    PRIMARY KEY (group_id, uid)
);
"""


def _ensure_message_tables(conn: sqlite3.Connection) -> None:
    """让查询层 JOIN 得起来。幂等。"""
    conn.executescript(_MESSAGE_TABLES_DDL)


def _seed_messages(conn: sqlite3.Connection) -> None:
    """造一张最小的 messages + sender_names，形状与 export.py 产出的完全一致。"""
    _ensure_message_tables(conn)
    conn.executemany(
        "INSERT INTO messages VALUES (?,?,?,?,?)",
        [
            (1, 100, 1000, "u_a", "明天有讲座"),
            (2, 100, 2000, "u_b", "收到"),
            (3, 200, 3000, "u_a", "二手自行车出"),
        ],
    )
    conn.executemany(
        "INSERT INTO sender_names VALUES (?,?,?,?,?,?)",
        [
            (100, "u_a", "张三", "三三", 111, 0),
            (100, "u_b", None, "李四", 222, 0),
            (200, "u_a", "张三", "三三", 111, 0),
        ],
    )
    conn.commit()


def test_ensure_schema_is_idempotent(memdb):
    store.ensure_schema(memdb)
    store.ensure_schema(memdb)
    names = {
        r[0]
        for r in memdb.execute("SELECT name FROM sqlite_master WHERE type='table'")
    }
    assert {"items", "item_sources", "refine_runs"} <= names


# ── 列级迁移（M7：`items.start_ts`）──────────────────────────────
#
# ⚠️ 这一组**必须造一个"迁移前形态"的库**，不能用 `memdb`：
# `memdb` 是空的，`ensure_schema` 会走 `CREATE TABLE` 一步到位、列本来就在
# ⇒ 迁移那几行**一次都不执行**，测试全绿地放过一个坏迁移。
# 实测踩到过：迁移放在 `executescript` **之后**时，`SCHEMA_DDL` 里的
# `CREATE INDEX ... ON items(start_ts)` 会炸在 `no such column`，
# 而**当时全部 867 条测试没有一条变红**。

_OLD_ITEMS_DDL = """
CREATE TABLE items (
    item_id     INTEGER PRIMARY KEY,
    kind        TEXT    NOT NULL,
    title       TEXT    NOT NULL,
    detail      TEXT,
    event_ts    INTEGER NOT NULL,
    deadline_ts INTEGER,
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
"""


def _legacy_db() -> sqlite3.Connection:
    """一个**迁移前**的库：有 items 表（旧列），且里面有一条数据。"""
    conn = sqlite3.connect(":memory:", uri=True)
    conn.executescript(_OLD_ITEMS_DDL)
    conn.execute(
        "INSERT INTO items (kind, title, detail, event_ts, deadline_ts,"
        " group_id, actor_uid, place, links, amount, confidence,"
        " model, prompt_ver, created_at)"
        " VALUES ('notice','旧条目',NULL,1000,NULL,100,NULL,NULL,'[]',NULL,"
        " 0.9,'m','v3',1000)"
    )
    conn.commit()
    return conn


def test_ensure_schema_adds_start_ts_to_a_legacy_db():
    """⭐ 老库跑 `ensure_schema` ⇒ 自动补出 `start_ts`，且**数据一条不丢**。

    ⚠️ 判据三半，缺一不可：列在、索引建成、旧行还在。
    只钉"列在"的话，一个把整表重建（丢数据）的实现照样绿。
    """
    conn = _legacy_db()
    assert "start_ts" not in {r[1] for r in conn.execute("PRAGMA table_info(items)")}

    store.ensure_schema(conn)

    cols = {r[1] for r in conn.execute("PRAGMA table_info(items)")}
    assert "start_ts" in cols
    indexes = {
        r[0]
        for r in conn.execute("SELECT name FROM sqlite_master WHERE type='index'")
    }
    assert "items_start" in indexes, "迁移必须发生在 DDL 之前，否则索引建不出来"
    # 数据一条不丢，且旧的 start_ts 是 NULL（不是 0 —— 0 是 1970，会被当成真实时刻）
    row = conn.execute("SELECT title, start_ts FROM items").fetchall()
    assert row == [("旧条目", None)]


def test_ensure_schema_migration_is_idempotent():
    """老库连跑两次不许抛 `duplicate column name`。"""
    conn = _legacy_db()
    store.ensure_schema(conn)
    store.ensure_schema(conn)
    assert "start_ts" in {r[1] for r in conn.execute("PRAGMA table_info(items)")}


def test_save_items_round_trips_start_ts(memdb):
    """⭐ `start_ts` 真的落库、真的读得回来。

    ⚠️ 不写这条的话，INSERT 里少一个占位符（或位置错开）会让**列数对不上** ——
    而那种错在 SQLite 上是 `OperationalError`，只有在真跑 `save_items` 时
    才现形；构造数据时看不出来。
    """
    store.ensure_schema(memdb)
    _seed_messages(memdb)
    started = 1700003600
    store.save_items(
        memdb,
        [
            ExtractedItem(
                kind="notice", title="讲座", detail=None, event_ts=1700000000,
                deadline_ts=None, group_id=100, actor_uid=None, place=None,
                links=(), amount=None, confidence=0.9, src_msg_ids=(1,),
                start_ts=started,
            )
        ],
        model="m", prompt_ver="v4",
    )
    got = memdb.execute("SELECT start_ts FROM items").fetchone()[0]
    assert got == started


def test_save_items_without_start_ts_writes_null(memdb):
    """⚠️ 不给 `start_ts` ⇒ 落 **NULL**，不是 0。

    ⚠️ 0 是 1970-01-01，是一个**真实时刻**：它会让这条条目在按活动时刻排序时
    排到最前面，而用户看到的是"这条日程在 1970 年"。NULL 才是"没有这个信息"。
    """
    store.ensure_schema(memdb)
    _seed_messages(memdb)
    store.save_items(
        memdb,
        [
            ExtractedItem(
                kind="notice", title="失物招领", detail=None, event_ts=1700000000,
                deadline_ts=None, group_id=100, actor_uid=None, place=None,
                links=(), amount=None, confidence=0.9, src_msg_ids=(1,),
            )
        ],
        model="m", prompt_ver="v4",
    )
    assert memdb.execute("SELECT start_ts FROM items").fetchone()[0] is None


def test_pending_messages_resolves_sender_name(memdb):
    _seed_messages(memdb)
    store.ensure_schema(memdb)
    pending = store.pending_messages(memdb)
    assert len(pending) == 3
    # 群昵称优先于 QQ 昵称
    assert pending[0].sender == "张三"
    # 群昵称为空时退回 QQ 昵称
    assert pending[1].sender == "李四"


def test_pending_messages_orders_by_time(memdb):
    _seed_messages(memdb)
    store.ensure_schema(memdb)
    assert [m.msg_id for m in store.pending_messages(memdb)] == [1, 2, 3]


def test_pending_messages_skips_already_run(memdb):
    _seed_messages(memdb)
    store.ensure_schema(memdb)
    store.record_run(memdb, [1], status=store.STATUS_OK, prompt_ver="v1")
    assert [m.msg_id for m in store.pending_messages(memdb)] == [2, 3]


def test_pending_messages_retries_error_rows(memdb):
    """⚠️ error 行必须被重试，不能当成「已处理」（Task 7 审查实测抓出）。

    写成 `NOT IN (SELECT msg_id FROM refine_runs)` 的话，一次网络抖动
    （429/超时）就会把那批消息**永久跳过**，且没有任何自动重试机制——
    静默的永久数据丢失。error 的语义是「试过但没成功」，不是「已处理」。
    """
    _seed_messages(memdb)
    store.ensure_schema(memdb)
    store.record_run(memdb, [1], status=store.STATUS_OK, prompt_ver="v1")
    store.record_run(memdb, [2], status=store.STATUS_ERROR, prompt_ver="v1", err="429")
    store.record_run(memdb, [3], status=store.STATUS_DISCARDED, prompt_ver="v1")

    # 只有 error 那条会被重新取出——ok 与 discarded 都算真处理过了
    assert [m.msg_id for m in store.pending_messages(memdb)] == [2]


def test_redo_returns_everything(memdb):
    _seed_messages(memdb)
    store.ensure_schema(memdb)
    store.record_run(memdb, [1, 2, 3], status=store.STATUS_OK, prompt_ver="v1")
    assert len(store.pending_messages(memdb, redo=True)) == 3


def test_pending_messages_respects_window_and_limit(memdb):
    _seed_messages(memdb)
    store.ensure_schema(memdb)
    assert [m.msg_id for m in store.pending_messages(memdb, since=2000)] == [2, 3]
    assert [m.msg_id for m in store.pending_messages(memdb, until=2000)] == [1]
    assert len(store.pending_messages(memdb, limit=2)) == 2


def test_save_items_writes_sources(memdb):
    _seed_messages(memdb)
    store.ensure_schema(memdb)
    item = ExtractedItem(
        kind="activity",
        title="西太湖报告厅有讲座",
        detail=None,
        event_ts=1000,
        deadline_ts=None,
        group_id=100,
        actor_uid="u_a",
        place="西太湖报告厅",
        links=(),
        amount=None,
        confidence=0.9,
        src_msg_ids=(1,),
    )
    assert store.save_items(memdb, [item], model="m", prompt_ver="v1", now=42) == 1

    row = memdb.execute(
        "SELECT kind, title, deadline_ts, actor_uid, created_at FROM items"
    ).fetchone()
    assert row == ("activity", "西太湖报告厅有讲座", None, "u_a", 42)
    assert memdb.execute("SELECT item_id, msg_id FROM item_sources").fetchall() == [(1, 1)]


def test_save_items_roundtrips_links(memdb):
    _seed_messages(memdb)
    store.ensure_schema(memdb)
    item = ExtractedItem(
        kind="notice", title="报名", detail=None, event_ts=1000, deadline_ts=None,
        group_id=100, actor_uid=None, place=None,
        links=("https://example.com/a", "https://example.com/b"),
        amount=None, confidence=0.5, src_msg_ids=(1,),
    )
    store.save_items(memdb, [item], model="m", prompt_ver="v1")
    raw = memdb.execute("SELECT links FROM items").fetchone()[0]
    assert "example.com/a" in raw and "example.com/b" in raw


def test_record_run_is_idempotent(memdb):
    store.ensure_schema(memdb)
    store.record_run(memdb, [7], status=store.STATUS_OK, prompt_ver="v1", item_count=2)
    store.record_run(memdb, [7], status=store.STATUS_DISCARDED, prompt_ver="v2")
    rows = memdb.execute("SELECT msg_id, status, prompt_ver FROM refine_runs").fetchall()
    assert rows == [(7, "discarded", "v2")]


def test_item_sources_supports_reverse_lookup(memdb):
    """『这条消息产出了哪条 item』必须可查——避免重复抽取靠它。"""
    _seed_messages(memdb)
    store.ensure_schema(memdb)
    item = ExtractedItem(
        kind="notice", title="t", detail=None, event_ts=1000, deadline_ts=None,
        group_id=100, actor_uid=None, place=None, links=(), amount=None,
        confidence=0.5, src_msg_ids=(1, 2),
    )
    store.save_items(memdb, [item], model="m", prompt_ver="v1")
    by_msg = memdb.execute(
        "SELECT item_id FROM item_sources WHERE msg_id = ?", (2,)
    ).fetchall()
    assert by_msg == [(1,)]


# ── 日报持久化（M2 Task 1）─────────────────────────────────


def _seed_items(conn: sqlite3.Connection) -> None:
    """造三条 item：两条在窗口内、一条在窗口外，含一条低置信度。"""
    store.ensure_schema(conn)
    conn.executemany(
        "INSERT INTO items (item_id, kind, title, detail, event_ts, deadline_ts,"
        " group_id, actor_uid, place, links, amount, confidence, model,"
        " prompt_ver, created_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        [
            (1, "notice", "体检表", "10月8日前交", 3000, 4000, 100, None,
             None, "[]", None, 0.9, "m", "v2", 1),
            (2, "activity", "讲座", None, 2000, None, 200, None,
             "报告厅", "[]", None, 0.3, "m", "v2", 1),
            (3, "notice", "窗口外的", None, 9999, None, 100, None,
             None, "[]", None, 0.9, "m", "v2", 1),
        ],
    )
    conn.commit()


def test_window_items_filters_and_orders(memdb):
    _seed_messages(memdb)
    _seed_items(memdb)

    got = store.window_items(memdb, since=1000, until=5000)

    assert [it.item_id for it in got] == [2, 1]   # 按 event_ts 升序
    assert got[0].title == "讲座"
    assert got[1].deadline_ts == 4000
    assert got[1].confidence == 0.9


def test_window_items_empty_when_no_items(memdb):
    _seed_messages(memdb)
    store.ensure_schema(memdb)

    assert store.window_items(memdb, since=1000, until=5000) == []


def test_window_stats_counts_messages_and_groups(memdb):
    _seed_messages(memdb)   # ts=1000(群100) / 2000(群100) / 3000(群200)

    assert store.window_stats(memdb, since=1000, until=4000) == (3, 2)
    assert store.window_stats(memdb, since=1000, until=2500) == (2, 1)
    assert store.window_stats(memdb, since=5000, until=6000) == (0, 0)


def test_save_digest_returns_id_and_links_items(memdb):
    _seed_items(memdb)

    digest_id = store.save_digest(
        memdb, window_from=1000, window_to=5000, body_md="# 日报",
        model="m", prompt_ver="v1", item_ids=[1, 2], now=12345,
    )

    assert digest_id > 0
    # find_digest 已按审查裁决删除（零生产调用方，见修复轮次 1）——直接查表。
    assert memdb.execute(
        "SELECT digest_id, body_md FROM digests WHERE window_from=? AND window_to=?",
        (1000, 5000),
    ).fetchone() == (digest_id, "# 日报")
    linked = memdb.execute(
        "SELECT item_id FROM digest_items WHERE digest_id=? ORDER BY item_id",
        (digest_id,),
    ).fetchall()
    assert [r[0] for r in linked] == [1, 2]


def test_save_digest_replaces_same_window(memdb):
    """同一天跑两次必须是替换，不是堆叠——否则会出两篇同日日报。"""
    _seed_items(memdb)

    store.save_digest(
        memdb, window_from=1000, window_to=5000, body_md="# 第一版",
        model="m", prompt_ver="v1", item_ids=[1, 2],
    )
    store.save_digest(
        memdb, window_from=1000, window_to=5000, body_md="# 第二版",
        model="m", prompt_ver="v1", item_ids=[1],
    )

    # ⚠️ 不要断言「两次的 digest_id 不同」——SQLite 的 INTEGER PRIMARY KEY
    # 在表被删空后会**复用 rowid**，这里两次都会拿到 1，那样断言会假红
    # （规划期实测踩到过）。要守的是「只剩一篇、旧关联行清干净、内容是新的」。
    rows = memdb.execute("SELECT COUNT(*) FROM digests").fetchone()[0]
    assert rows == 1
    # 第二次只挂了 item 1，所以 item 2 的关联必须已经被清掉。
    # ⚠️ 也不能用「digest_items WHERE digest_id=? 的条数」判孤儿：id 被复用了，
    # 那个查询会查到**新日报自己的**关联行，测不出任何东西。
    linked = [
        r[0]
        for r in memdb.execute(
            "SELECT item_id FROM digest_items ORDER BY item_id"
        ).fetchall()
    ]
    assert linked == [1]
    # 真正的孤儿判据：关联行指向一篇不存在的日报
    orphan = memdb.execute(
        "SELECT COUNT(*) FROM digest_items"
        " WHERE digest_id NOT IN (SELECT digest_id FROM digests)"
    ).fetchone()[0]
    assert orphan == 0
    assert memdb.execute(
        "SELECT body_md FROM digests WHERE window_from=? AND window_to=?",
        (1000, 5000),
    ).fetchone()[0] == "# 第二版"


def test_ensure_schema_is_idempotent_for_digests(memdb):
    """ensure_schema 每次 refine/digest 都调，必须能重复执行。"""
    store.ensure_schema(memdb)
    store.ensure_schema(memdb)

    names = {
        r[0] for r in memdb.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        )
    }
    assert {"digests", "digest_items"} <= names


# ── 修复轮次 1：补守卫（F1/F2/F4/F5）─────────────────────────


def _insert_items(conn: sqlite3.Connection, rows: list[tuple]) -> None:
    """按 (item_id, kind, title, detail, event_ts, deadline_ts, group_id,
    place, amount, confidence) 造条目。只给边界/映射用例用。"""
    conn.executemany(
        "INSERT INTO items (item_id, kind, title, detail, event_ts, deadline_ts,"
        " group_id, actor_uid, place, links, amount, confidence, model,"
        " prompt_ver, created_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        [
            (iid, kind, title, detail, ts, dl, gid, None, place, "[]", amount,
             conf, "m", "v1", 1)
            for (iid, kind, title, detail, ts, dl, gid, place, amount, conf)
            in rows
        ],
    )
    conn.commit()


def test_window_items_bounds_are_half_open(memdb):
    """窗口是半开区间 [since, until)：下界**含**、上界**不含**。

    ⚠️ 这条守的是「跨日不重复计数」。``day_window`` 是 ``[今日00:00, 明日00:00)``，
    今天的 ``until`` 正好是明天的 ``since``——上界一旦写成闭区间，边界那条
    条目会在昨天和今天的日报里**各出现一次**：数字对不上、条目重复，
    而且是静默的，没有任何报错。
    """
    store.ensure_schema(memdb)
    _insert_items(memdb, [
        (11, "notice", "下界外", None, 999, None, 100, None, None, 0.9),
        (12, "notice", "恰在下界", None, 1000, None, 100, None, None, 0.9),
        (13, "notice", "窗口内", None, 4999, None, 100, None, None, 0.9),
        (14, "notice", "恰在上界", None, 5000, None, 100, None, None, 0.9),
        (15, "notice", "上界外", None, 5001, None, 100, None, None, 0.9),
    ])

    got = store.window_items(memdb, since=1000, until=5000)

    # 边界两条的决定性：12 含（>= 不是 >）、14 不含（< 不是 <=）
    assert [it.item_id for it in got] == [12, 13]


def test_window_stats_bounds_are_half_open(memdb):
    """``window_stats`` 的窗口同样必须半开——否则当日消息数会跨日重复计。

    种子故意放在 ts=9999..20001，与 ``_seed_messages`` 的 1000/2000/3000
    完全错开，所以不需要删任何既有行。
    """
    _seed_messages(memdb)
    memdb.executemany(
        "INSERT INTO messages VALUES (?,?,?,?,?)",
        [
            (901, 100, 9999, "u_a", "下界外"),
            (902, 100, 10000, "u_a", "恰在下界"),
            (903, 100, 19999, "u_a", "窗口内"),
            (904, 200, 20000, "u_a", "恰在上界"),
            (905, 200, 20001, "u_a", "上界外"),
        ],
    )
    memdb.commit()

    # 2 条消息、同属群 100 → (2, 1)：上界闭区间会变 (3, 2)、下界开区间会变 (1, 1)
    assert store.window_stats(memdb, since=10000, until=20000) == (2, 1)


def test_window_items_orders_by_event_ts_ascending(memdb):
    """按 ``event_ts`` 升序——即使 ``item_id`` 顺序与之相同也不能退化成 id 序。

    ⚠️ brief 原来的种子（id 1→ts 3000、id 2→ts 2000）里 **id 序与 ts 序恰好相反**，
    于是 ``ORDER BY item_id DESC`` 与正确实现返回同一个 ``[2, 1]``，变异抓不住。
    本条刻意让 **id 序与 ts 序相同**，把这条路堵上。
    """
    store.ensure_schema(memdb)
    _insert_items(memdb, [
        (21, "notice", "最早", None, 1000, None, 100, None, None, 0.9),
        (22, "notice", "居中", None, 2000, None, 100, None, None, 0.9),
        (23, "notice", "最晚", None, 3000, None, 100, None, None, 0.9),
    ])

    got = store.window_items(memdb, since=0, until=9999)

    assert [it.item_id for it in got] == [21, 22, 23]


def test_window_items_tie_break_is_item_id(memdb):
    """并列 ``event_ts`` 的条目按 ``item_id`` 升序——守 ``(event_ts, item_id)`` 契约。

    种子两条 ``event_ts`` 相同、``group_id`` 不同、``item_id`` 与群号顺序相反，
    对齐真实库的形状（审查实测：53 个有 item 的本地日里约 8 天存在 ``event_ts`` 撞车）。

    ⚠️ **诚实标注（实测，非推断）：只删掉 ORDER BY 里 ``item_id ASC`` 这一个子句，
    本测试抓不住，而且没有任何种子能抓住。**

    原因是 ``item_id`` 就是 rowid，而本函数的 SELECT 取全部 10 列、用不上覆盖索引
    （实测 plan = ``SCAN items`` + ``USE TEMP B-TREE``），并列键在临时 B 树里保持
    rowid 序，结果与显式 tie-break 完全相同。
    反例才是陷阱：若把 SELECT 收窄成只取 ``item_id``，plan 变成
    ``SCAN items USING COVERING INDEX items_group_ts``，并列序改由群号决定，
    此时两者才会分道扬镳（实测 ``[9, 3]`` vs ``[3, 9]``）——但那是另一种查询形状，
    不是 ``window_items``。

    所以保留 ``item_id ASC`` 是**纵深防御**：一旦将来 SELECT 收窄到能被
    ``items_group_ts`` 覆盖，或 SQLite 的排序稳定性假设变化，它就是决定性的一行。
    本测试真正守住的，是「排序键是 ``event_ts`` 而不是别的」这一层。
    """
    store.ensure_schema(memdb)
    _insert_items(memdb, [
        (9, "notice", "群 100 的", None, 2000, None, 100, None, None, 0.9),
        (3, "notice", "群 300 的", None, 2000, None, 300, None, None, 0.9),
    ])

    got = store.window_items(memdb, since=0, until=9999)

    assert [it.item_id for it in got] == [3, 9]
    # 契约形式：返回序必须已按 (event_ts, item_id) 排好
    assert got == sorted(got, key=lambda it: (it.event_ts, it.item_id))


def test_window_items_maps_every_field(memdb):
    """逐字段核对 ``WindowItem(*row)`` 的 10 个位置映射。

    ⚠️ 这是本模块**真正会静默错配**的那条路：``kind`` 与 ``group_id`` 是
    ``build_rows`` 锚点（分区与署名群）的来源，``place`` / ``amount`` 直接进渲染。
    原来只断言了 4/10 个字段（item_id/title/deadline_ts/confidence），
    所以把 SELECT 里的 ``place, amount`` 对调、或将来 ``items`` 加列后顺序没对齐，
    测试会全绿，而日报的分区、群名、地点、金额会静默错乱。

    种子的每个字段都取**互不相同**的值，任何两列对调都会撞红。
    """
    store.ensure_schema(memdb)
    _insert_items(memdb, [
        (7, "activity", "讲座", "10月8日前交", 2000, 4000, 200, "报告厅",
         "免费", 0.75),
    ])

    got = store.window_items(memdb, since=0, until=9999)

    assert len(got) == 1
    it = got[0]
    assert it.item_id == 7
    assert it.kind == "activity"
    assert it.title == "讲座"
    assert it.detail == "10月8日前交"
    assert it.event_ts == 2000
    assert it.deadline_ts == 4000
    assert it.group_id == 200
    assert it.place == "报告厅"
    assert it.amount == "免费"
    assert it.confidence == 0.75


def test_save_digest_records_provenance_columns(memdb):
    """``model`` / ``prompt_ver`` / ``created_at`` 必须真的按位写进去。

    ⚠️ 顶到 Global Constraint 11：改了提示词要能说清哪批产出是哪一版。
    原来 ``now=12345`` 传了却**从没断言过**，且三列无一被读回——
    ``model`` 与 ``prompt_ver`` 对调、``created_at`` 写死成 0，全都不会报警，
    溯源能力形同虚设。

    model 与 prompt_ver 取**类型不同、值不相似**的字符串，对调必撞红。
    """
    _seed_items(memdb)

    digest_id = store.save_digest(
        memdb, window_from=1000, window_to=5000, body_md="# 日报",
        model="qwen3.5-test", prompt_ver="digest-v9", item_ids=[1], now=12345,
    )

    row = memdb.execute(
        "SELECT model, prompt_ver, created_at FROM digests WHERE digest_id=?",
        (digest_id,),
    ).fetchone()
    assert row == ("qwen3.5-test", "digest-v9", 12345)


# ── 源消息正文（M2 Task 6：截止日核验）─────────────────────


def test_item_sources_text_joins_all_messages(memdb):
    _seed_messages(memdb)          # msg 1/2/3
    store.ensure_schema(memdb)
    conn = memdb
    conn.execute(
        "INSERT INTO items (item_id, kind, title, detail, event_ts, deadline_ts,"
        " group_id, actor_uid, place, links, amount, confidence, model,"
        " prompt_ver, created_at) VALUES (1,'notice','t',NULL,1000,NULL,100,"
        " NULL,NULL,'[]',NULL,0.9,'m','v2',1)"
    )
    conn.executemany(
        "INSERT INTO item_sources (item_id, msg_id) VALUES (?,?)", [(1, 1), (1, 3)]
    )
    conn.commit()

    got = store.item_sources_text(conn, [1])

    assert set(got) == {1}
    assert "明天有讲座" in got[1] and "二手自行车出" in got[1]


def test_item_sources_text_skips_items_without_sources(memdb):
    _seed_messages(memdb)
    store.ensure_schema(memdb)

    assert store.item_sources_text(memdb, [999]) == {}


def test_item_sources_text_empty_input(memdb):
    store.ensure_schema(memdb)

    assert store.item_sources_text(memdb, []) == {}


# ── 空窗日的抽取覆盖自证（Task 9，用户验收后追加）──────────────


def test_window_refine_coverage_counts_recorded_messages(memdb):
    _seed_messages(memdb)          # 3 条消息
    store.ensure_schema(memdb)
    store.record_run(memdb, [1, 2], status=store.STATUS_DISCARDED, prompt_ver="v2")

    assert store.window_refine_coverage(memdb, since=1000, until=4000) == (3, 2)


def test_window_refine_coverage_empty_window(memdb):
    _seed_messages(memdb)
    store.ensure_schema(memdb)

    assert store.window_refine_coverage(memdb, since=9000, until=9999) == (0, 0)


def test_window_refine_coverage_ignores_error_rows(memdb):
    """⚠️ ``status='error'`` 不算「已抽取」——与 refine 的重试口径对齐。

    ``pending_messages(redo=False)`` 的语义是「试过但没成功 ≠ 已处理」，那批消息
    下次还会被重新取出来跑。首版只查「有没有 refine_runs 行」，于是「一批失败了」
    会被日报当成「抽取完了、确实没事」——又一个假话入口。

    变异反证：把 `WHERE status != ?` 去掉 → 本条红（(3, 3) 而不是 (3, 2)）。
    """
    _seed_messages(memdb)          # 3 条消息
    store.ensure_schema(memdb)
    store.record_run(memdb, [1, 2], status=store.STATUS_DISCARDED, prompt_ver="v2")
    store.record_run(memdb, [3], status=store.STATUS_ERROR, prompt_ver="v2",
                     err="429")

    assert store.window_refine_coverage(memdb, since=1000, until=4000) == (3, 2)


# ── 全库时间跨度：分清「这天没数据」与「这天没人说话」（Task 11）──────


def test_message_span_returns_min_and_max(memdb):
    _seed_messages(memdb)          # ts = 1000 / 2000 / 3000

    assert store.message_span(memdb) == (1000, 3000)


def test_message_span_none_when_no_messages(memdb):
    _seed_messages(memdb)
    memdb.execute("DELETE FROM messages")

    assert store.message_span(memdb) is None


def test_message_span_ignores_zero_ts_rows(memdb):
    """⚠️ ``ts=0`` 的脏行不许把 ``MIN(ts)`` 拉走（修复轮次 1）。

    真库里有 14 条 1970 的脏数据。它们把 ``MIN(ts)`` 按在 0 时，
    「窗口早于全部数据」那一侧就永远判不出来（``window[1] <= 0`` 为假）——
    于是查一个**根本没数据**的日期，日报会说「这天真的没人说话」。
    """
    _seed_messages(memdb)                      # ts = 1000 / 2000 / 3000
    memdb.execute(
        "INSERT INTO messages VALUES (?,?,?,?,?)", (4, 100, 0, "u_x", "1970 脏行")
    )

    span = store.message_span(memdb)

    assert span == (1000, 3000)                # 不是 (0, 3000)
    assert span[0] != 0


def test_message_span_ignores_negative_ts_rows(memdb):
    """负数 ts 同样不该被当成「最早」——同一类脏数据、同一个过滤器管住。"""
    _seed_messages(memdb)
    memdb.execute(
        "INSERT INTO messages VALUES (?,?,?,?,?)", (4, 100, -1, "u_x", "脏行")
    )

    assert store.message_span(memdb) == (1000, 3000)


def test_message_span_none_when_only_dirty_rows(memdb):
    """全是脏行 = 一条可用数据也没有 → None（说「没数据」而不是「没人说话」）。"""
    _seed_messages(memdb)
    memdb.execute("DELETE FROM messages")
    memdb.execute(
        "INSERT INTO messages VALUES (?,?,?,?,?)", (4, 100, 0, "u_x", "1970 脏行")
    )

    assert store.message_span(memdb) is None


# ── 截止日回填：读哪些 / 清哪些（M3 Task 1）────────────────────


def _insert_item(
    conn: sqlite3.Connection,
    item_id: int,
    *,
    kind: str = "notice",
    title: str | None = None,
    event_ts: int = 1000,
    deadline_ts: int | None = None,
) -> None:
    """造一条最小 item（只为截止日回填用例服务）。

    ⚠️ 本文件既有的插桩是 `_seed_items`（固定三行）与 `_insert_items(conn, rows)`
    （元组列表），**没有** brief 里写的 `_insert_item`。这里按 `_seed_items` 的
    列清单照补一个单行版本——**列名一个不差**，不自己发明形状。

    M3 Task 2 扩展：加了 `kind` / `title` / `event_ts` 三个关键字参数，供搜索用例
    构造不同类目与时间的行。三个默认值与 T1 原版**逐字相同**（`"notice"` /
    `f"条目{item_id}"` / `1000`），所以 T1 既有的三处调用产出的行一个字节都没变。

    顺带立起 messages / sender_names：查询层（`search_items` / `get_item` /
    `digest_items`）LEFT JOIN 后者取署名，而 `store.ensure_schema` 不建它们
    （真库里由 export.py 产出）。不立的话测试会炸 `no such table: sender_names`。
    """
    _ensure_message_tables(conn)
    conn.execute(
        "INSERT INTO items (item_id, kind, title, detail, event_ts, deadline_ts,"
        " group_id, actor_uid, place, links, amount, confidence, model,"
        " prompt_ver, created_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (item_id, kind, title if title is not None else f"条目{item_id}", None,
         event_ts, deadline_ts, 100, None, None, "[]", None, 0.9, "m", "v1", 1),
    )
    conn.commit()


def _insert_message(
    conn: sqlite3.Connection,
    msg_id: int,
    *,
    content: str = "",
    ts: int = 1000,
    group_id: int = 100,
    sender_uid: str | None = None,
) -> None:
    """造一条源消息（`source_messages` 的「点回原文」用例服务）。

    形状照 `_seed_messages` 的列清单，只是单行、可指定内容。
    """
    _ensure_message_tables(conn)
    conn.execute(
        "INSERT INTO messages VALUES (?,?,?,?,?)",
        (msg_id, group_id, ts, sender_uid, content),
    )
    conn.commit()


def test_items_with_deadline_only_returns_rows_that_have_one():
    conn = sqlite3.connect(":memory:", uri=True)
    store.ensure_schema(conn)
    _insert_item(conn, 1, deadline_ts=1000)
    _insert_item(conn, 2, deadline_ts=None)
    assert store.items_with_deadline(conn) == [(1, 1000)]


def test_clear_deadlines_only_touches_that_column():
    """两个性质一起守：**只动那一列**，且**只动传入的那些行**。

    ⚠️ 第二个性质是审查退回后补的（Task 1 修复轮次 1）。初版只插了**一条** item，
    于是「把全表 deadline_ts 都置 NULL」（= 完全忽略 `item_ids` 参数）这种实现
    照样全绿——**它守了列，没守行**。这不是假想：`clear_deadlines` 是本里程碑
    唯一一处对真库的不可逆写操作（`vigil deadline-audit --apply` 直接改
    `data/vigil.db`），忽略参数就等于**静默清掉全库的截止日**。
    """
    conn = sqlite3.connect(":memory:", uri=True)
    store.ensure_schema(conn)
    _insert_item(conn, 1, deadline_ts=1000)
    _insert_item(conn, 2, deadline_ts=2000)
    # item 1 的 deadline_ts 会变；item 2 **一列都不许变**（除主键外全列快照）
    other_cols = (
        "kind, title, detail, event_ts, group_id, actor_uid, place, links,"
        " amount, confidence, model, prompt_ver, created_at"
    )
    before2 = conn.execute(
        f"SELECT {other_cols} FROM items WHERE item_id = 2"
    ).fetchone()

    assert store.clear_deadlines(conn, [1]) == 1

    assert conn.execute(
        "SELECT deadline_ts FROM items WHERE item_id = 1"
    ).fetchone()[0] is None
    # ⚠️ 关键守卫：item 2 不在传入列表里，它的截止日必须**原样留着**。
    # 变异反证：把 `WHERE item_id IN (...)` 改成恒真，这一行立刻变红。
    assert conn.execute(
        "SELECT deadline_ts FROM items WHERE item_id = 2"
    ).fetchone()[0] == 2000
    assert conn.execute(
        f"SELECT {other_cols} FROM items WHERE item_id = 2"
    ).fetchone() == before2
    assert conn.execute("SELECT COUNT(*) FROM items").fetchone()[0] == 2, "不许删行"


def test_clear_deadlines_with_empty_list_is_a_noop():
    conn = sqlite3.connect(":memory:", uri=True)
    store.ensure_schema(conn)
    assert store.clear_deadlines(conn, []) == 0


# ── FTS5 搜索索引 + 只读查询层（M3 Task 2）──────────────────────


def test_search_finds_two_chinese_chars_via_like_fallback():
    """⭐ 双字词必须搜得到——这是本任务的头号陷阱。

    FTS5 trigram 对**短于 3 字符**的查询静默返回 0 条（不报错），
    而中文双字词（「选课」「讲座」）恰恰是最常见的查询。
    没有 LIKE 兜底的话，「搜不到」会被读成「库里没有」。
    """
    conn = sqlite3.connect(":memory:", uri=True)
    store.ensure_schema(conn)
    _insert_item(conn, 1, title="选课通知", kind="academic")
    _insert_item(conn, 2, title="失物招领", kind="lostfound")
    items, total = store.search_items(conn, q="选课")
    assert total == 1 and items[0].item_id == 1


def test_search_finds_three_chinese_chars_via_trigram():
    conn = sqlite3.connect(":memory:", uri=True)
    store.ensure_schema(conn)
    _insert_item(conn, 1, title="校园卡补办", kind="life")
    items, total = store.search_items(conn, q="校园卡")
    assert total == 1 and items[0].item_id == 1


def test_search_survives_fts5_syntax_characters():
    """用户输入直接喂 MATCH 会抛 OperationalError——必须被包成短语。

    ⚠️ **必须带断言 + 阳性对照**。「调用一下不抛异常」是空守卫：
    搜索整个坏掉（永远返回 0）时，它照样绿。
    """
    conn = sqlite3.connect(":memory:", uri=True)
    store.ensure_schema(conn)
    _insert_item(conn, 1, title="选课通知", kind="academic")
    for q in ["NOT", 'a"b', "(", "补退选 -卡", "a OR b"]:
        _, total = store.search_items(conn, q=q)
        assert total == 0, f"{q!r} 应 0 条，实际 {total}"
    # 阳性对照：这些字符**确实能被搜到**——否则上面那组 0 分不清
    # 「搜索坏了」与「确实没有」。
    _insert_item(conn, 2, title="通知（补）", kind="notice")
    _, total = store.search_items(conn, q="（补）")
    assert total == 1


def test_search_escapes_like_wildcards():
    """`%` 不转义就会匹配一切——那是个假数字。"""
    conn = sqlite3.connect(":memory:", uri=True)
    store.ensure_schema(conn)
    _insert_item(conn, 1, title="选课通知", kind="academic")
    _, total = store.search_items(conn, q="%")
    assert total == 0


def _seed_two_items_for_search(conn: sqlite3.Connection) -> None:
    """两行，且**第二行让「返回全表」区分得出来**（全表 = 2 ≠ 任何正确答案 1）。"""
    store.ensure_schema(conn)
    _insert_item(conn, 1, title="选课通知", kind="academic")
    _insert_item(conn, 2, title="失物招领", kind="lostfound")


def test_search_strips_nul_before_the_trigram_branch():
    """⚠️ 分支一（`len(q) >= 3` → FTS5）：NUL 会让**引号不闭合**。

    FTS5 解析器在 NUL 处截断，`_fts_phrase` 包出来的短语因此缺右引号
    → 未捕获的 `OperationalError: unterminated string` → T3 上线后就是一个 **500**。

    ⚠️ 阳性对照不是可选项：只断言「NUL 查询不抛异常」的话，
    「搜索整个坏掉、永远返 0」也照样绿。所以紧跟一个**不带 NUL 的同义查询**。
    """
    conn = sqlite3.connect(":memory:", uri=True)
    _seed_two_items_for_search(conn)

    assert store.search_items(conn, q="\x00选课通知")[1] == 1
    assert store.search_items(conn, q="选\x00课\x00通\x00知")[1] == 1
    # 阳性对照：剥掉 NUL 就是同一个查询，结果必须一致
    assert store.search_items(conn, q="选课通知")[1] == 1
    # 阴性对照：确实没有的词仍旧返回 0（守「不是永远返 1」）
    assert store.search_items(conn, q="\x00不存在的词条")[1] == 0


def test_search_strips_nul_before_the_like_branch():
    """⚠️ 分支二（`len(q) <= 2` → LIKE）：NUL 会让 pattern **截断成 `%`**。

    实测（不净化时，2 行的库里）：`q="\\x00课"` → `total=2`，
    而正确答案是 **1**（只有「选课通知」含「课」）。这比 500 危险得多——
    它是个**假数字**：页面说「共 2 条」，用户以为筛过了，其实一行都没筛。

    ⚠️ 阳性对照同上：每条 NUL 查询都配一条不带 NUL 的同义查询。
    """
    conn = sqlite3.connect(":memory:", uri=True)
    _seed_two_items_for_search(conn)

    # 正确答案 1；不净化时 LIKE 被截断成 '%' → 2（全表）
    assert store.search_items(conn, q="\x00课")[1] == 1
    assert store.search_items(conn, q="\x00选课")[1] == 1
    # 阳性对照
    assert store.search_items(conn, q="课")[1] == 1
    assert store.search_items(conn, q="选课")[1] == 1

    # ── 剥完为空串 = 不筛选（与既有「空 q 等于不筛选」契约一致）──
    # 这里与 `q=""` 同义、返回全表是**契约**而非 bug：用户没输入任何可搜的字符。
    assert store.search_items(conn, q="\x00\x00")[1] == store.search_items(conn, q="")[1] == 2


def test_search_filters_by_kind_and_window():
    conn = sqlite3.connect(":memory:", uri=True)
    store.ensure_schema(conn)
    _insert_item(conn, 1, kind="academic", event_ts=100)
    _insert_item(conn, 2, kind="job", event_ts=200)
    assert store.search_items(conn, kinds=["job"])[1] == 1
    assert store.search_items(conn, since=150)[1] == 1
    assert store.search_items(conn, until=150)[1] == 1


def test_search_is_newest_first():
    """信息流按时间倒着看。⚠️ 与 window_items 的 ASC 不同是**故意的**。"""
    conn = sqlite3.connect(":memory:", uri=True)
    store.ensure_schema(conn)
    _insert_item(conn, 1, event_ts=100)
    _insert_item(conn, 2, event_ts=200)
    items, _ = store.search_items(conn)
    assert [i.item_id for i in items] == [2, 1]


def test_source_messages_returns_the_linked_messages():
    conn = sqlite3.connect(":memory:", uri=True)
    store.ensure_schema(conn)
    _insert_item(conn, 1)
    _insert_message(conn, 11, content="原文在这里")
    conn.execute("INSERT INTO item_sources (item_id, msg_id) VALUES (1, 11)")
    srcs = store.source_messages(conn, 1)
    assert len(srcs) == 1 and srcs[0].content == "原文在这里"


def test_source_messages_of_unknown_item_is_empty():
    conn = sqlite3.connect(":memory:", uri=True)
    store.ensure_schema(conn)
    # ⚠️ 偏差（逃逸舱）：brief 原版没有这一行。`source_messages` JOIN `messages`，
    # 而 SQLite **在 prepare 阶段**就报 `no such table: messages`——哪怕 WHERE
    # 匹配不到任何行、结果本来就是空。必须先把表立起来，这条守的才是
    # 「未知 item 返回空」而不是「表不存在会炸」。
    _ensure_message_tables(conn)
    assert store.source_messages(conn, 999) == []


def test_digest_items_are_in_ascending_order_like_the_digest_body():
    conn = sqlite3.connect(":memory:", uri=True)
    store.ensure_schema(conn)
    _insert_item(conn, 1, event_ts=200)
    _insert_item(conn, 2, event_ts=100)
    conn.execute(
        "INSERT INTO digests (digest_id, window_from, window_to, body_md, model,"
        " prompt_ver, created_at) VALUES (1, 100, 300, '正文', 'm', 'v1', 1)"
    )
    conn.execute("INSERT INTO digest_items (digest_id, item_id) VALUES (1, 1), (1, 2)")
    assert [i.item_id for i in store.digest_items(conn, 1)] == [2, 1]


def test_kind_counts_groups_by_kind():
    conn = sqlite3.connect(":memory:", uri=True)
    store.ensure_schema(conn)
    _insert_item(conn, 1, kind="academic")
    _insert_item(conn, 2, kind="academic")
    _insert_item(conn, 3, kind="job")
    assert store.kind_counts(conn) == {"academic": 2, "job": 1}


# ── 查询层的补充守卫（brief 未覆盖的三个函数）────────────────────
#
# brief 的 10 个测试覆盖了 search_items / source_messages / kind_counts /
# digest_items，但 get_item / list_digests / **get_digest 一个测试都没有**——
# 而 get_digest 的 `DigestRow(*row)` 列序错位正是 brief 自己点名的
# 「本任务最容易犯的错」（SELECT 多加一列就静默错位，不抛任何异常）。
# T3 的 FastAPI 要直接把这几个函数的返回值序列化出去，所以补上最小守卫：
# 用**整个 dataclass 相等**断言，六个字段一个不抽查。


def test_get_item_maps_every_column_or_none():
    conn = sqlite3.connect(":memory:", uri=True)
    store.ensure_schema(conn)
    _ensure_message_tables(conn)
    conn.execute(
        "INSERT INTO sender_names VALUES (?,?,?,?,?,?)",
        (100, "u_a", "张三", "三三", 111, 0),
    )
    conn.execute(
        "INSERT INTO items (item_id, kind, title, detail, event_ts, deadline_ts,"
        " group_id, actor_uid, place, links, amount, confidence, model,"
        " prompt_ver, created_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (1, "job", "招兼职", "周末两小时", 1700, 1800, 100, "u_a", "三教",
         '["http://a"]', "200/天", 0.9, "m", "v1", 1),
    )
    _insert_message(conn, 11, content="招兼职，周末两小时")
    conn.execute("INSERT INTO item_sources (item_id, msg_id) VALUES (1, 11)")
    conn.commit()

    assert store.get_item(conn, 1) == store.ApiItem(
        item_id=1, kind="job", title="招兼职", detail="周末两小时", event_ts=1700,
        deadline_ts=1800, group_id=100, actor="张三", actor_uin=111, place="三教",
        amount="200/天", links=("http://a",), source_count=1,
    )
    assert store.get_item(conn, 999) is None


def test_get_digest_matches_the_row_field_by_field():
    """⚠️ brief 点名的头号实现陷阱：``DigestRow(*row)`` 靠**位置**对齐。

    ``SELECT`` 里多加一列（或用错顺序）不会抛异常，只会让六个字段整体串位——
    正文跑到窗口起点、时间戳跑到条数上，读起来还像正常 JSON。
    所以这里断言**整个 dataclass 相等**，而不是抽查一两个字段。
    变异反证：把 `get_digest` 的 SELECT 里任意两列对调，这条立刻变红。
    """
    conn = sqlite3.connect(":memory:", uri=True)
    store.ensure_schema(conn)
    conn.execute(
        "INSERT INTO digests (digest_id, window_from, window_to, body_md, model,"
        " prompt_ver, created_at) VALUES (7, 111, 222, '正文九号', 'm', 'v1', 333)"
    )
    _insert_item(conn, 1)
    _insert_item(conn, 2)
    conn.execute("INSERT INTO digest_items (digest_id, item_id) VALUES (7, 1), (7, 2)")
    conn.commit()

    assert store.get_digest(conn, 7) == store.DigestRow(
        digest_id=7, window_from=111, window_to=222, body_md="正文九号",
        created_at=333, item_count=2,
    )
    assert store.get_digest(conn, 999) is None


def test_search_total_is_independent_of_limit_and_offset():
    """⚠️ 契约（计划 §「契约空洞」，波 1 窄审查定的）：`total` = **符合筛选条件的
    总数**，与 `limit`/`offset` **无关**——不是当页条数。

    前端重度依赖它：`items.length < total` 决定「加载更多」按钮出不出来。
    一旦把 total 实现成 `len(rows)`，按钮要么永远不出现、要么空转永远点不完，
    而且**接口不报错**（数字看着还很像样）。
    """
    conn = sqlite3.connect(":memory:", uri=True)
    store.ensure_schema(conn)
    for i in (1, 2, 3):
        _insert_item(conn, i, event_ts=1000 + i)

    page1, total1 = store.search_items(conn, limit=2)
    assert total1 == 3 and [i.item_id for i in page1] == [3, 2]

    page2, total2 = store.search_items(conn, limit=2, offset=2)
    assert total2 == 3 and [i.item_id for i in page2] == [1]


def test_ensure_schema_rebuilds_the_index_for_rows_that_predate_it():
    """⚠️ **存量行**：触发器只管「索引建好之后」的新数据。

    这里模拟一个「索引还不存在」的老库（只建 items、直接塞一行），再跑
    ``ensure_schema``——那一行**不会**经过任何触发器，全靠 ``rebuild`` 捞进索引。
    变异反证：把 ``ensure_schema`` 里的 ``rebuild_search_index(conn)`` 删掉，
    这条立刻变红（而其余 12 个搜索用例**照样全绿**——它们都在 ensure_schema
    之后才插数据，触发器兜住了，这正是本条存在的理由）。
    """
    conn = sqlite3.connect(":memory:", uri=True)
    conn.executescript(store._ITEMS_DDL)   # 只有 items：索引尚不存在的状态
    _insert_item(conn, 1, title="校园卡补办")

    store.ensure_schema(conn)

    items, total = store.search_items(conn, q="校园卡")
    assert total == 1 and items[0].item_id == 1


def test_list_digests_is_newest_window_first():
    """日报列表按窗口倒序（最近的在前），且带上每条引用了多少条目。"""
    conn = sqlite3.connect(":memory:", uri=True)
    store.ensure_schema(conn)
    for did, wf in ((1, 100), (2, 200)):
        conn.execute(
            "INSERT INTO digests (digest_id, window_from, window_to, body_md, model,"
            " prompt_ver, created_at) VALUES (?,?,?,'正文','m','v1',1)",
            (did, wf, wf + 50),
        )
    _insert_item(conn, 1)
    conn.execute("INSERT INTO digest_items (digest_id, item_id) VALUES (2, 1)")
    conn.commit()

    got = store.list_digests(conn)
    assert [d.digest_id for d in got] == [2, 1]
    assert got[0] == store.DigestSummaryRow(
        digest_id=2, window_from=200, window_to=250, created_at=1, item_count=1
    )


# ── M4 T5：写入原子性 ──────────────────────────────────────────────


def _make_item(conn: sqlite3.Connection, *, msg_id: int) -> ExtractedItem:
    """造一条 ExtractedItem，来源挂在 ``msg_id`` 上。

    ⚠️ 偏差（逃逸舱）：brief 里这一行是 ``_make_item(memdb, msg_id=1)``，
    并注明「用本文件既有的构造夹具」——**本文件没有这个夹具**。既有的
    ``_insert_item`` / ``_seed_items`` 都是**直接 SQL 插行**、不返回对象，
    而 ``save_items`` 收的是 ``ExtractedItem``；本文件既有的「造对象」写法是
    **内联构造**（见 ``test_save_items_writes_sources``）。这里只是把同一形状
    收成一个函数，字段清单与那条既有用例逐字对齐。
    """
    _ensure_message_tables(conn)
    return ExtractedItem(
        kind="activity",
        title="西太湖报告厅有讲座",
        detail=None,
        event_ts=1000,
        deadline_ts=None,
        group_id=100,
        actor_uid="u_a",
        place="西太湖报告厅",
        links=(),
        amount=None,
        confidence=0.9,
        src_msg_ids=(msg_id,),
    )


def test_transaction_rolls_back_on_exception(memdb):
    """事务里抛异常 → 之前的写全部不见。"""
    store.ensure_schema(memdb)
    item = _make_item(memdb, msg_id=1)          # 用本文件既有的构造夹具

    with pytest.raises(RuntimeError):
        with store.transaction(memdb):
            store.save_items(memdb, [item], model="m", prompt_ver="v1", commit=False)
            raise RuntimeError("模拟崩在两步之间")

    assert memdb.execute("SELECT count(*) FROM items").fetchone()[0] == 0
    assert memdb.execute("SELECT count(*) FROM item_sources").fetchone()[0] == 0


def test_transaction_commits_on_success(memdb):
    store.ensure_schema(memdb)
    item = _make_item(memdb, msg_id=1)
    with store.transaction(memdb):
        store.save_items(memdb, [item], model="m", prompt_ver="v1", commit=False)
        store.record_run(memdb, [1], status=store.STATUS_OK,
                         prompt_ver="v1", commit=False)
    assert memdb.execute("SELECT count(*) FROM items").fetchone()[0] == 1
    assert memdb.execute("SELECT count(*) FROM refine_runs").fetchone()[0] == 1


class _InsertIntoDigestsFails(sqlite3.Connection):
    """写日报时让 ``INSERT INTO digests`` 抛 sqlite3.OperationalError。

    ⚠️ 偏差（逃逸舱）：brief 原版注入的是 ``memdb.execute = boom``——**做不到**，
    实测 ``AttributeError: 'sqlite3.Connection' object attribute 'execute' is
    read-only``（C 类型的实例属性只读，Python 3.13 / 本机实测）。
    改成**子类覆盖**：``factory=`` 让连接自带这个行为。注入点与 brief 想注入的
    位置完全一致（三步里的第三步 ``INSERT INTO digests``），抛的也仍是 brief
    指定的 ``sqlite3.OperationalError``（对照组：用触发器 ``RAISE(ABORT)`` 注入
    抛的是 ``IntegrityError``，与 brief 写死的异常类型对不上）。
    """

    fail_insert = False

    def execute(self, sql, *args):  # type: ignore[override]
        if self.fail_insert and sql.strip().startswith("INSERT INTO digests"):
            raise sqlite3.OperationalError("模拟写库失败")
        return super().execute(sql, *args)


def test_save_digest_is_atomic():
    """⭐ DELETE 与 INSERT 之间崩掉，**旧日报必须还在**。

    `save_digest` 是 先删 digest_items、再删 digests、最后 INSERT。
    三步不在一个事务里的话，崩在中间会让**当天日报凭空消失**——
    而 docs/digests/ 的文件还在，归档不变量（文件集合 == 窗口日期集合）
    当场破掉，且没有任何东西会报错。

    ⚠️ 这条不用 ``memdb`` 夹具：注入点需要 ``factory=`` 的子类连接
    （见 ``_InsertIntoDigestsFails`` 的说明），``memdb`` 给的是普通连接。
    连接本身仍是 ``sqlite3.connect(":memory:", uri=True)``——与夹具同一个形状。
    """
    conn = sqlite3.connect(":memory:", uri=True, factory=_InsertIntoDigestsFails)
    store.ensure_schema(conn)
    first = store.save_digest(conn, window_from=10, window_to=20,
                              body_md="第一版", model="m", prompt_ver="v1",
                              item_ids=[])
    assert conn.execute("SELECT count(*) FROM digests").fetchone()[0] == 1

    conn.fail_insert = True
    try:
        with pytest.raises(sqlite3.OperationalError):
            store.save_digest(conn, window_from=10, window_to=20,
                              body_md="第二版", model="m", prompt_ver="v1",
                              item_ids=[])
    finally:
        conn.fail_insert = False

    rows = conn.execute("SELECT digest_id, body_md FROM digests").fetchall()
    assert rows == [(first, "第一版")], "崩在 INSERT 时旧日报必须原样还在"


class _CommitFailsOnce(sqlite3.Connection):
    """第 1 次 ``commit()`` 抛 sqlite3.OperationalError，之后放行。

    ⚠️ 存在的理由同 `_InsertIntoDigestsFails`：C 类型的实例属性只读，
    注入只能走 `factory=` 子类。这里注入的是 **commit 本身**失败
    （磁盘满 / 库被锁死那种），而不是某一条 SQL 失败。
    """

    fail_next_commit = False

    def commit(self):  # type: ignore[override]
        if self.fail_next_commit:
            self.fail_next_commit = False
            raise sqlite3.OperationalError("模拟 commit 失败")
        return super().commit()


def test_transaction_rolls_back_when_commit_itself_fails():
    """⭐ commit **自己**失败时也要回滚。

    这是 `else: conn.commit()` 那种写法的洞：commit 在自己的保护圈**外**，
    它抛了没有任何人 rollback——那批**悬挂的写会留在连接上**，被**下一次**
    commit 顺手刷出去（真管线里的下一次就是 `_record_error` 的记账），
    于是「items 落库了、消息却记成 error」⇒ 下轮重抽 ⇒ **重复 items 且无声**。
    换句话说：`transaction()` docstring 里「要么都成、要么都不成」这句承诺，
    在 commit 自己失败时**曾经是假的**。

    审查复现的链条就是下面这两步：第 1 次 commit 失败（悬挂）→ 第 2 次成功
    （把上一批一起刷出去）。

    ⚠️ 与 `test_save_digest_is_atomic` 同因：不用 `memdb` 夹具，
    因为注入点需要 `factory=` 的子类连接。
    """
    conn = sqlite3.connect(":memory:", uri=True, factory=_CommitFailsOnce)
    store.ensure_schema(conn)
    item = _make_item(conn, msg_id=1)

    # ── 第 1 次事务：refine 成功路径的形状（落库 + 记账），但 commit 会炸 ──
    conn.fail_next_commit = True
    with pytest.raises(sqlite3.OperationalError):
        with store.transaction(conn):
            store.save_items(conn, [item], model="m", prompt_ver="v1", commit=False)
            store.record_run(conn, [1], status=store.STATUS_OK,
                             prompt_ver="v1", commit=False)

    # ── 第 2 次事务：模拟 `_record_error` 照常记账（它自己会 commit） ──
    with store.transaction(conn):
        store.record_run(conn, [2], status=store.STATUS_ERROR,
                         prompt_ver="v1", err="boom", commit=False)

    assert conn.execute("SELECT count(*) FROM items").fetchone()[0] == 0, \
        "commit 失败后悬挂的 items 不许被**下一次** commit 顺手刷出去"
    assert conn.execute("SELECT count(*) FROM item_sources").fetchone()[0] == 0, \
        "来源行同理：它和 items 是同一次事务里的"
    assert [r[0] for r in conn.execute(
        "SELECT msg_id FROM refine_runs ORDER BY msg_id"
    ).fetchall()] == [2], \
        "只该有第 2 次那笔记账——第 1 次那批（msg 1）必须跟着回滚"


# ── Task 3：批内去重 ─────────────────────────────────────────────


def _item(title="标题", *, event_ts=1_700_000_000, group_id=100, src=(1,), kind="notice"):
    from vigil.store import ExtractedItem

    return ExtractedItem(
        kind=kind, title=title, detail=None, event_ts=event_ts,
        deadline_ts=None, group_id=group_id, actor_uid="u_x", place=None,
        links=(), amount=None, confidence=0.9, src_msg_ids=src,
    )


def test_dedupe_batch_collapses_identical_items():
    """item 290/296 的真实形状：同批同标题同 event_ts 同群。"""
    out = store._dedupe_batch([_item(src=(11,)), _item(src=(11,))])
    assert len(out) == 1


def test_dedupe_batch_merges_sources():
    """⚠️ 重复项的来源取并集，不是丢掉第二条——否则 item_sources 少一行且无声。"""
    out = store._dedupe_batch([_item(src=(11,)), _item(src=(22,))])
    assert len(out) == 1
    assert out[0].src_msg_ids == (11, 22)


def test_dedupe_batch_keeps_distinct_titles():
    out = store._dedupe_batch([_item("甲"), _item("乙")])
    assert [i.title for i in out] == ["甲", "乙"]


def test_dedupe_batch_keeps_same_title_in_other_group():
    out = store._dedupe_batch([_item(group_id=1), _item(group_id=2)])
    assert len(out) == 2


def test_dedupe_batch_keeps_same_title_at_other_time():
    out = store._dedupe_batch([_item(event_ts=1), _item(event_ts=2)])
    assert len(out) == 2


def test_save_items_writes_deduped_rows(memdb):
    store.ensure_schema(memdb)
    n = store.save_items(
        memdb, [_item(src=(11,)), _item(src=(11,))],
        model="m", prompt_ver="v3",
    )
    assert n == 1
    assert memdb.execute("SELECT COUNT(*) FROM items").fetchone()[0] == 1


# ── Task 3 补的守卫（变异 G1/G2 实测：上面 6 条全绿也拦不住这两个坏实现）──
# 与 T2 的 482e5e9 同族：**计划写进 docstring 的约束，得有东西在执行它**。


def test_dedupe_batch_union_has_no_duplicate_ids():
    """⚠️ 并集是**集合**，来源重叠时不许留重复 id。

    库里有 `INSERT OR IGNORE` 兜着，重复 id 落不了盘——所以这条只在对象层
    看得出，正因如此才容易被改坏而无人知（变异 G1：改成 `(*a, *b)` 全量全绿）。
    """
    out = store._dedupe_batch([_item(src=(11, 22)), _item(src=(22, 33))])
    assert len(out) == 1
    assert out[0].src_msg_ids == (11, 22, 33)


def test_dedupe_batch_keeps_the_earliest_payload():
    """同键时留下**最早**那一条的正文（docstring 明写，变异 G2 实测零守护）。

    `kind` 是用户可见的（筛选、日报都读它）——同键两条若模型给了不同类目，
    「留哪条」不能是偶然。
    """
    from dataclasses import replace

    first = _item(kind="notice")
    second = replace(_item(kind="job", src=(22,)), detail="后一条的正文",
                     confidence=0.1)
    out = store._dedupe_batch([first, second])
    assert len(out) == 1
    assert out[0].kind == "notice", "留下的该是最早那条的类目"
    assert out[0].detail is None and out[0].confidence == 0.9
    assert out[0].src_msg_ids == (1, 22), "并集照旧"


def test_save_items_writes_sources_of_every_duplicate(memdb):
    """去重后那一条要带上**全部**来源行——M1「可回溯到原文」的落点。

    计划给的第 6 条只数了 `items` 的行数，`item_sources` 一行没数；
    而「合并而不是丢弃」的全部意义就在这张表里（变异 M7：静默丢来源时它红）。
    """
    store.ensure_schema(memdb)
    n = store.save_items(
        memdb, [_item(src=(11,)), _item(src=(22,))],
        model="m", prompt_ver="v3",
    )
    assert n == 1
    assert memdb.execute(
        "SELECT msg_id FROM item_sources ORDER BY msg_id"
    ).fetchall() == [(11,), (22,)]

# ── Task 5：overlay 接入查询层 ────────────────────────────────────


def _seed(conn, *, title="标题", kind="notice", ts=1_700_000_000, group_id=100):
    from vigil.store import ExtractedItem

    # ⚠️ brief 之外新增（逃逸舱 1）：查询层 LEFT JOIN `sender_names` 取署名，
    # 而它**不由** `store.ensure_schema` 建（真库里是 export.py 的产物）。
    # 少了这一行，`search_items` / `get_item` / `digest_items` 报的是
    # `no such table: sender_names`——brief 的 Expected 说会报
    # `no such table: ov.item_state`，实测不是那个。
    _ensure_message_tables(conn)
    conn.execute(
        "INSERT INTO items (kind, title, detail, event_ts, deadline_ts, group_id,"
        " actor_uid, place, links, amount, confidence, model, prompt_ver, created_at)"
        " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (kind, title, None, ts, None, group_id, "u_x", None, "[]", None,
         0.9, "m", "v3", ts),
    )
    return conn.execute("SELECT MAX(item_id) FROM items").fetchone()[0]


def _ov_conn():
    from vigil import overrides

    overrides.ensure_schema()
    conn = overrides.connect()
    overrides.ensure_tables(conn)
    return conn


def test_soft_deleted_item_disappears_from_search(memdb):
    store.ensure_schema(memdb)
    iid = _seed(memdb)
    ov = _ov_conn()
    overrides.delete_item(ov, item_id=iid, msg_id=None)
    ov.close()

    items, total = store.search_items(memdb, limit=50, offset=0)
    assert total == 0, "⚠️ total 也必须跟着变——否则分页与『共 N 条』会说谎"
    assert items == []


def test_set_kind_overrides_kind_in_search(memdb):
    store.ensure_schema(memdb)
    iid = _seed(memdb, kind="notice")
    ov = _ov_conn()
    overrides.set_kind(ov, item_id=iid, kind="academic")
    ov.close()

    items, _ = store.search_items(memdb, limit=50, offset=0)
    assert items[0].kind == "academic"


def test_kind_filter_uses_effective_kind(memdb):
    """⚠️ 筛选必须按**覆盖后**的类目——否则改了分类却筛不出来。"""
    store.ensure_schema(memdb)
    iid = _seed(memdb, kind="notice")
    ov = _ov_conn()
    overrides.set_kind(ov, item_id=iid, kind="academic")
    ov.close()

    assert store.search_items(memdb, kinds=["academic"], limit=50, offset=0)[1] == 1
    assert store.search_items(memdb, kinds=["notice"], limit=50, offset=0)[1] == 0


def test_soft_deleted_item_disappears_from_window(memdb):
    """⚠️ R13 的典型形态：只改 API 不改 window_items ⇒「界面上删了、日报里还在」。"""
    store.ensure_schema(memdb)
    iid = _seed(memdb, ts=1_700_000_000)
    ov = _ov_conn()
    overrides.delete_item(ov, item_id=iid, msg_id=None)
    ov.close()

    got = store.window_items(memdb, since=1_699_000_000, until=1_701_000_000)
    assert got == []


def test_get_item_returns_none_for_deleted(memdb):
    store.ensure_schema(memdb)
    iid = _seed(memdb)
    ov = _ov_conn()
    overrides.delete_item(ov, item_id=iid, msg_id=None)
    ov.close()

    assert store.get_item(memdb, iid) is None


def test_kind_counts_uses_effective_kind_and_skips_deleted(memdb):
    store.ensure_schema(memdb)
    a = _seed(memdb, title="甲", kind="notice")
    _seed(memdb, title="乙", kind="notice")
    ov = _ov_conn()
    overrides.set_kind(ov, item_id=a, kind="academic")
    ov.close()

    counts = store.kind_counts(memdb)
    assert counts == {"academic": 1, "notice": 1}


def test_deleted_item_missing_from_digest_items(memdb):
    store.ensure_schema(memdb)
    iid = _seed(memdb)
    store.save_digest(
        memdb, window_from=1, window_to=2, body_md="x", model="m",
        prompt_ver="v1", item_ids=[iid],
    )
    ov = _ov_conn()
    overrides.delete_item(ov, item_id=iid, msg_id=None)
    ov.close()

    did = memdb.execute("SELECT MAX(digest_id) FROM digests").fetchone()[0]
    assert store.digest_items(memdb, did) == []



def test_query_layer_provisions_a_missing_overlay_file(memdb):
    """❓ 读取路径必须能在「从没写过干预」的库上工作（brief Step 3 的新分支）。

    ⚠️ 变异反证（Task 5 实测）：把 `attach_readonly` 里
    「`if not target.is_file(): ensure_schema(target)`」两行删掉，
    全量 **477 条里一条都不红**——brief 那批用例都先经 `_ov_conn()` 把库造了出来，
    没有一条覆盖"库不存在"。真后果：用户**从没点过任何一条干预**（或干预库被删掉、
    换了机器）时，`/api/items` 与日报的**第一条**查询就是 500——而它本该只是"没有干预"。
    """
    store.ensure_schema(memdb)        # 立 items 表（它顺带也会把 overlay 库建出来）
    _seed(memdb)
    overrides.OVERRIDES_DB.unlink()   # 抹掉：模拟"从没写过干预"
    assert not overrides.OVERRIDES_DB.exists()

    items, total = store.search_items(memdb, limit=50, offset=0)
    assert (total, len(items)) == (1, 1), "没有干预时，读取路径必须照常返回全部条目"
    assert overrides.OVERRIDES_DB.exists(), "读取路径应当把空的 overlay 库就位"



def test_digest_item_count_agrees_with_digest_items_when_one_is_deleted(memdb):
    """⭐ 裁决 R9（T5 Fix loop 第 1 轮）：**同一个响应里计数与列表不许打架**。

    ⚠️ 这个分歧是 T5 **引入的**：`item_count` 原先与 `digest_items` 一致，
    过滤只加在了 `digest_items` 上 ⇒ 软删一条被历史日报引用的条目后，
    `GET /api/digests/{id}` 与 `/api/digests` 都返回 `item_count: 3` 而
    `items` 只有 2 条——而 `web/src/types.ts` 直接用这个字段渲染。

    判据（R9）：D15 要求被删条目从界面**与日报**一起消失；
    计数与列表是**同一份数据的两种读法**，必须同源。
    """
    store.ensure_schema(memdb)
    ids = [_seed(memdb, title=f"条目{i}") for i in (1, 2, 3)]
    store.save_digest(
        memdb, window_from=1, window_to=2, body_md="x", model="m",
        prompt_ver="v1", item_ids=ids,
    )
    did = memdb.execute("SELECT MAX(digest_id) FROM digests").fetchone()[0]

    # 删之前：三者一致
    assert len(store.digest_items(memdb, did)) == 3
    assert store.get_digest(memdb, did).item_count == 3
    assert store.list_digests(memdb)[0].item_count == 3

    ov = _ov_conn()
    overrides.delete_item(ov, item_id=ids[0], msg_id=None)
    ov.close()

    assert len(store.digest_items(memdb, did)) == 2
    assert store.get_digest(memdb, did).item_count == 2, (
        "`/api/digests/{id}` 的 item_count 与 items 打架（R9）"
    )
    assert store.list_digests(memdb)[0].item_count == 2, (
        "`/api/digests` 的 item_count 与 `/api/digests/{id}` 的 items 打架（R9）"
    )


# ── Task 8：多值与人物维度 ────────────────────────────────────────


def _seed_with_sender(conn, *, title, kind, ts, group_id=100, uid="u_1", uin=111):
    from vigil.store import ExtractedItem

    # ⚠️ plan 的 `_seed_with_sender` 少了这一行，实测的 RED 因此是
    # `sqlite3.OperationalError: no such table: sender_names`（不是 Expected 文本）。
    # `sender_names` 是 export.py 的产物，`store.ensure_schema` **不建它**
    # （本文件顶部第 22 行的注释早就写着这件事）——补上已有的幂等助手。
    _ensure_message_tables(conn)
    conn.execute(
        "INSERT OR REPLACE INTO sender_names (group_id, uid, group_nick, qq_nick, uin)"
        " VALUES (?,?,?,?,?)",
        (group_id, uid, "昵称", "QQ昵称", uin),
    )
    conn.execute(
        "INSERT INTO items (kind, title, detail, event_ts, deadline_ts, group_id,"
        " actor_uid, place, links, amount, confidence, model, prompt_ver, created_at)"
        " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (kind, title, None, ts, None, group_id, uid, None, "[]", None, 0.9, "m", "v3", ts),
    )
    return conn.execute("SELECT MAX(item_id) FROM items").fetchone()[0]


def test_search_items_accepts_multiple_kinds(memdb):
    store.ensure_schema(memdb)
    _seed_with_sender(memdb, title="甲", kind="notice", ts=1)
    _seed_with_sender(memdb, title="乙", kind="academic", ts=2)
    _seed_with_sender(memdb, title="丙", kind="life", ts=3)

    _, total = store.search_items(memdb, kinds=["notice", "academic"], limit=50, offset=0)
    assert total == 2


def test_search_items_multiple_persons_is_union(memdb):
    """维度内是**并集**。"""
    store.ensure_schema(memdb)
    _seed_with_sender(memdb, title="甲", kind="notice", ts=1, uid="u_a", uin=111)
    _seed_with_sender(memdb, title="乙", kind="notice", ts=2, uid="u_b", uin=222)
    _seed_with_sender(memdb, title="丙", kind="notice", ts=3, uid="u_c", uin=333)

    _, total = store.search_items(memdb, actor_uins=[111, 222], limit=50, offset=0)
    assert total == 2


def test_kinds_and_persons_are_intersection(memdb):
    """⭐ 维度**间**是**交集**——这是 spec §四 #4 的原始要求，别写成并集。"""
    store.ensure_schema(memdb)
    _seed_with_sender(memdb, title="甲", kind="notice", ts=1, uid="u_a", uin=111)
    _seed_with_sender(memdb, title="乙", kind="academic", ts=2, uid="u_a", uin=111)
    _seed_with_sender(memdb, title="丙", kind="notice", ts=3, uid="u_b", uin=222)

    _, total = store.search_items(
        memdb, kinds=["notice"], actor_uins=[111], limit=50, offset=0
    )
    assert total == 1


def test_actor_uin_exposed_on_api_item(memdb):
    store.ensure_schema(memdb)
    _seed_with_sender(memdb, title="甲", kind="notice", ts=1, uid="u_a", uin=111)
    items, _ = store.search_items(memdb, limit=50, offset=0)
    assert items[0].actor_uin == 111


def test_actor_uin_is_none_for_anonymous(memdb):
    """匿名消息（sender_uid 为空）不该匹配任何人物筛选。"""
    store.ensure_schema(memdb)
    _seed_with_sender(memdb, title="甲", kind="notice", ts=1, uid="", uin=None)
    items, _ = store.search_items(memdb, limit=50, offset=0)
    assert items[0].actor_uin is None
    assert store.search_items(memdb, actor_uins=[111], limit=50, offset=0)[1] == 0


def test_dedupe_batch_key_is_exact_not_normalized():
    """⭐ M5 终审 Minor-1：去重**键的严格度**零守护（变异实测 578 passed / 0 red）。

    上面那一组守的是「合不合并」（同标题同秒同群要合成一条、换群换秒不许合），
    **没有一条**守「键有多严」。终审的变异把键改成
    `(it.title.strip().lower(), it.event_ts, it.group_id)` ⇒ **全绿**，
    而那个变异的后果是：同群同秒的两条**不同**条目被合并、其中一条从 Web
    与日报同时消失（`src_msg_ids` 取并集还能掩盖掉一半痕迹），
    且没有任何测试变红。

    ⚠️ 键是**逐字**的（`(title, event_ts, group_id)`），「顺手规范化一下标题」
    是**产品决策**而不是重构——这条测试就是要把那个决策钉在明处。

    变异「键改成 `.strip()` / `.lower()` / 两者都上」⇒ 本条红。
    """
    out = store.dedupe_batch(
        [
            _item("选课通知", src=(11,)),
            _item("选课通知 ", src=(12,)),      # 只差一个尾空格 ⇒ 仍然是**另一条**
            _item("CET-4 报名", src=(13,)),
            _item("cet-4 报名", src=(14,)),      # 只差大小写 ⇒ 仍然是**另一条**
        ]
    )
    assert [i.title for i in out] == [
        "选课通知", "选课通知 ", "CET-4 报名", "cet-4 报名",
    ], "键被规范化了：两条不同的条目被合并，其中一条会从 Web 与日报消失"
    assert [i.src_msg_ids for i in out] == [(11,), (12,), (13,), (14,)]


# ── M6 T5：kind_alias 进读取层（四处 SQL 的收敛）────────────────────
#
# ⚠️ 偏差（逃逸舱）：brief 这条用例的夹具写的是 `con_with_two_items`，本文件
# **没有**这个夹具（既有的造数助手是 `_seed` / `_insert_item` /
# `_seed_two_items_for_search`）。这里按 `_seed` 补一个最小夹具：
# **两条 kind='life' 的条目**，且两条都落在 `window_items` 的窗口内。


@pytest.fixture
def con_with_two_items():
    """两条**原始 kind 不同**、但**有效 kind 相同**的条目（内存库 + overlay 就位）。

    ⚠️ 「原始 kind 必须不同」是 Fix loop 1 / FIX 3 实测出来的**必需条件**：
    从前两条都写 `kind='life'`，于是 `kind_counts` 的 **SELECT 与 GROUP BY
    在那组数据上答案相同** ⇒ 「只回退 GROUP BY 那一半」全量 708 绿，
    而它的后果是角标报 1、`window_items` 报 2（**静默少数**）。

    现在两条分别走 `life` 与 `notice` 两个旧 slug、由用例把它们都映射到 `daily`：
      · E（有效类目）= `daily` / `daily` ⇒ **同一个分组**
      · R（只含 `ost`/`i.kind` 的旧写法）= `life` / `notice` ⇒ **两个不同的键**
    ⇒ 只回退 SELECT、只回退 GROUP BY、两处一起回退——**三种都红**。
    """
    conn = sqlite3.connect(":memory:", uri=True)
    store.ensure_schema(conn)
    _seed(conn, title="甲", kind="life")
    _seed(conn, title="乙", kind="notice")
    yield conn
    conn.close()


def test_kind_alias_applies_to_every_reading_path(con_with_two_items):
    """⭐ 一条别名必须同时改变**四个**读取路径的回答（R13 的落点）。

    ⚠️ 这条测试的形状是刻意的：它**在一个用例里同时断言四处**。
    拆成四条独立用例的话，「只改了 `_ITEM_COLS`、忘了 `kind_counts`」
    会各自绿一条、红一条——而那恰恰是最可能发生的漏法（四处里有三处
    长得完全一样，只有 `window_items` 是手抄的）。

    ⚠️ 变异反证（四个方向各验一次，**实施时都要跑**）：
      · 把 `_ITEM_COLS` 里的 `_EFFECTIVE_KIND` 换回 `COALESCE(ost.kind, i.kind)` ⇒ 红
      · 把 `_item_filters` 那处换回 ⇒ 红
      · 把 `kind_counts` 那处换回（**两处都要**）⇒ 红
      · 把 `window_items:419` 那处换回 ⇒ 红
    ⚠️ Fix loop 1 追加（reviewer 实测的漏法）：**只换 `kind_counts` 的一半**也要红
    （夹具因此改成「原始 kind 不同、有效 kind 相同」，见 `con_with_two_items`）。
    """
    from vigil import overrides

    overrides.attach_readonly(con_with_two_items)
    ov = overrides.connect()
    try:
        # 两个**不同的旧 slug** 都映射到同一个新 slug —— 这正是 FIX 3 要的形状。
        overrides.set_kind_alias(ov, old_kind="life", new_kind="daily")
        overrides.set_kind_alias(ov, old_kind="notice", new_kind="daily")
    finally:
        ov.close()

    # ① _ITEM_COLS（列表接口）
    items, total = store.search_items(con_with_two_items)
    assert {it.kind for it in items} == {"daily"}

    # ② _item_filters（按类目筛选）
    hit, _ = store.search_items(con_with_two_items, kinds=["daily"])
    assert len(hit) == total
    miss, _ = store.search_items(con_with_two_items, kinds=["life"])
    assert miss == [], "旧 slug 不该还能筛出东西——它已经不是任何条目的有效类目了"

    # ③ kind_counts（角标）
    assert store.kind_counts(con_with_two_items) == {"daily": total}

    # ④ window_items（日报）
    win = store.window_items(con_with_two_items, since=0, until=2**31)
    assert {w.kind for w in win} == {"daily"}


# ── M6 T5 Fix loop 2：手改配置的对账（FIX 2）────────────────────────


def _cats_toml(*slugs: str) -> str:
    """一份最小类目表（对账只取 slug 集）。"""
    return "".join(
        f'[[categories]]\nslug = "{s}"\nlabel = "{s}"\ndesc = "d"\n\n' for s in slugs
    )


def test_a_hand_edited_config_reconciles_the_alias_table(monkeypatch):
    """⭐ FIX 2：**手改 `categories.toml`** 也要把别名表对平（本里程碑明确邀请手改）。

    场景（reviewer 实测的那一条）：
      ① 人工把某条条目的类目改成 `life`（`ost.kind`）；
      ② **改名** `life→life_v2` ⇒ 别名 `life→life_v2`，配置里只剩 `life_v2`；
      ③ **手改**把 `life` 加回配置——它不经过任何写端点 ⇒ 剪枝跑不到。
    ⇒ 不清账的话 `ka2` 会把合法的 `life` 顶成 `life_v2`：条目显示 `life_v2`、
    `life` 角标 0，而**出口检查也看不见**（它读的是有效类目）。

    ⚠️ 判据必须是**条目那一侧**的 `kind`（不是"别名表有几行"）：后者只说明
    "表里没有那一行"，而用户看到的是条目的类目。

    ⚠️ 变异反证：把 `store._ensure_overlay` 里那句 `reconcile_if_stale()`
    删掉 ⇒ 本条**必红**（条目仍是 `life_v2`）。
    """
    from vigil import categories as categories_mod
    from vigil import overrides

    # ① 改名的结果：现存 slug 里**没有** life（那条别名才因此是合法的）
    path = categories_mod.DEFAULT_CATEGORIES
    path.write_text(_cats_toml("notice", "life_v2"), encoding="utf-8")

    conn = sqlite3.connect(":memory:", uri=True)
    try:
        store.ensure_schema(conn)
        iid = _seed(conn, title="甲", kind="notice")
        # ⚠️ **必须显式 commit**（实测，见下）：`_seed` 自己不 commit，于是
        # `conn` 上开着一段**未提交的写事务**，而 SQLite 的读快照是**按连接**
        # 取的——后面那个 `ov` 的读会一直停在**那一刻**的快照上，对账在另一个
        # 连接里做的删除**看不见**（实测：不 commit 时 `ov.kind_alias` 里始终
        # 还看得见那一行，而新连接查同一个文件是空的；
        # `conn.rollback()` 之后立刻就能看见 ⇒ 就是快照被冻住）。
        # ⚠️ 生产侧不受影响：`api.connect()` **每请求一条新连接**。
        conn.commit()
        w = _ov_conn()
        try:
            overrides.set_kind(w, item_id=iid, kind="life")          # 人工选 life
            overrides.set_kind_alias(w, old_kind="life", new_kind="life_v2")
        finally:
            w.close()

        assert store.get_item(conn, iid).kind == "life_v2", (
            "第一次读就不对：对账把一条**合法**的别名剪掉了（old_kind 不在现存集里）"
        )

        # ② **手改**配置：把 life 加回来（mtime 与 size 都变了 ⇒ 闸门开）
        path.write_text(_cats_toml("notice", "life_v2", "life"), encoding="utf-8")
        _w = overrides.connect()
        _w.close()

        assert store.get_item(conn, iid).kind == "life", (
            "手改配置之后别名表没对平：手工选的**合法** life 被旧别名顶成了 life_v2"
        )
        # 而且表里那一行确实走了（不变量：old_kind 不许是现存 slug）
        w = overrides.connect()
        try:
            assert "life" not in overrides.kind_aliases(w)
        finally:
            w.close()
    finally:
        conn.close()
