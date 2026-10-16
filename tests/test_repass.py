"""存量重判：只删不换 + 字段降级同步。

⚠️ 本文件比 brief 多出三组用例，对应计划勘误的 **E4 / E5 / E6**——
它们全是「变异全红但根本没被测到」的形状，brief 自带的七条一条都拦不住：

* **E4（批内去重）**：brief 的 `test_plan_deletes_duplicate_extras` 手工喂
  `verdicts={1: 1}`，**测的是配额逻辑本身，不是 `_produce` 的计数**。
  真库 item 290/296 的形状是「模型对同一条消息重抽两次」⇒ `n_new` 算成 2
  ⇒ 两条重复都留下、报「0 deleted」。只有**穿过 `repass()` 的端到端用例**
  才碰得到那条路径。
* **E5（LLM 失败 ≠ 产出 0 条）**：brief 里**没有任何一条**用例调 `repass()`，
  于是「批失败 → `verdicts=0` → 批量删条目」这条数据丢失路径全程无人守。
* **E6（不许物理删用户软删的行）**：brief 的测试里根本没有 overlay。

⚠️ brief 的 `test_apply_plan_downgrades_place` **按字面写不出来**：它不传
`judged`，而 `build_plan` 里 `if judged:` 为假 ⇒ 一个降级都算不出来 ⇒
断言必红。下面那条补上了 `judged`，另有一条端到端用例钉住
「`repass()` 真的把 `judged` 传下去了」——单元那条对「忘了传」是瞎的。
"""

from __future__ import annotations

import json
import pathlib
import sqlite3
from typing import Any

import pytest

from vigil import overrides, repass, store
from vigil.llm import LLMError, LLMResult

# messages + sender_names 的形状与 export.py 产出的完全一致。
# ⚠️ 这两张表**不由 `store.ensure_schema` 建**（它们是 export 的产物、真库里一直在），
# 而 `collect_source_messages` 与 `source_messages` 都会 JOIN 它们。
# brief 的测试夹具缺了这一段 ⇒ 它的 `_msg()` 会在 `no such table: messages` 上炸。
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


@pytest.fixture
def con():
    """内存库 + messages/sender_names。

    ⚠️ `uri=True` 不能省：`build_plan` 现在要读 overlay
    （`store.soft_deleted_item_ids`），而 `ATTACH 'file:...?mode=ro'`
    **只在连接带 `SQLITE_OPEN_URI` 时才被解析**——不带就抛 `OverlayError`
    （裁决 R4，四条生产连接都已带上）。brief 的夹具写的是
    `sqlite3.connect(":memory:")`，加上 E6 之后它会**每一条用例都炸**。
    """
    conn = sqlite3.connect(":memory:", uri=True)
    store.ensure_schema(conn)
    conn.executescript(_MESSAGE_TABLES_DDL)
    conn.commit()
    yield conn
    conn.close()


def _msg(conn, msg_id, content, *, group_id=100, ts=1_700_000_000, uid="u_1"):
    conn.execute(
        "INSERT INTO messages (msg_id, group_id, ts, sender_uid, content)"
        " VALUES (?,?,?,?,?)",
        (msg_id, group_id, ts, uid, content),
    )
    conn.commit()


def _item(conn, msg_id, *, title="标题", kind="notice", event_ts=1_700_000_000,
          group_id=100, place=None, deadline_ts=None, prompt_ver="v2"):
    cur = conn.execute(
        "INSERT INTO items (kind, title, detail, event_ts, deadline_ts, group_id,"
        " actor_uid, place, links, amount, confidence, model, prompt_ver, created_at)"
        " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (kind, title, None, event_ts, deadline_ts, group_id, "u_1", place,
         "[]", None, 0.9, "m", prompt_ver, event_ts),
    )
    conn.execute(
        "INSERT INTO item_sources (item_id, msg_id) VALUES (?,?)",
        (cur.lastrowid, msg_id),
    )
    conn.commit()
    return cur.lastrowid


def _soft_delete(item_id, msg_id):
    """走真实的人工干预层写一条软删事件（不是直接改表）。"""
    ov = overrides.connect()
    try:
        overrides.delete_item(ov, item_id=item_id, msg_id=msg_id, actor="test")
    finally:
        ov.close()


def _cfg():
    """`repass()` 只在 `conn is None` 时才用 config——但传 `None` 会让
    「实现日后真的读了 config」变成静默 AttributeError，所以给一个真的。"""
    from vigil.config import Config

    return Config(
        source_dir=pathlib.Path("."),
        output_db=pathlib.Path("unused.db"),
        groups=(),
    )


def _raw(quote: str, *, title="标题", kind="notice", **kw: Any) -> dict:
    """模型返回的一条 item 的原始形状。"""
    raw = {"quote": quote, "kind": kind, "title": title, "confidence": 0.9}
    raw.update(kw)
    return raw


def _stub_llm(monkeypatch, payload: object, *, input_tokens=10, output_tokens=5):
    """把 `repass.chat_json` 换掉。`payload` 给异常实例 ⇒ 每次调用都抛。"""
    seen: list[str] = []

    def fake(cfg, *, system, user, **kw):  # noqa: ANN001, ANN003
        seen.append(user)
        if isinstance(payload, BaseException):
            raise payload
        return LLMResult(
            payload=payload, input_tokens=input_tokens, output_tokens=output_tokens
        )

    monkeypatch.setattr(repass, "chat_json", fake)
    return seen


# ── 清单：只删不换 ───────────────────────────────────────────────


def test_plan_deletes_item_whose_message_no_longer_produces(con):
    """v3 判为推广 ⇒ 对应旧 item 该删。"""
    _msg(con, 1, "校园卡办理，需要的联系我")
    iid = _item(con, 1, title="校园卡办理")
    plan = repass.build_plan(con, verdicts={1: 0})
    assert plan.to_delete == [iid]


def test_plan_keeps_item_whose_message_still_produces(con):
    _msg(con, 1, "最早9.5")
    iid = _item(con, 1, title="宿舍最早入住时间")
    plan = repass.build_plan(con, verdicts={1: 1})
    assert plan.to_delete == []
    assert iid in plan.to_keep


def test_plan_deletes_duplicate_extras(con):
    """⭐ 真库 item 290/296 的形状：同一条消息产出了两条，v3 只产出 1 条。

    只留第一条，多余的删掉——**这是批内去重之外的第二道防线**。
    """
    _msg(con, 1, "征集一起去恐龙园活动")
    first = _item(con, 1, title="征集一起去恐龙园活动")
    second = _item(con, 1, title="征集一起去恐龙园活动")
    plan = repass.build_plan(con, verdicts={1: 1})
    assert plan.to_delete == [second]
    assert plan.to_keep == [first]


def test_plan_skips_items_referenced_by_digests(con):
    """⭐ 被日报引用的**不删**，进 `skipped_referenced` 并报告——不静默处理。"""
    _msg(con, 1, "校园卡办理，需要的联系我")
    iid = _item(con, 1, title="校园卡办理")
    store.save_digest(
        con, window_from=1, window_to=2, body_md="x", model="m",
        prompt_ver="v1", item_ids=[iid],
    )
    plan = repass.build_plan(con, verdicts={1: 0})
    assert plan.to_delete == []
    assert plan.skipped_referenced == [iid]


