"""编排层测试。用假 LLM 与内存库，绝不联网、绝不碰真实数据。"""

from __future__ import annotations

import sqlite3

import pytest

from vigil import refine, store
from vigil.llm import LLMResult
from vigil.store import STATUS_DISCARDED, STATUS_OK


class FakeLLM:
    """记录调用、按脚本返回。用于验证编排逻辑而非模型质量。"""

    def __init__(self, script=None):
        self.calls = []
        self.script = script or (lambda messages: {"items": []})

    def __call__(self, cfg, *, system, user, sleep=None):
        self.calls.append({"system": system, "user": user})
        return LLMResult(
            payload=self.script(user), input_tokens=100, output_tokens=20
        )


@pytest.fixture
def seeded(memdb):
    memdb.executescript(
        """
        CREATE TABLE messages (
            msg_id INTEGER PRIMARY KEY, group_id INTEGER NOT NULL,
            ts INTEGER NOT NULL, sender_uid TEXT, content TEXT NOT NULL
        );
        CREATE TABLE sender_names (
            group_id INTEGER NOT NULL, uid TEXT NOT NULL, group_nick TEXT,
            qq_nick TEXT, uin INTEGER, in_group INTEGER,
            PRIMARY KEY (group_id, uid)
        );
        """
    )
    memdb.executemany(
        "INSERT INTO messages VALUES (?,?,?,?,?)",
        [
            (1, 100, 1000, "u_a", "明天下午有讲座，地点西太湖报告厅"),
            (2, 100, 1001, "u_b", "已完成"),
            (3, 100, 1002, "u_a", "数学作业截止到9月7号"),
        ],
    )
    memdb.executemany(
        "INSERT INTO sender_names VALUES (?,?,?,?,?,?)",
        [(100, "u_a", "班长小王", "小王", 111, 0), (100, "u_b", "李四", "李四", 222, 0)],
    )
    memdb.commit()
    store.ensure_schema(memdb)
    return memdb


class StubConfig:
    """refine 只用到 tier_of 与群名，不需要真实配置文件。"""

    def tier_of(self, gid: int) -> str:
        return "high"

    def group_name(self, gid: int) -> str:
        return "测试群"


def test_build_system_prompt_contains_categories():
    from vigil.categories import load_categories

    prompt = refine.build_system_prompt(load_categories())
    assert "notice" in prompt and "secondhand" in prompt
    assert "items" in prompt, "必须要求 items 外层包裹（探针实测）"


def test_build_user_prompt_injects_today():
    """不给今天，模型会把「9月7号」猜成 2023（探针实测）。"""
    from vigil.redact import Redactor
    from vigil.store import PendingMessage

    msgs = [
        PendingMessage(
            msg_id=1, group_id=100, ts=1000, sender_uid="u_a",
            sender="班长小王", content="作业截止9月7号",
        )
    ]
    text = refine.build_user_prompt(msgs, Redactor(), today="2026-09-13")
    assert "2026-09-13" in text
    # 行格式：`- 代号 称呼: 内容`，不带任何编号。
    # ⚠️ 称呼是 `redactor.name()` 的产出——**角色词留下、人名抹掉**（P3）。
    # 「班长小王」里"小王"是人名本体，出网的只有「班长」。
    assert "- U1 班长: 作业截止9月7号" in text


def test_build_user_prompt_redacts_numbers():
    from vigil.redact import Redactor
    from vigil.store import PendingMessage

    msgs = [
        PendingMessage(
            msg_id=1, group_id=100, ts=1000, sender_uid="u_a",
            sender="2026000001张三13900000001", content="打我电话13812345678",
        )
    ]
    text = refine.build_user_prompt(msgs, Redactor(), today="2026-09-13")
    assert "13900000001" not in text
    assert "13812345678" not in text
    # ⚠️ 姓名**不许**出网（P3）。这条曾是 `assert "张三" in text` —— 那断言
    # 编码的是"本机内部保留姓名"的旧行为，而出网边界上姓名是最直接的
    # 个人标识符（赛题第⑧条要求"不得侵犯他人隐私"）。
    # 见 redact.py 的 `Redactor.name()`。
    assert "张三" not in text, "姓名不许出网"
    assert "2026000001" not in text, "学号（10 位长数字）也不许出网"


def test_build_user_prompt_distinguishes_anonymous_senders():
    """⚠️ 匿名发送者必须**逐条区分**。

    本条的存在本身就是审查的产物：Task 7 审查实测「**删掉这个特判，128 条
    测试全绿**」——实现是对的，但零守护。这是「空守卫」在同一里程碑里的
    第四次重现，所以补上。

    为什么重要：W1 波级审查实测全库 1,218 条匿名消息里 **1,196 条是实质正文**，
    含两位班助各 11 条。若全归一个代号，模型会把互不相识的
    人当成同一个人，并把这个错误认知写进 title/detail。
    """
    from vigil.redact import Redactor
    from vigil.store import PendingMessage

    msgs = [
        PendingMessage(
            msg_id=1, group_id=100, ts=1000, sender_uid="",
            sender="", content="转发通知：明天停课",
        ),
        PendingMessage(
            msg_id=2, group_id=200, ts=1001, sender_uid="",
            sender="", content="转发通知：明天停课",
        ),
    ]
    text = refine.build_user_prompt(msgs, Redactor(), today="2026-09-13")
    assert "匿名1" in text, "第一条匿名消息要有自己的标记"
    assert "匿名2" in text, "第二条必须是**不同**的标记"
    assert text.count("匿名") == 2


def test_build_user_prompt_keeps_named_senders_stable():
    """有 uid 的仍走稳定代号——同一人两次发言必须看得出是同一人。

    与上一条互补：匿名者**逐条区分**，有身份者**稳定复用**。两条一起才
    完整描述 `build_user_prompt` 的发信人标记规则。
    """
    from vigil.redact import Redactor
    from vigil.store import PendingMessage

    msgs = [
        PendingMessage(msg_id=1, group_id=100, ts=1000, sender_uid="u_a",
                       sender="班长小王", content="第一句"),
        PendingMessage(msg_id=2, group_id=100, ts=1001, sender_uid="u_a",
                       sender="班长小王", content="第二句"),
    ]
    text = refine.build_user_prompt(msgs, Redactor(), today="2026-09-13")
    assert text.count("U1") == 2, "同一人的两条消息应共用一个代号"


def test_prompt_carries_no_numbers_at_all():
    """⚠️ 提示词里**不能出现任何编号**——msg_id 和序号都试过了，都不行。

    Task 8 冒烟实测逼出的结论：
      * **msg_id**（19 位雪花号，同批只差末 3 位）→ 命中 **0/2**
      * **批内序号** → A/B 测试命中 1/1，但**真实管线里仍然错**：
        4 条消息的批次，真来源在第 3 位，模型读对了内容却返回 `idx=1`

    所以改成**不问模型编号**：消息不带编号，来源靠 `quote` 逐字摘录本地匹配。
    这条测试守「不给模型任何可抄的编号」这个前提。
    """
    from vigil.redact import Redactor
    from vigil.store import PendingMessage

    msgs = [
        PendingMessage(msg_id=7685024133064673881, group_id=100, ts=1000,
                       sender_uid="u_a", sender="甲", content="第一条"),
        PendingMessage(msg_id=7685024133064673978, group_id=100, ts=1001,
                       sender_uid="", sender="", content="第二条"),
        PendingMessage(msg_id=7685024133064673918, group_id=100, ts=1002,
                       sender_uid="u_b", sender="乙", content="第三条"),
    ]
    text = refine.build_user_prompt(msgs, Redactor(), today="2026-09-13")

    # 19 位 msg_id 一个都不许出现
    for mid in (7685024133064673881, 7685024133064673978, 7685024133064673918):
        assert str(mid) not in text, f"提示词里不该出现长 msg_id: {mid}"
    # 也不该有行首序号
    for i in (1, 2, 3):
        assert f"\n{i}. " not in text, f"提示词里不该出现序号 {i}"
    # 发信人代号仍要保留——它是判断「同一人说了两次」的信号。
    # ⚠️ 代号后面跟的是 `redactor.name()` 的产出（角色词/兜底词），
    # **不再是昵称原文**——「甲」「乙」是人名本体，出网时被抹掉（P3）。
    assert "U1 " in text and "U2 " in text, "代号必须在（否则看不出同一人说了两次）"
    assert "甲" not in text and "乙" not in text, "人名本体不许出网"
    # 匿名者仍要逐条区分
    assert "匿名1" in text


def test_refine_rejects_both_db_path_and_conn_being_none(monkeypatch, tmp_path):
    """⭐ 两个都给 None ⇒ **响亮失败**，而不是建一个叫 `None` 的库文件。

    ⚠️ 这条守的是一个**真实发生过的静默破坏**（2026-10-15）：`str(None)` 是
    字面量 `"None"`，于是 `sqlite3.connect(str(db_path))` 会在**当前工作目录**
    建出一个名为 `None` 的库——表结构齐全、一行数据没有，而调用方以为自己在
    往真库写。实测在 `content/` 根目录留下过一份 73KB 的空库。

    ⚠️ 它比"多一个文件"严重：**`*.db` 通配符匹配不到 `None`** ⇒ 不在
    `.gitignore` 保护范围内 ⇒ 会随开源仓库发出去（赛题复现红线相关）。

    ⚠️ 判据必须**两半都钉**：抛 ValueError 且**没有文件被建出来**。
    只钉前者的话，一个"先连一下再抛"的实现照样绿。
    """
    monkeypatch.chdir(tmp_path)  # 若真建了文件，落在这里而不是仓库里

    with pytest.raises(ValueError, match="db_path"):
        refine.refine(object(), api_key="k", db_path=None, conn=None)

    assert list(tmp_path.iterdir()) == [], "不许建出任何文件"


def test_cli_refine_model_falls_back_to_default(monkeypatch, tmp_path):
    """⚠️ argparse 不给 `--model` 时传的是 `None`，而 `None` 会**绕过**
    `refine()` 的默认参数值（默认值只在「未传参」时生效）。

    CLI 不自己兜底的话，会把 `None` 当模型名发给 SiliconFlow。
    """
    from vigil import cli, refine as refine_mod

    captured = {}

    def fake_refine(config, **kwargs):
        captured.update(kwargs)
        return refine_mod.RefineStats()

    monkeypatch.setattr(refine_mod, "refine", fake_refine)
    monkeypatch.setattr(cli, "_load_config_only", lambda: object())
    monkeypatch.setattr(cli, "_require_export_db", lambda c: tmp_path / "x.db")

    assert cli.main(["refine", "--dry-run"]) == 0
    assert captured["model"] == refine_mod.DEFAULT_MODEL
    assert captured["dry_run"] is True


