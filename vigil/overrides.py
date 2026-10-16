"""人工干预层：对**数据**的人工修改，不写 `data/vigil.db`。

为什么要单独一层（D14）：用户要「手动改分类 / 删除条目 / 撤销」，这三件事都要写数据，
而 M3 起 `data/vigil.db` 的只读是**机械保证**（`mode=ro`）。把它们写进主库，
那道保证就没了；而**一张 append-only 的事件表同时给出这三件事**——
撤销天然就有（事件可回溯），删除变成 tombstone，重排变成 `set_kind` 事件
（从而 `item_id` 永不翻新，见 D16）。

⚠️ **overlay 是视图层修正，不是"数据被改真了"**。直接 `sqlite3 data/vigil.db` 查，
被删的条目仍然在。这个性质是有意的，写在这里免得将来有人当成缺陷。

⚠️ **两张表的分工**：
* `item_edits` 是**唯一真相**（append-only，撤销就是对它做删除）
* `item_state` 是**物化的折叠结果**，由写入路径维护。
  读取路径 JOIN 它即可，不需要窗口函数——SQL 保持简单是刻意的。
  它可以在任何时候由 `rebuild_state()` 从 `item_edits` 完整重算。
"""

from __future__ import annotations

import json
import logging
import pathlib
import sqlite3
import threading
import time
from collections.abc import Mapping, Sequence

from .config import REPO_ROOT

OVERRIDES_DB = REPO_ROOT / "data" / "overrides.db"

# ⚠️ 情形②的降级**必须响亮**（裁决 R8）：静默降级会把真正的部署问题
# （`data/` 不可写 / 路径非法）藏起来，而表现是「人工干预全都不见了」。
_log = logging.getLogger(__name__)

ACTION_SET_KIND = "set_kind"
ACTION_SET_FIELD = "set_field"
ACTION_DELETE = "delete"

# 墓碑行（T8/E7）：它记的是「这条源消息属于一个**最终态被软删**的 item」。
# ⚠️ 它对 `item_state` **完全惰性**（`rebuild_state` / `_apply_to_state` 都不认它），
# 因此它只经 `deleted_msg_ids` 那一次折叠起作用——**"垫在 delete 行之前"是承重的**，
# 理由写在 `delete_item` 的 docstring 里（undo 只删一条事件）。
ACTION_TOMBSTONE = "tombstone"

_TABLES_DDL = """
CREATE TABLE IF NOT EXISTS item_edits (
    edit_id    INTEGER PRIMARY KEY,
    item_id    INTEGER NOT NULL,
    msg_id     INTEGER,
    action     TEXT    NOT NULL,
    field      TEXT,
    old_value  TEXT,
    new_value  TEXT,
    actor      TEXT    NOT NULL,
    created_at INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS item_edits_item ON item_edits(item_id);

CREATE TABLE IF NOT EXISTS item_state (
    item_id INTEGER PRIMARY KEY,
    kind    TEXT,
    deleted INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS config_snapshots (
    snapshot_id INTEGER PRIMARY KEY,
    saved_at    INTEGER NOT NULL,
    body        TEXT    NOT NULL,
    aliases     TEXT
);

CREATE TABLE IF NOT EXISTS kind_alias (
    old_kind   TEXT PRIMARY KEY,
    new_kind   TEXT NOT NULL,
    created_at INTEGER NOT NULL
);
"""

# ⚠️ 上面这两张新表（M6 T5）**刻意不加索引**：都是个位数到几十行的规模，
# 而 `_qualified_ddl` 的索引处理有一条极易踩的坑——索引名要限定 schema、
# 但**表名不许带 schema**（SQLite 语法限制，见 `_qualified_ddl` 的实测记录）。
# 不加索引就不会碰到它。

# 补列语句（升级已存在的库用；新库由 `_TABLES_DDL` 的 CREATE 直接带上）。
# ⚠️ **不是 _TABLES_DDL 的一部分**：`_qualified_ddl` 会把 `CREATE TABLE` 加上
# schema 前缀，而 `ALTER TABLE` 要多一层处理（`ALTER TABLE ov.x` 合法，但语句里
# 还带 `IF NOT EXISTS` 之类就没法拼）——更重要的是**内存兜底库不需要它**
# （`CREATE TABLE` 已经带上了那一列）。
#
# ⚠️ 每条写成 `(表, 列, 语句)` 三元组：收尾要拿前两项去 `PRAGMA table_info` **核**。
_MIGRATIONS: tuple[tuple[str, str, str], ...] = (
    (
        "config_snapshots",
        "aliases",
        "ALTER TABLE config_snapshots ADD COLUMN aliases TEXT",
    ),
)


class OverlayError(RuntimeError):
    """人工干预层的使用错误——快速失败。"""


class UndoConflict(OverlayError):
    """撤销与**日志当前状态**冲突（可证明会静默弄坏状态）⇒ API 侧映射 **409**。

    ⚠️ **为什么必须与 `OverlayError` 分得开**（M5 收尾 scoped re-review A2）：
    `OverlayError` 这个父类今天罩着**两种后果完全不同**的失败——

    * **与数据冲突**：`_reject_non_tail_undo` 那两道闸门。请求本身合法，
      冲突的是「日志现在长什么样」⇒ 客户端该收到 **409**，
      而且**换一条编辑撤就能成**（有自救路径）。
    * **基础设施故障**：overlay 文件缺失 / 挂不上 / 路径非法
      （`attach_readonly` 的 `OverlayError`）。这是**服务端自己坏了**，
      客户端重试多少次都一样 ⇒ 该是 **5xx**。

    把两者映成同一个码（修复前是「任何 `OverlayError` ⇒ 409」），
    排障会指向**错的方向**：运维去看「是不是有人并发改了日志」，
    而真相是 overlay 挂不上。**同族教训**：`grep -c $'\\r$'` 那种「看着像量具、
    其实量的是别的东西」的表述——这里量的就是把两种病合成了一种。

    ⚠️ 仍是 `OverlayError` 的子类：既有的 `except OverlayError`（测试、
    将来的调用方）一个都不用改，只是**新的**调用方要按需分开捕。
    """


def _qualified_ddl(schema: str) -> list[str]:
    """把 `_TABLES_DDL` 的库名限定到 `schema`，并按语句拆开返回。

    ⚠️ **不手抄第二份 DDL**：抄一份就会与 `_TABLES_DDL` 漂移，
    而漂移方向恰好是「兜底时看到的表 ≠ 生产里读到的表」。
    ⚠️ 返回**语句列表**而不是整段脚本：`executescript` 会先隐式 COMMIT，
    而调用方（`digest` / `refine` 的连接）可能正开着一段事务。
    """
    out = _TABLES_DDL
    out = out.replace(
        "CREATE TABLE IF NOT EXISTS ", f"CREATE TABLE IF NOT EXISTS {schema}."
    )
    out = out.replace(
        "CREATE INDEX IF NOT EXISTS ", f"CREATE INDEX IF NOT EXISTS {schema}."
    )
    # ⚠️ 索引的**表名不许带库名**（SQLite 语法：`CREATE INDEX ov.x ON item_edits(...)`
    # 是 `near ".": syntax error`）；只限定索引名，表名由 SQLite 在同一 schema 内解析
    # （实测：`ov` 里没有 `main.item_edits` 也能建成功）。
    return [stmt for stmt in (s.strip() for s in out.split(";")) if stmt]


def _attach_empty_overlay(conn: sqlite3.Connection) -> None:
    """情形②的兜底：挂一个**内存空库**当 `ov`，并把两张表立起来。

    ⚠️ 只用于「库本来就不存在、而且建不出来」——那时「没有任何干预」是**确定的**
    （文件都没有）。文件存在时**绝不许**走这里：`attach_readonly` 会抛 `OverlayError`。
    ⚠️ 零写：`:memory:` 不产生任何文件，也不碰主库——所以"读取路径不许写盘"仍然成立。
    """
    conn.execute("ATTACH DATABASE ':memory:' AS ov")
    for stmt in _qualified_ddl("ov"):
        conn.execute(stmt)


