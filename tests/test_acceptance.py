"""`vigil_acceptance.py` 的判据守卫（M6 终审 **F3**，Important）。

⚠️ 这个脚本不是普通工具，是**验收证据的生成器**：`vigil_exit_check.py` 的末尾
直接指引用户跑它（`--n 100 --m 20 > 新抽样.md`），而 M1 那个「95/100」的出口
材料就是它出的。而它是三个仪器里**唯一**没跟上 M6 T5 的（`grep -rn
"vigil_acceptance" tests/` 从前是 **0 命中**）⇒ 人工验收会对着

  · 一个**已经不存在的类目名**（改名 `life → life_v2` 之后它还印「生活」，
    而网页上同一刻显示的是新名字），
  · 以及**界面上根本看不到的条目**（软删的，D15 说它在所有读取路径上都消失）

判「这条类别对不对」——**证据本身**被污染，而结论会写进里程碑出口。

⚠️ 本文件的两条用例各钉一半，都配了变异反证（见各自的 docstring）。
"""

from __future__ import annotations

import sqlite3
import sys

import pytest

import vigil_acceptance
from vigil import ingest, overrides, store
from vigil.categories import Category

# `messages` 由导出层（`vigil/ingest.py`）建，`store.ensure_schema` 不管它。
_MESSAGE_TABLES_DDL = """
CREATE TABLE IF NOT EXISTS messages (
    msg_id     INTEGER PRIMARY KEY,
    group_id   INTEGER NOT NULL,
    ts         INTEGER NOT NULL,
    sender_uid TEXT,
    content    TEXT NOT NULL
);
"""

# 类目表在测试里是**受控**的（与 `test_exit_check.py` 同一条判据）：真去读仓库里
# 那份 `config/categories.toml` 等于让这条用例跟着用户的配置红绿。
_LIVE = (Category(slug="life_v2", label="生活·新", desc="改过名之后的那一类"),)


def _fake_db(tmp_path, monkeypatch, *, rows: list[tuple[int, str, int]]) -> sqlite3.Connection:
    """造一个最小主库：`rows` 是 `(item_id, 库里的原值 kind, 是否软删)`。

    ⚠️ `sender_names` 必须建（`store._ITEM_JOINS` 会 LEFT JOIN 它，而
    `store.ensure_schema` **不管**这张表——它是导出层建的）：不建的话，
    被测的那条查询会红在 `no such table: sender_names` 上——**红在异常上**，
    与本用例要守的东西毫无关系（勘误 E7 第 4 格）。
    """
    db = tmp_path / "vigil.db"
    conn = sqlite3.connect(str(db))
    store.ensure_schema(conn)
    conn.executescript(_MESSAGE_TABLES_DDL)
    conn.executescript(ingest._SENDER_TABLE_DDL)
    for item_id, raw_kind, ts in rows:
        conn.execute(
            "INSERT INTO messages (msg_id, group_id, ts, sender_uid, content)"
            " VALUES (?, 100, ?, 'u1', ?)",
            (item_id, ts, f"源消息 {item_id}"),
        )
        conn.execute(
            "INSERT INTO items (item_id, kind, title, detail, event_ts, deadline_ts,"
            " group_id, actor_uid, place, amount, links, confidence, model,"
            " prompt_ver, created_at)"
            " VALUES (?, ?, ?, NULL, ?, NULL, 100, 'u1', NULL, NULL,"
            " '[]', 0.9, 'm', 'v1', 1)",
            (item_id, raw_kind, f"标题 {item_id}", ts),
        )
        conn.execute(
            "INSERT INTO item_sources (item_id, msg_id) VALUES (?, ?)", (item_id, item_id)
        )
    conn.commit()
    conn.close()

    # 类目表受控（现读的那一份）
    monkeypatch.setattr(vigil_acceptance, "load_categories", lambda *a, **k: _LIVE)
    # 抽样脚本读的是模块常量 `DB`（它没有 `--db` 参数）——这是它唯一的接缝。
    monkeypatch.setattr(vigil_acceptance, "DB", str(db))
    return conn


