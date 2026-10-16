"""`vigil_exit_check.py` 的判据守卫（M6 T5 Fix loop 1 / FIX 4）。

⚠️ 这个脚本不是普通工具，是**冒烟 / 出口检查的仪器**：它那一项「类目越界」的判据
坏了会**误导里程碑验收**。而它从前读的是 **raw kind**，于是双向都不可信：

  · **误报**：合法改名（`life → life_v2`）之后条目显示的是新 slug、一切正常，
    而它报 `bad_kind > 0` ⇒ 把一次**做对了**的操作判成出口不合格；
  · **漏报**：`ost.kind` 指向一个已经不存在 slug 时它报 0 ⇒ 悬空条目从验收里溜过去。

所以这里**两个方向各钉一条**，且共用同一个「其余各项全过」的夹具
（那是个阳性对照：`test_a_legitimate_rename_is_not_reported...` 返回 0
⇒ 其余各项真的都过 ⇒ 另一条的 1 只能来自 `bad_kind`）。
"""

from __future__ import annotations

import sqlite3
import sys

import vigil_exit_check
from vigil import overrides, store
from vigil.categories import Category

# `messages` 由导出层（`vigil/export.py`）建，`store.ensure_schema` 不管它。
_MESSAGE_TABLES_DDL = """
CREATE TABLE IF NOT EXISTS messages (
    msg_id     INTEGER PRIMARY KEY,
    group_id   INTEGER NOT NULL,
    ts         INTEGER NOT NULL,
    sender_uid TEXT,
    content    TEXT NOT NULL
);
"""

# 类目表在测试里是**受控**的：真去读仓库里那份 `config/categories.toml`
# 等于让这条用例跟着用户的配置红绿（M6 的整个卖点就是让用户改那份文件）。
_LIVE = (
    Category(slug="notice", label="通知公告", desc="d1"),
    Category(slug="life_v2", label="生活", desc="d2"),
)


def _fake_repo(tmp_path, monkeypatch, *, raw_kind: str, overlay_kind: str | None = None):
    """造一个「其余各项全过」的临时库，并把类目表与 argv 换成受控的。"""
    db = tmp_path / "vigil.db"
    conn = sqlite3.connect(str(db))
    try:
        store.ensure_schema(conn)
        conn.executescript(_MESSAGE_TABLES_DDL)
        conn.execute(
            "INSERT INTO messages (msg_id, group_id, ts, sender_uid, content)"
            " VALUES (1, 100, 1700000000, 'u1', '原文')"
        )
        conn.execute(
            "INSERT INTO refine_runs (msg_id, refined_at, status, item_count,"
            " prompt_ver, err) VALUES (1, 1700000000, 'ok', 1, 'v1', NULL)"
        )
        conn.execute(
            "INSERT INTO items (item_id, kind, title, detail, event_ts, deadline_ts,"
            " group_id, actor_uid, place, amount, links, confidence, model,"
            " prompt_ver, created_at)"
            " VALUES (1, ?, '标题', NULL, 1700000000, NULL, 100, 'u1', NULL, NULL,"
            " '[]', 0.9, 'm', 'v1', 1)",
            (raw_kind,),
        )
        conn.execute("INSERT INTO item_sources (item_id, msg_id) VALUES (1, 1)")
        conn.commit()
    finally:
        conn.close()

    if overlay_kind is not None:
        overrides.ensure_schema()          # autouse 夹具已把 OVERRIDES_DB 挪到 tmp
        w = overrides.connect()
        try:
            overrides.set_kind(w, item_id=1, kind=overlay_kind)
        finally:
            w.close()

    monkeypatch.setattr(vigil_exit_check, "load_categories", lambda *a, **k: _LIVE)
    monkeypatch.setattr(sys, "argv", ["vigil_exit_check.py", "--db", str(db)])
    return db


def test_a_legitimate_rename_is_not_reported_as_out_of_range(tmp_path, monkeypatch, capsys):
    """⚠️ **误报**方向：库里是旧名 `life`，但别名把它映射到了存活着的 `life_v2`。

    ⇒ 条目**显示**的是 `life_v2`（四条读路径一致），这一项必须是 0。

    ⚠️ 变异反证：把 `_bad_kind_count` 换回
    `SELECT COUNT(*) FROM items WHERE kind NOT IN (…)` ⇒ 本条**必红**（报 1 ⇒ exit 1）。
    实测过：这正是改判据之前的真实行为（合法改名被报成越界）。
    """
    _fake_repo(tmp_path, monkeypatch, raw_kind="life")
    w = overrides.connect()
    try:
        overrides.set_kind_alias(w, old_kind="life", new_kind="life_v2")
    finally:
        w.close()

    assert vigil_exit_check.main() == 0, (
        "合法改名被判成类目越界——仪器把一次做对了的操作判成出口不合格\n"
        + capsys.readouterr().out
    )