def ensure_tables(conn: sqlite3.Connection) -> None:
    """建表 + 补列。幂等。**不 commit**——调用方决定事务边界。"""
    conn.executescript(_TABLES_DDL)
    for table, column, stmt in _MIGRATIONS:
        try:
            conn.execute(stmt)
        except sqlite3.OperationalError as exc:
            # ⚠️ `duplicate column name: aliases` —— **正常路径，不是错误**：
            # SQLite 没有 `ADD COLUMN IF NOT EXISTS`，升级已有的库只能
            # "试着加，重复了就当没有"。
            # ⚠️ **但只吞这一种**（Fix loop 3 / FIX 4）：从前是裸的
            # `except sqlite3.OperationalError: pass` ⇒ `database is locked`、
            # 磁盘满、表被改名……**统统被咽下去**，服务照常起来，
            # 直到某次撤销才 500（那时已经离现场很远了）。
            # ⇒ 其余的必须**响亮**。
            if "duplicate column" not in str(exc).lower():
                raise

    # ⚠️ **收尾核验：列真的在**（Fix loop 3 / FIX 4 的第二道）。
    # 上面那句 `raise` 只挡住"抛得出来"的失败；这一道挡的是
    # **"ALTER 悄悄没生效"**（比如将来有人把语句改成一句无害的 no-op）。
    # 判据是 `PRAGMA table_info`——SQLite 里唯一说得清"这一列在不在"的东西。
    for table, column, _ in _MIGRATIONS:
        cols = {str(r[1]) for r in conn.execute(f"PRAGMA table_info({table})")}
        if column not in cols:
            raise OverlayError(
                f"补列没生效：{table}.{column} 不在表里。"
                "这张表是 overlay 的，缺列会让撤销在**运行时**才 500——"
                "宁可现在响亮地拒绝。"
            )


def ensure_schema(path: pathlib.Path | None = None) -> None:
    """确保 overlay 文件存在且表齐全。幂等，供服务启动与写路径调用。

    ⚠️ 读取路径（`api.py`）**不能**在这里建库——它们是只读的。所以服务启动时
    先调这个函数（D11 允许 Web 写 overlay），之后每个请求才敢无条件 ATTACH。
    """
    target = path or OVERRIDES_DB
    target.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(target))
    try:
        # ⚠️ WAL **必须在这里设**（裁定 R6）：T5 之后 `api.py` 的 `mode=ro` 只读连接
        # 会与 `refine` / T8 的写端点**并发**访问同一个库。rollback-journal 下写者持
        # EXCLUSIVE 锁 ⇒ 读请求撞 `database is locked`；而 `mode=ro` 的连接**不能**
        # 把库转成 WAL（它连库头都写不了）⇒ 只能在建库时设。
        # 实测：WAL 不影响 `mode=ro` 的 ATTACH（`-shm`/`-wal` 由 SQLite 按需重建）。
        conn.execute("PRAGMA journal_mode=WAL")
        ensure_tables(conn)
        conn.commit()
    finally:
        conn.close()


def connect(path: pathlib.Path | None = None) -> sqlite3.Connection:
    """**可写**连接（写端点专用）。"""
    conn = sqlite3.connect(str(path or OVERRIDES_DB))
    conn.row_factory = sqlite3.Row
    return conn


def attach_readonly(conn: sqlite3.Connection, path: pathlib.Path | None = None) -> None:
    """把 overlay 挂成 `ov`（只读）。读取路径专用。

    ⚠️ 用 `mode=ro` 挂——读取路径不该有写能力，哪怕只是"顺手"。
    ⚠️ 幂等：已挂过就什么都不做（`sqlite3` 重复 ATTACH 同名会报错）。
    ⚠️ 文件不存在时**先建一个空的**：读取路径必须能在"从没写过干预"的库上工作。
    这不违反 D11（overlay 本来就在 Web 的可写范围内），也比"读路径分支 SQL"干净得多
    ——分支 SQL 会让"忘了处理没 attach 的情况"重新变成一种可能。

    ⚠️ **挂不上时分两种情形处置**（裁决 R8 / T5 修复轮 1）：
    * **文件存在**但挂不上 ⇒ 抛 `OverlayError`，**绝不降级**：文件里可能有墓碑，
      降级 = 被软删的条目悄悄复活 + 改过的类目回退（直接违反 D15）。
    * **文件不存在、而且建不出来**（`data/` 不可写 / 路径非法）⇒ 降级为「无干预」：
      `ATTACH ':memory:' AS ov` + 建表（**零写**），并**响亮地记一条日志**。
      这时"没有任何干预存在"是确定的语义，不是猜测。

    ⚠️ **Task 4 实测的平台事实（brief 的写法在本机对部分调用方不可用）**：
    `file:...?mode=ro` 这个 URI 形式**只有连接带 `SQLITE_OPEN_URI` 时才被解析**，
    也就是必须由 `sqlite3.connect(..., uri=True)` 打开。连接没带这个标志时，
    SQLite 把整串当**普通文件名**去开，于是报出极难定位的
    `OperationalError: unable to open database: file:C:/...?mode=ro`
    （三种 URI 写法全试过，无一可用；见 task-4-report.md）。
    T5 起**四条生产连接全部带上了这个标志**（`api.py` 原本就有；`cli.py` 的
    `deadline-audit`、`digest.py`、`refine.py` 三处由 T5 补——controller 裁决 R4）。
    ⚠️ 将来新增任何走查询层的连接都必须带上它，否则会在 `_ensure_overlay` 上抛
    `OverlayError`——**响亮**的失败，不是静默降级（这正是设计要的）。

    ⚠️ **为什么不"兜底挂一次可写的再 PRAGMA 冻住"**：`PRAGMA ov.query_only=1`
    实测是**连接级**的（主库也一起写不了），而 `digest.py:577` / `refine.py:399`
    的连接**既要读** `window_items`**又要写**自己的表——兜底会把它们写坏。
    所以这里**绝不**退化成"挂可写的真库再冻住"：文件**存在**时挂不上就报错
    （情形①），只有"文件本来就不存在且建不出来"才降级成内存空库（情形②）。
    """
    if any(r[1] == "ov" for r in conn.execute("PRAGMA database_list")):
        return
    target = path or OVERRIDES_DB
    created = False
    if not target.is_file():
        try:
            ensure_schema(target)
            created = True
        except (OSError, sqlite3.Error) as exc:
            # ── 降级分支。「**谁**失败」才是判据，不是「文件在不在」（R8-bis）──
            # `ensure_schema` 失败 = **根本碰不到文件** ⇒ 此时"没有任何干预
            # 存在"是**确定的**，降级成"无干预"语义正确、不是掩盖错误。
            # ⚠️ 但必须**响亮**：静默降级会让真正的部署问题（不可写的 data/）
            # 表现成「我的干预全没了」。
            _attach_empty_overlay(conn)
            _log.warning(
                "overlay 库不存在且建不出来（%s）：本连接降级为「无干预」（内存空库）。人工改过的分类与软删在这一进程里看不到。原因：%s",
                target, exc,
            )
            return
    try:
        conn.execute(f"ATTACH DATABASE 'file:{target.as_posix()}?mode=ro' AS ov")
    except sqlite3.OperationalError as exc:
        # ── 响亮失败分支：`ensure_schema` 成功（或文件本来就在）但 ATTACH 失败 ──
        # **绝不降级**，哪怕库是刚建出来的空库（`created`）：
        # ① T4 把「新连接忘带 `uri=True` ⇒ 抛 `OverlayError`，**响亮**、不是静默
        #    降级」写成了**刻意设计**；按"库是刚建的空库、里面肯定没有干预"去
        #    降级，就把 R4 的那部分价值退了回去。
        # ② 文件已存在时更不许降级：里面**可能有墓碑**，降级 = 被软删的条目
        #    悄悄复活 + 改过的类目回退，直接违反 D15。
        raise OverlayError(
            "overlay 只读挂载失败（**不降级**）。常见原因：本连接不是"
            " sqlite3.connect(..., uri=True) 打开的（四条生产连接见"
            " task-4-report.md / task-5-report.md）；或 data/ 不可写导致 WAL 的"
            " -shm/-wal 无法重建。"
            + (
                "⚠️ 本次的库是**刚建出来的空库**（里面确定没有干预），但照样不降级"
                "——否则「新连接忘带标志」就会变成静默降级。"
                if created
                else "库是**已存在**的：它可能带着墓碑。"
            )
            + f"原始错误：{exc}"
        ) from exc


