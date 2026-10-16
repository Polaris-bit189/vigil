"""重排：LLM 重判已有条目 —— M6 T6（D16）。

⚠️ **本文件比 brief 多两处**（理由与原始输出见 `task-6-report.md` 的
「偏差」与 Step 8 一节；两条都是「brief 的断言抓不住 brief 自己的变异」）：

* `test_reorder_writes_only_overlay_and_never_touches_items` 里，
  除了 brief 的 `item_ids` 断言，还有**主库整行快照**与**主库文件指纹**两条。
  实测：brief 的 Step 8 变异（`ov.execute("UPDATE items …")`）在 overlay 连接上
  报 `no such table: items` ⇒ **红，但不是那条断言红的**（E7 形态 6）；
  而真正会成功的那种写法（把结果顺手 `UPDATE` 回 `items`，正是 `reorder.py`
  docstring 警告的那件事）**根本不改 `item_id`** ⇒ 只断言 id 集合的话它会**全绿**。
* `test_a_hallucinated_slug_never_reaches_the_plan` 多一句「计划非空」：
  没有它的话，闸门把**每一条**都丢掉时 `plan.changes == ()`，
  那条 `all(...)` 在空元组上**恒真**——一条永远不可能红的守卫。
"""

from __future__ import annotations

import hashlib
import pathlib
import sqlite3
from dataclasses import dataclass

import pytest

from vigil import overrides, reorder, store
from vigil.categories import Category
from vigil.config import Config, Group
from vigil.llm import LLMError, LLMResult

# 与 `tests/test_repass.py` 逐字同形：`messages` / `sender_names` 是 export 的产物，
# `store.ensure_schema` 不建它们，而 `collect_source_messages` 会 JOIN `messages`。
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

# ⚠️ 类目表**自带**、不读仓库里那份真文件（与 `tests/conftest.py` 的
# `_ISOLATED_CATEGORIES_TOML` 同一条判据）：读真文件的话，用户在「配置」页
# 删掉一个类目就会让本文件红绿——而 M6 的整个卖点就是把那份文件交给用户编辑。
_CATS = (
    Category(slug="notice", label="通知公告", desc="隔离用类目：通知公告"),
    Category(slug="academic", label="学业", desc="隔离用类目：学业"),
    Category(slug="life", label="生活", desc="隔离用类目：生活"),
)

# ⚠️ **两个群是承重的**：`prefilter.make_batches` **不跨群混批**（不同群的对话
# 毫无关系），所以「两个群各有一条源消息」⇒ **两批**。
# `test_should_stop_halts_between_batches` 要的正是「第一批之后喊停」——
# 只有一批时那个 `should_stop` 只会被调一次、`stats.stopped` 恒为 False，
# 一条本来该守卫「取消能生效」的用例会**永远红**（而不是永远绿，两种都是坏的）。
# 用 tier="high" 让每条消息都是候选，免得用例的成败取决于关键词表。
_GROUPS = (
    Group(id=100, name="甲群", tier="high"),
    Group(id=200, name="乙群", tier="high"),
)

# (msg_id, group_id, content, item_id, 库里原 kind)
_SEED = (
    (1, 100, "选课通知：9月16日截止", 1, "notice"),
    (2, 100, "讲座报名：周三下午三点开始", 2, "notice"),
    (3, 200, "兼职招聘：图书馆整理资料", 3, "notice"),
)
_CONTENT = {mid: content for mid, _gid, content, _iid, _k in _SEED}

# 假模型判出的新类目：与库里的 "notice" 不同 ⇒ 每条源消息都产生一条变更。
_NEW_KIND = "academic"


@dataclass
class _Fixture:
    """`db_with_items` 交给用例的东西。

    ⚠️ `item_ids` / `rows()` 都是**每次现读**库里真实的行，不是夹具创建时的常量：
    常量的话 `before` 与 `after` 是同一份数据，那条「前后不变」的断言
    **永远不可能红**（E7 形态 4：断言区分不出它声称区分的东西）。
    """

    conn: sqlite3.Connection
    db_path: pathlib.Path
    overlay_path: pathlib.Path
    cats: tuple[Category, ...]
    config: Config

    @property
    def item_ids(self) -> list[int]:
        return sorted(int(r[0]) for r in self.conn.execute("SELECT item_id FROM items"))

    def rows(self) -> list[tuple[int, str]]:
        """`(item_id, 库里的 kind)` 全表快照——**直读表**，绕过 `_EFFECTIVE_KIND`。

        与 `item_ids` 是两个不同的判据：id 集合只看得见「删了/加了行」，
        而**改一行**（`UPDATE items SET kind=…`）只有它能看见。
        """
        return sorted(
            (int(r[0]), str(r[1])) for r in self.conn.execute("SELECT item_id, kind FROM items")
        )


