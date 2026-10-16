"""⭐ overlay 端到端验收：**一条人工干预，两处消费方都生效**（M5 出口判据 6 的机械守卫）。

为什么这个文件必须存在（T5 审查的结论）：出口判据 6 原先只挂在 Task 8 Step 10 的
**手工**「浏览器逐条验」上——**手工不构成机械守卫**；而变异实测（W1b / W3）说明
**日报侧的语义继承当时没有任何自动测试**（6 条 overlay 测试全在 `test_store.py`）。

本文件钉三件事，**全部走产品自己的入口**（不在 store 层打桩）：
① Web：`/api/items`、`/api/items/{id}`、kind 筛选、`/api/categories` 角标按**覆盖后**的值；
② 日报：被软删的条目不进正文、被改过类目的条目按**新**类目分桶
   （`digest.digest(db_path=…)`——正是 R4 补了 `uri=True` 的那条连接）；
③ **阴性对照**：把 `OVERRIDES_DB` 指到空库，同一天**照旧**进日报——否则 ② 证明不了因果。

⚠️ 用**真库文件**而不是内存库：`create_app` 只认 db 路径（它自己开 `mode=ro` 连接），
而「两处都验」的意义就在于两个**独立进程内入口**各自开连接、各自挂 overlay。
"""

from __future__ import annotations

import datetime as dt
import pathlib
import sqlite3
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from vigil import digest, overrides, store
from vigil.api import create_app
from vigil.config import Config, Group

GID = 12345

# ⚠️ `messages` / `sender_names` 由导出层（`vigil/export.py`）建，`store.ensure_schema`
# 不管这两张表。形状与 `export.py` 的 DDL、`tests/test_store.py::_MESSAGE_TABLES_DDL`
# 一致——造真库里本来就有的表，不是放宽断言。
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

# 两条时间线：09-13 有两条（改类目的 + 不动的），09-14 **只有**将被软删的那条。
# ⇒ 软删之后 09-14 变成「空窗日」：不调模型、正文直接算得出来（对照才有意义）。
_D13 = int(dt.datetime(2026, 9, 13, 12, 0).timestamp())
_D14 = int(dt.datetime(2026, 9, 14, 12, 0).timestamp())

_ITEMS = (
    # (item_id, kind, title, event_ts)
    (1, "notice", "办卡广告（要被删掉）", _D14),
    (2, "notice", "选课通知（要改成 academic）", _D13),
    (3, "lostfound", "失物招领（不动）", _D13),
)


class _StubLLM:
    """假 LLM：记录调用次数、返回预设载荷。**绝不联网**。"""

    def __init__(self, lines):
        self.lines = lines
        self.calls = 0

    def __call__(self, cfg, *, system, user, sleep=None):
        from vigil.llm import LLMResult

        self.calls += 1
        return LLMResult(payload={"lines": self.lines}, input_tokens=1, output_tokens=1)


def _window(day: int, month: int) -> tuple[int, int]:
    a = int(dt.datetime(2026, month, day).timestamp())
    b = int(dt.datetime(2026, month, day + 1).timestamp())
    return a, b