def _append(
    conn: sqlite3.Connection,
    *,
    item_id: int,
    msg_id: int | None,
    action: str,
    field: str | None,
    old_value: str | None,
    new_value: str | None,
    actor: str,
    now: int | None,
) -> int:
    stamp = int(time.time()) if now is None else now
    cur = conn.execute(
        "INSERT INTO item_edits"
        " (item_id, msg_id, action, field, old_value, new_value, actor, created_at)"
        " VALUES (?,?,?,?,?,?,?,?)",
        (item_id, msg_id, action, field, old_value, new_value, actor, stamp),
    )
    _apply_to_state(
        conn, item_id=item_id, action=action, new_value=new_value, edit_id=cur.lastrowid
    )
    conn.commit()
    return int(cur.lastrowid)


def set_kind(
    conn: sqlite3.Connection, *, item_id: int, kind: str, actor: str = "web",
    now: int | None = None,
) -> int:
    """把某条 item 的类目改掉（写一条事件，不改 `items` 表）。"""
    if not kind:
        raise OverlayError("kind 不能为空")
    return _append(
        conn, item_id=item_id, msg_id=None, action=ACTION_SET_KIND, field="kind",
        old_value=None, new_value=kind, actor=actor, now=now,
    )


def delete_item(
    conn: sqlite3.Connection, *, item_id: int, msg_id: int | None,
    extra_msg_ids: Sequence[int] = (), actor: str = "web",
    now: int | None = None,
) -> int:
    """软删（D15）。`msg_id` 是「永不被重抽复活」的键——**能不传就一定要传**。

    ⚠️ 不知道 `msg_id` 时传 None 是允许的（界面仍然会隐藏它），
    但那条源消息将来 `--redo` 时**就会复活**。调用方有责任尽量带上。

    ⚠️ **`extra_msg_ids` = 同一个 item 的其余源消息**（T8 的 E7）：批内去重的
    来源是**并集**（`store.py` 的 `_dedupe_batch`），所以一个 item 可以有 ≥2 条
    源消息。只墓碑化第一条的话，`refine --redo` 重抽兄弟消息会让条目
    **以新 item_id 复活**（直接违反 D15）⇒ **每一条源消息都要落一条墓碑行**。

    ⚠️ **顺序是承重的：墓碑行垫在 delete 行之前**，且 delete 行必须落在尾部。
    因为 `undo` 只删**一条**事件（`DELETE FROM item_edits WHERE edit_id = ?`）：
    若把 N 条源消息都写成 `delete` 行，撤掉最近那条之后 `item_state.deleted`
    仍是 1 ⇒ **条目照旧隐藏、undo 却报成功**（用户看到「撤销没用」）。
    墓碑行对 `item_state` 惰性，于是：
      · 撤掉末尾那条 delete ⇒ 重放出的最终态里没有 delete ⇒ 条目**复活**；
      · 复活后墓碑行**自动失效**（`deleted_msg_ids` 按**最终态** JOIN item_state），
        与 `test_delete_then_set_kind_last_wins` 的语义一致。
    ⚠️ 这同时**显式承认**了 T4 记下的那条契约放宽：**非 delete 事件带 `msg_id`、
    且所属 item 最终态 `deleted=1` 时，该 msg 也会被墓碑化**（今天是唯一可观测处）。

    ⚠️ 逐行 `_append`（各自 commit），**刻意不做批量写**：`undo` 的「成功」路径
    仍会 `conn.commit()`（T4 fix 记的 FIX 2 同族），调用方自己开着的未提交事务
    会被它一起冲掉。这里不给自己造那个窗口。
    """
    for extra in extra_msg_ids:
        _append(
            conn, item_id=item_id, msg_id=extra, action=ACTION_TOMBSTONE,
            field=None, old_value=None, new_value=None, actor=actor, now=now,
        )
    return _append(
        conn, item_id=item_id, msg_id=msg_id, action=ACTION_DELETE, field=None,
        old_value=None, new_value=None, actor=actor, now=now,
    )


# ── 配置版本快照（M6 T5）────────────────────────────────────────────
#
# ⚠️ 它**不是** overlay。（spec §3.3：「类目配置的回滚是另一件事
# （`config/categories.toml` 的版本快照），两者都做，别混为一谈」。）
# overlay 记的是「人对**条目**的修改」，快照记的是「程序对**配置文件**的
# 上一次覆盖」——前者用事件日志（可逐条撤销），后者用整份存档
# （只能是整体回滚）。
#
# ⚠️ 为什么快照存在 overlay 库里而不是 `config/history/` 下：
# `config/` 是**要进 git** 的、且它的语义是「唯一真相源」；往里堆历史副本
# 会让「哪一份是真的」变得需要解释，而 `data/overrides.db` 本来就是
# 「会随时间增长的本地可写数据」——这正是快照的性质。
#
# ⚠️ **升级路径**：`config_snapshots` / `kind_alias` 是 M6 T5 新加的表，
# 而已有的 `data/overrides.db` 里没有它们。`ensure_schema` 用的是
# `CREATE TABLE IF NOT EXISTS`，`create_app`（服务启动）与 `store.ensure_schema`
# 都会调它 ⇒ **升级后让服务重启一次**（或跑一次 `vigil refine`）即可。
# ⚠️ 而**读取路径**（`attach_readonly`）只在「文件本来就不存在」时才建表——
# 所以没重启就会撞 `no such table: ov.kind_alias`。那是**响亮**的失败，
# 不是静默降级（与 R4 的既有权衡一致：宁可报错，也不许"查不到别名 ⇒ 按旧
# slug 显示"这种静默的错分类）。


def _decode_aliases(raw: object) -> dict[str, str] | None:
    """快照里那一列 → `{旧 slug: 新 slug}`。

    ⚠️ **NULL 与"解析不了"一律返回 `None`**（=「这份快照没有别名表」），
    **绝不返回空字典**：把旧格式当成"空表"会让撤销**静默清空整张 `kind_alias`**
    ——所有改过名的条目一起退化成裸 slug，比"不动它"危险得多。
    """
    if raw is None:
        return None
    try:
        data = json.loads(str(raw))
    except (TypeError, ValueError):
        return None
    if not isinstance(data, dict):
        return None
    return {str(k): str(v) for k, v in data.items()}