def test_load_llm_key_prefers_env_over_file(monkeypatch, tmp_path):
    """密钥读取：环境变量优先于 `.env`；都没有时返回空串而不是抛错。"""
    from vigil.config import load_llm_key

    env = tmp_path / ".env"
    env.write_text("SILICONFLOW_API_KEY=sk-from-file\n", encoding="utf-8")

    monkeypatch.delenv("SILICONFLOW_API_KEY", raising=False)
    assert load_llm_key(env_path=env) == "sk-from-file"

    monkeypatch.setenv("SILICONFLOW_API_KEY", "sk-from-env")
    assert load_llm_key(env_path=env) == "sk-from-env", "环境变量应优先于 .env"

    monkeypatch.delenv("SILICONFLOW_API_KEY", raising=False)
    assert load_llm_key(env_path=tmp_path / "nonexistent.env") == "", "文件不存在不该崩"


def test_refine_saves_items(seeded, monkeypatch):
    """一条消息产出条目 → 落库 → 记账。"""
    fake = FakeLLM(
        lambda user: {
            "items": [
                {
                    "quote": "明天下午有讲座", "kind": "activity",
                    "title": "西太湖报告厅有讲座",
                    "detail": None, "deadline": None, "place": "西太湖报告厅",
                    "amount": None, "confidence": 0.9,
                }
            ]
        }
    )
    monkeypatch.setattr(refine, "chat_json", fake)

    stats = refine.refine(
        StubConfig(), api_key="k", db_path=None, conn=seeded,
        on_progress=lambda *a, **k: None,
    )

    assert stats.items_saved == 1
    assert seeded.execute("SELECT kind, title FROM items").fetchone() == (
        "activity", "西太湖报告厅有讲座",
    )
    assert seeded.execute("SELECT item_id, msg_id FROM item_sources").fetchall() == [(1, 1)]


def test_refine_passes_thinking_switch_to_llm(seeded, monkeypatch):
    """思考开关必须一路到得了请求层。

    只断言 `refine()` 收得下参数是不够的——那正是「空守卫」的形状：
    参数在签名里、被接受、被忽略，测试照样绿。这里断言的是它
    **真的传到了交给 chat_json 的 LLMConfig 上**。

    实测依据见 llm.LLMConfig 的注释：Qwen3.5-35B-A3B 开思考 111.5s /
    11,124 输出 token，关掉 2.8s / 220 token。这个参数丢了就是 40 倍代价。
    """
    seen = []

    def fake(cfg, *, system, user, sleep=None):
        seen.append(cfg)
        return LLMResult(payload={"items": []}, input_tokens=1, output_tokens=1)

    monkeypatch.setattr(refine, "chat_json", fake)
    quiet = lambda *a, **k: None

    refine.refine(
        StubConfig(), api_key="k", conn=seeded, batch_size=1, on_progress=quiet
    )
    assert seen, "一批都没跑，这条测试就没在验证任何东西"
    assert all(c.enable_thinking is False for c in seen), "默认必须关思考"

    seen.clear()
    refine.refine(
        StubConfig(), api_key="k", conn=seeded, batch_size=1, redo=True,
        enable_thinking=True, on_progress=quiet,
    )
    assert seen, "redo 那轮一批都没跑"
    assert all(c.enable_thinking is True for c in seen), "--think 没传到请求层"


def test_refine_records_discarded_for_noise(seeded, monkeypatch):
    """硬丢弃的消息也要记账——M1 出口要求 refine_runs 无遗漏。"""
    monkeypatch.setattr(refine, "chat_json", FakeLLM())
    refine.refine(StubConfig(), api_key="k", conn=seeded, on_progress=lambda *a, **k: None)

    row = seeded.execute(
        "SELECT status FROM refine_runs WHERE msg_id = 2"
    ).fetchone()
    assert row == (STATUS_DISCARDED,)


def test_refine_is_idempotent(seeded, monkeypatch):
    """跑两次，第二次不该重复调模型、不该重复写条目。"""
    fake = FakeLLM(
        lambda user: {
            "items": [
                {"quote": "明天下午有讲座", "kind": "activity", "title": "讲座", "detail": None,
                 "deadline": None, "place": None, "amount": None, "confidence": 0.9}
            ]
        }
    )
    monkeypatch.setattr(refine, "chat_json", fake)

    refine.refine(StubConfig(), api_key="k", conn=seeded, on_progress=lambda *a, **k: None)
    first_calls = len(fake.calls)
    refine.refine(StubConfig(), api_key="k", conn=seeded, on_progress=lambda *a, **k: None)

    assert len(fake.calls) == first_calls, "第二次不该再调模型"
    assert seeded.execute("SELECT count(*) FROM items").fetchone()[0] == 1


def test_refine_parses_deadline(seeded, monkeypatch):
    fake = FakeLLM(
        lambda user: {
            "items": [
                {"quote": "数学作业截止到9月7号", "kind": "academic",
                 "title": "数学作业截止",
                 "detail": None, "deadline": "2026-09-07", "place": None,
                 "amount": None, "confidence": 0.9}
            ]
        }
    )
    monkeypatch.setattr(refine, "chat_json", fake)
    refine.refine(StubConfig(), api_key="k", conn=seeded, on_progress=lambda *a, **k: None)

    deadline = seeded.execute(
        "SELECT deadline_ts FROM items WHERE title = '数学作业截止'"
    ).fetchone()[0]
    import datetime as dt
    assert dt.datetime.fromtimestamp(deadline).strftime("%Y-%m-%d") == "2026-09-07"


def _seeded_with(memdb, content: str) -> sqlite3.Connection:
    """一个**只有一条消息**的库，消息正文是 `content`。

    ⚠️ 刻意**不用 `seeded` 夹具**：那个夹具是**共享**的（96 处用例在用），
    往里加一条消息会连带打红一批"恰好 3 条"的断言（实测踩到过）。
    本函数的用例只关心「源文里写了什么时刻」，自己造库最干净。
    """
    memdb.executescript(
        """
        CREATE TABLE messages (
            msg_id INTEGER PRIMARY KEY, group_id INTEGER NOT NULL,
            ts INTEGER NOT NULL, sender_uid TEXT, content TEXT NOT NULL
        );
        CREATE TABLE sender_names (
            group_id INTEGER NOT NULL, uid TEXT NOT NULL, group_nick TEXT,
            qq_nick TEXT, uin INTEGER, in_group INTEGER,
            PRIMARY KEY (group_id, uid)
        );
        """
    )
    memdb.execute("INSERT INTO messages VALUES (1, 100, 1000, 'u_a', ?)", (content,))
    memdb.execute(
        "INSERT INTO sender_names VALUES (100, 'u_a', '班长小王', '小王', 111, 0)"
    )
    memdb.commit()
    store.ensure_schema(memdb)
    return memdb


def test_refine_saves_start_ts_when_source_has_the_time(memdb, monkeypatch):
    """⭐ 源文写了时刻 ⇒ `start_ts` 落库（端到端：模型输出 → 闸门 → 库）。"""
    conn = _seeded_with(memdb, "周三14:00在体育馆集合，别迟到")
    fake = FakeLLM(
        lambda user: {
            "items": [
                {"quote": "周三14:00在体育馆集合", "kind": "activity",
                 "title": "体育馆集合", "detail": None, "deadline": None,
                 "start": "2026-09-16 14:00", "place": "体育馆",
                 "amount": None, "confidence": 0.9}
            ]
        }
    )
    monkeypatch.setattr(refine, "chat_json", fake)
    refine.refine(StubConfig(), api_key="k", conn=conn, on_progress=lambda *a, **k: None)

    import datetime as dt
    got = conn.execute(
        "SELECT start_ts FROM items WHERE title = '体育馆集合'"
    ).fetchone()[0]
    assert got is not None, "源文有「14:00」，闸门不该把它清掉"
    assert dt.datetime.fromtimestamp(got).strftime("%Y-%m-%d %H:%M") == "2026-09-16 14:00"


def test_refine_drops_start_ts_that_source_never_said(memdb, monkeypatch):
    """⭐⭐ **这道闸门存在的全部理由**：源文只说「明早集合」，模型编了个 08:00。

    ⚠️ 处置是**降级成 NULL**、条目照常保留（不是丢条目）——与 `deadline` 一致。
    判据必须同时钉两半：`start_ts` 是 NULL **且**条目还在。
    只钉前者的话，一个"整条丢弃"的实现照样绿，而那会让用户少看到一条通知。
    """
    conn = _seeded_with(memdb, "明早各班在宿舍楼下集合")
    fake = FakeLLM(
        lambda user: {
            "items": [
                {"quote": "明早各班在宿舍楼下集合", "kind": "notice",
                 "title": "明早集合", "detail": None, "deadline": None,
                 "start": "2026-09-16 08:00", "place": "宿舍楼下",
                 "amount": None, "confidence": 0.9}
            ]
        }
    )
    monkeypatch.setattr(refine, "chat_json", fake)
    refine.refine(StubConfig(), api_key="k", conn=conn, on_progress=lambda *a, **k: None)

    rows = conn.execute(
        "SELECT title, start_ts FROM items WHERE title = '明早集合'"
    ).fetchall()
    assert len(rows) == 1, "条目必须保留（降级的是字段，不是整条）"
    assert rows[0][1] is None, "源文没写几点，编出来的 08:00 必须被清掉"


def test_refine_drops_unmatchable_quote(seeded, monkeypatch):
    """摘录匹配不上就必须丢弃，不能挂到随便某条消息上。

    这是「可回溯」的最后一道闸：M1 出口标准要求能跳回原文**且内容对得上**，
    **挂错的来源比没有来源更糟**——它会让人以为数据没问题。
    """
    fake = FakeLLM(
        lambda user: {
            "items": [
                {"quote": "这段文字不在任何消息里啊", "kind": "notice",
                 "title": "编的", "detail": None, "deadline": None,
                 "place": None, "amount": None, "confidence": 0.9}
            ]
        }
    )
    monkeypatch.setattr(refine, "chat_json", fake)
    stats = refine.refine(StubConfig(), api_key="k", conn=seeded,
                          on_progress=lambda *a, **k: None)
    assert stats.items_saved == 0
    assert seeded.execute("SELECT count(*) FROM items").fetchone()[0] == 0