def _fingerprint(db: pathlib.Path) -> dict[str, str]:
    """主库 + 全部侧车文件的 sha256（`test_repass.test_apply_does_not_touch_vigil_db_bytes` 同形）。

    ⚠️ `-journal` 也收进来：主库用的是 rollback journal（`store.ensure_schema` 没设 WAL），
    一次**未提交**的写只在 `-journal` 里留痕——只看主文件的话会漏掉它。
    """
    out: dict[str, str] = {}
    for suffix in ("", "-wal", "-shm", "-journal"):
        p = db if not suffix else db.with_name(db.name + suffix)
        out[suffix or "main"] = (
            hashlib.sha256(p.read_bytes()).hexdigest() if p.is_file() else "<missing>"
        )
    return out


@pytest.fixture
def db_with_items(tmp_path, monkeypatch):
    """一个**文件**主库（**可写**连接）+ 三条条目 + 它们的源消息 + 已就绪的 overlay。

    ⚠️⭐ **主库连接刻意是「可写」的**（`sqlite3.connect(str(db), uri=True)`，
    **不是** `file:…?mode=ro`）。这是本文件最容易被搞成空守卫的一处：

      只读连接上，任何 `UPDATE items` 都会自己抛 `attempt to write a readonly
      database` ⇒ 测试**确实会红**，但红的是**连接模式**、不是那条断言
      ——「没写过主库」这条判据**一辈子没被考验过**（M5 最值钱的那条教训：
      一个专门验证「没写过主库」的守卫，因测试用只读连接而永远无法被考验）。
      可写连接下，写会**成功**⇒ 唯一能拦住它的就是下面那两条断言
      （行快照 + 文件指纹）。

    ⚠️ `uri=True` 不能省：`store.get_item` 会 `ATTACH 'file:…?mode=ro' AS ov`，
    而 URI 形式只在连接带 `SQLITE_OPEN_URI` 时才被解析（裁决 R4）。
    """
    db = tmp_path / "vigil.db"
    con = sqlite3.connect(str(db), uri=True)      # ← 可写（这是刻意的，见 docstring）
    store.ensure_schema(con)                      # 顺带在隔离根里建好 overlay
    con.executescript(_MESSAGE_TABLES_DDL)
    for msg_id, gid, content, item_id, kind in _SEED:
        ts = 1_700_000_000 + msg_id
        con.execute(
            "INSERT INTO messages (msg_id, group_id, ts, sender_uid, content)"
            " VALUES (?,?,?,?,?)",
            (msg_id, gid, ts, "u_1", content),
        )
        con.execute(
            "INSERT INTO items (item_id, kind, title, detail, event_ts, deadline_ts,"
            " group_id, actor_uid, place, links, amount, confidence, model,"
            " prompt_ver, created_at) VALUES (?,?,?,NULL,?,NULL,?,'u_1',NULL,'[]',"
            "NULL,0.9,'m','v2',?)",
            (item_id, kind, f"第 {msg_id} 条", ts, gid, ts),
        )
        con.execute(
            "INSERT INTO item_sources (item_id, msg_id) VALUES (?,?)", (item_id, msg_id)
        )
    con.commit()

    # ⚠️⭐ **响亮地确认这条连接是可写的**：不可写的话下面的探针自己会抛。
    # 留着它是为了「这条守卫是可考验的」本身是个**机械判据**，而不是一句注释
    # ——将来有人把它改成 `mode=ro`，这里当场红（而不是让上面那条断言悄悄变空）。
    mark = con.total_changes
    con.execute("UPDATE items SET kind = '__probe__' WHERE item_id = ?", (1,))
    assert con.total_changes > mark, (
        "夹具的主库连接不可写——那条「重排没写过主库」的断言将永远无法被考验"
    )
    con.execute("UPDATE items SET kind = ? WHERE item_id = ?", (_SEED[0][4], 1))
    con.commit()

    # ⚠️ `plan_reorder(cats=None)` 会走 `load_categories()`（默认路径 = 仓库里
    # 那份真文件）。这里把它换成夹具自带的表：用例要守的是**重排逻辑**，
    # 不是类目文件的读取（那是 T2/T5 的判据），而读真文件会让本文件跟着用户的
    # 类目编辑红绿（与 conftest 的 `_ISOLATED_CATEGORIES_TOML` 同一条判据）。
    monkeypatch.setattr(reorder, "load_categories", lambda: _CATS)

    fx = _Fixture(
        conn=con,
        db_path=db,
        overlay_path=overrides.OVERRIDES_DB,
        cats=_CATS,
        config=Config(source_dir=tmp_path, output_db=db, groups=_GROUPS),
    )
    yield fx
    con.close()