def save_config_snapshot(
    conn: sqlite3.Connection, *, body: str, aliases: Mapping[str, str] | None = None,
    now: int | None = None,
) -> int:
    """存一份配置的**原文**＋**当时的别名表**。返回 `snapshot_id`。

    ⚠️ 存**原文**而不是解析后的结构：撤销要还原的是**那份文件**，
    重新序列化会丢掉用户手工加的注释（而 `_CATEGORIES_HEADER` 之外的东西
    正是「用户改过这里」的痕迹）。

    ⚠️ **为什么要连别名表一起存**（Fix loop 2 / FIX 1）：撤销只把文件写回去是
    **不够**的——`set_kind_alias` 的压平 UPDATE 会**改写别的行**（`X→A` 在
    `A→B` 之后变成 `X→B`），那一步丢掉的信息**无法从文件名反推**
    （改两次名再撤销一次，配置停在中间名上，而条目显示的是**裸 slug**）。
    ⇒ 撤销要还原的是「**文件 + 别名表**」这一对，两者必须来自**同一时刻**。

    ⚠️ `aliases=None` 表示"**读当前表**"（默认）。调用方若想表达"这份快照没有
    别名表"（旧格式），必须显式传一个值——`None` 不是那个意思。
    """
    stamp = int(time.time()) if now is None else now
    payload = kind_aliases(conn) if aliases is None else dict(aliases)
    cur = conn.execute(
        "INSERT INTO config_snapshots (saved_at, body, aliases) VALUES (?,?,?)",
        (stamp, body, json.dumps(payload, ensure_ascii=False, sort_keys=True)),
    )
    conn.commit()
    return int(cur.lastrowid)


def list_config_snapshots(
    conn: sqlite3.Connection, *, limit: int = 20
) -> list[tuple[int, int]]:
    """`[(snapshot_id, saved_at)]`，**新的在前**。"""
    return [
        (int(a), int(b))
        for a, b in conn.execute(
            "SELECT snapshot_id, saved_at FROM config_snapshots"
            " ORDER BY snapshot_id DESC LIMIT ?",
            (limit,),
        )
    ]


def pop_config_snapshot(
    conn: sqlite3.Connection,
) -> tuple[int, str, dict[str, str] | None] | None:
    """取最近一条快照**并删掉它**，返回 `(snapshot_id, body, aliases)`；空栈 `None`。

    ⚠️ `aliases` 为 `None` = **这份快照没有别名表**（M6 T5 之前的旧格式），
    不是"空表"——调用方必须区别对待（`_decode_aliases` 的 docstring 有论证）。

    ⚠️ **必须删掉那一行**，否则「撤销 → 再撤销」会无限撤销同一个状态
    （用户看到的是"点了没反应"）。删行之后每一次撤销都真的往回走一格——
    这与 `undo` 删事件是同一个思路（两者都是"往回走一格"）。
    """
    row = conn.execute(
        "SELECT snapshot_id, body, aliases FROM config_snapshots"
        " ORDER BY snapshot_id DESC LIMIT 1"
    ).fetchone()
    if row is None:
        return None
    conn.execute("DELETE FROM config_snapshots WHERE snapshot_id = ?", (row[0],))
    conn.commit()
    return (int(row[0]), str(row[1]), _decode_aliases(row[2]))


def peek_config_snapshot(
    conn: sqlite3.Connection,
) -> tuple[int, str, dict[str, str] | None] | None:
    """看最近一条快照**但不消费它**；空栈返回 `None`。返回形状同 `pop`。

    ⚠️ 与 `pop_config_snapshot` 分成两个函数的理由是**校验必须在消费之前**
    （Fix loop 1 / FIX 5）：`api_category_undo` 要先校验快照内容再落盘，
    而校验不过时**不能把它从撤销栈里拿走**——否则用户下一次撤销会**多退一格**
    （跳过更早那次编辑），而接口只说了「配置未被改动」——**那句话只对文件成立**。
    """
    row = conn.execute(
        "SELECT snapshot_id, body, aliases FROM config_snapshots"
        " ORDER BY snapshot_id DESC LIMIT 1"
    ).fetchone()
    return (int(row[0]), str(row[1]), _decode_aliases(row[2])) if row else None


def replace_kind_aliases(
    conn: sqlite3.Connection, aliases: Mapping[str, str]
) -> int:
    """把 `kind_alias` **整表换成** `aliases`。返回写进去的行数。

    ⚠️ **撤销必须用它，不能只剪**（Fix loop 2 / FIX 1）：剪枝只能删"与当前
    slug 集冲突"的行，而成不了**被压平 UPDATE 抹掉的那些旧行**
    （`X→A` 在 `A→B` 之后已经变成 `X→B`，信息没了）——那正是「改两次名再撤销
    一次」时配置停在中间名、条目显示裸 slug 的根因。
    整表替换还原的是**快照那一刻的表**，信息完整。

    ⚠️ `created_at` 用写入时刻（快照里没存它）：它只是留痕字段，
    没有任何读路径依赖它（`kind_aliases` 只取两列）。
    """
    stamp = int(time.time())
    conn.execute("DELETE FROM kind_alias")
    for old_kind, new_kind in sorted(aliases.items()):
        conn.execute(
            "INSERT INTO kind_alias (old_kind, new_kind, created_at) VALUES (?,?,?)",
            (str(old_kind), str(new_kind), stamp),
        )
    conn.commit()
    return len(aliases)


# ── slug 别名（M6 T5）───────────────────────────────────────────────
#
# ⚠️ 它解决的是一个**结构性**矛盾：spec §4.2 ④ 要求「slug 重命名/删除必须级联」，
# 而 §7/§9 又说 `data/vigil.db` 只读 ⇒ **级联只能发生在读取层**。
# 于是 `store._EFFECTIVE_KIND` 写成
# `COALESCE(ka2.new_kind, ost.kind, ka.new_kind, i.kind)`，而这张表就是 `ka`/`ka2`。


def set_kind_alias(
    conn: sqlite3.Connection, *, old_kind: str, new_kind: str,
    now: int | None = None,
) -> str:
    """记下「旧 slug 现在叫新 slug」。返回**最终**落进表里的 `new_kind`。

    ⚠️ **写入时压平，读取时只做一次 JOIN**（这是本函数存在的全部理由）。
    别名可以链式追加（`life → daily` 之后又 `daily → daily_v2`）。若表里留着链，
    读路径就得用**递归 CTE** 才能算出最终 slug——而那会把复杂度塞进
    `store.py` 的**四处**查询里（`_ITEM_COLS` / `_item_filters` / `kind_counts`
    两处 / `window_items`），漏一处就是静默的错分类。
    在这里压平，读路径永远只面对「一层映射」。

    ⚠️ 写入顺序在 API 侧是承重的：**先写别名、再写配置文件**。
    反过来的话，中间崩溃会留下「配置里已经没有 life 了、但别名还没建」⇒
    条目永久悬空；而先写别名的话，中间崩溃只留下一条约不到的别名（无害，
    配置写好之后就生效了）。
    """
    if not old_kind or not new_kind:
        raise OverlayError("别名两端都不能为空")
    if old_kind == new_kind:
        raise OverlayError(
            f"别名两端相同（{old_kind}）——那是一条什么都不做的映射，"
            "多半是改名时把新旧名字填成了同一个"
        )

    stamp = int(time.time()) if now is None else now

    # ① 先跟到底：新名字自己可能已经被改过名
    final = new_kind
    seen = {old_kind}
    while True:
        row = conn.execute(
            "SELECT new_kind FROM kind_alias WHERE old_kind = ?", (final,)
        ).fetchone()
        if row is None:
            break
        if row[0] in seen:
            # 环只可能来自手工改库，但**死循环比报错糟得多**——它会挂住整个请求，
            # 而请求是跑在线程池里的，几个环就能把服务拖垮。
            raise OverlayError(
                f"别名成环：{old_kind} → … → {row[0]}。"
                "这张表是程序维护的，出现环说明有人手工改过它。"
            )
        seen.add(row[0])
        final = str(row[0])

    # ② 链式传播：所有指向 old_kind 的行一起走到 final
    conn.execute(
        "UPDATE kind_alias SET new_kind = ? WHERE new_kind = ?", (final, old_kind)
    )
    # ③ 记下/更新自己
    conn.execute(
        "INSERT INTO kind_alias (old_kind, new_kind, created_at) VALUES (?,?,?)"
        " ON CONFLICT(old_kind) DO UPDATE SET new_kind = excluded.new_kind",
        (old_kind, final, stamp),
    )
    conn.commit()
    return final