def test_the_sample_prints_the_effective_label_not_the_hardcoded_old_one(
    tmp_path, monkeypatch, capsys
):
    """⭐ **改名**方向：库里的原值是 `life`，别名把它映射到存活着的 `life_v2`。

    ⇒ 抽样表里必须是**当前**那个类目的中文名（`生活·新`），而不是硬编码表里
    M1 那个「生活」——后者是一个**已经不存在的**类目名，而网页上同一刻显示的
    是新名字（同一份数据，两处说法不同 = R13）。

    ⚠️ 变异反证（真跑过，见 fix 报告）：
      · 把 `load()` 的 `{store._EFFECTIVE_KIND}` 换回 `i.kind`（原值口径）
        ⇒ 本条**必红**（印出的是旧 slug 的映射结果）；
      · 把 `labels` 换回硬编码的 M1 七类表 ⇒ 本条**必红**（印出「生活」）。
    """
    from vigil import overrides as ov

    _fake_db(tmp_path, monkeypatch, rows=[(1, "life", 1_700_000_000)])
    ov.ensure_schema()                      # autouse 夹具已把 OVERRIDES_DB 挪到 tmp
    w = ov.connect()
    try:
        ov.set_kind_alias(w, old_kind="life", new_kind="life_v2")
    finally:
        w.close()

    monkeypatch.setattr(sys, "argv", ["vigil_acceptance.py", "--n", "1", "--m", "1"])
    assert vigil_acceptance.main() == 0
    out = capsys.readouterr().out

    assert "【生活·新】标题 1" in out, (
        "抽样表印的不是**有效类目**的当前中文名——人工会对着一个已经不存在"
        f"（或旧名）的类目判「这条类别对不对」：\n{out}"
    )
    assert "【生活】" not in out, (
        "硬编码的 M1 类目名又回来了：改名之后它印的是一个**不存在的**类目名\n" + out
    )


def test_soft_deleted_items_never_enter_the_sample(tmp_path, monkeypatch, capsys):
    """⭐ **软删**方向：D15 说软删的条目在所有读取路径上都不存在。

    ⇒ 人工验收的抽样清单里**不许**出现它（否则用户会对着一条界面上根本
    看不到的条目判类别），而表头那个总数也必须是**可见**的条数。

    ⚠️ 变异反证：把 `load()` 里的 `WHERE {store._NOT_DELETED}` 删掉
    ⇒ 本条**必红**（两条都进抽样、表头报 2）。
    """
    from vigil import overrides as ov

    _fake_db(
        tmp_path,
        monkeypatch,
        rows=[(1, "life_v2", 1_700_000_000), (2, "life_v2", 1_700_000_100)],
    )
    ov.ensure_schema()
    w = ov.connect()
    try:
        ov.delete_item(w, item_id=2, msg_id=2, actor="t", now=1_700_000_200)
    finally:
        w.close()

    monkeypatch.setattr(sys, "argv", ["vigil_acceptance.py", "--n", "5", "--m", "5"])
    assert vigil_acceptance.main() == 0
    out = capsys.readouterr().out

    assert "标题 2" not in out, "软删的条目进了抽样清单——界面上它根本不存在\n" + out
    assert "标题 1" in out
    assert "可见 items 共 1 条" in out, (
        "表头那个数必须与**抽样范围**同源（可见条数），否则两个数互相矛盾\n" + out
    )


def test_the_sample_never_writes_the_main_db(tmp_path, monkeypatch):
    """⚠️ 仪器**只读**主库：`file:...?mode=ro`（F3 把它从可写连接改过来的）。

    ⚠️ 它同时是"夹具真的落在了 tmp 上"的机械证据：主库 sha256 与 `-wal` 侧车
    一个字节都不许变（含只读连接可能动的 `-shm`——这一条按 D11 的老口径
    **不量 `-shm`**，只量主文件与 `-wal`）。
    """
    import hashlib
    import pathlib

    from vigil import overrides as ov

    _fake_db(tmp_path, monkeypatch, rows=[(1, "life_v2", 1_700_000_000)])
    monkeypatch.setattr(sys, "argv", ["vigil_acceptance.py", "--n", "1", "--m", "1"])
    db = pathlib.Path(vigil_acceptance.DB)
    before = hashlib.sha256(db.read_bytes()).hexdigest()

    assert vigil_acceptance.main() == 0
    assert hashlib.sha256(db.read_bytes()).hexdigest() == before
    # ⚠️ 顺带钉住"夹具没把真仓库拖进来"：这个根下只有 tmp 库与它的侧车。
    assert db.parent == tmp_path
    assert ov.OVERRIDES_DB != db          # 干预层是另一个库（autouse 夹具挪过）