def _stub_llm(monkeypatch, payload: object) -> list[str]:
    """把 `reorder.chat_json` 换掉（`tests/test_repass.py::_stub_llm` 同形）。

    ⚠️ 打在 `reorder` 的模块属性上，不是 `llm.chat_json` 上：`reorder.py` 用的是
    `from .llm import chat_json` 绑进来的名字，打原模块**够不着**它。
    """
    seen: list[str] = []

    def fake(cfg, *, system, user, **kw):  # noqa: ANN001, ANN003
        seen.append(user)
        if isinstance(payload, BaseException):
            raise payload
        return LLMResult(payload=payload, input_tokens=10, output_tokens=5)

    monkeypatch.setattr(reorder, "chat_json", fake)
    return seen


def _payload_quoting_everything(kinds: dict[int, str]) -> dict:
    """一条消息一条 item 的假响应（`quote` 必须**逐字**出现在正文里，见 `_match_source`）。"""
    return {
        "items": [
            {
                "quote": content,
                "kind": kinds[mid],
                "title": f"重判第 {mid} 条",
                "confidence": 0.9,
            }
            for mid, content in _CONTENT.items()
        ]
    }


@pytest.fixture
def fake_llm(monkeypatch):
    """每条源消息都判成 `_NEW_KIND` 的假模型。"""
    return _stub_llm(
        monkeypatch, _payload_quoting_everything({mid: _NEW_KIND for mid in _CONTENT})
    )


@pytest.fixture
def failing_llm(monkeypatch):
    """**第一批**抛 LLMError（一次 429 的形状），其余批返回空 payload。

    ⚠️ 刻意**不是**「每次都抛」：夹具有两个群 ⇒ 两批，全抛的话
    `stats.failed_batches` 会是 2，而那条用例断言的是 `== 1`
    ——断言的是一个与它想守的东西无关的数字。第一批抛错、第二批返回空
    （a0：什么都没产出），于是「没判成 ⇒ 一条都不改」是**确定**的。
    """
    calls = {"n": 0}

    def fake(cfg, *, system, user, **kw):  # noqa: ANN001, ANN003
        calls["n"] += 1
        if calls["n"] == 1:
            raise LLMError("429 Too Many Requests")
        return LLMResult(payload={"items": []}, input_tokens=0, output_tokens=0)

    monkeypatch.setattr(reorder, "chat_json", fake)
    return calls


@pytest.fixture
def llm_returning_unknown_kind(monkeypatch):
    """模型呕出一个类目表里没有的 slug（`boss`）——**同时**给一条合法的。

    ⚠️ 混合是刻意的：全给非法的话计划会是空的，那条
    `all(c.new_kind in known …)` 在空元组上**恒真**（一条永远不可能红的守卫）。
    """
    kinds = {1: _NEW_KIND, 2: _NEW_KIND, 3: "boss"}
    return _stub_llm(monkeypatch, _payload_quoting_everything(kinds))