def kind_aliases(conn: sqlite3.Connection) -> dict[str, str]:
    """`{旧 slug: 新 slug}`。**已是压平后的**（见 `set_kind_alias`）。"""
    return {
        str(a): str(b) for a, b in conn.execute("SELECT old_kind, new_kind FROM kind_alias")
    }


def prune_kind_aliases(
    conn: sqlite3.Connection, *, live_slugs: Sequence[str]
) -> int:
    """把别名表剪到与**当前**类目表一致。返回删了几行。

    **删条件**：`old_kind in live_slugs or new_kind not in live_slugs`。

    ⭐ **不变量是承重的：一行的 `old_kind` 若仍是现存 slug，该行必须删掉。**
    因为 `store._EFFECTIVE_KIND` 里作用在 overlay 值之上的那个 `ka2`
    （`LEFT JOIN ov.kind_alias ka2 ON ka2.old_kind = ost.kind`）**无条件优先于
    `ost.kind`**——只要「表里不可能有 `old_kind` ∈ 现存 slug 的行」，`ka2`
    就不可能把用户手工选的**合法**类目顶掉（`ost.kind` 是合法 slug 时匹配不到
    任何行 ⇒ `ka2.new_kind` 是 NULL ⇒ 落回 `ost.kind`）。
    **反例（实测）**：删掉 `life` 再把它加回来之后，旧行 `life→life_v2` 还在 ⇒
    界面上手工选的 `life` 会被顶成 `life_v2`。⇒ **剪枝与 `ka2` 是同一个修复的两半。**

    另一条：`new_kind` 不在现存 slug 里 ⇒ 该行指向一个不存在的目标 ⇒ 删掉
    （它映射不出任何合法类目，留着只会把条目引到裸 slug）。

    ⚠️ **它还是「改回原名」那条路的必需品，不只是清理**：改名 `A→B` 之后再把
    `B` 改回 `A`，若 `A→B` 还在表里，`set_kind_alias(B, A)` 的第一步就会跟到
    `A→B` ⇒ 判成**环** ⇒ 抛 `OverlayError`（从前这里是 500，用户**永远回不去**）。
    先剪枝（`A` 已在 live 集里）就没有这个环。

    ⚠️ 调用方的 `live_slugs` 是**即将生效的那套 slug**；调用时机见
    `api.commit_categories` 与 `api_update_category` / `api_delete_category`
    （后者多含一个待废 slug，理由写在那里）。
    """
    live = set(live_slugs)
    rows = [
        (str(a), str(b))
        for a, b in conn.execute("SELECT old_kind, new_kind FROM kind_alias")
    ]
    dead = [old for old, new in rows if old in live or new not in live]
    for old_kind in dead:
        conn.execute("DELETE FROM kind_alias WHERE old_kind = ?", (old_kind,))
    conn.commit()
    return len(dead)


# 上一次对账时 `categories.toml` 的 `(mtime_ns, size)`。`None` = 还没对过账。
#
# ⚠️ 做成**模块级**是因为对账挂在**每一次查询入口**上（`store._ensure_overlay`），
# 而闸门必须跨调用、跨连接记住"这份文件我看过了"。
#
# ⚠️ **这里从前写的是「最坏只是两个线程各对一次账（剪枝幂等），不需要锁」——
# 那是错的论证**（Fix loop 3 / FIX 2，reviewer 指出）。真正的问题不是"剪两次"，
# 而是**剪的时机**：写端点是「先写别名、后写文件」，
# 对账若在**那个窗口**里跑（它读的是**磁盘上那份还是旧的文件**），
# 就会把刚写进去的那条别名当成"目标不存在"**剪掉** ⇒ 文件写完之后
# 条目**永久悬空**（spec §4.2 ④）。⇒ 需要一把锁，见 `ALIAS_LOCK`。
_LAST_RECONCILE: tuple[int, int] | None = None


# 对账**连续**失败的次数与升级阈值（M6 T10；T5 延迟清单里那条「T10 之前收」）。
#
# ⚠️ **为什么需要它**：不变量「`kind_alias` 里没有 `old_kind ∈ 现存 slug` 的行」
# **只由 `reconcile_if_stale` 的剪枝维持**，而剪枝失败时只有一句 `_log.warning`
# （删掉那句 warning 全量照样绿）。于是**长期破着的不变量零信号**：
# 条目会显示出**旧类目**（`old_kind` 仍是现存 slug 的那一行顶掉了合法映射），
# 而 `vigil_exit_check.py` 的「类目越界」判据**查不到这种破法**。
#
# ⚠️ 「连续」是承重的：一次成功就已经对平了，之后从 1 数起——否则一个跑了
# 一整天的进程会把偶发失败累计成 ERROR，而噪声日志的下场是被关掉。
_RECONCILE_FAILURES = 0
_RECONCILE_LOUD_AFTER = 3


# ⚠️ **别名表 + `categories.toml` 的写侧互斥锁**（Fix loop 3 / FIX 2）。
#
# 放在 `overrides` 而不是 `api` 的理由：对账是 `store._ensure_overlay` 触发的，
# 而 `refine` / `digest` 也会走到那条路（它们**不 import api**，也**不该**为了
# 一把锁去 import）。锁必须在**两端都够得着**的地方。
#
# * **对账**（`reconcile_if_stale`）取它 ⇒ 不会在别人的写窗口里剪。
# * **写端点**（`api_update_category` / `api_delete_category` / `api_category_undo`）
#   取它，覆盖 `set_kind_alias → snapshot → 写文件` **整段**。
#
# ⚠️ **保留 `RLock`（不是 `Lock`），但理由按实情写**（M6 终审 #16 / 勘误 E7：
# **没被考验的理由不许写成结论**）：
#   · 从前的写法断言「写端点自己也会经 `store.kind_counts(...)` 走到对账
#     （同一条线程）⇒ 可重入才不死锁」。**今天不成立**：`api_delete_category`
#     那次 `store.kind_counts()` 在**取锁之前**；`api_update_category` 在锁内
#     走的是 `prune_aliases`（不经过对账）。
#   · **实测**（终审记录在 `final-review.md`）：`_is_owned()` 命中 **0 次**，
#     把 `RLock` 换成 `Lock` **照样全量绿** ⇒ 可重入的必要性**没有被任何判据
#     考验过**，所以这里**不许**把它写成"必须"。
#   · 那为什么还留着它：**防御性**，而且防的是一个具体的形状——「持锁时再走一次
#     查询入口」。对账（`reconcile_if_stale`）**挂在每一个查询入口上**、
#     **也取这把锁**；只要将来有人在锁内加一次 `store.kind_counts` / `get_item`
#     （"顺手读一下现在有几条"是最自然的下一步），`Lock` 就会**死锁**——没有
#     超时、没有日志、服务卡在一条请求上。代价只是一个可重入计数。
#   · 取用顺序只有一个方向（`write_lock` → `ALIAS_LOCK`），对账那一侧**从不**
#     去拿 `write_lock`。
#
# ⚠️ **范围（如实写，不编理由）**：这把锁是**进程内**的 ⇒ **服务进程内已闭合**。
# 跨进程仍有一个**毫秒级窗口**：`refine` / `digest` 是**别的进程**，
# 它们触发对账时可能正好落在服务的写窗口里——那时会剪掉那条还没生效的别名，
# 条目回落到**原来那个仍现存的 slug**（不是永久悬空：下一次写/对账会把它纠正回来）。
# **单用户工具下未处理**：真要闭合它得跨进程文件锁，
# 而 `lock.SingleInstance` 保护的是 QQ 库与主库、语义不同，不能顺手挪用。
ALIAS_LOCK = threading.RLock()