@pytest.fixture
def scene(tmp_path):
    """一个**真库文件** + 一张最小 messages/sender_names（不碰 `data/vigil.db`）。

    ⚠️ `store.ensure_schema` 会顺带把 overlay 库建到 conftest 那条 autouse 夹具
    挪过的默认路径上——所以这里不需要、也**不应该**自己去碰真实的 `data/overrides.db`。
    """
    db = tmp_path / "vigil.db"
    conn = sqlite3.connect(str(db))
    try:
        store.ensure_schema(conn)
        conn.executescript(_MESSAGE_TABLES_DDL)
        conn.execute(
            "INSERT INTO sender_names VALUES (?,?,?,?,?,?)",
            (GID, "u1", "张三", "三三", 111, 0),
        )
        for item_id, kind, title, ts in _ITEMS:
            conn.execute(
                "INSERT INTO items (item_id, kind, title, detail, event_ts,"
                " deadline_ts, group_id, actor_uid, place, amount, links, confidence,"
                " model, prompt_ver, created_at)"
                " VALUES (?,?,?,NULL,?,NULL,?,'u1',NULL,NULL,'[]',0.9,'m','v1',1)",
                (item_id, kind, title, ts, GID),
            )
            conn.execute(
                "INSERT INTO messages (msg_id, group_id, ts, sender_uid, content)"
                " VALUES (?,?,?,'u1',?)",
                (100 + item_id, GID, ts, title),
            )
            conn.execute(
                "INSERT INTO item_sources (item_id, msg_id) VALUES (?,?)",
                (item_id, 100 + item_id),
            )
            # 覆盖率记账：空窗日靠它区分「没有值得一提的信息」与「还没抽取」
            conn.execute(
                "INSERT INTO refine_runs (msg_id, refined_at, status, item_count,"
                " prompt_ver, err) VALUES (?,?,'ok',1,'v3',NULL)",
                (100 + item_id, ts),
            )
        conn.commit()
    finally:
        conn.close()

    cfg = Config(source_dir=tmp_path, output_db=db, groups=(Group(id=GID, name="测试群"),))
    return SimpleNamespace(db=db, cfg=cfg, out_dir=tmp_path)


def _apply_interventions() -> None:
    """走**真实写路径**落两条干预：删 item 1、把 item 2 改成 academic。"""
    overrides.ensure_schema()      # autouse 夹具已把 OVERRIDES_DB 挪到 tmp
    w = overrides.connect()
    try:
        overrides.delete_item(w, item_id=1, msg_id=101)
        overrides.set_kind(w, item_id=2, kind="academic")
    finally:
        w.close()


# ── ① Web 侧 ──────────────────────────────────────────────────────────


def test_overlay_applies_to_every_web_reading_path(scene):
    """Web 的**四条**读路径都要按覆盖后的值：列表 / 单条 / 筛选 / 类目角标。

    这四条分别落在 `search_items`（含它的 COUNT）、`get_item`、`_item_filters`、
    `kind_counts` 上——即 spec §3.3 那张表里 Web 侧的全部入口。
    """
    _apply_interventions()
    client = TestClient(create_app(scene.cfg))

    # ① 列表：被删的条目消失（**含 total**——否则分页与「共 N 条」会说谎）
    r = client.get("/api/items")
    assert r.status_code == 200
    payload = r.json()
    assert payload["total"] == 2, "被软删的条目必须从列表与 total 里一起消失"
    got = {i["item_id"]: i["kind"] for i in payload["items"]}
    assert 1 not in got, "被软删的条目还在列表里"
    # ② 改过类目的条目显示**新**类目
    assert got[2] == "academic", "改过分类的条目必须显示新类目"

    # ③ 单条：软删 ⇒ 404（不是 200 + 旧数据）
    assert client.get("/api/items/1").status_code == 404

    # ④ 筛选：按**覆盖后**的类目筛（改了分类却筛不出来 = 用户看不见自己的修改）
    assert client.get("/api/items", params={"kind": "academic"}).json()["total"] == 1
    assert client.get("/api/items", params={"kind": "notice"}).json()["total"] == 0

    # ⑤ 类目角标（`kind_counts`）：计数与列表同源
    counts = {
        c["slug"]: c["count"]
        for c in client.get("/api/categories").json()["categories"]
    }
    assert (counts["academic"], counts["notice"], counts["lostfound"]) == (1, 0, 1)


# ── ② 日报侧（+ ③ 阴性对照）────────────────────────────────────────────