def test_apply_soft_deletes_and_leaves_the_row_in_place(con):
    """⭐ D15：删除 = **软删**——库里行仍在，只是视图层看不见（Fix loop 第 2 轮）。

    ⚠️ 变异判据：把 `apply_plan` 改回 `store.delete_items`（**物理删**）⇒ 本条红
    （`items` / `item_sources` 的行会消失）。这正是本轮的形状。
    """
    _msg(con, 1, "校园卡办理，需要的联系我")
    iid = _item(con, 1, title="校园卡办理")
    plan = repass.build_plan(con, verdicts={1: 0})

    stats = repass.apply_plan(con, plan)

    assert (stats.deleted, stats.tombstones) == (1, 1)
    assert con.execute(
        "SELECT COUNT(*) FROM items WHERE item_id=?", (iid,)
    ).fetchone()[0] == 1, "行必须在——D15 说「库里行仍在」"
    assert con.execute(
        "SELECT COUNT(*) FROM item_sources WHERE item_id=?", (iid,)
    ).fetchone()[0] == 1, "来源行也在（它不是物理删，没有悬空一说）"

    # 视图层：读路径必须看不到它（否则软删等于没删）
    got, total = store.search_items(con, q="校园卡", limit=50, offset=0)
    assert (got, total) == ([], 0)
    assert store.get_item(con, iid) is None

    # 墓碑集：`refine` 重抽时跳过的唯一依据（D15）
    ov = overrides.connect()
    try:
        assert overrides.deleted_msg_ids(ov) == frozenset({1})
    finally:
        ov.close()


def test_store_delete_items_is_still_the_physical_path(con):
    """`store.delete_items` 仍是**物理删**的唯一实现（repass 已不再用它）。

    ⚠️ 留一条用例钉住它：软删上线之后它就成了「没有调用方」的代码，
    而没有调用方的代码**最容易在下一次重构里被改坏而无人发现**。
    """
    _msg(con, 1, "校园卡办理，需要的联系我")
    iid = _item(con, 1, title="校园卡办理")
    with store.transaction(con):
        n = store.delete_items(con, [iid])
    assert n == 1
    assert con.execute("SELECT COUNT(*) FROM items WHERE item_id=?", (iid,)).fetchone()[0] == 0
    assert con.execute(
        "SELECT COUNT(*) FROM item_sources WHERE item_id=?", (iid,)
    ).fetchone()[0] == 0, "⚠️ 来源行必须一起删——否则留下悬空的 item_sources"


def test_downgrades_are_computed_but_not_written(con):
    """降级值仍要**算出来**（它进清单），但本轮**不执行**——overlay 没有 `set_field`，
    写主库会打破「`vigil.db` 一字未改」。

    ⚠️ `judged` **必须传**：降级值是从「新判据下那条消息产出的条目」算出来的，
    不传就等于没有任何降级可言（brief 的那一版漏了它，断言必红）。
    """
    _msg(con, 1, "大概这周会有面试 到时候具体时间通知大家")
    iid = _item(con, 1, title="面试通知", place="立德楼1阶")
    judged = {
        1: store.ExtractedItem(
            kind="notice", title="面试通知", detail=None, event_ts=1_700_000_000,
            deadline_ts=None, group_id=100, actor_uid="u_1", place=None,
            links=(), amount=None, confidence=0.9, src_msg_ids=(1,),
        )
    }
    plan = repass.build_plan(con, verdicts={1: 1}, judged=judged)
    assert plan.downgrades == [(iid, None, None)], "清单上必须写着这条要降级"

    stats = repass.apply_plan(con, plan)

    assert (stats.downgraded, stats.downgrades_deferred) == (0, 1)
    assert con.execute(
        "SELECT place FROM items WHERE item_id=?", (iid,)
    ).fetchone()[0] == "立德楼1阶", "本轮不许写主库——降级留给后续决定"


def test_upsert_fts_after_delete(con):
    """⚠️ 删了 items 之后 FTS 索引必须跟着走——否则搜索会返回不存在的条目。

    实测过这个形态：`items_fts` 是 external-content 表 + 三个触发器，
    `DELETE FROM items` 会触发 `items_fts_ad`。但**触发器只在 SQLite 自己
    执行 DELETE 时才触发**——用 `DROP`/重建绕过就不会。
    这条测试钉的是"我们走的是那条会触发触发器的主路径"。
    """
    _msg(con, 1, "校园卡办理，需要的联系我")
    iid = _item(con, 1, title="校园卡办理")
    got, _ = store.search_items(con, q="校园卡", limit=50, offset=0)
    assert len(got) == 1

    plan = repass.build_plan(con, verdicts={1: 0})
    repass.apply_plan(con, plan)

    got, _ = store.search_items(con, q="校园卡", limit=50, offset=0)
    assert got == [], "⚠️ 删了条目但 FTS 还能搜到 ⇒ 触发器没跑"


# ── E4：批内去重必须在算计数**之前** ────────────────────────────


def test_repass_dedupes_produced_items_before_counting(con, monkeypatch):
    """⭐ E4：模型对**同一条消息**重抽两次 ⇒ 新判据下仍只算产出 1 条。

    这正是真库 item 290/296 的真实行为（`_dedupe_batch` 的 docstring 记着）。
    `_produce` 若不去重（brief 的原样），`counts[1] = 2` ⇒ `keep_n = 2`
    ⇒ **两条重复都留下、报「0 deleted」**，而 `refine` 路径（`save_items`
    里有去重）只会写 1 行——同一个消息在两条路径上得到相反结论。

    变异：删掉 `_produce` 里的 `store.dedupe_batch(...)` ⇒ 本条红
    （`to_delete` 变空）。注意 brief 自带的
    `test_plan_deletes_duplicate_extras` **拦不住这个变异**——它手工喂
    `verdicts={1: 1}`，把被测的那一步整个跳过去了。
    """
    content = "通知：征集一起去恐龙园活动"
    _msg(con, 1, content)
    first = _item(con, 1, title=content)
    second = _item(con, 1, title=content)
    _stub_llm(monkeypatch, {"items": [_raw(content, title=content),
                                      _raw(content, title=content)]})

    plan, stats = repass.repass(_cfg(), api_key="k", conn=con)

    assert stats.errors == []
    assert plan.verdicts == {1: 1}, "重抽两次不算产出两条——那是同一份数据"
    assert plan.to_delete == [second]
    assert plan.to_keep == [first]
    assert plan.deaths == {second: "quota"}, (
        "重复项的死法是 quota（旧条目超额），**不是 a2**——"
        "它跟「模型没为这条产出」是两回事，混起来会让 --subset a2 顺手删掉重复项"
    )