def reconcile_if_stale() -> int:
    """配置若被**外部**改过，就把 `kind_alias` 对齐到当前的 slug 集。返回剪了几行。

    ⚠️ **这一条是为了「手改 `categories.toml`」**（Fix loop 2 / FIX 2）：
    本里程碑**明确邀请手改**（`api.py`：「配置仍是唯一真相源…只是从此有了一个比
    手写更常见的来源」；`refine.py`：「改 categories.toml 即改提示词」），
    而手改**不经过任何写端点** ⇒ 剪枝跑不到 ⇒ 不变量破。
    实测的破法：手工把某条条目的类目改成 `life` → 改名 `life→life_v2` →
    **手改把 `life` 加回配置** ⇒ 条目仍显示 `life_v2`（`ka2` 顶掉了合法的
    `life`），而 `life` 的角标是 0、出口检查也看不见。

    ⚠️ **不能用 `ov` 那个 attach**：读路径 attach 的是 `mode=ro`（设计如此，
    见 `attach_readonly`），剪枝是**写** ⇒ 这里自己开一条可写的
    `overrides.connect()`，而且**只在真的要剪时才开**（见下面的闸门）。

    ⚠️ **闸门是 `stat`**：文件没变 ⇒ 立刻返回，不连库、不解析。判据取
    `(st_mtime_ns, st_size)` **两个都记**：只记 mtime 的话，同一秒内的两次写入
    （或某些文件系统上粗糙的 mtime 粒度）会被漏掉；只记 size 的话，
    「`life` 换成 `life_v2` 又换回来」这种等长的改动会被漏掉。

    ⚠️ **闸门只在成功之后置位**（Fix loop 3 / FIX 1，reviewer 实测）：从前是
    **动手之前**就置 ⇒ 剪枝抛一次**瞬时**异常（`database is locked`、磁盘忙）
    就**再也不会重试**，而调用方拿到的是 0（看起来"已经对过账了"）
    ⇒ **不变量静默地一直破着**。现在失败不置位 ⇒ 下一次查询自然重试。

    ⚠️ 与 `_ensure_overlay` 的局部 import 同形，这里也**局部 import `categories`**：
    `overrides` 与 `categories` 谁先被 import 都不该出问题。
    ⚠️ 配置文件**不存在 / 读不出来**⇒ **什么都不做**：这是挂在**每一次查询**上的
    函数，绝不能让它把查询入口炸掉（真正要报错的是 `/api/categories` 与
    `save_categories`，它们照旧响亮）。
    """
    global _LAST_RECONCILE, _RECONCILE_FAILURES

    from . import categories as categories_mod

    path = categories_mod.DEFAULT_CATEGORIES

    # ⚠️ **锁必须罩住「读文件 + 判断 + 剪枝」整段，不能只罩剪枝那一句**
    # （Fix loop 3，实测踩出来的）：只罩剪枝的话就是**经典 TOCTOU**——
    # 读线程在写窗口里先把**旧**文件读进内存（那里面还有 `life`），
    # 然后在锁上等到写者收工，再拿**手里那份过期的解析结果**去剪
    # ⇒ 照样把刚写进去、已经生效的别名剪掉。
    # **本条用例第一次就是这么红的**（我先写的版本只罩了剪枝那一句）。
    with ALIAS_LOCK:
        try:
            st = path.stat()
        except OSError:
            # 文件不在（或读不到 stat）⇒ 没有配置可对账。
            # ⚠️ 不设闸门：文件出现时（mtime/size 变了）下一次就轮到它了。
            return 0
        key = (st.st_mtime_ns, st.st_size)
        if key == _LAST_RECONCILE:
            return 0
        try:
            cats = categories_mod.load_categories(path)
        except categories_mod.CategoryError:
            # ⚠️ **这一支置闸门**：坏文件不必被**每一句查询**反复解析一遍
            # （文件被修好时 mtime 会变，那时自然重来）。它**不是** FIX 1 说的那种
            # 瞬时失败——它是"这份文件此刻就是这么坏"，重试读的还是同一份。
            _LAST_RECONCILE = key
            return 0
        try:
            conn = connect()
            try:
                pruned = prune_kind_aliases(conn, live_slugs=[c.slug for c in cats])
            finally:
                conn.close()
        except sqlite3.Error as exc:
            # ⚠️ **一次瞬时失败不许把读路径变成 500**（FIX 1 的另一半）：
            # 对账挂在**每一次查询**上，而 overlay 上有别的写者（T7 的 job、
            # 另一次干预、另一个进程）⇒ 偶发 `database is locked` 是**预期内**的。
            # 判据与 `group_names_map` 的降级同族：**显示路径宁可降级，也不整页黑**。
            # ⇒ 响亮地记一条日志（不是静默），并且**不置闸门** ⇒ 下一次查询重试。
            #
            # ⚠️ **连续失败到阈值就升 ERROR**（M6 T10 / T5 延迟清单）：
            # 一次失败是预期内的（overlay 上有别的写者），**持续**失败不是——
            # 它意味着那个不变量正**一直**破着，而这件事**只有这里能说**：
            # 剪枝失败的痕迹只在这条日志里，`vigil_exit_check.py` 的越界判据
            # 看不见它（错的是映射，不是类目名）。
            _RECONCILE_FAILURES += 1
            if _RECONCILE_FAILURES >= _RECONCILE_LOUD_AFTER:
                _log.error(
                    "overlay 对账**连续**失败 %d 次（闸门没关，下一次查询还会重试）：%s"
                    "\n⚠️ 后果：别名表已与 categories.toml 不一致 ⇒ 改过名的条目"
                    "会显示**旧类目**（`old_kind` 仍是现存 slug 的那一行顶掉了"
                    "合法映射），而这一点在其它任何判据里都看不见。",
                    _RECONCILE_FAILURES, exc,
                )
            else:
                _log.warning("overlay 对账失败（闸门没关，下一次查询会重试）：%s", exc)
            return 0
        # ⚠️ **成功之后才置闸门**（FIX 1）：从前的写法是**动手之前**就置
        # ⇒ 一次瞬时失败（锁库、磁盘满）就**永不重试**，而调用方拿到的是 0
        # （看起来"已经对过账了"）⇒ **不变量静默地一直破着**。
        _LAST_RECONCILE = key
        # ⚠️ 成功 ⇒ **连续**失败计数归零（M6 T10）：不变量此刻已经对平，
        # 之后的失败是新的计数起点——否则偶发失败会被累计成 ERROR（噪声）。
        _RECONCILE_FAILURES = 0
        return pruned


def _state_of(conn: sqlite3.Connection, item_id: int) -> tuple[str | None, int]:
    """物化状态里一条 item 的 `(kind, deleted)`；没有行 ⇒ `(None, 0)`（= 没被干预过）。

    ⚠️ 取 `item_state` 而不是自己把事件日志再折叠一遍：那张表的契约就是
    「与折叠结果恒等」（`test_rebuild_state_matches_incremental` 守着）。
    """
    row = conn.execute(
        "SELECT kind, deleted FROM item_state WHERE item_id = ?", (item_id,)
    ).fetchone()
    return (row[0], int(row[1])) if row else (None, 0)