def test_overlay_drives_the_daily_digest(scene, monkeypatch):
    """软删之后，那天在日报里**真的**变成「没有值得一提的信息」——且不调模型。

    ⚠️ 这条是 R13 在日报侧的机械守卫：`digest.py` 不直接读 `items`，它走
    `store.window_items`；本用例从 `db_path` 进（**不是**传内存 conn），
    所以它同时覆盖了 R4 给 `digest.py` 补的 `uri=True`。
    """
    fake = _StubLLM([{"quotes": ["办卡广告"], "label": "办卡", "text": "速办"}])
    monkeypatch.setattr("vigil.digest.chat_json", fake)
    _apply_interventions()

    since, until = _window(14, 9)
    stats = digest.digest(
        scene.cfg, api_key="k", db_path=scene.db, since=since, until=until,
        day_label="2026-09-14", output_dir=scene.out_dir,
    )

    assert stats.items == 0, "软删的条目不许进日报"
    assert fake.calls == 0, "窗口里没有可见条目 ⇒ 不该调模型（空窗日不花钱）"
    body = (scene.out_dir / "2026-09-14.md").read_text(encoding="utf-8")
    assert "办卡广告" not in body, "「界面上删了、日报里还在」= R13"
    assert "没有值得一提的信息" in body


def test_daily_digest_control_without_the_overlay(scene, monkeypatch, tmp_path):
    """**阴性对照**：干预层是空的时候，**同一天照旧进日报**。

    没有这条对照，上面那条「日报里没有它」证明不了**因果**——它也可能只是
    因为窗口/筛选写错了。⚠️ 这里的空 overlay 用的是**从不存在的路径**，
    顺带覆盖「读取路径自己把 overlay 库就位」（brief Step 3 / T4 的 F5 形态）。
    """
    fake = _StubLLM([{"quotes": ["办卡广告"], "label": "办卡", "text": "速办"}])
    monkeypatch.setattr("vigil.digest.chat_json", fake)
    empty = tmp_path / "never_written" / "overrides.db"
    assert not empty.exists()
    monkeypatch.setattr(overrides, "OVERRIDES_DB", empty)

    since, until = _window(14, 9)
    stats = digest.digest(
        scene.cfg, api_key="k", db_path=scene.db, since=since, until=until,
        day_label="2026-09-14", output_dir=scene.out_dir,
    )

    assert stats.items == 1, "没有干预时它必须照旧进日报（否则上面那条不算数）"
    assert fake.calls == 1
    body = (scene.out_dir / "2026-09-14.md").read_text(encoding="utf-8")
    assert "办卡" in body
    assert empty.is_file(), "读取路径应当把缺失的 overlay 库就位"


# ── ④ T8 的写端点：干预的**第三个入口**（HTTP），两个消费方都要同效 ──────


def test_http_write_endpoints_drive_both_consumers(scene, monkeypatch):
    """⭐ 在 Web 上删/改（走 T8 的 HTTP 端点），日报侧必须**同效**。

    与 ①② 的分工：那两条把干预**直接写进 overlay**（`overrides.delete_item(...)`），
    这条走**产品自己的 HTTP 端点**。没有这条，「端点的写入落到了哪条连接、哪个库」
    就没人验过——`create_app` 里的 `overrides.ensure_schema()` 与端点里的
    `overrides.connect()` **各自读一次** `OVERRIDES_DB` 全局，任何一处被冻进闭包
    （或写死成真实 `data/` 路径），Web 侧自己的测试**照样全绿**，
    而日报（另一条连接）什么都看不到。

    ⚠️ 走 `db_path` 进 `digest.digest`（不是传内存 conn）：这是另一个进程内入口、
    自己开连接、自己 ATTACH overlay（R4 补的 `uri=True`）。
    """
    fake = _StubLLM([{"quotes": ["办卡广告"], "label": "办卡", "text": "速办"}])
    monkeypatch.setattr("vigil.digest.chat_json", fake)
    client = TestClient(create_app(scene.cfg))

    # ① 用**端点**落两条干预：删 item 1（09-14 那天唯一一条）、把 item 2 改成 academic
    assert client.delete("/api/items/1").status_code == 200
    assert client.post(
        "/api/items/2/kind", json={"kind": "academic"}
    ).status_code == 200

    # ② Web 侧：与端点写的一致
    payload = client.get("/api/items").json()
    assert payload["total"] == 2
    assert {i["item_id"] for i in payload["items"]} == {2, 3}
    assert client.get("/api/items", params={"kind": "academic"}).json()["total"] == 1

    # ③ 日报侧：09-14 变成空窗、**不调模型**
    since, until = _window(14, 9)
    stats = digest.digest(
        scene.cfg, api_key="k", db_path=scene.db, since=since, until=until,
        day_label="2026-09-14", output_dir=scene.out_dir,
    )
    assert stats.items == 0, "HTTP 端点删掉的条目不许进日报（R13）"
    assert fake.calls == 0, "窗口里没有可见条目 ⇒ 不该调模型"
    body = (scene.out_dir / "2026-09-14.md").read_text(encoding="utf-8")
    assert "办卡广告" not in body

    # ④ 改分类那条：日报的**读取路径**（`window_items`）看到的是**新**类目
    con = sqlite3.connect(f"file:{scene.db.as_posix()}?mode=ro", uri=True)
    try:
        got = {
            w.item_id: w.kind
            for w in store.window_items(con, since=_D13, until=_D13 + 3600)
        }
    finally:
        con.close()
    assert got.get(2) == "academic", "HTTP 端点改的类目没进日报的读取路径"
    assert 1 not in got, "被端点软删的条目还在日报的读取路径里"