def test_match_source_tolerates_punctuation_but_not_rewriting():
    """摘录匹配容忍**标点/空白**差异，但不容忍**改写**。

    这条守的是 `_normalize` 的两侧边界：
      · **容忍**：全角括号 vs 半角、多余空格——模型转写时的格式差异
      · **不容忍**：换了词——那就是编的，必须丢（挂错来源比没来源更糟）
      · **太短不匹配**（`len(needle) < 4`）——否则「收到」会命中一大片

    ⚠️ 原先这里是一条「序号必须相对 batch 而非 in_scope」的测试。
    改用摘录匹配后它**失去了区分力**：`in_scope` 是所有批次的并集，
    摘录匹配到哪条就是哪条，搜 `batch` 与搜 `in_scope` 结果必然相同。
    测试的前提没了就该删，而不是留着装作还在守什么。
    """
    from vigil.store import PendingMessage

    batch = [
        PendingMessage(
            msg_id=9, group_id=1, ts=1, sender_uid="u", sender="x",
            content="有人捡到校园卡和钥匙（640）吗",
        )
    ]
    # 全角 → 半角：容忍
    assert refine._match_source("有人捡到校园卡和钥匙(640)吗", batch) is not None
    # 标点被剥掉：容忍
    assert refine._match_source("有人捡到校园卡和钥匙", batch) is not None
    # 改写：不容忍
    assert refine._match_source("有人丢失了校园卡和钥匙", batch) is None
    # 太短：不匹配
    assert refine._match_source("校园卡", batch) is None


def test_refine_respects_budget(seeded, monkeypatch):
    """预算护栏必须在超限时停下并如实报告。

    batch_size=1 是必须的：护栏在**每批开始前**检查，样本只有 3 条消息，
    默认 batch_size=30 会全挤进一批，护栏根本没机会触发。
    """
    monkeypatch.setattr(
        refine, "chat_json",
        lambda cfg, *, system, user, sleep=None: LLMResult(
            payload={"items": []}, input_tokens=10_000, output_tokens=10_000
        ),
    )
    stats = refine.refine(
        StubConfig(), api_key="k", conn=seeded, budget_tokens=5_000, batch_size=1,
        on_progress=lambda *a, **k: None,
    )
    assert stats.budget_hit is True
    # ⚠️ 计划批数与**实际**批数必须分开断言（I-3 修复的连带改动）：
    # batch_size=1 → 计划 3 批，但第 2 批开始前的预算闸就 break 了，
    # 所以实际只跑了 1 批。原先断言 `stats.batches == 3` 是在断言**旧口径**
    # （把计划数当成已跑数），那正是 CLI 在预算截断时误报「317 批」的根因。
    assert stats.batches_planned == 3, "计划 3 批"
    assert stats.batches == 1, "预算闸在第 2 批前触发，实际只跑了 1 批"
    assert stats.batches < stats.batches_planned
    assert len(seeded.execute(
        "SELECT 1 FROM refine_runs WHERE status = 'ok'"
    ).fetchall()) < 3


def test_refine_survives_llm_error(seeded, monkeypatch):
    """单批失败不该让整轮崩掉——降级并记录，沿用 export.py 的既有取舍。"""
    from vigil.llm import LLMError

    def boom(cfg, *, system, user, sleep=None):
        raise LLMError("模拟失败")

    monkeypatch.setattr(refine, "chat_json", boom)
    stats = refine.refine(StubConfig(), api_key="k", conn=seeded,
                          on_progress=lambda *a, **k: None)
    assert stats.errors, "失败要如实记录"
    assert seeded.execute(
        "SELECT count(*) FROM refine_runs WHERE status = 'error'"
    ).fetchone()[0] > 0


def test_refine_rejects_redo_with_limit(seeded):
    """redo + limit 会重复产出 items 并白烧 token——必须快速失败挡住。

    W1 Task 4 审查实测：`pending_messages(redo=True, limit=2)` 返回 [1,2,3,4,5]
    里含已处理的；CLI 上 `--redo --limit N` 是个安静的烧钱陷阱。
    """
    with pytest.raises(ValueError, match="redo"):
        refine.refine(
            StubConfig(), api_key="k", conn=seeded, redo=True, limit=2,
            on_progress=lambda *a, **k: None,
        )


def test_dry_run_makes_no_calls(seeded, monkeypatch):
    """--dry-run 只报告不调模型、不写库。"""
    fake = FakeLLM()
    monkeypatch.setattr(refine, "chat_json", fake)
    stats = refine.refine(StubConfig(), api_key="k", conn=seeded, dry_run=True,
                          on_progress=lambda *a, **k: None)
    assert fake.calls == []
    assert stats.items_saved == 0
    assert seeded.execute("SELECT count(*) FROM refine_runs").fetchone()[0] == 0


# ── 截止日写入前核验（M3 Task 1）────────────────────────────
#
# ⚠️ 本文件原本**没有** `_msg` / `_extracted`。brief 已预告这一点，按本文件既有风格补：
# 造 `PendingMessage` 的既有做法是**就地构造**（见 test_match_source_tolerates_...），
# 这里只是把同一形状收成一个函数；conftest 里另有一个 `msg_factory` fixture，
# 但它造的是「任意内容」的通用消息，而这两条用例关心的是**正文本身**
# （核验就是拿正文去比日期），所以留了局部版本、签名也保持 brief 的 `_msg` 形状。


def _msg(msg_id: int, *, content: str) -> store.PendingMessage:
    """造一条源消息。核验只关心 msg_id 与正文，其余字段取固定值。"""
    return store.PendingMessage(
        msg_id=msg_id, group_id=100, ts=1000, sender_uid="u_a",
        sender="班长小王", content=content,
    )


def _extracted(
    *, deadline_ts: int | None, title: str = "数学作业截止"
) -> store.ExtractedItem:
    """造一条 ExtractedItem，字段形状与 `_to_item` 的产出一致，来源挂在 msg 1。"""
    return store.ExtractedItem(
        kind="academic", title=title, detail=None, event_ts=1000,
        deadline_ts=deadline_ts, group_id=100, actor_uid="u_a", place=None,
        links=(), amount=None, confidence=0.9, src_msg_ids=(1,),
    )


def test_refine_drops_deadline_without_literal_evidence():
    """源文写「明早」，模型却填了具体日期——这个截止日不许落库。

    真实数据里的原样：「明早7：20各班在宿舍楼下集合」被填成 09-15。
    """
    import datetime as dt

    ts = int(dt.datetime(2026, 9, 15, 12, 0).timestamp())
    batch = [_msg(1, content="各位小班：明早7：20各班在宿舍楼下集合")]
    item = _extracted(deadline_ts=ts)
    out, dropped, backdated = refine._verify_deadlines([item], batch)
    assert dropped == 1
    assert backdated == 0, "「源文里没依据」与「时序倒挂」是两个数，不许混在一起"
    assert out[0].deadline_ts is None
    assert out[0].title == item.title       # 其余字段一个都不许动


def test_refine_keeps_deadline_with_literal_evidence():
    import datetime as dt

    ts = int(dt.datetime(2026, 9, 16, 12, 0).timestamp())
    batch = [_msg(1, content="9月16日12:00开始报名缴费")]
    out, dropped, backdated = refine._verify_deadlines(
        [_extracted(deadline_ts=ts)], batch
    )
    assert dropped == 0
    assert backdated == 0
    assert out[0].deadline_ts == ts


def test_an_item_whose_source_is_not_in_the_batch_is_caught_by_the_first_gate():
    """⭐ M6 终审 #26：`_verify_deadlines` 里那句 `if srcs` 的**不可达性**是实测的。

    ⚠️ 现象（reviewer 记的）：`srcs` 取不到任何 ts 时，时序核验（第二道闸）会被
    **静默放弃**——一个 fail-open 的分支，"一旦可达就是零信号"。
    而它今天不可达，**理由必须被机械钉住、不能只是一句散文**：

      源消息不在本批里 ⇒ 第一道闸门的 `texts.get(mid, "")` 拿到**空串**
      ⇒ `deadline_supported(死线, "")` 为假 ⇒ 条目被记进 `unsupported`、
      死线被清 ⇒ 下面那条 `elif`（第二道闸门）**根本走不到**。

    ⇒ 本用例就是那个证明：源消息不在批里时，走的是 **`unsupported`**（1），
    不是 `backdated`，而且死线确实被清掉了（fail-**closed**，不是 fail-open）。

    ⚠️ 变异反证：把第一道闸门改成 `if it.deadline_ts and srcs and not
    deadline_supported(...)`（= 让"取不到源文"也跳过它）⇒ 本条**必红**
    （`unsupported` 变 0，而那条死线会活下来——一个源文未知却带死线的条目）。
    """
    import datetime as dt

    ts = int(dt.datetime(2026, 9, 16, 12, 0).timestamp())
    # 批里**没有** msg 1（item 声称的来源）
    batch = [_msg(2, content="9月16日12:00开始报名缴费")]
    out, unsupported, backdated = refine._verify_deadlines(
        [_extracted(deadline_ts=ts)], batch
    )
    assert (unsupported, backdated) == (1, 0), (
        "源消息不在本批时，第一道闸门**没有** fail-closed 地拦住它——"
        "那么下面那条 `if srcs` 就真的可达、真的会静默放弃时序核验"
    )
    assert out[0].deadline_ts is None


def test_parse_deadline_treats_out_of_range_dates_as_no_deadline():
    """⚠️ 「能解析成日期」与「能转成 unix 秒」是**两件事**——坏输入有两种形状。

    `_parse_deadline` 原先只捕 ValueError（挡"格式错"），而 `"1970-01-01"`
    是**合法日期**，却在 `timestamp()` 上抛 OSError（本机实测；Windows 拒绝
    1970-01-03 之前的日期）。而它偏偏是模型表达「没有截止日」最经典的哨兵
    ——最常见的那个值成了地雷。既有语义是"解析不了就当没有——**不猜**"，
    格式错与范围外在这条语义下没有区别，所以都返回 None。

    ⚠️ 这几格是**平台相关**的（POSIX 的 `timestamp()` 通常接受 1970 年）。
    本项目是 Windows 本地工具，按本机事实写；换平台时这条要重新裁决，
    别把它的绿当成跨平台的绿。
    """
    # 本体：无论"吞掉异常"还是"本来就转得成"，**都不许抛**出去——
    # 抛出去就是「一条 item 掀掉整轮」的起点（见下面两条端到端用例）
    for value in ("1970-01-01", "1970-01-02", "4000-01-01", "9999-12-30"):
        assert refine._parse_deadline(value) is None, value
    assert refine._parse_deadline("不是日期") is None   # 原有的格式错照旧
    assert refine._parse_deadline("") is None
    assert refine._parse_deadline(None) is None
    # 阳性对照：窗口内的正常日期照常解析——上面那些 None 不是"整个函数瘫了"
    import datetime as dt

    assert refine._parse_deadline("2026-09-07") == int(
        dt.datetime(2026, 9, 7).timestamp()
    )