def _reject_non_tail_undo(
    conn: sqlite3.Connection, *, edit_id: int, item_id: int, action: str, tail: int
) -> None:
    """非尾部事件的删除：会**静默弄坏状态**的那两种形状，响亮拒绝。

    `undo` 的 docstring 里有完整论证。两条判据：

    * **墓碑行一律拒绝**——它唯一的职责就是 D15 那道「永不被重抽复活」的保护。
      撤掉它不会让条目回来（它对 `item_state` 惰性），却会让那条源消息失去保护；
      而且整个状态**看上去一点没变**（终审记的第二形态：用户点「撤销」、
      界面毫无反应、系统说成功）。
    * **撤掉之后条目会从「可见」变成「被软删」⇒ 拒绝**。这是终审 Important-1 的
      那一格：`delete` 之后再 `set_kind` 的条目本来是可见的，撤掉那条 `set_kind`
      会让它之前那条软删**重新生效**，条目从 Web 与日报同时消失，
      而调用方拿到的是「撤销成功」。

    ⚠️ 试算是**真删一次再回滚**（savepoint），不是把折叠规则在这里抄第二遍
    ——「两份实现会漂移」是本项目反复吃亏的形态，而这正是判据唯一说得清的地方。

    ⚠️ 抛的是 **`UndoConflict`**（`OverlayError` 的子类）而不是裸的 `OverlayError`：
    API 侧要把它映成 409，而把 overlay 的**基础设施故障**映成 5xx——
    两种病混在一个类型里，就会得到「overlay 挂不上 ⇒ 客户端看到『与日志状态冲突』」
    这种把排障指向错方向的映射（M5 收尾 scoped re-review A2）。
    """
    if action == ACTION_TOMBSTONE:
        raise UndoConflict(
            f"edit {edit_id} 是 item {item_id} 的墓碑行，不能单独撤：它不是一次"
            "独立操作，而是那条删除事件的**一半**（垫在 delete 行之前）。撤掉它"
            "不会让条目回来（墓碑行对 item_state 惰性），却会让这条源消息失去"
            "「永不被重抽复活」的保护（D15）——`refine --redo` 会把条目以新 item_id"
            "抽出来。要让条目回来，请撤销那条 delete 事件。"
        )
    before_deleted = _state_of(conn, item_id)[1]
    conn.execute("SAVEPOINT undo_probe")
    try:
        conn.execute("DELETE FROM item_edits WHERE edit_id = ?", (edit_id,))
        rebuild_state(conn)
        after_deleted = _state_of(conn, item_id)[1]
    finally:
        # ⚠️ `ROLLBACK TO` 只撤掉试算写进去的东西，**不碰**调用方在这之前
        # 未提交的写入（那是 FIX 2 最重的一半，见下面失败分支的注释）。
        conn.execute("ROLLBACK TO SAVEPOINT undo_probe")
        conn.execute("RELEASE SAVEPOINT undo_probe")
    if before_deleted == 0 and after_deleted == 1:
        raise UndoConflict(
            f"edit {edit_id} 是 item {item_id} 的**最后一条**事件，且撤掉它会让"
            f"这个条目**重新被软删**（那条更早的 delete 会重新生效），但它不是"
            f"日志尾部（尾部是 edit {tail}）：用户点的是「撤销改分类」，看到的会是"
            "条目从 Web 与日报同时消失——而接口返回成功。请先撤掉它之后的编辑，"
            "或直接撤销那条 delete 事件。"
        )


def is_deleted(conn: sqlite3.Connection, item_id: int) -> bool:
    """这条 item 的**最终态**是不是「被软删」——`_state_of` 的公开门面。

    ⚠️ 存在理由是**跨模块调用方**（`reorder.apply_reorder` 在写之前要复查，
    见 T6 复审 Important-2 的 TOCTOU）。与 `store.dedupe_batch` 是
    `_dedupe_batch` 的公开别名**同一个形状**：跨模块读私有名会让
    「哪天改名/改语义」变成一件没人拦得住的事，而让调用方自己抄一句
    `SELECT deleted FROM item_state` 是**第二份实现**——它一定会漂移
    （本项目反复吃亏的那个形状）。
    """
    return _state_of(conn, item_id)[1] == 1


def undo(conn: sqlite3.Connection, *, edit_id: int) -> bool:
    """撤掉一条事件，然后重建状态。事件不存在返回 False。

    ⚠️ 这里是全模块**唯一**一处 DELETE——append-only 的例外。
    它的安全性来自「事件表的尾部没有外部引用」：没有任何东西按 edit_id 持有指针。

    ⚠️ **非尾部事件不许静默弄坏状态**（M5 终审 Important-1 实测）。这条 DELETE
    只按 `edit_id` 删行，而日志是**按顺序折叠**的（每条目的最终态由它**最后一条**
    事件决定），于是从中间抽走一条会让它**后面**那些事件改换含义：

      * 抽走**墓碑行** ⇒ `deleted_msg_ids` 少一条 ⇒ `refine --redo` 会把被软删的
        条目**以新 item_id 复活**（直接违反 D15），而 `item_state` 看上去毫无变化。
      * 抽走**某条目的最后一条**事件（但它不是全局尾部）⇒ 该条目倒退回上一条
        事件决定的状态。最刺眼的形状是「先软删、后改分类」：撤销那次改分类会让
        条目**重新被软删**，从 Web 与日报同时消失，而接口返回
        `{"undone": true}`（终审用探针复现过）。
      * 其余形状（被后面事件覆盖的、或只是把类目退回基础值的）⇒ **放行**，
        且结果与终审之前逐字一致：`test_undo_accepts_an_explicit_edit_id` 钉着
        「撤一条被后面 delete 覆盖的 set_kind 必须返回 True」，而"撤掉我早先给
        某条改的类目"是正常用法，它退回 `items.kind` 正是用户要的。

    被拒绝的那两种形状一律抛 `UndoConflict`（`OverlayError` 的子类；API 侧映射成
    409，而基础设施故障映成 5xx），**在写之前**就抛出——调用方响亮地知道
    「这一撤会改变你看见的东西」，而不是静默地改掉它。

    ⚠️ 失败分支**不许** rollback（reviewer 实测的 FIX 2，见下）。
    """
    row = conn.execute(
        "SELECT item_id, action FROM item_edits WHERE edit_id = ?", (edit_id,)
    ).fetchone()
    if row is None:
        # ⚠️ **不许** rollback（reviewer 实测的 FIX 2）：调用方可能正开着一段事务
        # （T8 的"批量写 + 末尾校验"），回滚会把**它**写进去还没 commit 的东西一起丢掉
        # （实测 1→0）。失败分支本身什么都没改，不需要任何补偿动作。
        return False
    item_id, action = int(row[0]), str(row[1])
    tail = conn.execute("SELECT MAX(edit_id) FROM item_edits").fetchone()[0]
    if edit_id != tail:
        _reject_non_tail_undo(
            conn, edit_id=edit_id, item_id=item_id, action=action, tail=int(tail)
        )
    conn.execute("DELETE FROM item_edits WHERE edit_id = ?", (edit_id,))
    rebuild_state(conn)
    conn.commit()
    return True