def test_two_distinct_items_from_one_message_still_count_two(con, monkeypatch):
    """阳性对照：**真的**产出两条（标题不同）时不能被去重顺手吃掉。

    没有这一条的话，「把 produced 一律砍成一条」也能让上一条全绿。
    """
    content = "9.5 报到，9.6 开学"
    _msg(con, 1, content)
    keep_a = _item(con, 1, title="报到时间")
    keep_b = _item(con, 1, title="开学时间")
    _stub_llm(monkeypatch, {"items": [_raw(content, title="报到时间"),
                                      _raw(content, title="开学时间")]})

    plan, _ = repass.repass(_cfg(), api_key="k", conn=con)

    assert plan.verdicts == {1: 2}
    assert plan.to_delete == []
    assert sorted(plan.to_keep) == sorted([keep_a, keep_b])


# ── E5：LLM 失败 ≠ 产出 0 条 ─────────────────────────────────────


def test_llm_failure_never_deletes_items(con, monkeypatch):
    """⭐⭐ E5（最该拦的一条）：批失败**不是**「这条消息不再产出条目」。

    brief 的写法是 `verdicts.setdefault(m.msg_id, 0)`——而 0 在本命令的语义里
    等于「它的旧 item 全删」。⇒ 一次 429 / 一次超时 / 一次解析失败就能删掉
    一整批真实条目，**方向是数据丢失**。

    变异：把 `except` 分支改回 `verdicts.setdefault(m.msg_id, 0)` ⇒ 本条红
    （item 会进 `to_delete`）。
    """
    _msg(con, 1, "校园卡办理，需要的联系我")
    iid = _item(con, 1, title="校园卡办理")
    _stub_llm(monkeypatch, LLMError("HTTP 429 Too Many Requests"))

    plan, stats = repass.repass(_cfg(), api_key="k", conn=con)

    assert plan.to_delete == [], "批失败不许删任何一条——那是数据丢失方向"
    assert iid in plan.unjudged, "没判成的条目要明着列出来，不许静默消失"
    assert stats.errors, "失败必须响亮"
    assert stats.failed_batches == 1
    assert stats.batches == 0, "失败的那批不算跑过"


def test_apply_with_a_failed_batch_still_deletes_nothing(con, monkeypatch):
    """⭐ E5 的**端到端**形式：`--apply` 跑一轮、其中一批炸了 ⇒ 库里那批条目还在。

    ⚠️ 上一条断言的是 `plan.to_delete == []`——**计划层**。它对「apply 阶段
    多删了东西」是瞎的：`apply_plan` 完全可能删的是 `to_delete + plan.unjudged`
    （或 `+ to_keep`），而清单照样干净、照样打印得好看。这条盯的是**库**。

    变异：`store.delete_items(conn, plan.to_delete)` →
    `store.delete_items(conn, [*plan.to_delete, *plan.unjudged])` ⇒ 本条红。
    """
    _msg(con, 1, "校园卡办理，需要的联系我")
    iid = _item(con, 1, title="校园卡办理")
    _stub_llm(monkeypatch, LLMError("HTTP 429"))

    repass.repass(_cfg(), api_key="k", conn=con, apply=True)

    assert con.execute(
        "SELECT COUNT(*) FROM items WHERE item_id=?", (iid,)
    ).fetchone()[0] == 1, "批失败的那批条目被 --apply 删了——清单干净也救不回"


def test_kept_items_are_not_upgraded_with_the_new_place(con, monkeypatch):
    """**只降级、不升级**：新判据抽到了 `place` 而旧条目没有的，不许顺手写进去。

    理由不是"省事"：那样等于把新一轮的值悄悄写进**旧那一轮**的行，而
    `items.prompt_ver` 还写着 `v2`——事后没人说得清这一列是哪一版抽的。
    本次要修的只有「旧判据放行了不该放行的值」。

    变异：`new_place = place if place_supported(...) else None` →
    `new_place = src.place or place` ⇒ 本条红。
    """
    content = "周六下午4.00-8.00 在西太湖连隔板集合"
    _msg(con, 1, content)
    iid = _item(con, 1, title="集合", place=None)
    _stub_llm(monkeypatch, {"items": [_raw(content, title="集合", place="西太湖")]})

    plan, _ = repass.repass(_cfg(), api_key="k", conn=con)

    assert plan.downgrades == [], "旧条目本来就没有 place ⇒ 没有任何要降级的"
    repass.apply_plan(con, plan)
    assert con.execute(
        "SELECT place FROM items WHERE item_id=?", (iid,)
    ).fetchone()[0] is None, "新抽到的 place 被写进旧行了——那是升级，不是本次的范围"


def test_verdict_zero_still_deletes(con, monkeypatch):
    """⭐ 阳性对照：**确实**判为「产出 0 条」时照删。

    没有这一条，「失败不删」与「永远不删」就无法区分——而后者是本命令
    存在的意义（46 条广告条目要靠它清掉）的反面。
    """
    _msg(con, 1, "校园卡办理，需要的联系我")
    iid = _item(con, 1, title="校园卡办理")
    _stub_llm(monkeypatch, {"items": []})

    plan, stats = repass.repass(_cfg(), api_key="k", conn=con)

    assert stats.errors == []
    assert plan.verdicts == {1: 0}
    assert plan.to_delete == [iid]
    assert plan.unjudged == []


def test_unjudged_batch_does_not_leak_into_plan_buckets(con, monkeypatch):
    """两个批次、只炸一批：炸的那批原样不动，没炸的那批照常判。

    真库是 ~11 批——一次抖动只该影响它自己那一批。
    """
    _msg(con, 1, "校园卡办理，需要的联系我", group_id=100)
    bad = _item(con, 1, title="校园卡办理")
    _msg(con, 2, "9.5 报到", group_id=200)          # 不同群 ⇒ 另一批
    good = _item(con, 2, title="报到时间")

    def fake(cfg, *, system, user, **kw):
        if "校园卡" in user:
            raise LLMError("boom")
        return LLMResult(
            payload={"items": [_raw("9.5 报到", title="报到时间")]},
            input_tokens=1, output_tokens=1,
        )

    monkeypatch.setattr(repass, "chat_json", fake)
    plan, stats = repass.repass(_cfg(), api_key="k", conn=con)

    assert plan.to_delete == []
    assert plan.unjudged == [bad]
    assert good in plan.to_keep
    assert bad not in plan.to_keep


# ── E6：用户软删的行不许被物理删掉 ───────────────────────────────


def test_soft_deleted_item_is_not_physically_deleted(con):
    """⭐ E6：软删（D15）说「库里行仍在」——repass 把它物理删掉就把它升级成了
    物理删，而**此后 `undo` 救不回**（`item_edits` 里那条 delete 事件还在，
    对应的 item 行却没了）。

    变异：去掉 `build_plan` 里的软删过滤 ⇒ 本条红。
    """
    _msg(con, 1, "校园卡办理，需要的联系我")
    iid = _item(con, 1, title="校园卡办理")
    _soft_delete(iid, msg_id=1)

    plan = repass.build_plan(con, verdicts={1: 0})

    assert plan.to_delete == []
    assert plan.skipped_soft_deleted == [iid]
    repass.apply_plan(con, plan)
    assert con.execute(
        "SELECT COUNT(*) FROM items WHERE item_id=?", (iid,)
    ).fetchone()[0] == 1, "行必须还在——否则 undo 之后什么都没有可恢复"