def test_reorder_writes_only_overlay_and_never_touches_items(db_with_items, fake_llm):
    """⭐⭐ **出口判据 7 的核心**：重排前后 `items.item_id` 集合**一模一样**。

    ⚠️ 这条同时是 D11（`data/vigil.db` 只读）与 D16（`item_id` 不变 ⇒ R9 化解）
    的机械形式。**两个不变量由同一条断言同时守住**——这不是巧合：
    重排只写 overlay 正是它们共同的机制。

    ⚠️ 变异反证：让 `apply_reorder` 改成调 `store.save_items(...)`（或任何
    直接 `UPDATE items` 的写法）⇒ 本条**必红**。
    """
    before = sorted(db_with_items.item_ids)
    before_rows = db_with_items.rows()
    before_fp = _fingerprint(db_with_items.db_path)

    plan, stats = reorder.plan_reorder(
        db_with_items.conn, config=db_with_items.config, api_key="k",
        on_progress=lambda _: None,
    )
    assert plan.changes, "夹具必须能造出至少一条要改的条目，否则本条是空守卫"

    ov = overrides.connect(db_with_items.overlay_path)
    try:
        n = reorder.apply_reorder(ov, plan, actor="reorder:test")
    finally:
        ov.close()
    assert n == len(plan.changes)

    after = sorted(db_with_items.item_ids)
    assert after == before, (
        "重排改了 items 表——D11 与 D16 同时被打破。重排只能写 overlay 的 set_kind 事件。"
    )

    # ⚠️⚠️ 以下两条是**brief 之外新增的**（T6 实测，见报告）：上面那条只看得见
    # 「行数/身份变了没」，**看不见 `UPDATE items SET kind=…`**——而那正是本模块
    # 的 docstring 点名要防的那件事（「顺手把结果写回 items」），它的 `item_id`
    # 一个都不动。少了这两条，「重排没写过主库」这条守卫对最现实的那种写**是瞎的**。
    assert db_with_items.rows() == before_rows, (
        "重排改了 items 表里的行（kind 被写回了库里）——重排只能写 overlay 的 set_kind 事件。"
    )
    assert _fingerprint(db_with_items.db_path) == before_fp, (
        "重排动了 data/vigil.db 的字节（D11）——主库必须全程只读。"
    )


def test_reorder_changes_the_effective_kind(db_with_items, fake_llm):
    """判出的新 kind 必须**在读取层可见**（走 `_EFFECTIVE_KIND`）。"""
    plan, _ = reorder.plan_reorder(
        db_with_items.conn, config=db_with_items.config, api_key="k",
        on_progress=lambda _: None,
    )
    ov = overrides.connect(db_with_items.overlay_path)
    try:
        reorder.apply_reorder(ov, plan, actor="reorder:test")
    finally:
        ov.close()

    changed = plan.changes[0]
    it = store.get_item(db_with_items.conn, changed.item_id)
    assert it is not None
    assert it.kind == changed.new_kind


def test_reorder_skips_soft_deleted_items(db_with_items, fake_llm):
    """⭐ 已软删的条目不参与重排。

    ⚠️ 不跳过的后果不只是浪费 token：一条 `delete` 之后的 `set_kind`
    会让「撤销这一批重排」把条目**从可见变回已删**（因为撤掉 set_kind
    之后，它前面那条 delete 重新生效）。那是一个**静默的状态翻转**，
    而调用方拿到的是一次"成功"的撤销。
    """
    target = db_with_items.item_ids[0]
    ov = overrides.connect(db_with_items.overlay_path)
    try:
        overrides.delete_item(ov, item_id=target, msg_id=None)
    finally:
        ov.close()

    plan, _ = reorder.plan_reorder(
        db_with_items.conn, config=db_with_items.config, api_key="k",
        on_progress=lambda _: None,
    )
    assert target not in {c.item_id for c in plan.changes}


def test_a_failed_batch_produces_no_changes(db_with_items, failing_llm):
    """⭐ 失败**不写判定**（`repass` 勘误 E5 的同一条判据）。

    ⚠️ 一次 429 若被当成「模型说它该是别的类目」，方向是**静默改错分类**——
    比 `repass` 的"删掉"轻一点，但同样是"把没取得的结论当成结论"。
    """
    plan, stats = reorder.plan_reorder(
        db_with_items.conn, config=db_with_items.config, api_key="k",
        on_progress=lambda _: None,
    )
    assert plan.changes == ()
    assert stats.failed_batches == 1
    # ⚠️ 两条路径都要报成「没判成」（冒烟 Bug A-1）：第一批**失败**、
    # 第二批**整批返回空**（a0）——两种都没有取得判定，而 item 1/2/3 都还在。
    # ⚠️ a0 算没判成而不是"判成 0 条"：M5 的实测结论是「整批放弃 ≠ 逐条判死」。
    assert plan.unjudged == (1, 2, 3), (
        "没判成的条目没被列出来——收尾文案会说「0 条没判成」（冒烟 Bug A）"
    )