def undo_batch(conn: sqlite3.Connection, *, actor: str) -> int:
    """撤掉**尾部连续的**、属于 `actor` 的全部事件。返回撤了几条。

    ⚠️ **为什么不能靠调用 `undo` N 次**：`undo` 一次只删尾部**一条**，
    而它带着 `_reject_non_tail_undo` 那道闸门。那道闸门是为「撤一条历史
    **中间**的事件」设的——那会让状态静默翻转（终审 Important-1：
    撤一条非尾部 `set_kind` 能让条目从 Web 与日报同时消失，而调用方拿到
    「成功」）。而**重排的 N 条是一个原子意图**，它们天然是尾部连续的一段，
    所以批量删它们**不会**碰到那个形状。

    ⚠️ **一旦遇到不属于 `actor` 的事件就停**——继续往下删就变成"从日志中间
    抽走事件"了，闸门要挡的正是那个。

    ⚠️ 与 `undo` 的另一处不同：本函数**只删、不判**。
    `_reject_non_tail_undo` 里那条「撤掉之后条目会从可见变成被软删 ⇒ 拒绝」
    在这里**不适用**——因为重排（`reorder.plan_reorder`）**已经跳过了软删的条目**，
    所以尾部这段 `set_kind` 背后不可能接着一条会重新生效的 `delete`。
    这条推理是**承重的**：调用方若绕过 `reorder` 自己造一批 `set_kind`，
    这条保证就不成立了。

    ⚠️ 删完走 `rebuild_state()`（**完整重算**，不是增量）：增量折叠只对
    「追加一条」成立，而删除是逆向操作。全表重算在这里是廉价的
    （单机自用、日志规模是几百到几千行），而"再写一套逆向折叠"是典型的
    「两份实现会漂移」——本项目反复吃亏的那个形状。
    """
    rows = conn.execute(
        "SELECT edit_id, actor FROM item_edits ORDER BY edit_id DESC"
    ).fetchall()
    ids: list[int] = []
    for edit_id, who in rows:
        if who != actor:
            break
        ids.append(int(edit_id))
    if not ids:
        return 0
    marks = ",".join("?" * len(ids))
    conn.execute(f"DELETE FROM item_edits WHERE edit_id IN ({marks})", ids)
    rebuild_state(conn)
    conn.commit()
    return len(ids)


def last_edit(conn: sqlite3.Connection) -> tuple[int, int, str] | None:
    row = conn.execute(
        "SELECT edit_id, item_id, action FROM item_edits ORDER BY edit_id DESC LIMIT 1"
    ).fetchone()
    return (int(row[0]), int(row[1]), str(row[2])) if row else None


def deleted_msg_ids(conn: sqlite3.Connection) -> frozenset[int]:
    """被软删条目对应的源消息 id——`refine` 靠它跳过重抽（D15）。

    ⚠️ **与 `item_state` 是同一次折叠**（裁定 R5）：墓碑化的是「**最终态**
    `deleted=1` 的 item 所引用的源消息」，不是"历史上有过 `delete` 事件"。
    `delete(i, msg=10)` 之后再 `set_kind(i)` ⇒ i 可见 ⇒ 10 **不**再被墓碑化
    （`test_delete_then_set_kind_last_wins` 早就裁定了这条语义；两次独立折叠会让
    两个视图对同一份日志给出**相反**答案——R13 同族）。

    ⚠️ **一条消息产出多条 item 时取 exists 语义**：该 msg 只要有**任意**一条
    最终态为 deleted 的 item 就墓碑化它。不这么做，重抽会把用户删掉的那条
    **复活**（直接违反 D15）；代价（同源的兄弟条目也会被冻结）见报告「疑虑」。
    """
    return frozenset(
        int(r[0])
        for r in conn.execute(
            "SELECT DISTINCT e.msg_id FROM item_edits e"
            " JOIN item_state s ON s.item_id = e.item_id"
            " WHERE s.deleted = 1 AND e.msg_id IS NOT NULL"
        )
    )


def rebuild_state(conn: sqlite3.Connection) -> None:
    """从事件日志**完整重算** `item_state`。幂等，任何时刻调都得到同一结果。"""
    conn.execute("DELETE FROM item_state")
    rows = conn.execute(
        "SELECT item_id, action, new_value FROM item_edits ORDER BY edit_id ASC"
    ).fetchall()
    for item_id, action, new_value in rows:
        if action == ACTION_DELETE:
            conn.execute(
                "INSERT INTO item_state (item_id, kind, deleted) VALUES (?, NULL, 1)"
                " ON CONFLICT(item_id) DO UPDATE SET deleted = 1",
                (item_id,),
            )
        elif action == ACTION_SET_KIND:
            conn.execute(
                "INSERT INTO item_state (item_id, kind, deleted) VALUES (?, ?, 0)"
                " ON CONFLICT(item_id) DO UPDATE SET kind = excluded.kind, deleted = 0",
                (item_id, new_value),
            )
        elif action == ACTION_TOMBSTONE:
            # 惰性：墓碑行对 `item_state` **没有任何表示**（这是承重的，
            # 见 `delete_item` 的 docstring）。显式写出来，而不是靠"落到 else"
            # ——下面那个 `else` 已经变成 `raise` 了。
            pass
        elif action == ACTION_SET_FIELD:
            # ⚠️ **与 tombstone 同形的惰性，实施时才确认的**（brief 只点名了
            # tombstone）：`item_state` 只有 `(kind, deleted)` 两列 ⇒ 字段降级
            # 在这张表里**没有表示**——它不是"没人管"，是"没有可写的地方"。
            # 既有用例 `test_deleted_msg_ids_ignores_msgs_of_items_that_are_not_deleted`
            # 正是走**真实写路径**（`_append`，不手搓 INSERT）造一条带 msg_id 的
            # `set_field`，少了这一支它会直接抛（实测）。
            pass
        else:
            raise OverlayError(
                f"未知的 action：{action!r}。合法值："
                f"{sorted({ACTION_SET_KIND, ACTION_SET_FIELD, ACTION_DELETE, ACTION_TOMBSTONE})}。"
                "⚠️ `ACTION_TOMBSTONE` 对 `item_state` **完全惰性**，它**不该**"
                "走到这里——若你正在给它加分支，先读 `delete_item` 的 docstring。"
            )


def _apply_to_state(
    conn: sqlite3.Connection, *, item_id: int, action: str, new_value: str | None,
    edit_id: int,
) -> None:
    """把一条新事件折叠进 `item_state`（增量，不重算全表）。

    ⚠️ 必须与 `rebuild_state` 的结果**恒等**——这是两者唯一的契约。
    `test_rebuild_state_matches_incremental` 守着它。
    """
    if action == ACTION_DELETE:
        conn.execute(
            "INSERT INTO item_state (item_id, kind, deleted) VALUES (?, NULL, 1)"
            " ON CONFLICT(item_id) DO UPDATE SET deleted = 1",
            (item_id,),
        )
    elif action == ACTION_SET_KIND:
        conn.execute(
            "INSERT INTO item_state (item_id, kind, deleted) VALUES (?, ?, 0)"
            " ON CONFLICT(item_id) DO UPDATE SET kind = excluded.kind, deleted = 0",
            (item_id, new_value),
        )
    elif action == ACTION_TOMBSTONE:
        # 惰性：墓碑行对 `item_state` **没有任何表示**（这是承重的，
        # 见 `delete_item` 的 docstring）。显式写出来，而不是靠"落到 else"。
        pass
    elif action == ACTION_SET_FIELD:
        # ⚠️ 与 tombstone 同形的惰性（见 `rebuild_state` 里那一支的论证）：
        # `item_state` 只有 `(kind, deleted)` 两列，字段降级在这里没有表示。
        pass
    else:
        # ⚠️ **未知 action 必须响亮**（M6 T10 / M5 遗留 #9）。此前是静默忽略，
        # 后果实测过：`_append(action="restore")` ⇒ 事件入库、`item_state`
        # 无表示、`rebuild_state()` 之后**该 item 整行消失**。
        # ⚠️ `rebuild_state` 里也有同一支，**少一处就会让两者不再恒等**
        # （而那正是这两个函数唯一的契约，`test_rebuild_state_matches_incremental`
        # 守着它）——"一个响一个不响"比"两个都静默"更难查。
        raise OverlayError(
            f"未知的 action：{action!r}。合法值："
            f"{sorted({ACTION_SET_KIND, ACTION_SET_FIELD, ACTION_DELETE, ACTION_TOMBSTONE})}。"
            "⚠️ `ACTION_TOMBSTONE` 对 `item_state` **完全惰性**，它**不该**"
            "走到这里——若你正在给它加分支，先读 `delete_item` 的 docstring。"
        )