# ── B-1：一个模型输出不许掀掉整轮 refine ────────────────────
#
# ⚠️ 下面两条端到端用例的分工，读之前先看明白（brief 的原始配方在这里做了
# 一处必要的拆分，理由写进报告）：
#   · `..._survives_the_1970_sentinel_...` 就是 brief 的配方本身：模型吐哨兵
#     日期 1970-01-01，整轮必须跑完。它钉的是修法**第一步**的语义（范围外的
#     日期 = 没有截止日），把 `_parse_deadline` 的捕获退回 ValueError 它就变红。
#   · `..._keeps_going_when_a_batch_postprocess_fails` 注入一个**真正的**本地
#     后处理异常（落库失败），钉的是修法**第二步**。把保护圈退回只捕 LLMError
#     它就变红——终审点名的性质（「单批失败不中断整轮」）由它验收。
# 两条合起来才盖住「M1 留的洞」与「M3 塞进去的新调用点」两个半边。


def _scripted(user: str, *, deadline: str | None) -> dict:
    """按批次内容产出条目——3 条种子消息 → batch_size=1 时正好 3 批。"""
    if "明天下午有讲座" in user:
        return {
            "items": [
                {"quote": "明天下午有讲座", "kind": "activity",
                 "title": "西太湖报告厅有讲座", "detail": None, "deadline": None,
                 "place": None, "amount": None, "confidence": 0.9}
            ]
        }
    if "数学作业截止到9月7号" in user:
        return {
            "items": [
                {"quote": "数学作业截止到9月7号", "kind": "academic",
                 "title": "数学作业截止", "detail": None, "deadline": deadline,
                 "place": None, "amount": None, "confidence": 0.9}
            ]
        }
    return {"items": []}


def test_refine_survives_the_1970_sentinel_end_to_end(seeded, monkeypatch):
    """⚠️ `"1970-01-01"` 是模型表达「没有截止日」最经典的哨兵，而它是**合法
    日期**——`strptime` 过了，`timestamp()` 抛 OSError。它从 `_to_item` 抛出，
    而 `_to_item` 在每批 try/except 的**外面**，于是整轮 refine 终止：
    前面跑完的批次白跑，剩下的批次永远不跑。

    这条从模型输出一路走到 items 表，断的是**整轮跑完**，
    不是"`_parse_deadline` 不抛"（那只是单元级、证明不了什么）。
    """
    fake = FakeLLM(lambda user: _scripted(user, deadline="1970-01-01"))
    monkeypatch.setattr(refine, "chat_json", fake)

    stats = refine.refine(
        StubConfig(), api_key="k", conn=seeded, batch_size=1,
        on_progress=lambda *a, **k: None,
    )

    assert stats.batches == 3, "3 批必须全部跑完（被掀掉的话停在 1）"
    assert stats.errors == [], "哨兵日期是「没有截止日」，不是错误"
    # 两条条目都落库：哨兵那条的 deadline_ts 是 NULL（"没有截止日"），
    # 其余字段照常——降级的是日期，不是条目本身
    assert dict(seeded.execute("SELECT title, deadline_ts FROM items").fetchall()) == {
        "西太湖报告厅有讲座": None, "数学作业截止": None,
    }
    assert stats.items_saved == 2
    assert stats.deadlines_dropped == 0, "哨兵不是「源文无依据」，不该计入降级"
    assert seeded.execute(
        "SELECT count(*) FROM refine_runs WHERE status = 'error'"
    ).fetchone()[0] == 0


def test_refine_keeps_going_when_a_batch_postprocess_fails(seeded, monkeypatch):
    """⭐ 单批的**本地后处理**失败，整轮不被打断。

    保护圈原先只圈住 `chat_json` 那一句（M1 的形状：只对**网络**失败成立），
    解析/核验/落库/记账全在圈外——`store.save_items(...)` 那个 sqlite3.Error
    正是 brief 亲自点名的一半。这条注入一个真的本地失败，然后断言：

      · 3 批全部跑完（被掀掉的话 `stats.batches == 1`）
      · 失败那批进 `stats.errors`、进 refine_runs 的 error 行（**可见**，
        不是静默吞异常——静默吞异常是不可接受的）
      · 别的批次的 item **真的落库了**（"继续跑"必须有产出）
    """
    fake = FakeLLM(lambda user: _scripted(user, deadline=None))
    monkeypatch.setattr(refine, "chat_json", fake)

    real_save = store.save_items

    def flaky_save(conn, items, **kwargs):
        if any(it.title == "西太湖报告厅有讲座" for it in items):
            raise sqlite3.OperationalError("模拟落库失败")
        return real_save(conn, items, **kwargs)

    monkeypatch.setattr(refine.store, "save_items", flaky_save)

    stats = refine.refine(
        StubConfig(), api_key="k", conn=seeded, batch_size=1,
        on_progress=lambda *a, **k: None,
    )

    assert stats.batches == 3, "3 批必须全部跑完（被掀掉的话停在 1）"
    assert any("第 1 批" in e for e in stats.errors), stats.errors
    assert seeded.execute(
        "SELECT count(*) FROM refine_runs WHERE status = 'error' AND msg_id = 1"
    ).fetchone()[0] == 1, "失败要记进 refine_runs，不能只写在 stats 里"
    assert seeded.execute("SELECT title FROM items").fetchall() == [
        ("数学作业截止",)
    ], "后面那批的条目照常落库——「继续跑」得是有产出的"
    assert stats.items_saved == 1


def test_refine_wires_the_deadline_drop_end_to_end(seeded, monkeypatch):
    """接线也要守——**brief 的两条用例直接调私有函数，一条都没经过 `refine()`**。

    那正是「空守卫」的形状：函数本身对，但批次循环里没人调它、或调了却忘了把计数
    累加进 stats，上面两条照样全绿，而库里仍然躺着模型编出来的日期。所以这条从模型
    输出一路走到 items 表。
    """
    fake = FakeLLM(
        lambda user: {
            "items": [
                {"quote": "数学作业截止到9月7号", "kind": "academic",
                 "title": "数学作业截止",
                 "detail": None, "deadline": "2026-09-10", "place": None,
                 "amount": None, "confidence": 0.9}
            ]
        }
    )
    monkeypatch.setattr(refine, "chat_json", fake)

    stats = refine.refine(StubConfig(), api_key="k", conn=seeded,
                          on_progress=lambda *a, **k: None)

    # 源文只有「9月7号」，模型填的 09-10 在源文里没有依据 → 只降级那一个字段
    assert stats.deadlines_dropped == 1, "降级计数必须流到 stats（CLI 靠它报警）"
    assert stats.items_saved == 1, "条目本身照常落库，降级的只是日期"
    assert seeded.execute(
        "SELECT title, deadline_ts FROM items"
    ).fetchone() == ("数学作业截止", None)
    assert seeded.execute(
        "SELECT count(*) FROM item_sources"
    ).fetchone()[0] == 1, "来源行不受影响"


# ── M4 T5：批次写入原子化 + record_run 保护圈 + M2-3 ────────────────


def test_batch_write_is_atomic_items_do_not_survive_a_failed_record(seeded, monkeypatch):
    """⭐⭐ 洞 B 的判据：**items 落库与消息记账要么都成、要么都不成。**

    注入一个"记账那一步炸掉"的失败。旧实现里 `save_items` 已经自己 commit
    过了，于是这一批的 items **留在库里**而消息**没被标记**——下次
    `pending_messages` 会把同一批再抽一遍，产出重复 items，全程无声。
    修好后：两者在同一个事务里，记账失败 ⇒ items 一并回滚。

    断言三件事：
      · 那一批的 items **不在**库里（回滚了）
      · 那一批的消息**没有**被标成 ok/discarded（否则下次就不再抽它了，
        那才是真丢数据）
      · 整轮没有被掀掉（别的批照常产出）——保护圈仍然有效
    """
    fake = FakeLLM(lambda user: _scripted(user, deadline=None))
    monkeypatch.setattr(refine, "chat_json", fake)

    real_record = store.record_run
    state = {"blown": False}

    def flaky_record(conn, msg_ids, **kwargs):
        # 只炸**成功路径**的那次（status=ok），不炸 error 记账——
        # 炸后者会连带把 _record_error 也弄坏，那就测不出想测的东西了
        if not state["blown"] and kwargs.get("status") == store.STATUS_OK:
            state["blown"] = True
            raise sqlite3.OperationalError("模拟记账失败")
        return real_record(conn, msg_ids, **kwargs)

    monkeypatch.setattr(refine.store, "record_run", flaky_record)

    stats = refine.refine(
        StubConfig(), api_key="k", conn=seeded, batch_size=1,
        on_progress=lambda *a, **k: None,
    )

    assert state["blown"], "注入没生效——测试本身有问题，别当成通过"
    assert stats.batches == 3, "3 批必须全部跑完"
    assert any("第 1 批" in e for e in stats.errors), stats.errors
    # 第 1 批那 3 条消息（scripts 里第 1 条）不许留下 ok/discarded 记账——
    # 留下了就意味着这条消息永远不会再被抽，而它的 item 已经回滚没了
    assert seeded.execute(
        "SELECT count(*) FROM refine_runs WHERE msg_id = 1 AND status != 'error'"
    ).fetchone()[0] == 0, "回滚要连记账一起回滚"
    # ⚠️ 补强（逃逸舱，见报告「偏差」）：brief 原版只断言 `count(*) >= 1`，
    # 而**旧实现照样满足它**——第 1 批的 item 还在库里、第 3 批的也在，
    # `>= 1` 分不清「回滚了」与「没回滚」。洞 B 的判据必须点名**那一批**的
    # item：它要么跟着记账一起消失，要么就是那个会引来重抽的重复条目。
    assert seeded.execute(
        "SELECT count(*) FROM items WHERE title = '西太湖报告厅有讲座'"
    ).fetchone()[0] == 0, "记账失败的批次，它的 items 必须一并回滚"
    assert seeded.execute("SELECT count(*) FROM items").fetchone()[0] >= 1, \
        "后面那批的条目照常落库——「继续跑」得是有产出的"
    # ⚠️ 补强（逃逸舱，见报告「偏差」）：**「落了盘」与「还挂在连接的事务里」
    # 在同一连接上看起来一模一样**——本文件其余 refine 用例全都在同一条连接上
    # 读，把 `with store.transaction(conn)` 整块删掉（只留 `commit=False`）
    # 它们**照样全绿**（实测，报告里的 M2）。而真管线用的是**文件库**、
    # `finally: conn.close()`：真没 commit 的话，`close()` 会把这一轮的产出
    # 整个回滚掉——无人值守下就是「跑完了、库里什么都没有」。
    # 所以这里钉一条「连接上不许留着没提交的事务」：它与 items 是否可见无关，
    # 是唯一能区分「已提交」与「pending」的判据。
    assert not seeded.in_transaction, "一轮跑完不许留着未提交的事务"