# ── M6 T4：Web 侧**一个文件都不许**在 `data/` 下新增（D11 的完整版）───────
#
# 既有的主库指纹守卫在 `tests/test_api.py`（`test_write_kind_does_not_touch_vigil_db`
# 那三条），它们只盯 `data/vigil.db` 与它的 `-wal`。spec §8 白纸黑字说旧表述
# 「挡不住『我可以写 `data/cache/` 啊』」，而 §1.6 那个地雷**正是这个形状**
# （`cmd_groups` 的实现经 `strip_fake_header` 写 `data/cache/nt_msg_clear.db`）。
# 所以判据升级成「`data/` 下的**文件集合**不变」。


def _make_source_dir(src_dir: pathlib.Path) -> None:
    """造一个数据源目录。

    ⚠️ **不是装饰**：下面那条守卫要证的是「`/api/groups` 不写 `data/`」。
    留一份真实存在的源目录，才能让"如果 api 真去动源侧文件，守卫抓得住"
    这个前提成立（E4/E5 的形态：变异体没落在被测代码的执行路径上，
    看起来像证据、其实是空的）。

    早年这里造的是「带 1024 字节假头的 QQ 库」——那是 `strip_fake_header`
    的输入形状，随 QQ 源一起不在开源版里了。守卫的内容与假头无关，
    普通的占位文件同样成立。
    """
    src_dir.mkdir(parents=True, exist_ok=True)
    (src_dir / "placeholder.db").write_bytes(b"\x00" * 512)