def test_soft_deleted_item_does_not_consume_the_keep_quota(con):
    """软删的那条**不占**「该留几条」的名额。

    否则同一个消息的两条 item 里，用户软删了一条、留了一条，而新判据仍产出
    1 条时——名额会花在看不见的那条上，**用户还能看见的那条反而被删**。
    """
    _msg(con, 1, "9.5 报到")
    soft = _item(con, 1, title="软的")
    live = _item(con, 1, title="活的")
    _soft_delete(soft, msg_id=1)

    plan = repass.build_plan(con, verdicts={1: 1})

    assert plan.to_delete == []
    assert plan.to_keep == [live]
    assert plan.skipped_soft_deleted == [soft]


def test_plan_buckets_are_pairwise_disjoint(con):
    """五只桶两两不交——同一条 item 不许既在「要删」又在「要留」。

    这条不是形式主义：`item_sources` 允许一个 item 挂 ≥2 条源消息
    （`_dedupe_batch` 的来源取并集），所以同一个 item 完全可能从两个消息的
    视角各被看到一次。
    """
    _msg(con, 1, "a")
    _msg(con, 2, "b")
    i1 = _item(con, 1, title="一")
    i2 = _item(con, 2, title="二")
    con.execute("INSERT INTO item_sources (item_id, msg_id) VALUES (?,?)", (i2, 1))
    con.commit()
    _soft_delete(i1, msg_id=1)

    plan = repass.build_plan(con, verdicts={1: 0, 2: 1})

    buckets = [
        plan.to_delete, plan.to_keep, plan.skipped_referenced,
        plan.skipped_soft_deleted, plan.unjudged,
    ]
    seen: set[int] = set()
    for bucket in buckets:
        for iid in bucket:
            assert iid not in seen, f"item {iid} 出现在两个桶里"
            seen.add(iid)

    assert plan.skipped_soft_deleted == [i1]
    assert plan.to_delete == []
    assert plan.to_keep == [i2], (
        "同一条 item 挂在两条源消息上、一个判 0 一个判 1 ⇒ **以留为准**"
        "（删它是不可逆的，而留着顶多多一条旧条目）"
    )


def test_unjudged_beats_keep_for_a_shared_item(con):
    """同上，但另一头是**没判成**：`unjudged` 压过 `to_keep`。

    两个桶都不删，区别只在报告里怎么归类——而「我们没取得判定」比
    「判过了、留下」更该让人看见。安全方向一致：一个都不删。
    """
    _msg(con, 1, "a")
    _msg(con, 2, "b")
    shared = _item(con, 2, title="共享")
    con.execute("INSERT INTO item_sources (item_id, msg_id) VALUES (?,?)", (shared, 1))
    con.commit()

    plan = repass.build_plan(con, verdicts={2: 1}, unjudged_msg_ids=[1])

    assert plan.to_delete == []
    assert plan.unjudged == [shared]
    assert plan.to_keep == []


# ── 端到端：字段降级真的接上了 ───────────────────────────────────


def test_repass_wires_downgrades_end_to_end(con, monkeypatch):
    """`repass()` 必须把 `judged` 传到 `build_plan`——否则降级永远算不出来，
    而 M5 出口判据 3（不再有 `deadline_ts < event_ts`）根本达不到。

    ⚠️ 单元那条（`test_apply_plan_downgrades_place`）对「忘了传 judged」是**瞎的**：
    它自己把 judged 喂进去，证明的只是 `_plan_downgrades` 会算。

    变异：`repass()` 里 `build_plan(conn, verdicts=verdicts, judged=judged)`
    → `build_plan(conn, verdicts=verdicts)` ⇒ 本条红。
    """
    content = "大概这周会有面试 到时候具体时间通知大家"
    _msg(con, 1, content)
    iid = _item(con, 1, title="面试通知", place="立德楼1阶")
    _stub_llm(monkeypatch, {"items": [_raw(content, title="面试通知")]})

    plan, _ = repass.repass(_cfg(), api_key="k", conn=con)

    assert plan.downgrades == [(iid, None, None)], (
        "place 在源文里没有依据（旧判据没管过它）⇒ 必须在这里降级"
    )


def test_repass_downgrades_deadline_before_event_ts(con, monkeypatch):
    """出口判据 3 的那一条：`deadline_ts < event_ts` 的存量条目要降级。

    死线早于消息当天 ⇒ 它是过去的事实，不满足「需要行动」的语义。
    """
    content = "通知：档案袋封口时间：5 月 6 日"
    _msg(con, 1, content, ts=1_754_000_000)      # 2025-08-05 前后
    iid = _item(con, 1, title="档案袋", event_ts=1_754_000_000, deadline_ts=1_746_000_000)
    _stub_llm(monkeypatch, {"items": [_raw(content, title="档案袋")]})

    plan, _ = repass.repass(_cfg(), api_key="k", conn=con)

    assert plan.downgrades == [(iid, None, None)]
    stats = repass.apply_plan(con, plan)
    assert stats.downgrades_deferred == 1
    assert con.execute(
        "SELECT deadline_ts FROM items WHERE item_id=?", (iid,)
    ).fetchone()[0] == 1_746_000_000, "本轮不写主库：降级只在清单上"


def test_repass_does_not_rebuild_items(con, monkeypatch):
    """⭐ 「只删不换」：判为**仍产出**的消息，旧 item 原封不动、`item_id` 不翻新。

    整批替换（删旧插新）会让 `digest_items` 的 66 行全部悬空，并把 item_id 全体
    翻新（D16/spec R9 实测）。本命令**不许**走 `save_items`。

    变异：在 `repass()` 里改用 `store.save_items(...)` 重建 ⇒ 本条红
    （item_id 会变、且 items 表多出一行）。
    """
    content = "通知：最早9.5"
    _msg(con, 1, content)
    old = _item(con, 1, title="宿舍最早入住时间")
    _stub_llm(monkeypatch, {"items": [_raw(content, title="宿舍最早入住时间")]})

    plan, _ = repass.repass(_cfg(), api_key="k", conn=con, apply=True)

    assert plan.to_keep == [old]
    assert con.execute("SELECT COUNT(*) FROM items").fetchone()[0] == 1
    assert con.execute(
        "SELECT item_id FROM items"
    ).fetchone()[0] == old
    assert con.execute(
        "SELECT prompt_ver FROM items WHERE item_id=?", (old,)
    ).fetchone()[0] == "v2", "旧行不许被重写——它记的是当时那一轮的账"


