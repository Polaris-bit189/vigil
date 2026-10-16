"""群清单快照 —— M6 T4（spec §1.6 的只读路径）。"""

from __future__ import annotations

import json
import pathlib

from vigil import config, groups_snapshot


def _cfg(tmp_path) -> config.Config:
    (tmp_path / "qqdb").mkdir(exist_ok=True)
    return config.Config(
        source_dir=tmp_path / "qqdb",
        output_db=tmp_path / "data" / "vigil.db",
        groups=(config.Group(id=111, name="甲群"),),
    )


def test_default_path_sits_next_to_the_main_db(tmp_path):
    """快照与主库同目录（`data/`）——它们是同一份「导出产物」的两个视角。"""
    p = groups_snapshot.default_path(_cfg(tmp_path))
    assert p.parent == tmp_path / "data"
    assert p.name == "groups-snapshot.json"


def test_write_then_load_roundtrips(tmp_path):
    groups = (
        groups_snapshot.GroupInfo(id=111, name="甲群", messages=10, last_ts=1700000000),
        groups_snapshot.GroupInfo(id=222, name="乙群", messages=0, last_ts=0),
    )
    p = groups_snapshot.write(groups, tmp_path / "s.json", now=1700000001)
    snap = groups_snapshot.load(p)
    assert snap is not None
    assert snap.generated_at == 1700000001
    assert snap.groups == groups


def test_load_returns_none_for_a_missing_file(tmp_path):
    """⚠️ **不抛异常**：快照是增强项（Web 的群下拉框），没有它时接口应该
    回一句「还没有快照，请先跑 vigil export」，而不是 500。"""
    assert groups_snapshot.load(tmp_path / "nope.json") is None


def test_load_returns_none_for_a_corrupt_file(tmp_path):
    """坏 JSON / 坏结构一律 `None`——同一条理由：它是增强项。

    ⚠️ 与 `load_config` 的「快速失败」**不矛盾**：`groups.toml` 是**必需**的
    配置，写错了要立刻死；而快照是**派生物**，它坏了只意味着"该重跑一次
    export"，把整个服务拖死是过度反应。

    ⚠️ **每条输入都必须真的走到它要测的那道闸**（M6 T4 复审 Important-1，
    E7 的「输入到不了」形态）：原稿的三条里，后两条都**缺 `schema`**，
    于是它们在**版本闸**就返回了 `None` —— `load()` 的「`groups` 是不是 list」
    与「逐条校验」**一行都没被执行过**，而测试看起来覆盖了「坏文件」。
    所以这里：① 每条坏输入都**只坏一处**（其余字段与阳性对照逐字相同）；
    ② 先跑阳性对照——它必须 `load` 得回来，否则说明这些 `None` 都是
    **更早的闸**给的，本条判据（连同它下面的每一条）就是空的。
    """
    p = tmp_path / "s.json"

    def load_of(payload) -> groups_snapshot.Snapshot | None:
        p.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
        return groups_snapshot.load(p)

    # ── 0. 阳性对照：下面每条坏输入都是**在它身上只坏一处** ──────────
    good = {
        "schema": groups_snapshot.SCHEMA_VERSION,
        "generated_at": 1,
        "groups": [{"id": 111, "name": "甲群", "messages": 1, "last_ts": 2}],
    }
    assert load_of(good) is not None, (
        "阳性对照都 load 不回来 ⇒ 下面那些 `None` 是更早的闸给的，本条是空守卫"
    )

    # ── 1. 连 JSON 都不是 / 顶层不是表 ────────────────────────────
    p.write_text("{not json", encoding="utf-8")
    assert groups_snapshot.load(p) is None
    p.write_text("[1, 2]", encoding="utf-8")
    assert groups_snapshot.load(p) is None

    # ── 2. 版本闸（其余字段全对）──────────────────────────────────
    assert load_of({**good, "schema": groups_snapshot.SCHEMA_VERSION + 1}) is None

    # ── 3. 形状错：generated_at / groups ──────────────────────────
    assert load_of({**good, "generated_at": "1700000000"}) is None, (
        "generated_at 不是整数 ⇒ 必须 None（否则 `int()` 那行在旧代码里会抛）"
    )
    assert load_of({**good, "groups": {}}) is None, "`groups` 不是列表 ⇒ 必须 None"
    assert load_of({**good, "groups": [[1, 2]]}) is None, "列表元素不是表 ⇒ 必须 None"

    # ── 4. 逐条校验（⚠️ 这一节是原稿**到不了**的那一段）────────────
    # 4a. 字段**全在、类型全错**：够得到 `int()` / `str()` 那四行，
    #     也只有它能证明「不是随便 `GroupInfo(**e)` 就放行」
    #     （缺键那条走的是同一个 except，但 `GroupInfo(**e)` 也会抛，
    #      所以单靠它区分不出「有校验」与「没校验」）。
    assert load_of({**good, "groups": [{"id": "x", "name": "甲群",
                                       "messages": 1, "last_ts": 2}]}) is None
    # 4b. 缺键
    assert load_of({**good, "groups": [{"id": 111}]}) is None