@pytest.fixture
def data_root(tmp_path, monkeypatch):
    """`data/` 的替身（含一份可用的主库）+ 把 `api.REPO_ROOT` 挪到 tmp。

    ⚠️ **两件事必须同时做**，否则守卫是空的：`api.config_path()` **每次现读**
    `api.REPO_ROOT`，不挪的话 `/api/groups` 拿到的 `current_config()` 来自
    **仓库里真实的** `config/groups.toml` ⇒ 它的 `output_db.parent` 是真实的
    `data/`，而用例量的却是这个 tmp 根——「被测端点只碰 tmp」这个前提直接不成立，
    断言却照样可能绿。
    """
    from vigil import api as _api

    root = tmp_path / "fake_root"
    (root / "config").mkdir(parents=True)
    data = tmp_path / "data"
    data.mkdir()
    qqdb_dir = tmp_path / "qqdb"
    _make_source_dir(qqdb_dir)
    (root / "config" / "groups.toml").write_text(
        "[settings]\n"
        # ⚠️ 绝对 + posix：相对路径的解析基准是 `config.REPO_ROOT`（那个全局
        # 没被挪走），反斜杠则是 TOML 基本字符串里的转义字符。
        f'source_dir = "{qqdb_dir.as_posix()}"\n'
        f'output_db = "{(data / "vigil.db").as_posix()}"\n'
        "\n"
        "[[groups]]\n"
        f"id = {GID}\n"
        'name = "测试群"\n'
        "enabled = true\n",
        encoding="utf-8",
    )
    # ⚠️ T9 fix loop 2 起还要一份 `categories.toml`：`POST /api/config/pack`
    # 的**导出**半边会 `load_categories(categories_path())`，而它在文件不存在时
    # 是**响亮报错**的（`CategoryError` ⇒ `/api/config/pack` 500）。
    # 这份表自带内容、不从仓库里抄：抄一份等于让测试取决于用户的类目配置。
    (root / "config" / "categories.toml").write_text(
        '[[categories]]\nslug = "notice"\nicon = "📋"\nlabel = "通知公告"\n'
        'desc = "隔离用类目：通知公告"\n\n'
        '[[categories]]\nslug = "life"\nicon = "🏠"\nlabel = "生活"\n'
        'desc = "隔离用类目：生活"\n',
        encoding="utf-8",
    )
    monkeypatch.setattr(_api, "REPO_ROOT", root)

    # 一个真库文件 + 一条 item（带源消息）：让 `/api/items/1/kind` 真的走完
    # 写路径而不是 404 提前返回——否则「跑一遍所有写端点」是句空话。
    db = data / "vigil.db"
    conn = sqlite3.connect(str(db))
    try:
        store.ensure_schema(conn)
        conn.executescript(_MESSAGE_TABLES_DDL)
        conn.execute(
            "INSERT INTO items (item_id, kind, title, detail, event_ts, deadline_ts,"
            " group_id, actor_uid, place, amount, links, confidence, model, prompt_ver,"
            " created_at) VALUES (1, 'notice', '某条', NULL, ?, NULL, ?, 'u1', NULL,"
            " NULL, '[]', 0.9, 'm', 'v1', 1)",
            (_D13, GID),
        )
        conn.execute(
            "INSERT INTO messages (msg_id, group_id, ts, sender_uid, content)"
            " VALUES (101, ?, ?, 'u1', '原文')",
            (GID, _D13),
        )
        conn.execute("INSERT INTO item_sources (item_id, msg_id) VALUES (1, 101)")
        conn.commit()
    finally:
        conn.close()
    return data


@pytest.fixture
def client(data_root):
    """跑在 tmp `data/` 上的 TestClient（配置的 `output_db` 也指向那里）。"""
    cfg = Config(
        source_dir=data_root.parent / "qqdb",
        output_db=data_root / "vigil.db",
        groups=(Group(id=GID, name="测试群"),),
    )
    return TestClient(create_app(cfg))


# ⚠️ `data/overrides.db` 是 **D11 明确允许**的例外（人工干预层），它连同
# `-wal` / `-shm` 都在允许列表里；其余一律不许新增。
# ⚠️ 写成**模块常量**而不是函数里的局部量（M6 T4 复审 ⚠️-1）：它原先在
# `test_no_web_endpoint_creates_anything_at_all_under_data` 里是**死代码**——
# conftest 的 `_isolate_overrides_path` 把 `OVERRIDES_DB` 挪到**另一个** mktemp
# 目录 ⇒ `data/overrides.db*` 永远不会出现在 `data_root` 里 ⇒ 这条例外
# **从未被行使**（实测 `before=['vigil.db']`）。抽成常量之后，
# 下面那条 `test_the_d11_exception_is_alive_and_still_narrow` 才能用**同一个**
# 集合把「例外真的被行使」与「例外仍然窄」两边都钉住。
_D11_ALLOWED_UNDER_DATA = frozenset({
    pathlib.Path("overrides.db"),
    pathlib.Path("overrides.db-wal"),
    pathlib.Path("overrides.db-shm"),
})