def test_record_error_failure_does_not_kill_the_run(seeded, monkeypatch):
    """⭐ M3-2：**记账自己也失败时，不许掀掉整轮。**

    三处 `record_run` 原先都裸奔在 `except` 体里。异常处理路径上再抛异常，
    `except` 接不住自己。这里让 `record_run` **永远**炸——模拟"库锁死/只读/
    磁盘满"，那正是最可能连续失败的时候。

    断言：整轮跑完、错误**仍然可见**（进 stats.errors），不是静默吞掉。
    """
    fake = FakeLLM(lambda user: _scripted(user, deadline=None))
    monkeypatch.setattr(refine, "chat_json", fake)

    def always_boom(conn, msg_ids, **kwargs):
        raise sqlite3.OperationalError("模拟库一直写不进去")

    monkeypatch.setattr(refine.store, "record_run", always_boom)

    stats = refine.refine(
        StubConfig(), api_key="k", conn=seeded, batch_size=1,
        on_progress=lambda *a, **k: None,
    )

    assert stats.batches == 3, "记账全炸也不许掀掉整轮"
    assert any("记账" in e for e in stats.errors), \
        f"记账失败必须可见（进 stats.errors），实际：{stats.errors}"


def test_prompt_sanitizes_fullwidth_quotes(seeded, monkeypatch):
    """M2-3 + 发现 9：出网文本里不许有全角引号。

    两重收益：① 防模型退化成无限空格循环（原先的理由）；② `“”` 换成
    `「」` 之后，refine 的归一化表（`_PUNCT`）**认得** `「」` 而不认得 `“”`
    ——所以模型摘录时引号包一层也不会再丢掉来源（发现 9 实测的那个洞）。

    ⚠️ 偏差（逃逸舱）：brief 里这条用的是 `_seeded_with(msg)` 这个**并不存在**
    的夹具。这里改成**用既有的 `seeded` 夹具**、往它里面补一条带全角引号的
    消息——batch_size=1 时每条消息自成一批，最后一批就是这条，断言落在
    最后一次出网的 `user` 上。没有新造/改写任何既有夹具。
    """
    sent = {}
    fake = FakeLLM()

    def capture(cfg, *, system, user, sleep=None):
        sent["user"] = user
        return fake(cfg, system=system, user=user)

    monkeypatch.setattr(refine, "chat_json", capture)

    # ts 取最大，保证它是**最后**一批（pending_messages 按时间升序）
    seeded.execute(
        "INSERT INTO messages VALUES (?,?,?,?,?)",
        (4, 100, 1003, "u_x", "他说“明天交作业”了"),
    )
    seeded.commit()

    refine.refine(StubConfig(), api_key="k", conn=seeded, batch_size=1,
                  on_progress=lambda *a, **k: None)

    assert sent, "一批都没跑，这条测试就没在验证任何东西"
    assert "“" not in sent["user"] and "”" not in sent["user"], \
        "全角引号必须被 sanitize_for_llm 换掉"
    assert "「明天交作业」" in sent["user"]


def test_quote_with_fullwidth_quotes_still_matches_its_source():
    """⭐ 源文**自己**含全角引号时，摘录整句仍要匹配回来源。

    这是 M2-3 修复**引入的回归**的判据：`sanitize_for_llm` 把 `“”` 换成 `「」`
    送出去，模型照抄回来的是 `「」`，而源文里躺着 `“”`。只剥 `「」` 不剥 `“”`
    的话两侧归一化不对称，**整句摘录的子串对不上 ⇒ 那条 item 丢掉来源**。
    修之前这些消息是匹配得上的（两侧都是 `“”`），所以这是净回归。
    实测全库 ~25/47,719 命中。

    ⚠️ 修法是让 `refine._PUNCT` 与 `digest._PUNCT` **逐字一致**（那边早就有
    这组引号，理由与这边同源：一侧出网时换过字符、另一侧没有）。
    """
    from vigil.store import PendingMessage

    batch = [
        PendingMessage(msg_id=9, group_id=1, ts=1, sender_uid="u", sender="x",
                       content="他说“明天交作业”了"),
    ]

    # ① 模型按**净化后**的提示词逐字摘录（提示词里是 「」）→ 必须命中（回归本体）
    assert refine._match_source("他说「明天交作业」了", batch) is not None
    # ② 模型照抄库内原文（“”）→ 也要命中：两个方向靠同一张表兜住
    assert refine._match_source("他说“明天交作业”了", batch) is not None
    # ③ 反面：不相干的摘录照样不命中——剥引号不许把「不同的句子」也放进来
    assert refine._match_source("他说「后天交作业」了", batch) is None
    assert refine._match_source("他说明天交体育作业了", batch) is None


def test_stripping_quote_characters_does_not_manufacture_wild_attribution():
    """反向阳性对照：多剥一组引号，会不会**制造假匹配 / 误归属**？

    ⚠️ 这是 `_PUNCT` 加 `“”`、`"` 之后必须回答的问题（比丢来源更糟的那种错）。
    分三层钉：

    ① **不许把只差一个字的句子混起来**（真正的鉴别力还在）：两条消息
       「明天交作业」/「后天交作业」只差一个字，摘录必须落回**对的那条**。
       剥引号若伤到正文，两条会归一化成同一个串，先到先得就会误归属。
    ② **不许跨到真正不同的消息上**：批里的无关消息永远不许被认成来源。
    ③ **同文异引号之间确实是歧义**，且这是**既有的**、不是本次改动引入的：
       实测（改 `_PUNCT` 之前跑同一份探针）「A 的净化形摘录」就已经会落到 B 上
       ——因为 `sanitize_for_llm` 抹掉的那两个字符，模型**本来就没看见**，
       两条消息对模型本来就不可区分。改后修复的方向（净化形摘录）反而开始
       落回真来源 A。所以这里只断言「返回的那条在文字上支持这条摘录」，
       不假装它总能挑对双胞胎。
    """
    from vigil.store import PendingMessage

    def m(mid: int, content: str) -> store.PendingMessage:
        return PendingMessage(msg_id=mid, group_id=1, ts=mid, sender_uid="u",
                              sender="x", content=content)

    # ① 只差一个字：剥引号之后仍必须分得开
    tomorrow = m(1, "他说“明天交作业”了")
    dayafter = m(2, "他说“后天交作业”了")
    assert refine._match_source(
        "他说「后天交作业」了", [tomorrow, dayafter]
    ).msg_id == 2, "剥引号不许把「明天/后天」也归一化掉"
    assert refine._match_source(
        "他说「明天交作业」了", [tomorrow, dayafter]
    ).msg_id == 1

    # ② 真正不同的消息绝不能被认成来源
    unrelated = m(3, "数学作业截止到9月7号")
    assert refine._match_source(
        "数学作业截止到9月7号", [tomorrow, dayafter, unrelated]
    ).msg_id == 3
    assert refine._match_source("他说「明天交作业」了", [unrelated]) is None

    # ③ 双胞胎（同文异引号）：返回的那条必须在**文字上支持**这条摘录
    twin_raw = m(1, "他说“明天交作业”了")
    twin_plain = m(2, "他说明天交作业了")
    for quote in ("他说「明天交作业」了", "他说“明天交作业”了", "他说明天交作业了"):
        src = refine._match_source(quote, [twin_raw, twin_plain])
        assert src is not None, quote
        assert refine._normalize(quote) in refine._normalize(src.content), (
            f"来源不支持这条摘录：{quote!r} -> {src.content!r}"
        )

    # ④ **表格的边界：只许剥标点/空白，不许碰正文。**
    #    ⚠️ 先把一条**错误论证**钉在这里（独立审查推翻、本机复现）：
    #    「两侧对称剥离，所以不会误归属」是**假的**——两侧同时剥字符，**仍然**
    #    会让「只差被剥字符」的两条消息互相顶替，摘录挂到另一个人头上。
    #    反例（③ 的双胞胎就是同一形状）：批序 [msg2 李四…, msg1 小王…]，
    #    模型照抄 msg1 的净化形 ⇒ 改前 None（fail-safe 丢来源）、**改后 → msg2**。
    #    让本次改动站得住的是**实测**：全库 47,719 条按群、按 message-id 簇比较，
    #    新增歧义簇 = 0（口径见 refine.py 的 `_PUNCT` 注释）。
    #    ⇒ 危险的是**剥到正文**：那才会把内容不同的句子变成同一条。
    #    这条直接钉住表格边界——往 `_PUNCT` 里加任何正文汉字（哪怕只加一个「天」）
    #    都会立刻变红。⚠️ 它同时是反向对照的牙齿：①②③ 单凭自身抓不住
    #    「表格被写宽了」（实测：只加「天」时 ①②③ 全绿）。
    assert refine._normalize("明天交作业") == "明天交作业", \
        "_PUNCT 只许剥标点/空白：剥到正文就会把不同的句子变成同一条"
    assert refine._normalize('他说“明天交作业”了') == "他说明天交作业了", \
        "全角引号（本次新增的那组）必须被剥掉"
    assert refine._normalize("　（640）\t") == "640", "标点/空白照旧剥掉（既有语义不变）"