def test_a_failed_batch_leaves_only_its_own_items_unjudged(db_with_items, monkeypatch):
    """⭐⭐ 冒烟 Bug A 的**部分失败**形状：`unjudged` 只收没判成的那批。

    夹具是两个群 ⇒ **两批**（甲群 msg 1/2、乙群 msg 3）。这里让**第一批**抛
    `LLMError`（`HTTP 503` 的形状、冒烟现场的那一种）、第二批正常返回 ⇒

      · `plan.unjudged` 必须是 **(1, 2)**——第一批那两条的条目；
      · **判过的那批照常出变更**（item 3）：一有失败就整轮放弃是另一种坏法，
        而且它会让这条用例在"两边都空"时**绿得看不出区别**。

    ⚠️ 修前的行为：`unjudged` 恒为 `()`（`all_items` 从 `judged` 反向派生 ⇒
    差集恒空）——于是这句「2 条没判成」在界面上永远说不出来。
    """
    calls = {"n": 0}
    # ⚠️ 只给 msg 3 一条条目（手写这个 payload，不走 `_payload_quoting_everything`：
    # 那个辅助函数**为每条消息各出一条**，喂一个只含 msg 3 的 kinds 表会
    # `KeyError(1)`——而那会被 `plan_reorder` 当成"本地后处理失败"记一笔，
    # 于是两条断言都会红在**与它们想守的东西无关**的原因上）。
    payload = {
        "items": [
            {
                "quote": _CONTENT[3],
                "kind": _NEW_KIND,
                "title": "重判第 3 条",
                "confidence": 0.9,
            }
        ]
    }

    def fake(cfg, *, system, user, **kw):  # noqa: ANN001, ANN003
        calls["n"] += 1
        if calls["n"] == 1:
            raise LLMError("HTTP 503 (code 50508)")
        return LLMResult(payload=payload, input_tokens=10, output_tokens=5)

    monkeypatch.setattr(reorder, "chat_json", fake)
    plan, stats = reorder.plan_reorder(
        db_with_items.conn, config=db_with_items.config, api_key="k",
        on_progress=lambda _: None,
    )
    assert (stats.failed_batches, stats.batches) == (1, 1)
    assert plan.unjudged == (1, 2), (
        "失败那批的条目没被报成「没判成」——收尾文案会说「0 条没判成」（冒烟 Bug A）"
    )
    assert {c.item_id for c in plan.changes} == {3}, (
        "判过的那批没照常出变更：失败不该让整轮放弃"
    )


def test_every_batch_failing_leaves_every_item_unjudged(db_with_items, monkeypatch):
    """⭐⭐ 冒烟 Bug A-1 的**全失败**形状（就是那条反证的现场）。

    冒烟那一轮把 `chat_json` 换成必抛的替身 ⇒ 165 批全失败、判定 0 批，
    而修前的 `unjudged` **仍是 `()`** ⇒ `api.py` 回一句
    **「0 条改了类目，0 条没判成（它们没被动）」**——事实是一条都没判成。
    这正是本项目最忌的那类话（「只说自己有资格说的话」）。
    """
    def fake(cfg, *, system, user, **kw):  # noqa: ANN001, ANN003
        raise LLMError("HTTP 503 (code 50508)")

    monkeypatch.setattr(reorder, "chat_json", fake)
    plan, stats = reorder.plan_reorder(
        db_with_items.conn, config=db_with_items.config, api_key="k",
        on_progress=lambda _: None,
    )
    assert stats.failed_batches == 2, "夹具是两批、两批都该失败（否则下面那句话无关）"
    assert plan.changes == ()
    assert plan.unjudged == (1, 2, 3), (
        "全失败时 unjudged 还是空的——用户会读到「0 条没判成」（冒烟 Bug A）"
    )


def test_soft_deleted_items_are_not_reported_as_unjudged(db_with_items, failing_llm):
    """软删的条目**不算**没判成：它本来就不参与重排（见 `alive` 的理由）。

    ⚠️ 判据要用**没判成的那一批**里的一条：这里 item 1 被软删，而两批都没取得
    判定（`failing_llm`：第一批抛、第二批整批空）。⇒ `unjudged` 是 **(2, 3)**：
    把 1 算进去等于让"用户自己删掉的"冒充成"我们没判的"，那个数会被读成
    "有一条该看的结果没出来"。
    """
    ov = overrides.connect(db_with_items.overlay_path)
    try:
        overrides.delete_item(ov, item_id=1, msg_id=None)
    finally:
        ov.close()

    plan, _ = reorder.plan_reorder(
        db_with_items.conn, config=db_with_items.config, api_key="k",
        on_progress=lambda _: None,
    )
    assert plan.unjudged == (2, 3), "软删的条目混进了「没判成」"