def _assert_no_new_files_under_data(data_root, before) -> None:
    """守卫本体：`data/` 下的文件集合**只许多出 D11 允许的那几条**。

    ⚠️ 抽成函数是为了让「容忍」与「抓得住」两侧走**同一条**判据：
    只看一份副本的断言会在判据被改坏时静默失效（本项目的墓碑清单里有同形的事）。
    """
    after = {p.relative_to(data_root) for p in data_root.rglob("*") if p.is_file()}
    assert after - before <= _D11_ALLOWED_UNDER_DATA, (
        f"Web 侧往 data/ 里新增了文件：{sorted(after - before - _D11_ALLOWED_UNDER_DATA)} —— "
        "D11 只允许写 data/overrides.db 与 config/"
    )


def test_no_web_endpoint_creates_anything_at_all_under_data(data_root, client):
    """⭐ D11 的完整版本：Web 侧跑一遍写端点，`data/` 下**一个文件都不许多**。

    ⚠️ **这句话原来写得比实际宽**（M6 T9 fix loop 1 / m4）：正文当时只跑了 5 条，
    漏了 `PUT /api/llm` **和** `POST /api/config/pack`。两条现在都在表里。

    ⚠️ **fix loop 2 修的是"理由"那一半**（reviewer 抓到的第二处假话）：
    §m4 的原话称 `/api/llm` 是「**唯一一个**『写的东西不在 `data/`』的端点」
    ——**紧邻上一句**就列着 `/api/categories*` 与 `/api/groups*`，
    而它们写的是同一族 `config/*.toml`（`llm_path()` / `config_path()` /
    `categories_path()` 是**同一个根**）。**「唯一」是假的。**

    ⚠️ 而那句"省略理由"（"它们各自需要有效的请求体，塞进来会变成夹具练习"）
    对 `POST /api/config/pack` **根本不成立**——reviewer 实测：往
    `api_import_pack` 里塞一句写 `data/pack-audit.log`，**全量 820 条零红**
    ⇒ **那个端点的 `data/` 足迹当时没有任何人守**。它不是"夹具练习"：
    先 `GET /api/config/pack` 拿一个合法包、再原样 POST 回来就完了
    （见下面正文），而且它是这张表里**破坏力最大**的一条（导入会重写
    `config/*.toml` 与 overlay 的别名表）。

    ⇒ 现表里 7 条。**仍然不在表里**的是 `/api/categories*`、`/api/groups*`、
    `/api/items/kinds`、`/api/bulk-undo`、`/api/reorder`、`/api/jobs/*`
    ——这几条**确实**各自需要有效的请求体（类目 slug、job id…），
    把它们都塞进来会把守卫写成"夹具练习"。
    ⚠️ **这是一句如实的话，不是挡箭牌**：它只覆盖列出来的那几条，
    `/api/llm` 与 `POST /api/config/pack` 已经从这句话里拿掉了。

    ⚠️ 为什么不能只盯 `data/vigil.db` 的指纹（既有那条守卫做的事）：
    spec §8 白纸黑字说旧表述「挡不住『我可以写 `data/cache/` 啊』」，
    而 §1.6 那个地雷**正是这个形状**。所以判据必须升级成
    「`data/` 下的文件集合不变」——新增一个文件也算违规。

    ⚠️ 变异反证：让 `api_groups` 改成去调 `qqdb.strip_fake_header(...)`
    ⇒ `data/cache/` 下多出一个文件 ⇒ 本条**必红**
    （而只看主库指纹的那条守卫**照样绿**——这就是本条的增量）。
    """
    before = {p.relative_to(data_root) for p in data_root.rglob("*") if p.is_file()}

    client.get("/api/groups")
    client.post("/api/persons", json={"uin": 10001, "label": "测试"})
    client.delete("/api/persons/10001")
    client.post("/api/items/1/kind", json={"kind": "life"})
    client.post("/api/undo", json={})
    # ⚠️ T9 新增：`PUT /api/llm` 写的是 `config/llm.toml`（**D11 允许的 `config/`**），
    # 正是"Web 的写边界钉在具体文件上"这条的第二个落点——
    # 它若被写成往 `data/` 下丢一份副本（或往 `data/cache/` 丢临时文件），
    # 下面那条断言必须翻脸。`data_root` 夹具已经把 `api.REPO_ROOT` 挪到 tmp，
    # 所以它写的是 tmp 的 `config/`，不会碰真实仓库。
    r = client.put(
        "/api/llm",
        json={"base_url": "https://x/v1", "model": "m", "enable_thinking": None},
    )
    assert r.status_code == 200, r.text
    # ⚠️ T9 fix loop 2：`POST /api/config/pack`（**导入**）也在表里了。
    # 它写的是 `config/groups.toml` / `config/categories.toml` /
    # `config/persons.toml` 与 overlay 的别名表——**一个字节都不该落在 `data/`**。
    # ⚠️ 不需要造夹具：**先导出、再原样导回来**（`apply=True`）。
    # 包里装的就是本机此刻的配置 ⇒ 走完整的写路径而内容不变，
    # 而且这正是用户在网页上会做的事（导出 → 改几行 → 导入）。
    pack = client.get("/api/config/pack").json()["toml"]
    r = client.post(
        "/api/config/pack",
        json={"toml": pack, "apply": True, "keep_settings": True},
    )
    assert r.status_code == 200, r.text

    _assert_no_new_files_under_data(data_root, before)