def test_load_refuses_a_future_schema_version(tmp_path):
    """⭐ 版本闸：将来改了快照形状时，**旧代码必须拒绝读新文件**。

    ⚠️ 没有这道闸的话，加字段的那天会得到一个静默的错读（新文件里的某个
    字段被旧代码当成别的意思）——而快照恰恰是「用户看着它决定加不加群」
    的东西，错读的代价是**加错群**。宁可返回 `None`（界面说「快照过期，
    请重跑 export」）。
    """
    p = tmp_path / "s.json"
    p.write_text(
        json.dumps(
            {"schema": groups_snapshot.SCHEMA_VERSION + 1, "generated_at": 1, "groups": []}
        ),
        encoding="utf-8",
    )
    assert groups_snapshot.load(p) is None


def test_write_is_atomic(tmp_path):
    """快照必须原子写（与全项目同一条规矩）。

    ⚠️ Web 每个请求都可能读它，而它由 CLI 在另一个进程里写 ——
    正是「读者可能看到半个文件」的经典场景。
    """
    p = tmp_path / "s.json"
    groups_snapshot.write(
        (groups_snapshot.GroupInfo(id=111, name="甲群", messages=1, last_ts=2),), p
    )
    body = p.read_text(encoding="utf-8")
    assert json.loads(body)["schema"] == groups_snapshot.SCHEMA_VERSION
    leftovers = [q.name for q in p.parent.iterdir() if ".tmp" in q.name]
    assert leftovers == []


def test_rewrite_replaces_the_file_rather_than_overwriting_it_in_place(tmp_path):
    """⭐ 原子性的**行为**判据：重写必须换一个文件（新 inode 顶替）。

    ⚠️ 上面那条只查「内容对、没留下 `.tmp`」——**它区分不出原子写与就地覆盖**：
    实测（M6 T4）把 `atomic_write_text(...)` 换成 `path.write_text(...)`，
    那条**照样全绿**。而「就地覆盖」正是 U-4 的缺陷形态
    （`qqdb.strip_fake_header` 的旧实现）：读者会看到**半个文件**。

    ⇒ 这里钉机制（文件身份变没变）：`os.replace` 顶替 ⇒ 新 inode；
    截断重写 ⇒ inode 不变。它确定、**无计时依赖**——比「起两个线程赛跑」
    可靠得多（那种测试会 flaky，而 flaky 的守卫最终会被注释掉）。

    ⚠️ 先响亮地确认度量工具本身可用：FAT 之类的文件系统上 `st_ino` 恒为 0，
    那样本条会**恒红**，而红的原因与原子性无关。NTFS 上 `st_ino` 有效
    （CPython 走 `GetFileInformationByHandle`），T1 的 `tests/test_qqdb.py`
    用的是同一条判据、同一条前置断言。
    """
    p = tmp_path / "s.json"
    one = (groups_snapshot.GroupInfo(id=111, name="甲群", messages=1, last_ts=2),)
    groups_snapshot.write(one, p, now=1)
    assert p.stat().st_ino != 0, "本机的 st_ino 不可用，本条判据失效——请改用别的度量"
    old_ino = p.stat().st_ino

    groups_snapshot.write(one, p, now=2)
    assert json.loads(p.read_text(encoding="utf-8"))["generated_at"] == 2
    assert p.stat().st_ino != old_ino, (
        "重写必须走 `os.replace`（新文件顶替），不能就地覆盖——"
        "后者会让并发读者看到半个文件（U-4 的形态）"
    )


def test_collect_merges_counts_and_names():
    """`collect` 把 `[(群号, 消息数, 最新ts)]` 与 `{群号: 群名}` 拼成 `GroupInfo`。

    ⚠️ 群名取不到时**留空串而不是 `"?"`**：`"?"` 是一个看起来像名字的
    占位符，而界面上「(无名)」与「?」给人的判断完全不同。名字缺失是
    正常状态（`group_info.db` 可能落后于 `nt_msg.db`）。
    """
    counts = [(111, 10, 1700000000), (222, 3, 1700000001)]
    names = {"111": "甲群"}
    out = groups_snapshot.collect(counts, names)
    assert out[0] == groups_snapshot.GroupInfo(
        id=111, name="甲群", messages=10, last_ts=1700000000
    )
    assert out[1].name == ""