def test_a_dangling_overlay_kind_is_reported(tmp_path, monkeypatch, capsys):
    """⚠️ **漏报**方向：库里 raw kind 合法（`notice`），但界面上那条显示的是
    `ost.kind = ghost`——一个**不存在**的 slug。

    ⇒ 这一项必须是 1（正是 spec §4.2 ④ 要禁的悬空状态）。

    ⚠️ 变异反证：把 `_bad_kind_count` 换回 raw 口径 ⇒ 本条**必红**（报 0 ⇒ exit 0）。
    """
    _fake_repo(tmp_path, monkeypatch, raw_kind="notice", overlay_kind="ghost")

    assert vigil_exit_check.main() == 1, (
        "悬空的 `ost.kind` 没被报出来——仪表对一个真实的越界视而不见\n"
        + capsys.readouterr().out
    )


# ── M6 T5 Fix loop 2 / FIX 4：仪器不许有副作用、不许 traceback ────────


def test_a_missing_overlay_library_is_not_created(tmp_path, monkeypatch):
    """⚠️ FIX 4：**只读仪器不许建库**。

    `store.kind_counts` 经 `attach_readonly` 会**就地建一个空的** overlay 库
    （那是读取路径刻意的语义）——但"检查"不该产生副作用，而且读完之后
    下一个读者会以为"这个用户有过干预记录"。

    ⚠️ "库不存在"时口径退化是**可证等价**的：没有任何干预/别名
    ⇒ `_EFFECTIVE_KIND` 的三处来源都没有行 ⇒ 有效类目 == 库里的原值。

    变异反证：把 `_bad_kind_count` 里那句 `if not …is_file(): return …` 删掉
    ⇒ 本条**必红**（库会被建出来）。
    """
    _fake_repo(tmp_path, monkeypatch, raw_kind="notice")
    overrides.OVERRIDES_DB.unlink(missing_ok=True)   # `store.ensure_schema` 建过它
    assert not overrides.OVERRIDES_DB.exists()

    assert vigil_exit_check.main() == 0
    assert not overrides.OVERRIDES_DB.exists(), "出口检查把 overlay 库建出来了"


def test_an_unattachable_overlay_is_a_criterion_not_a_traceback(
    tmp_path, monkeypatch, capsys
):
    """⚠️ FIX 4：overlay **挂不上** ⇒ 报一条 ✗ 判据 + 原因，**不是**裸 traceback。

    ⚠️ 仪器"坏掉"与"判过"是两件事：从前它直接 traceback 退出，脚本的退出码
    虽然是非 0，但**没有任何判据被印出来**——看输出的人只会以为脚本自己崩了，
    而真正要说的那句「这一项判不了」一个字都没有。

    变异反证：把 `_bad_kind_count` 里 `except overrides.OverlayError` 那两支删掉
    ⇒ 本条**必红**（异常冒出 `main()`，测试直接报 error）。
    """
    _fake_repo(tmp_path, monkeypatch, raw_kind="notice")
    overrides.ensure_schema()                       # 库**存在**（走的才是 attach 那一支）

    def boom(*_a, **_kw):
        raise overrides.OverlayError("只读挂载失败（探针）")

    monkeypatch.setattr(overrides, "attach_readonly", boom)

    # ⚠️ **把"抛异常"也变成断言失败**（T6 的教训：红的是异常时，那条断言
    # 根本没执行——看起来像红了，其实什么都没验）。这里显式接住，让它变成
    # 一句说清方向的 AssertionError。
    try:
        rc = vigil_exit_check.main()
    except BaseException as exc:  # noqa: BLE001
        raise AssertionError(
            f"仪器挂不上 overlay 时**抛了异常**而不是给出判据：{exc!r}"
        ) from exc
    assert rc == 1, "判不了的判据不许被算成通过"
    out = capsys.readouterr().out
    assert "判不了" in out, out
    assert "只读挂载失败（探针）" in out, out