def test_the_d11_exception_is_alive_and_still_narrow(data_root, monkeypatch):
    """⭐ `data/overrides.db*` 真的出现过：守卫**容忍**它，但**不放行别的**。

    ⚠️ 为什么要有这一条（M6 T4 复审 ⚠️-1）：上面那条守卫里的 `allowed` 三条
    在默认夹具下**从未被行使**（`OVERRIDES_DB` 被 conftest 挪到另一个目录了）
    ——「看起来有、其实没人看」的代码，本项目是给过墓碑的
    （`daily.RunReport.counters`）。这里让它**活**：
    把 `OVERRIDES_DB` 指进 `data_root`，于是那条例外真的被用到。

    ⚠️ 判据三件，缺一条就会退化成空守卫：
      ① **阳性对照**：`overrides.db*` 必须真的落进 `data_root`；
      ② 守卫**容忍**它（用上面那条一模一样的 `_assert_no_new_files_under_data`）；
      ③ 但它**只**容忍它——亲手多写一个 `cache/nt_msg_clear.db`
         （= §1.6 那个地雷的形状），守卫必须**翻脸**。
    """
    monkeypatch.setattr(overrides, "OVERRIDES_DB", data_root / "overrides.db")
    # ⚠️ 必须**重造 app**：`overrides.ensure_schema()` 是在 `create_app` 里跑的，
    # 而 `client` 夹具是在这次 monkeypatch **之前**造出来的。
    cfg = Config(
        source_dir=data_root.parent / "qqdb",
        output_db=data_root / "vigil.db",
        groups=(Group(id=GID, name="测试群"),),
    )
    # ⚠️ `before` 必须在**造 app 之前**取：`create_app` 里的
    # `overrides.ensure_schema()` 就会把 `overrides.db` 建出来（那也算 Web 侧写
    # `data/`），在它之后取快照的话，这个文件会被算进「本来就有的」⇒ 阳性对照
    # 永远是空的（实测第一版就踩了这个：`new=set()`）。
    before = {p.relative_to(data_root) for p in data_root.rglob("*") if p.is_file()}
    c = TestClient(create_app(cfg))

    assert c.post("/api/items/1/kind", json={"kind": "life"}).status_code == 200
    new = {p.relative_to(data_root) for p in data_root.rglob("*") if p.is_file()} - before

    assert any(p.name.startswith("overrides.db") for p in new), (
        f"阳性对照失败：`data/overrides.db*` 没有出现 ⇒ 本条与上面那条一样是空守卫（new={new}）"
    )
    # ② 走**同一条**守卫判据：容忍例外
    _assert_no_new_files_under_data(data_root, before)

    # ③ 例外是**窄**的：§1.6 的地雷仍然要被抓住
    cache_dir = data_root / "cache"
    cache_dir.mkdir(exist_ok=True)
    (cache_dir / "nt_msg_clear.db").write_bytes(b"x")
    with pytest.raises(AssertionError):
        _assert_no_new_files_under_data(data_root, before)