def test_progress_failure_after_commit_does_not_poison_the_ledger(seeded, monkeypatch):
    """⭐⭐ 事务**提交之后**报进度失败，**不许**把已成功的批次改记成 error。

    旧形状：成功话术的 `on_progress` 在 `try` **里面** ⇒ 它抛（写日志失败）
    会被下面那个 `except Exception` 接住 ⇒ `_record_error` 对**整批** `batch_ids`
    做 `INSERT OR REPLACE` 记 `error` ⇒ **把刚提交的 `ok` 覆盖掉**（`hit_ids ⊆
    batch_ids`，所以一定被覆盖）⇒ 下轮 `pending_messages` 把那些消息**重新取出来**
    ⇒ **重复 items 且无声**。

    ⇒ 它是 M4 要消灭的形状，只在「事务已提交、报进度失败」这个**夹缝**里触发：
    提交之前炸会被回滚（安全），提交之后炸才会毒化账目。

    修法：成功话术必须放在 `try/except` **之外**——报告类失败要**响**（冒出去），
    不许被那个 `except Exception` 接住。
    """
    fake = FakeLLM(lambda user: _scripted(user, deadline=None))
    monkeypatch.setattr(refine, "chat_json", fake)

    def flaky_progress(msg, *a, **kw):
        # 只炸**成功话术**那一条（`批次 N/M`）；错误话术（`第 N 批…失败`）不炸——
        # 炸后者会把 `_record_error` 那条路径也弄坏，就测不出想测的东西了
        if "批次" in msg:
            raise RuntimeError("模拟写日志失败")
        return None

    # ⚠️ 不用 `pytest.raises` 包住整个调用：那样旧代码下会红在「有没有抛」，
    # 而本条要钉的是**账目有没有被毒化**——账目断言必须排在**最前面**，
    # 否则「红在正确的那条断言上」这件事就没被证明。
    raised: RuntimeError | None = None
    try:
        refine.refine(StubConfig(), api_key="k", conn=seeded, batch_size=1,
                      on_progress=flaky_progress)
    except RuntimeError as exc:
        raised = exc

    # ① 已提交的那批，账目必须还是 ok——**这条就是本修复的全部意义**
    assert seeded.execute(
        "SELECT count(*) FROM refine_runs WHERE msg_id = 1 AND status = 'ok'"
    ).fetchone()[0] == 1, "提交成功的批次被 _record_error 改记成 error 了"
    # ② 它**不许**再被当成待处理：重抽 = 重复 items（洞 B 的同一个形状）
    assert 1 not in [m.msg_id for m in store.pending_messages(seeded)], \
        "被改记成 error 的消息下轮会被重抽 ⇒ 重复 items 且无声"
    # ③ 条目数不许因这次失败变多：只有第 1 批提交过，items 就只有它那一条
    assert seeded.execute("SELECT count(*) FROM items").fetchone()[0] == 1
    # ④ 报告类失败要**响**（计划 §8.5）：它必须冒出去，不许被批次循环的
    #    `except Exception` 吞掉——吞掉的话整轮会带着被毒化的账目继续跑。
    assert raised is not None, "报进度失败必须冒出去"


def test_punct_tables_stay_identical():
    """⭐ 两张 `_PUNCT` 必须逐字一致——一致性只有注释在承重时不叫契约。

    它们必须相同的理由见 `refine.py` 里那段注释：`sanitize_for_llm` 把 `“”` 换成
    `「」` 送出去、模型照抄回来，而**源文里是 `“”`**；两侧字符集不一致就会
    一边剥一边不剥 ⇒ **丢来源或误归属**（实测 ~25/47,719）。
    ⇒ 日后扩大这个字符集，**必须两处同改，且必须重跑包含关系的测量**（见 refine.py 注释）。
    """
    from vigil import digest, refine
    assert refine._PUNCT.pattern == digest._PUNCT.pattern


# ── Task 1：prompt 版本的回归锁 ──────────────────────────────────


def test_prompt_version_is_v4():
    """版本号必须跟着提示词一起走。

    ⚠️ 这个断言是**回归锁**，不是行为测试——它只证明"文案改了、版本也改了"。
    真正的行为测试在 M5 出口判据 2（5 条已知好条目重抽后必须仍在），
    以及 `tests/test_repass.py` 的误杀守卫。**别把它当成"规则生效了"的证据。**

    ⚠️ **v3 → v4 的原因**（M7）：提示词新增了 `start`（活动开始时刻）字段，
    模型看到的要求变了 ⇒ 产出与 v3 **不可比**。锁在这里，是为了让下次改
    提示词的人**必须显式面对**这件事（改一个字就该动这个数），
    而不是让两版数据混在同一个 `prompt_ver` 下、事后分不清。
    """
    assert refine.PROMPT_VERSION == "v4"


def test_prompt_asks_for_start_with_its_own_time_precision():
    """⭐ 提示词里必须有 `start`，且**明确到分钟**。

    ⚠️ 只断言"有 start 这个词"是不够的：写成 `"start": "YYYY-MM-DD"`
    也能过。判据要钉住**精度**（HH:MM）—— 那是这个字段存在的全部意义
    （死线只到日，活动到分钟，两者精度不同）。
    """
    from vigil.categories import load_categories

    prompt = refine.build_system_prompt(load_categories())
    assert '"start"' in prompt, "提示词必须要求 start 字段"
    assert "YYYY-MM-DD HH:MM" in prompt, "start 必须要求到分钟（这是它与 deadline 的区别）"
    # ⚠️ 还要说明"没写时刻就填 null"—— 否则模型会硬凑一个
    assert "null" in prompt


def test_system_prompt_discards_commercial_promo():
    """prompt 里必须有商业推广的丢弃规则，且必须写明「看目的不看词」。"""
    from vigil.categories import load_categories

    prompt = refine.build_system_prompt(load_categories())
    assert "商业推广" in prompt
    # 反例必须出现在 prompt 里：它们正是 D13 判据的由来（实测误杀样本）
    assert "打电话办卡的都别信" in prompt
    assert "最早9.5" in prompt


# ── Task 2：两道闸门接进 _to_item ─────────────────────────────────


def test_to_item_drops_place_without_evidence(msg_factory):
    """源文里没有地点 ⇒ place 降级为 None，**条目本身保留**（与 deadline 同一取舍）。"""
    batch = [
        msg_factory(
            1,
            "大概这周会有面试 到时候具体时间通知大家",
            uid="u_1",
        )
    ]
    it = refine._to_item(
        {"quote": "到时候具体时间通知大家", "kind": "notice",
         "title": "面试通知", "place": "立德楼1阶", "confidence": 0.9},
        batch,
        known_kinds=frozenset({"notice"}),
    )
    assert it is not None          # ⚠️ 条目必须还在
    assert it.place is None        # ⚠️ 只有 place 被降级


def test_to_item_keeps_place_with_evidence(msg_factory):
    batch = [msg_factory(1, "西太湖连隔板", uid="u_1")]
    it = refine._to_item(
        {"quote": "西太湖连隔板", "kind": "notice", "title": "澡堂隔板",
         "place": "西太湖校区", "confidence": 0.9},
        batch,
        known_kinds=frozenset({"notice"}),
    )
    assert it.place == "西太湖校区"


def test_to_item_no_longer_applies_the_timing_gate(msg_factory):
    """⭐ 时序闸门**不在** `_to_item` 里了（M6 T10 / M5 遗留 #4 的第二半）。

    原来是 `test_to_item_drops_past_deadline`（item 83 的形状：死线早于消息日）。
    T10 把闸门挪去了写入边界的 `_verify_deadlines`——**唯一**的理由是那里有
    `RefineStats` 的两个计数器，而留在 `_to_item` 里时它清掉的值**没有任何
    计数器**（`deadlines_dropped` 只数 `deadline_supported` 那道闸），
    后果是「一批 12 条过期死线被全清」在运维侧**零告警**。

    ⇒ 所以本条断的是「日期**留着**」。谁把闸门挪回 `_to_item`（不删、也不计数），
    **本条立刻变红**——而那正是本任务要挡的形状：条目结果完全一样，
    只有账目变了，没有任何用户可见的差异（brief 的 Step 2 ⚠️ 原话）。
    """
    # ⚠️ ts 是本机实算值（1785902400 = 2026-08-05 12:00 本地时，见报告「偏差」）
    batch = [msg_factory(1, "档案袋封口时间：5 月 6 日", ts=1785902400, uid="u_1")]
    it = refine._to_item(
        {"quote": "档案袋封口时间：5 月 6 日", "kind": "notice",
         "title": "档案袋封口", "deadline": "2026-05-06", "confidence": 0.9},
        batch,
        known_kinds=frozenset({"notice"}),
    )
    assert it is not None
    assert it.deadline_ts is not None, (
        "时序闸门被挪回了 `_to_item`：它清掉的值没有任何计数器（M5 遗留 #4），"
        "「一批 12 条过期死线被全清」会重新变成零告警"
    )


def test_to_item_keeps_future_deadline(msg_factory):
    # ⚠️ ts 是本机实算值（1789531200 = 2026-09-16 12:00 本地时，见报告「偏差」）
    batch = [msg_factory(1, "9月20日前交表", ts=1789531200, uid="u_1")]
    it = refine._to_item(
        {"quote": "9月20日前交表", "kind": "notice", "title": "交表",
         "deadline": "2026-09-20", "confidence": 0.9},
        batch,
        known_kinds=frozenset({"notice"}),
    )
    assert it.deadline_ts is not None


def test_to_item_place_evidence_must_come_from_the_source_message(msg_factory):
    """⚠️ 补的守卫（偏差，见报告「偏差」第 8 条）：place 的依据**只许来自那一条**源消息。

    brief 的 Step 7 专门警告过「**不要**把 `sources_text` 写成整个 batch 的正文拼接……
    跨消息拼接凭空造出证据」，但它给的 4 条接线用例**全是单消息批次**——
    实测把 `sources_text` 改成整批正文之后**全量 439 条一条都不红**（变异 M7）。
    即：这条警告当前**零守护**，正是 M4「记下来了 ≠ 派下去了」的同族。

    这条用例是唯一能把两种写法分开的形状：摘录命中 msg 1，而 place 只出现在 msg 2。
      · 依据只取源消息（正确）→ msg 1 里没有「立德楼1阶」的任何 2 字子串 → 清掉
      · 依据取整批（错误）→ msg 2 的正文给了它背书 → 保留（本用例变红）
    """
    batch = [
        msg_factory(1, "西太湖连隔板", uid="u_1"),
        msg_factory(2, "公告：立德楼1阶今天停电", uid="u_2"),
    ]
    it = refine._to_item(
        {"quote": "西太湖连隔板", "kind": "notice", "title": "澡堂隔板",
         "place": "立德楼1阶", "confidence": 0.9},
        batch,
        known_kinds=frozenset({"notice"}),
    )
    assert it is not None
    assert it.place is None, "依据只许来自源消息；跨消息拼接会凭空造出证据"