def test_repass_is_idempotent(con, monkeypatch):
    """出口判据 5：重判跑两次，第二次**清单为空**。

    ⚠️ 软删之后这条的含义变了：`item_sources` 的行**还在**（D15），所以第二轮
    照样会重判这些消息——但它们的条目已经是「软删过」的，按 E6 的优先级进
    `skipped_soft_deleted`、**不再进 `to_delete`**。也就是说幂等性由**软删状态**保证，
    不再由「源消息被删光」保证。
    """
    content = "校园卡办理，需要的联系我"
    _msg(con, 1, content)
    iid = _item(con, 1, title="校园卡办理")
    _stub_llm(monkeypatch, {"items": []})

    first, _ = repass.repass(_cfg(), api_key="k", conn=con, apply=True)
    assert first.to_delete == [iid]

    second, stats = repass.repass(_cfg(), api_key="k", conn=con, apply=True)
    assert second.to_delete == [], "第二轮不该再删一次"
    assert second.skipped_soft_deleted == [iid], "它已经是软删态 ⇒ 进保护桶"
    assert stats.scanned_msgs == 1, "源消息还在（软删不删行）——第二轮照样重判"


def test_repass_apply_soft_deletes_the_whole_plan(con, monkeypatch):
    """一次 `--apply` 把清单上的删除都做掉（软删），且 stats 如实报数。"""
    content = "大概这周会有面试 到时候具体时间通知大家"
    _msg(con, 1, content)
    doomed = _item(con, 1, title="面试通知", place="立德楼1阶")
    _msg(con, 2, "校园卡办理，需要的联系我", group_id=100, ts=1_700_000_100)
    junk = _item(con, 2, title="校园卡办理", event_ts=1_700_000_100)

    def fake(cfg, *, system, user, **kw):
        raw = []
        if "面试" in user:
            raw = [_raw(content, title="面试通知")]
        return LLMResult(payload={"items": raw}, input_tokens=3, output_tokens=4)

    monkeypatch.setattr(repass, "chat_json", fake)
    plan, stats = repass.repass(_cfg(), api_key="k", conn=con, apply=True)

    assert plan.to_delete == [junk]
    assert plan.downgrades == [(doomed, None, None)]
    assert stats.deleted == 1, "软删 1 条"
    assert stats.tombstones == 1
    assert stats.downgraded == 0 and stats.downgrades_deferred == 1
    assert stats.kept == 1
    assert stats.input_tokens == 3 and stats.output_tokens == 4
    assert con.execute(
        "SELECT place FROM items WHERE item_id=?", (doomed,)
    ).fetchone()[0] == "立德楼1阶", "降级本轮不执行"
    assert con.execute("SELECT COUNT(*) FROM items").fetchone()[0] == 2, (
        "软删不删行——两条都还在（看不见的那条只是不再出现在视图里）"
    )
    assert store.get_item(con, junk) is None
    assert store.get_item(con, doomed) is not None


def test_build_plan_does_not_write(con):
    """`build_plan` 是**纯的**（只读 conn）——真库上「先跑一遍看清单」全靠这一条。

    ⚠️ `total_changes` 是连接级的「改了行没有」计数：SELECT 不动它，
    任何 INSERT/UPDATE/DELETE（包括触发器里的）都会。比「items 表看起来没变」
    强——后者对「顺手写了一条 refine_runs」「顺手更新了 created_at」是瞎的。

    变异：在 `build_plan` 里加任何一条写语句 ⇒ 本条红。
    """
    _msg(con, 1, "校园卡办理，需要的联系我")
    _item(con, 1, title="校园卡办理")

    before = con.total_changes
    repass.build_plan(con, verdicts={1: 0})

    assert con.total_changes == before, (
        "build_plan 写库了——那它就不再是「只看不改」的清单生成器"
    )


def test_partial_soft_delete_is_recoverable(con, monkeypatch):
    """中途炸掉会留下**部分软删**——但它**可撤销**，与物理删的「删一半」不同量级。

    ⚠️ 这是本轮的**已知取舍**（写在 `apply_plan` 的 docstring 里）：
    `overrides.delete_item` 逐事件 commit（它自己的设计），所以没有"整批一个事务"。
    换来的性质是：**每一条软删都能单独 undo**，而且主库一字未改。

    变异：把 `apply_plan` 改回物理删 ⇒ 本条红（行没了 ⇒ 撤销只能撤出个空）。
    """
    for i, content in ((1, "校园卡办理，需要的联系我"), (2, "办卡的来")):
        _msg(con, i, content, ts=1_700_000_000 + i)
        _item(con, i, title=f"广告{i}", event_ts=1_700_000_000 + i)
    plan = repass.build_plan(con, verdicts={1: 0, 2: 0})
    assert len(plan.to_delete) == 2

    from vigil import overrides as ov_mod

    real_delete = ov_mod.delete_item
    calls = {"n": 0}

    def flaky(conn, **kw):
        calls["n"] += 1
        if calls["n"] == 2:
            raise RuntimeError("第二条写到一半炸了")
        return real_delete(conn, **kw)

    monkeypatch.setattr(ov_mod, "delete_item", flaky)
    with pytest.raises(RuntimeError):
        repass.apply_plan(con, plan)

    # 第一条已经软删（部分状态），但——行还在、可以撤回来
    first = plan.to_delete[0]
    assert con.execute(
        "SELECT COUNT(*) FROM items WHERE item_id=?", (first,)
    ).fetchone()[0] == 1, "部分软删也**不许**变成物理删"
    ov = overrides.connect()
    try:
        assert overrides.deleted_msg_ids(ov) == frozenset({1})
        last = overrides.last_edit(ov)
        assert last is not None and last[1] == first
        assert overrides.undo(ov, edit_id=last[0]) is True
        assert overrides.deleted_msg_ids(ov) == frozenset()
    finally:
        ov.close()
    assert store.get_item(con, first) is not None, "撤销之后条目回到视图里"


def test_tombstones_cover_every_source_message(con):
    """⭐ E7：一个 item 有 ≥2 条源消息时，**每一条**都要落墓碑。

    否则 `refine --redo` 重抽兄弟消息会让条目**以新 item_id 复活**（违反 D15）。
    变异：`extra_msg_ids=srcs[1:]` → `extra_msg_ids=()` ⇒ 本条红。
    """
    _msg(con, 1, "校园卡办理，需要的联系我")
    _msg(con, 2, "办卡找我", ts=1_700_000_100)
    iid = _item(con, 1, title="校园卡办理")
    con.execute("INSERT INTO item_sources (item_id, msg_id) VALUES (?,?)", (iid, 2))
    con.commit()
    assert [r.msg_id for r in store.source_messages(con, iid)] == [1, 2], "造场景失败"

    repass.apply_plan(con, repass.build_plan(con, verdicts={1: 0}))

    ov = overrides.connect()
    try:
        assert overrides.deleted_msg_ids(ov) == frozenset({1, 2}), (
            "只墓碑化了第一条源消息——refine --redo 重抽兄弟消息会让条目复活"
        )
        # 反向：undo 之后条目回来、**墓碑全消失**（不是"撤了一条、还剩一条"）
        last = overrides.last_edit(ov)
        assert overrides.undo(ov, edit_id=last[0]) is True
        assert overrides.deleted_msg_ids(ov) == frozenset()
    finally:
        ov.close()
    assert store.get_item(con, iid) is not None