def test_budget_stop_leaves_the_untouched_batches_unjudged(db_with_items, fake_llm):
    """预算提前停止 ⇒ 没跑到的那些批次一条判定都没有（与取消同一条判据）。

    ⚠️ 预算闸在**每一批之前**检查，而假模型每批报 10+5=15 token ⇒
    取 `budget_tokens=15`：第一批跑完、第二批之前停下。于是 msg 3 没判成、
    msg 1/2 判过（item 1/2 照常改类目）。
    """
    plan, stats = reorder.plan_reorder(
        db_with_items.conn, config=db_with_items.config, api_key="k",
        budget_tokens=15, on_progress=lambda _: None,
    )
    assert stats.budget_hit is True, "预算闸没生效——下面那条断言就与预算无关了"
    assert len(plan.changes) == 2
    assert plan.unjudged == (3,), "撞预算之后没跑的那批没被报成「没判成」"


def test_no_batch_at_all_leaves_every_item_unjudged(db_with_items, monkeypatch):
    """⭐⭐ **第三条早退路径**：一条「含源消息的上下文批次」都没有（`if not batches:`）。

    ⚠️ 这一格是终审 re-review 点出来的：失败 / 取消 / 预算 / 整批返回空四条路径
    都有用例，**独缺这一条**——把它改回 `ReorderPlan()` 时全量 **0 红**，
    于是用户又会读到那句「**0 条没判成**」（而这几个条目一条都没判过）。

    ⚠️⭐ 前置**真的上场**（格 8），不是替身喂出来的：把三条源消息的正文改成
    「收到」——预筛的 `BARE_ACK` **硬丢弃**它（`_drop_reason` 的丢弃规则
    **先于**保留规则，所以 tier=high 也留不住它）⇒ `screen` 一条候选都收不上来
    ⇒ `expand_context` 为空 ⇒ `make_batches([])` 回 0 批。
    ⚠️ 下面第一句**机械地**确认这件事：前置一旦蒸发（预筛的规则表改了），
    它当场红，而不是让后面那条断言红在**与它无关**的原因上。

    ⚠️ `chat_json` 换成**必炸**的替身：真拼出批次就说明前置蒸发了——那必须是
    **响亮**的失败，而不是拿一个假 key 去发一次真实网络请求（Global Constraint 9）。

    ⚠️ 变异反证：这一行改回 `ReorderPlan()` ⇒ 本条**必红**。
    """
    db_with_items.conn.execute("UPDATE messages SET content = '收到'")
    db_with_items.conn.commit()

    def boom(cfg, *, system, user, **kw):  # noqa: ANN001, ANN003
        raise AssertionError("chat_json 不该被调用——「0 批」这个前置蒸发了")

    monkeypatch.setattr(reorder, "chat_json", boom)

    # 与 `plan_reorder` 调的是同一个函数、同一组默认值（默认值真被改了的话，
    # 这条前置断言会先红，而不是让下面的断言红在一个无关的原因上）
    batches, _ = reorder.context_batches(db_with_items.conn, config=db_with_items.config)
    assert batches == [], (
        "前置没上场：这批源消息仍然拼得出批次，下面那条断言与「0 批」这条早退路径无关"
    )

    plan, stats = reorder.plan_reorder(
        db_with_items.conn, config=db_with_items.config, api_key="k",
        on_progress=lambda _: None,
    )
    assert stats.sent_batches == 0, "真发出批次了——「0 批」这个前置蒸发了"
    assert plan.changes == (), "「0 批」时一条改动都不许有"
    assert plan.unjudged == (1, 2, 3), (
        "一条批次都没有 = 一条判定都没取得，而条目 1/2/3 都还在库里 ⇒ 它们必须"
        "全部被报成「没判成」；回空集的话收尾那句对用户说的是「0 条没判成」"
    )