# ── Task 6：墓碑阻复活（D15） ─────────────────────────────────────


def test_refine_skips_tombstoned_messages_on_redo(seeded, monkeypatch):
    """被软删条目的源消息，`--redo` 时不得再被抽取（D15）。

    用 `seeded` 夹具里既有的 msg_id=3（"数学作业截止到9月7号"）。
    """
    from vigil import overrides

    overrides.ensure_schema()
    ov = overrides.connect()
    overrides.ensure_tables(ov)
    overrides.delete_item(ov, item_id=1, msg_id=3)
    ov.close()

    fake = FakeLLM(script=lambda _user: {"items": [
        {"quote": "数学作业截止到9月7号", "kind": "academic",
         "title": "数学作业截止", "deadline": "2026-09-07", "confidence": 0.9}
    ]})
    monkeypatch.setattr(refine, "chat_json", fake)

    stats = refine.refine(
        StubConfig(), api_key="k", conn=seeded, batch_size=1, redo=True,
        on_progress=lambda *_: None,
    )
    assert stats.skipped_deleted == 1
    sent = " ".join(c["user"] for c in fake.calls)
    assert "数学作业" not in sent, "⚠️ 被墓碑挡下的消息**一步都不许出网**"


def test_refine_skips_deleted_counts_zero_without_tombstones(seeded, monkeypatch):
    """没有墓碑时该字段必须是 0——不能因为字段存在就恒为 1（否则判据是装饰）。"""
    fake = FakeLLM(script=lambda _user: {"items": []})
    monkeypatch.setattr(refine, "chat_json", fake)

    stats = refine.refine(
        StubConfig(), api_key="k", conn=seeded, batch_size=1, redo=True,
        on_progress=lambda *_: None,
    )
    assert stats.skipped_deleted == 0


def test_refine_survives_soft_delete_without_msg_id(seeded, monkeypatch):
    """⚠️ 补的守卫（偏差，见报告「偏差」第 1 条）：`msg_id=None` 的软删不许让 refine 崩。

    `overrides.delete_item` 明确允许不传 `msg_id`（D2/T4 的既有事实：界面仍然
    会隐藏这条 item，只是那条源消息将来 `--redo` 时**会**复活），而 T6 起
    **每一轮** refine 都会去读墓碑。⇒ 这条路径必须在「没有源消息可墓碑化」时
    安静地什么都不做，而不是抛异常把整条每日管线停摆。

    三条一起才完整：①有名有姓的墓碑要挡（上一条）、②没有墓碑时字段是 0
    （再上一条）、③**有墓碑但没有 msg_id** 时既不崩也不误伤。
    第 ③ 条此前零守护——brief 的两条用例都只走 `msg_id=3` 这条路。
    """
    from vigil import overrides

    overrides.ensure_schema()
    ov = overrides.connect()
    overrides.delete_item(ov, item_id=1, msg_id=None)
    ov.close()

    fake = FakeLLM(script=lambda _user: {"items": []})
    monkeypatch.setattr(refine, "chat_json", fake)

    stats = refine.refine(
        StubConfig(), api_key="k", conn=seeded, batch_size=1, redo=True,
        on_progress=lambda *_: None,
    )
    assert stats.skipped_deleted == 0, "没有 msg_id ⇒ 没有源消息可墓碑化，不许误伤"
    assert stats.batches == 3, "3 批照常跑完——无 msg_id 的软删不该挡住任何消息"
    assert stats.errors == [], f"读墓碑不该抛：{stats.errors}"
def test_refine_tombstones_survive_a_full_state_rebuild(seeded, monkeypatch):
    """⚠️ 补的守卫（偏差，见报告「偏差」第 2 条）：墓碑不许只活在物化的 `item_state` 里。

    `deleted_msg_ids` 是**读 `item_state`** 的（`item_edits JOIN item_state`），而
    `ensure_schema` 只建表、**不重建**它——`rebuild_state()` 只在 `undo` 里被调用。
    ⇒ 这张物化表一旦被整体重算，墓碑集必须**一个不差**；否则它会静默变小
    ⇒ 重抽把用户删掉的条目复活，而**没有任何东西会报错**。
    T6 是 `deleted_msg_ids` 的第一个消费者，所以由消费者这一侧把它钉住。
    """
    from vigil import overrides

    overrides.ensure_schema()
    ov = overrides.connect()
    overrides.delete_item(ov, item_id=1, msg_id=3)
    overrides.rebuild_state(ov)
    assert overrides.deleted_msg_ids(ov) == frozenset({3}), \
        "全量重算之后墓碑集必须一字不差——它是重抽跳过的唯一依据"
    ov.close()

    fake = FakeLLM(script=lambda _user: {"items": [
        {"quote": "数学作业截止到9月7号", "kind": "academic",
         "title": "数学作业截止", "deadline": "2026-09-07", "confidence": 0.9}
    ]})
    monkeypatch.setattr(refine, "chat_json", fake)

    stats = refine.refine(
        StubConfig(), api_key="k", conn=seeded, batch_size=1, redo=True,
        on_progress=lambda *_: None,
    )
    assert stats.skipped_deleted == 1
    sent = " ".join(c["user"] for c in fake.calls)
    assert "数学作业" not in sent


def test_refine_skips_tombstoned_even_without_redo(seeded, monkeypatch):
    """⚠️ 补的守卫（偏差，见报告「偏差」第 3 条）：墓碑过滤不许被收窄到 `redo` 分支上。

    brief 的注释说「非 redo 时……被软删的条目本来就抽过（有 ok 记录）⇒ 本来就不在
    集合里」——**这句话有个反例**：`record_run` 按 `msg_id` 做 `INSERT OR REPLACE`，
    所以「抽过（ok + item）→ 用户软删 → 后来某轮 `--redo` 失败」会把那条记账
    **从 ok 覆盖成 error**，而 `pending_messages` 的既有语义正是「error 行要重试、
    不算已处理」⇒ **下一轮非 redo 的 refine 照样会把这条消息取出来重抽**
    ⇒ 用户删掉的条目以新的 `item_id` 复活（overlay 挡的是旧 `item_id`）。

    即：墓碑过滤是**全部路径**上的闸门，不是 redo 专用的。把它写成
    `if redo and tombstones:` 时，本条变红。
    """
    from vigil import overrides

    overrides.ensure_schema()
    ov = overrides.connect()
    overrides.delete_item(ov, item_id=1, msg_id=3)
    ov.close()
    # 那条消息的记账被一次失败的 redo 覆盖成 error ⇒ 它重新变成「待处理」
    store.record_run(
        seeded, [3], status=store.STATUS_ERROR, prompt_ver=refine.PROMPT_VERSION,
    )

    fake = FakeLLM(script=lambda _user: {"items": [
        {"quote": "数学作业截止到9月7号", "kind": "academic",
         "title": "数学作业截止", "deadline": "2026-09-07", "confidence": 0.9}
    ]})
    monkeypatch.setattr(refine, "chat_json", fake)

    stats = refine.refine(
        StubConfig(), api_key="k", conn=seeded, batch_size=1,
        on_progress=lambda *_: None,
    )
    assert stats.skipped_deleted == 1, "非 redo 路径上墓碑照样要挡"
    sent = " ".join(c["user"] for c in fake.calls)
    assert "数学作业" not in sent
def test_refine_fails_safe_when_the_overlay_is_unreadable(seeded, monkeypatch):
    """⚠️ 补的守卫（偏差，见报告「偏差」第 4 条）：overlay 读不到时 refine 必须照常跑完。

    `_tombstoned_msg_ids` 的 docstring 承诺「读不到一律返回空集」，理由是
    **代价不对称**：最坏后果只是「这次重抽把某条复活了」（用户再删一次即可），
    而在这里抛异常会让**整条每日管线停摆**（每天的定时 refine 直接失败）。

    ⚠️ 这条承诺此前**零守护**：变异实测——把 `except sqlite3.Error` 整段去掉、
    让异常照常冒出去，44 条测试**一条都不红**。而它正是那种「只在出事那天
    才起作用」的代码（锁死 / 权限 / 路径不可写）。
    """
    from vigil import overrides

    def boom(*_a, **_k):
        raise sqlite3.OperationalError("模拟 overlay 读不了：被锁死/没权限")

    monkeypatch.setattr(overrides, "connect", boom)

    fake = FakeLLM(script=lambda _user: {"items": []})
    monkeypatch.setattr(refine, "chat_json", fake)

    stats = refine.refine(
        StubConfig(), api_key="k", conn=seeded, batch_size=1, redo=True,
        on_progress=lambda *_: None,
    )
    assert stats.errors == [], f"读墓碑失败不许变成错误：{stats.errors}"
    assert stats.skipped_deleted == 0, "读不到墓碑 ⇒ 一个都不跳（宁可复活，不可停摆）"
    assert stats.batches == 3, "整轮照常跑完"


def test_refine_skipped_deleted_counts_only_what_it_actually_skipped(
    seeded, monkeypatch
):
    """⚠️ 补的守卫（偏差，见报告「偏差」第 5 条）：报告里的两个数都要说真话。

    · `skipped_deleted` 数的是**这轮实际跳过的消息**，不是「墓碑总数」——
      墓碑可以指向窗口外 / 库里根本没有的消息（非 redo、since/until、异地删除）。
    · `stats.scanned` 是**过滤之后**的（"这轮真正要处理多少条"）：CLI 拿
      `scanned - sent_messages` 报「本地筛掉」，把墓碑算进 scanned 会让它
      把"被墓碑挡下的"错报成"本地筛掉的"。

    这两条此前**零守护**：变异实测（J 把计数改成 `len(tombstones)`、H 把
    `stats.scanned` 提到过滤之前）44 条测试**一条都不红**。
    """
    from vigil import overrides

    overrides.ensure_schema()
    ov = overrides.connect()
    overrides.delete_item(ov, item_id=1, msg_id=3)      # 在待抽集合里
    overrides.delete_item(ov, item_id=2, msg_id=999)    # 窗口外 / 库里没有
    ov.close()

    fake = FakeLLM(script=lambda _user: {"items": []})
    monkeypatch.setattr(refine, "chat_json", fake)

    stats = refine.refine(
        StubConfig(), api_key="k", conn=seeded, batch_size=1, redo=True,
        on_progress=lambda *_: None,
    )
    assert stats.skipped_deleted == 1, "数的是实际跳过的 1 条，不是墓碑总数 2"
    assert stats.scanned == 2, "scanned 是过滤之后的（CLI 拿它做减法）"