def test_refine_redo_does_not_revive_soft_deleted_items(con, monkeypatch):
    """⭐ D15 的**机械判据**：软删之后 `refine --redo` 重抽同一条消息，条目不复活。

    这是「墓碑」存在的唯一理由（`refine._tombstoned_msg_ids` 读的就是它）。
    变异：`apply_plan` 少落墓碑（`msg_id=None` + 无 extra）⇒ 本条红（条目会以新 id 复活）。
    """
    content = "校园卡办理，需要的联系我"
    _msg(con, 1, content)
    iid = _item(con, 1, title="校园卡办理")
    repass.apply_plan(con, repass.build_plan(con, verdicts={1: 0}))
    assert store.get_item(con, iid) is None

    from vigil import refine as refine_mod
    from vigil.llm import LLMResult

    def fake(cfg, *, system, user, **kw):
        return LLMResult(
            payload={"items": [{"quote": content, "kind": "notice",
                                "title": "校园卡办理", "confidence": 0.9}]},
            input_tokens=1, output_tokens=1,
        )

    monkeypatch.setattr(refine_mod, "chat_json", fake)
    stats = refine_mod.refine(
        _cfg(), api_key="k", conn=con, batch_size=1, redo=True,
        on_progress=lambda *_: None,
    )

    assert stats.skipped_deleted == 1, "墓碑没起作用——它会被重抽"
    assert stats.items_saved == 0
    assert con.execute("SELECT COUNT(*) FROM items").fetchone()[0] == 1, (
        "复活的判定就是「items 多了一行」——那正是 D15 要拦的"
    )


def test_apply_does_not_touch_vigil_db_bytes(tmp_path):
    """⭐ 机械断言：apply 跑完，`vigil.db`（主文件 + `-wal`）**逐字节相同**。

    ⚠️ 全部写操作都发生在 overlay，主库只有 SELECT ⇒ D11 的保证在 repass 上也成立。
    变异：`apply_plan` 改回 `store.delete_items`（会写主库）⇒ 本条红。

    ⚠️ 取指纹时**保持一条读连接开着**（T8 的实测教训）：生产里 reader 与写者并发，
    写者 close 时不是最后一个连接 ⇒ 不 checkpoint ⇒ 证据只留在 `-wal` 里。
    先「预热」一次读再取 before，是为了不让「侧车刚被建出来」被读成「写过了」。

    ⚠️⚠️ 这条连接**刻意是「可写」的**（与 `repass()` 里那条一样）：
    用 `mode=ro` 的话，物理删会直接抛 `attempt to write a readonly database`
    ——测试确实会红，但**指纹断言本身一辈子没被考验过**（假红掩盖零守护）。
    可写连接下，物理删会**成功**⇒ 唯一能拦住它的就是这条指纹断言。
    """
    import hashlib

    db = tmp_path / "vigil.db"
    conn = sqlite3.connect(str(db), uri=True)
    store.ensure_schema(conn)
    conn.executescript(_MESSAGE_TABLES_DDL)
    _msg(conn, 1, "校园卡办理，需要的联系我")
    _item(conn, 1, title="校园卡办理")
    conn.commit()
    conn.close()

    def fingerprint() -> dict:
        # ⚠️ **`-shm` 不在这张表里**（M6 T10 / M5 遗留 #23，终审已复现）。
        # 它的形状是**假红**：一条读写连接开-关且为最后一条时（合规、零写入），
        # `-wal`/`-shm` 双双变 `<missing>` ⇒ 守卫报「变了」，而人去看一个
        # **不存在的问题**。实测它的检出价值 = 0（「变了的集合」只有 `{-wal}`）。
        # ⚠️ 保留下来的 `-wal` 是**承重**的：`INSERT` 在 WAL 模式下先落 `-wal`，
        # 只盯主文件会漏掉「写了但还没 checkpoint」这一整类。
        out = {}
        for suffix in ("", "-wal"):
            p = db if not suffix else db.with_name(db.name + suffix)
            out[suffix or "main"] = (
                hashlib.sha256(p.read_bytes()).hexdigest() if p.is_file() else "<missing>"
            )
        return out

    rw = sqlite3.connect(str(db), uri=True)       # 与生产同形：可写连接
    try:
        rw.execute("SELECT COUNT(*) FROM items").fetchone()   # 预热
        before = fingerprint()
        plan = repass.build_plan(rw, verdicts={1: 0})
        stats = repass.apply_plan(rw, plan)
        after = fingerprint()
    finally:
        rw.close()

    assert stats.deleted == 1
    changed = [k for k in before if before[k] != after[k]]
    assert not changed, (
        f"apply 动了 data/vigil.db：{ {k: (before[k], after[k]) for k in changed} }"
    )
    # 效果对照：软删**确实生效了**（否则"指纹没变"可能只是"什么都没做"）
    check = sqlite3.connect(f"file:{db.as_posix()}?mode=ro", uri=True)
    try:
        assert check.execute("SELECT COUNT(*) FROM items").fetchone()[0] == 1, "行还在"
        assert store.get_item(check, 1) is None, "但视图层已经看不到它了"
    finally:
        check.close()
    ov = overrides.connect()
    try:
        assert overrides.deleted_msg_ids(ov) == frozenset({1})
    finally:
        ov.close()


def test_subset_a2_only_touches_the_contrastive_class(con, monkeypatch):
    """⭐ 用户裁定「本轮只上 a2 的 46 条」的机械判据。

    造形：批一（群 100）整批返回空 ⇒ a0；批二（群 200）为 B 产出了、唯独没为 C 产出 ⇒ a2。
    `subset="a2"` 之后：只有 C 的条目被软删，A 的条目进 `plan.deferred`（**本轮不动**）。

    变异：`_restrict_subset` 直接 `return`（不做过滤）⇒ 本条红（A 也被删）。
    """
    _msg(con, 1, "通知：明天的讲座", group_id=100)
    a0_item = _item(con, 1, title="讲座通知", group_id=100)
    _msg(con, 2, "通知：报到时间", group_id=200, ts=1_700_000_100)
    b_item = _item(con, 2, title="报到时间", group_id=200, event_ts=1_700_000_100)
    _msg(con, 3, "那我要不要空着肚子去", group_id=200, ts=1_700_000_200)
    c_item = _item(con, 3, title="疑问", group_id=200, event_ts=1_700_000_200)

    def fake(cfg, *, system, user, **kw):
        from vigil.llm import LLMResult
        if "讲座" in user:
            return LLMResult(payload={"items": []}, input_tokens=1, output_tokens=1)
        return LLMResult(
            payload={"items": [_raw("通知：报到时间", title="报到时间")]},
            input_tokens=1, output_tokens=1,
        )

    monkeypatch.setattr(repass, "chat_json", fake)
    plan, stats = repass.repass(_cfg(), api_key="k", conn=con, subset="a2", apply=True)

    assert plan.deaths == {c_item: "a2", a0_item: "a0"}
    assert plan.to_delete == [c_item]
    assert plan.deferred == [a0_item], "a0 的条目必须留在清单里但不执行"
    assert stats.deleted == 1
    assert store.get_item(con, c_item) is None, "a2 的软删掉了"
    assert store.get_item(con, a0_item) is not None, "a0 本轮不动"
    assert store.get_item(con, b_item) is not None, "B 有产出 ⇒ 保留"