def test_a_hallucinated_slug_never_reaches_the_plan(db_with_items, llm_returning_unknown_kind):
    """模型呕出一个类目表里没有的 slug ⇒ **不许进计划**。

    ⚠️ 它一旦落进 overlay，那个条目就会在界面上显示成一个没人认识的类目，
    而日报会把它排到「类表里没有的 kind」那一段末尾（`digest.py:465-469`
    **不静默丢**，所以它至少是可见的——但可见的错误仍然是错误）。
    闸在 `repass._scan_payload(..., known_kinds=...)`，本任务只是**必须继续用它**。
    """
    plan, _ = reorder.plan_reorder(
        db_with_items.conn, config=db_with_items.config, api_key="k",
        on_progress=lambda _: None,
    )
    known = {c.slug for c in db_with_items.cats}
    # ⚠️ 先确认这条守卫**不是空的**：假模型同时给了一条合法类目 ⇒ 计划非空。
    # 没有这一句的话，「闸门把每一条都丢掉」也能让下面那条 `all(...)` 绿。
    assert plan.changes, "假模型一条合法类目都没给到计划里，下面那条断言恒真"
    assert all(c.new_kind in known for c in plan.changes)
    assert "boss" not in {c.new_kind for c in plan.changes}, (
        "幻觉 slug 进了计划——闸门（`_scan_payload(known_kinds=…)`）没接上"
    )


def test_apply_reorder_does_not_revive_an_item_deleted_after_planning(db_with_items, fake_llm):
    """⭐⭐ 出口判据 7 的 TOCTOU 边界：**plan 之后**用户删掉的那一条，apply 不许把它复活。

    ⚠️ 窗口是**整轮 LLM 的时间**（1–2 分钟，见 `jobs.py` 的模块 docstring）——
    用户在界面上删掉一条、而那条的 `set_kind` 正躺在待写的计划里，这不是
    假想场景，是**每次重排都会开的窗口**。

    ⚠️ 不复查的后果不是一个"多改了一条类目"的小毛病：`set_kind` 落在 `delete`
    **之后** ⇒ `item_state.deleted` 被翻回 0 ⇒ 条目**重新进入 Web 与日报**，
    而用户以为自己已经删掉它了。D15（软删是持久的）就这样被一次"成功"的重排
    静默推翻。

    ⚠️ 变异反证：`apply_reorder` 里不复查 `deleted` ⇒ 本条**必红**
    （`store.get_item` 又会返回那条）。
    """
    plan, _ = reorder.plan_reorder(
        db_with_items.conn, config=db_with_items.config, api_key="k",
        on_progress=lambda _: None,
    )
    assert plan.changes, "夹具必须能造出至少一条要改的条目，否则本条是空守卫"
    target = plan.changes[0].item_id

    ov = overrides.connect(db_with_items.overlay_path)
    try:
        # ── 用户在这 1–2 分钟里把这条删了 ──
        overrides.delete_item(ov, item_id=target, msg_id=None)
        n = reorder.apply_reorder(ov, plan, actor="reorder:test")
    finally:
        ov.close()

    assert n == len(plan.changes) - 1, "被删掉的那条不该写 set_kind"
    assert store.get_item(db_with_items.conn, target) is None, (
        "重排把用户刚删掉的条目复活了——`set_kind` 落在 `delete` 之后会把 "
        "`item_state.deleted` 翻回 0，它重新出现在 Web 与日报里（D15 被打破）"
    )


def test_should_stop_halts_between_batches(db_with_items, fake_llm):
    """取消是协作式的：`should_stop` 在**每一批之间**被检查。

    ⚠️ 取消之后**没跑到的那些批次**一条判定都没有 ⇒ 它们的条目必须出现在
    `unjudged` 里（冒烟 Bug A 的另一条提前退出路径）。第一批判过了，所以
    只有 msg 3 的那一条没判成——两件事都要断言，否则"取消后统统算没判成"
    也能绿。
    """
    calls = {"n": 0}

    def stop():
        calls["n"] += 1
        return calls["n"] > 1     # 第一批之后喊停

    plan, stats = reorder.plan_reorder(
        db_with_items.conn, config=db_with_items.config, api_key="k",
        should_stop=stop, on_progress=lambda _: None,
    )
    assert stats.stopped is True
    assert len(plan.changes) == 2, "第一批判过的两条该照常出变更"
    assert plan.unjudged == (3,), "取消之后没跑的那批没被报成「没判成」"