def test_refine_reports_the_skip_on_the_progress_line(seeded, monkeypatch):
    """⚠️ 补的守卫（偏差，见报告「偏差」第 6 条）：跳过数必须**出现在进度行里**。

    brief 的 Interfaces 要求 `skipped_deleted`「报告里要看得见」，而让操作员
    看得见它的唯一机制就是 Step 4 那行进度——CLI 并不单独打印它
    （`cli.py` 只报 scanned / 本地筛掉 / 送模型）。删掉那行时 46 条测试
    **一条都不红**（变异 I 实测）⇒ 整条要求零守护。
    """
    from vigil import overrides

    overrides.ensure_schema()
    ov = overrides.connect()
    overrides.delete_item(ov, item_id=1, msg_id=3)
    ov.close()

    monkeypatch.setattr(
        refine, "chat_json", FakeLLM(script=lambda _user: {"items": []})
    )

    lines: list[str] = []
    refine.refine(
        StubConfig(), api_key="k", conn=seeded, batch_size=1, redo=True,
        on_progress=lines.append,
    )
    assert any("已软删" in x and "1" in x for x in lines), \
        f"跳过数必须出现在进度里，实际：{lines}"


# ── M6 T10：M5 遗留清理（#4 时序闸门的计数、#7 话术、F1 墓碑日志）────────


def test_a_backdated_deadline_is_counted(seeded, monkeypatch):
    """⭐ M5 遗留 #4：时序倒挂必须**有计数**，不许静默清掉。

    造的是 `item 83` 的形状——死线 `2026-05-06`、消息日 `2026-08-05`。
    ⚠️ 与「模型编日期」**刻意分开**：那条路径的源文里**找不到**那个日期
    （`deadline_supported` 挡下），这一条的源文里**有**「5月6日」，是**过去
    的既成事实**被当成了新死线。两者的运维含义完全不同（前者说明模型在编，
    后者说明提示词或数据源有问题），合成一个数会让两者都说不清。

    ⚠️ 变异反证：把 `_verify_deadlines` 里那个 `elif it.deadline_ts:` 分支
    整段删掉 ⇒ 本条**必红**（`backdated` 会是 0，而条目的 deadline 仍被清）。

    ⚠️ 更值钱的那条变异：把闸门**挪回** `_to_item`（不删、但也不计数）
    ⇒ 条目结果**完全一样**，而 `stats.deadlines_backdated` 是 0。
    **这条才是本任务要挡的形状**——「行为对、账目错」比「行为错」更难发现，
    因为没有任何用户可见的差异。（`test_to_item_no_longer_applies_the_timing_gate`
    是它的另一半。）
    """
    import datetime as dt

    event_ts = int(dt.datetime(2026, 8, 5, 12, 0).timestamp())
    seeded.execute(
        "INSERT INTO messages (msg_id, group_id, ts, sender_uid, content)"
        " VALUES (9, 100, ?, 'u_a', '档案袋封口时间：5月6日')",
        (event_ts,),
    )
    seeded.commit()

    def script(_user: str) -> dict:
        return {"items": [
            {"quote": "档案袋封口时间：5月6日", "kind": "notice",
             "title": "档案袋封口", "detail": None, "deadline": "2026-05-06",
             "place": None, "amount": None, "confidence": 0.9}
        ]}

    monkeypatch.setattr(refine, "chat_json", FakeLLM(script=script))

    stats = refine.refine(
        StubConfig(), api_key="k", conn=seeded, batch_size=1,
        on_progress=lambda *_: None,
    )

    assert stats.deadlines_backdated == 1, (
        "死线早于消息时间的事实必须出现在账目里（M5 遗留 #4）："
        "此前它被清掉而**无任何计数**，运维侧零告警"
    )
    assert stats.deadlines_dropped == 0, (
        "源文里**有**「5月6日」的字面依据 ⇒ 不该计进「模型在编」那一格"
    )
    # 只降级 deadline_ts 这一个字段：条目本身照常落库（与既有取舍一致）
    assert seeded.execute(
        "SELECT title, deadline_ts FROM items WHERE title = '档案袋封口'"
    ).fetchone() == ("档案袋封口", None)


def test_a_fully_tombstoned_window_says_so(seeded, monkeypatch):
    """⭐ M5 遗留 F1：整窗全被墓碑时，日志必须**与「窗口本来就空」不同形**。

    从前那行日志排在 `if not messages: return` **之后**——于是它本该说出的
    那件事（整窗都被墓碑挡下了）恰恰让它自己永远不执行。用户看到的是
    「[refine] 没有待处理的消息」，与「这个窗口本来就没有消息」**一字不差**：
    一句无法区分两种事实的日志，等于没有日志。

    ⚠️ 变异反证：把那句 `on_progress` 挪回 `if not messages: return` **之后**
    ⇒ 本条**必红**（一行日志都收不到）。这个变异**不改变任何返回值、
    不改变任何数据库状态**——它只让一句人话消失。所以只有把日志本身
    当断言对象，才挡得住它。
    """
    from vigil import overrides

    overrides.ensure_schema()
    ov = overrides.connect()
    try:
        for item_id, msg_id in ((1, 1), (2, 2), (3, 3)):
            overrides.delete_item(ov, item_id=item_id, msg_id=msg_id)
    finally:
        ov.close()

    lines: list[str] = []
    stats = refine.refine(
        StubConfig(), api_key="k", conn=seeded, batch_size=1,
        on_progress=lines.append,
    )

    assert stats.scanned == 0, "整窗都被墓碑挡下 ⇒ 一条都不剩（这正是触发早退的形状）"
    assert any("已软删" in ln for ln in lines), (
        f"整窗全被墓碑时没有说出原因，实际日志：{lines}"
    )


def test_the_progress_line_reports_the_deduped_count(seeded, monkeypatch):
    """⭐ M5 遗留 #7：进度话术必须报**去重后**的落库条数。

    `store.save_items` 会把同 `(title, event_ts, group_id)` 的条目并成一条
    （`store._dedupe_batch`），所以「模型产出了几条」与「库里落了几条」**会**
    不同。报去重**前**的那个数，就是报了一个**没落库**的条数——纯话术，
    但它是账目（用户看到「2 条 → 2 条 item」而库里只有 1 行）。

    ⚠️ 变异反证：把 `{saved} 条 item` 改回 `{len(produced)} 条 item`
    ⇒ 本条**必红**（收到的是「2 条 item」）。
    """
    def script(_user: str) -> dict:
        # 两条**同标题**、都摘录自同一条消息 ⇒ `(title, event_ts, group_id)` 相同
        return {"items": [
            {"quote": "明天下午有讲座", "kind": "activity",
             "title": "西太湖报告厅有讲座", "detail": None, "deadline": None,
             "place": None, "amount": None, "confidence": 0.9},
            {"quote": "地点西太湖报告厅", "kind": "activity",
             "title": "西太湖报告厅有讲座", "detail": None, "deadline": None,
             "place": None, "amount": None, "confidence": 0.9},
        ]}

    monkeypatch.setattr(refine, "chat_json", FakeLLM(script=script))

    lines: list[str] = []
    stats = refine.refine(
        StubConfig(), api_key="k", conn=seeded, batch_size=30,
        on_progress=lines.append,
    )

    assert stats.items_saved == 1, (
        "两条同 (title, event_ts, group_id) 的条目必须并成一条——"
        "本条的前提条件不成立的话，下面的断言测的就不是去重"
    )
    assert any(
        "1 条 item（另有 1 条与批次内重复，已合并）" in ln for ln in lines
    ), f"进度话术报的是去重**前**的条数，实际日志：{lines}"


def test_prompt_contains_no_real_name():
    """⭐ **出网边界上姓名必须清零** —— 这是 P3 的核心机械判据。

    ⚠️ 为什么需要两条路一起走（称呼 + 正文）：
      · 称呼那一处走 `redactor.name()`（保留角色词、抹掉人名本体）
      · **正文里可能直接写着人名**（「张三明天交材料」）—— 只抹称呼
        等于没抹。这个洞是出口验收时实测抓出来的：第一版只改称呼，
        验收脚本一跑，四个夹具姓名**全部**泄漏。

    ⚠️ 名单来自**昵称**，而正文里是**裸名** —— 所以要走
    `Redactor.person_names()` 剥出人名本体再匹配。直接拿完整昵称去比，
    一个字都命中不了。
    """
    from vigil.redact import Redactor
    from vigil.store import PendingMessage

    NAMES = {"张三", "李四", "王五", "赵六"}
    nicks = ["22-1班班主任助理张三", "22-1班班助李四", "社团负责人王五", "辅导员赵六"]
    msgs = [
        PendingMessage(
            msg_id=i, group_id=100, ts=1000 + i, sender_uid=f"u{i}",
            sender=nick,
            # 正文里点名道姓 —— 这一条才是这条用例真正在压的路径
            content="通知：张三和李四明天交材料，王五负责收，赵六也来",
        )
        for i, nick in enumerate(nicks)
    ]
    redactor = Redactor(
        n for m in msgs if m.sender for n in Redactor.person_names(m.sender)
    )
    text = refine.build_user_prompt(msgs, redactor, today="2026-10-14")

    leaked = sorted(n for n in NAMES if n in text)
    assert not leaked, f"姓名出网了：{leaked}"
    # 但代号必须仍在 —— 否则"同一人说了两次"就看不出来了
    for i in range(len(msgs)):
        assert f"U{i + 1} " in text
    # 角色信号也要仍在（它是判断"是不是官方"的依据）
    assert "班主任" in text and "辅导员" in text


def test_person_names_strips_role_and_prefix():
    """昵称 → 人名本体的剥法。剥不干净就等于遮蔽失效（正文里的裸名匹配不上）。"""
    from vigil.redact import Redactor

    assert Redactor.person_names("22-1班班主任助理张三") == {"张三"}
    assert Redactor.person_names("辅导员赵六") == {"赵六"}
    assert Redactor.person_names("社团负责人王五") == {"王五"}
    assert Redactor.person_names("张三") == {"张三"}
    # 没有可辨认人名的长昵称**不**当作人名（否则会把正文里的常用词也抹掉）
    assert Redactor.person_names("求索技术部的小窝") != {"求索技术部的小窝"}
    assert Redactor.person_names("") == set()