def test_subset_rejects_unknown_names(con):
    """`--subset` 只认 a2 / a0——写错就响亮报错，不许静默当成"不过滤"。"""
    _msg(con, 1, "通知：明天体检")
    _item(con, 1, title="体检通知")
    with pytest.raises(ValueError, match="subset"):
        repass.repass(_cfg(), api_key="k", conn=con, subset="a3",
                      on_progress=lambda *_: None)


def test_collect_source_messages_scope_is_only_messages_with_items(con):
    """D10 划定的重判范围：**只碰产出过条目的那批源消息**（真库实测 310 条），
    不碰全量（47,719 条）。

    变异：`FROM item_sources src JOIN messages m` → `FROM messages m` ⇒ 本条红。
    这条守的是**成本**：范围一旦变成全量，下一次 `vigil repass` 会在无人察觉的
    情况下把 LLM 账单乘上 150 倍，而它照样「跑得通、结果也对」。
    """
    _msg(con, 30, "没人抽过的消息", ts=300)
    _msg(con, 10, "抽过的后一条", ts=200)
    _msg(con, 20, "抽过的先一条", ts=100)
    _item(con, 10, title="后")
    _item(con, 20, title="先")

    got = repass.collect_source_messages(con)

    assert [m.msg_id for m in got] == [20, 10], "按 (ts, msg_id) 升序——喂模型的序"
    assert 30 not in {m.msg_id for m in got}, (
        "没产出过条目的消息不在重判范围内：D10 只要求重判「已有产物」的那批"
    )


def test_dry_run_does_not_write(con, monkeypatch):
    """默认（不 apply）**只出清单、不写库**——存量重判不可逆，事前可见比事后回滚值钱。

    变异：把 `apply` 的默认值改成 True（或让 `apply_plan` 无条件跑）⇒ 本条红。
    """
    content = "校园卡办理，需要的联系我"
    _msg(con, 1, content)
    iid = _item(con, 1, title="校园卡办理", place="立德楼1阶")
    _stub_llm(monkeypatch, {"items": []})

    before = con.total_changes
    plan, _ = repass.repass(_cfg(), api_key="k", conn=con)

    assert plan.to_delete == [iid]
    assert con.execute("SELECT COUNT(*) FROM items WHERE item_id=?", (iid,)).fetchone()[0] == 1
    assert con.execute(
        "SELECT place FROM items WHERE item_id=?", (iid,)
    ).fetchone()[0] == "立德楼1阶"
    assert con.total_changes == before, (
        "默认那条路对库**一行都没改**（含触发器与 refine_runs）——"
        "「先看清单再决定」的全部价值建立在这一点上"
    )


# ── Fix loop 第 1 轮：在**当初那个上下文**里重判 ──────────────────


def test_repass_sends_the_context_neighbours_not_just_the_source(con, monkeypatch):
    """⭐ 核心：重判必须在 refine 当初用的那个上下文里做。

    造形：源消息（msg3）自己**不是候选**（没有保留关键词），它只是候选 msg2 的
    ±2 邻居。真库实测这是常态：`refine` 的批中位 30 条（候选 ±2 邻居），
    而在稀疏源消息集上分批时中位 **1** 条、63 个单条批 ⇒ 62/311 条被「孤立判」。

    变异：把 `context_batches` 换回「在源消息集上 make_batches」⇒ 本条立刻红
    （批里只剩源消息自己，`明天体检`/`记得早点睡` 都不在 prompt 里）。
    """
    _msg(con, 1, "隔壁宿舍昨天搬走了", ts=1_700_000_000)
    _msg(con, 2, "通知：明天体检", ts=1_700_000_060)          # 候选（关键词「通知」）
    _msg(con, 3, "那我要不要空着肚子去", ts=1_700_000_120)    # ← 源消息，自己不是候选
    _msg(con, 4, "记得早点睡", ts=1_700_000_180)
    iid = _item(con, 3, title="体检疑问", event_ts=1_700_000_120)
    seen = _stub_llm(monkeypatch, {"items": [_raw("那我要不要空着肚子去", title="体检疑问")]})

    plan, stats = repass.repass(_cfg(), api_key="k", conn=con)

    assert stats.sent_batches == 1
    assert len(seen) == 1, "只该发一批"
    prompt = seen[0]
    assert "明天体检" in prompt and "记得早点睡" in prompt, (
        "批次里必须带上上下文邻居——去掉它们就是「换了个条件重判」，"
        "而诊断实测：换条件会让 14/86/182 这类条目被无辜判死"
    )
    assert plan.to_keep == [iid]


def test_context_window_is_configurable_and_defaults_to_refine_s_two(con, monkeypatch):
    """`--context` 必须真的接到 `expand_context` 上（默认 2，与 refine 一致）。

    变异：把 `context=context` 写死成 `context=0` ⇒ 本条红
    （0 邻居 ⇒ 批里只剩候选自己，`隔壁宿舍昨天搬走了` 不在 prompt 里）。
    """
    _msg(con, 1, "隔壁宿舍昨天搬走了", ts=1_700_000_000)
    _msg(con, 2, "通知：明天体检", ts=1_700_000_060)
    _item(con, 2, title="体检通知", event_ts=1_700_000_060)
    seen = _stub_llm(monkeypatch, {"items": [_raw("通知：明天体检", title="体检通知")]})

    repass.repass(_cfg(), api_key="k", conn=con, context=0)

    assert "隔壁宿舍昨天搬走了" not in seen[0], "context=0 就是不要邻居"


def test_source_message_outside_every_context_batch_is_unjudged(con, monkeypatch):
    """源消息既不是候选、也没落在任何候选的 ±2 邻域里 ⇒ 这一轮**判不到它**。

    处置必须与 E5 同族：它的条目进 `plan.unjudged`、**一条都不许删**。
    （真库实测：312 条源消息里有 1 条是这种。）

    变异：把 `unjudged_ids` 收窄成「只统计批次失败的」⇒ 本条红（item 会被删）。
    """
    _msg(con, 1, "那我要不要空着肚子去", group_id=200, ts=1_700_000_000)
    lonely = _item(con, 1, title="疑问", group_id=200)
    _msg(con, 99, "通知：明天体检", group_id=100, ts=1_700_000_000)
    other = _item(con, 99, title="体检通知", group_id=100)
    _stub_llm(monkeypatch, {"items": []})

    plan, stats = repass.repass(_cfg(), api_key="k", conn=con)

    assert plan.unjudged == [lonely], "判不到的消息要明着列出来，一条都不许删"
    assert plan.to_delete == [other], "判过的照常判（它确实产出 0 条）"
    assert stats.sent_batches == 1, "只有含源消息的批次才发"


def test_audit_jsonl_records_payload_and_verdict(con, monkeypatch, tmp_path):
    """⭐ 可审计性：每一批的**模型原话**与每条源消息的判定必须落盘。

    诊断结论：上一轮干跑没留 payload ⇒ 174 条删除**事后一条都复核不了**，
    只能靠重放做代理测量。这条钉住「以后不用再靠代理测量」。

    变异：`_audit_record` 里去掉 `payload` / `counts` 任一 ⇒ 本条红。
    """
    _msg(con, 1, "通知：明天体检")
    _item(con, 1, title="体检通知")
    _stub_llm(monkeypatch, {"items": []})
    audit = tmp_path / "audit.jsonl"

    repass.repass(_cfg(), api_key="k", conn=con, audit_path=audit)

    lines = [json.loads(x) for x in audit.read_text(encoding="utf-8").splitlines()]
    assert len(lines) == 1, "一批一条 JSONL"
    rec = lines[0]
    assert rec["batch"] == 1
    assert rec["payload"] == {"items": []}, "模型原话必须落盘"
    assert rec["source_msg_ids"] == [1]
    assert rec["counts"] == {"1": 0}, "每条源消息的判定必须落盘"
    assert rec["empty_payload"] is True


def test_gate_drops_and_unmatched_are_counted_separately(con, monkeypatch):
    """⭐ `_to_item` 的静默丢弃必须可见——诊断实测它无计数、无日志、无 payload。

    三种情况分开记（含义完全不同）：

    * 摘录没匹配上任何消息 ⇒ `dropped_unmatched`（模型抄错了摘录）
    * 摘录命中了、但类目不在表里 ⇒ `dropped_by_gate`（**我们的闸门**，不是模型的错）
    * 落地 ⇒ `items_landed`

    变异：删掉 `dropped_by_gate += 1` 那一支（并回 unmatched）⇒ 本条红。
    """
    content = "通知：明天体检"
    _msg(con, 1, content)
    _item(con, 1, title="体检通知")
    _stub_llm(monkeypatch, {"items": [
        _raw(content, title="体检通知"),
        _raw("这段字根本不在任何消息里出现过", title="幽灵"),
        _raw(content, title="类目非法", kind="nope"),
    ]})

    _plan, stats = repass.repass(_cfg(), api_key="k", conn=con)

    assert (stats.items_raw, stats.items_landed) == (3, 1)
    assert stats.dropped_unmatched == 1
    assert stats.dropped_by_gate == 1


def test_a0_and_a2_are_counted_separately(con, monkeypatch):
    """⭐ 「整批返回 []」（a0）与「模型读了但没为这条产出」（a2）必须分开统计。

    两者含义不同：a0 = 这批没什么可抽；a2 = 这批有东西，但**这条**不在其中。
    混成一句「没产出」，就分不清「模型没干活」与「模型干了活却没带上这条」。

    变异：把 `empty_payloads` 与 `msgs_no_output` 并成一个计数器 ⇒ 本条红。
    """
    _msg(con, 1, "通知：明天体检", group_id=100)
    _item(con, 1, title="体检通知")
    _msg(con, 2, "通知：后天体检", group_id=200)
    _item(con, 2, title="体检通知2", group_id=200)

    def fake(cfg, *, system, user, **kw):
        if "明天" in user:
            return LLMResult(payload={"items": []}, input_tokens=1, output_tokens=1)
        return LLMResult(
            payload={"items": [_raw("这段字不在任何消息里", title="幽灵")]},
            input_tokens=1, output_tokens=1,
        )

    monkeypatch.setattr(repass, "chat_json", fake)
    _plan, stats = repass.repass(_cfg(), api_key="k", conn=con)

    assert stats.batches == 2
    assert stats.empty_payloads == 1, "第一批整批返回空 ⇒ a0"
    assert stats.msgs_no_output == 1, "第二批有产出但没为 msg2 产出 ⇒ a2"


def test_budget_stops_remaining_batches_and_never_deletes(con, monkeypatch):
    """预算打到就停：没跑到的批次**不许**被当成「产出 0 条」。

    与 E5 同一条道理——「没判」与「判成 0」必须是两种状态。
    """
    _msg(con, 1, "校园卡办理，需要的联系我", group_id=100)
    first = _item(con, 1, title="校园卡办理")
    _msg(con, 2, "9.5 报到", group_id=200)
    second = _item(con, 2, title="报到时间")

    def fake(cfg, *, system, user, **kw):
        return LLMResult(payload={"items": []}, input_tokens=100, output_tokens=100)

    monkeypatch.setattr(repass, "chat_json", fake)
    plan, stats = repass.repass(_cfg(), api_key="k", conn=con, budget_tokens=1)

    assert stats.budget_hit is True
    assert stats.batches == 1, "预算闸在跑之前就拦下了第二批"
    assert plan.unjudged == [second], "没跑到的那批要明着列出来，一条都不许删"
    assert plan.to_delete == [first], "跑过的那批照常判（它确实产出 0 条）"


def test_repass_defaults_to_thinking_off(con, monkeypatch):
    """⭐⭐ `repass()` 的 `enable_thinking` 默认值必须是 `False`，**不是 `None`**。

    ⚠️ re-review Minor-1：这个默认值原来**一条判据都没有**——把它改成 `None`
    实测 **645 passed**（一条不红）。而这两个值在报文里是**两件不同的事**：

      · `False` ⇒ 发 `false`（Qwen3/Qwen3.5 这类混合思考模型要的就是它；
        不发会慢 **40 倍**：111.5s vs 2.8s/批，实测）
      · `None` ⇒ **不发这个字段**（通用 OpenAI 端点的安全形状，R12）

    `refine` / `digest` 的同一个默认值**各自有用例钉着**
    （`test_refine_passes_thinking_switch_to_llm` / `test_digest.py:887`），
    只有 `repass` 漏了——而 `repass` 是**无人值守批量跑**的那条路，
    真退回 `None` 的代价是整轮重判慢 40 倍，且**不会有任何东西变红**。

    ⚠️ 变异反证：把 `repass()` 签名里的 `enable_thinking: bool | None = False`
    改成 `= None` ⇒ 本条**必红**。
    """
    content = "校园卡办理，需要的联系我"
    _msg(con, 1, content)
    _item(con, 1, title=content)

    seen: list = []

    def fake(cfg, *, system, user, **kw):
        seen.append(cfg)
        return LLMResult(payload={"items": []}, input_tokens=1, output_tokens=1)

    monkeypatch.setattr(repass, "chat_json", fake)
    repass.repass(_cfg(), api_key="k", conn=con)

    assert seen, "一批都没跑，这条测试就没在验证任何东西"
    assert all(c.enable_thinking is False for c in seen), (
        "`repass()` 的默认 thinking 必须是**发 false**，不是「不发」——"
        "后者会让混合思考模型退回 111.5s/批（见 llm.LLMConfig 的注释）"
    )
