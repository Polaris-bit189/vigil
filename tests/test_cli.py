"""CLI 层的退出码与接线。

⚠️ 退出码是 M4 的**核心交付**：任务计划就只能看见这一个数。所以这里
每一条都在钉「什么时候返回几」，而不是钉输出文字。

范式抄 `tests/test_digest.py::cli_env`（那一段守的是同一个契约的 digest 分支）：
用 `cli.main([...])` 驱动，把 `_load_config_only` / `_require_export_db` /
`load_llm_key` 换成替身，**一件都不许落到真实环境**（真库、真日志目录、真的锁）。

⚠️ LLM 配置重构后 `load_llm_key` / `load_llm_settings` / `requires_llm_key`
在 `cli.py` 里是**模块级**名字（`from .config import …` 在 import 期绑定），
`cmd_refine` / `cmd_digest` / `cmd_repass` 都以**裸名**调它们 ⇒ 打补丁只能打在
`vigil.cli` 上，打 `vigil.config` **够不着**（那是 `cli.py` 自己的一份引用）。
`cmd_daily` 同理（见 `daily_env`，它两处都接是为了不依赖"别的夹具恰好还在"）。
"""

from __future__ import annotations

import pytest

from vigil import cli


class _null_ctx:
    """假上下文管理器：`with` 进去什么都不做、什么都不锁。"""

    def __enter__(self):
        return None

    def __exit__(self, *exc):
        return None


def _exit_code_of(argv: list[str]) -> int | str:
    """跑一次 CLI，把「异常上抛」折算成退出码。

    ⚠️ 这里**只折算、不吞**：未捕获的异常本身就是「非零退出」的一种形态
    （`__main__` 走 `sys.exit(main())`，解释器对未捕获异常一律非零退出），
    与 `return 1` 是同一个契约的两种实现（计划 §8.5 第 3 条）。
    唯一被这条判据拒绝的是「抛了却仍然报成功」。
    """
    try:
        return cli.main(argv)
    except SystemExit as exc:
        return exc.code if exc.code is not None else 0
    except Exception:  # noqa: BLE001 — 见 docstring：上抛 == 非零退出
        return 1


def _config(tmp_path):
    from vigil.config import Config, Group

    return Config(
        source_dir=tmp_path / "qq",
        output_db=tmp_path / "export.db",
        groups=(Group(id=100, name="班级群"), Group(id=200, name="新生群")),
    )


@pytest.fixture
def cli_env(monkeypatch, tmp_path):
    """把 export / refine / digest 的外部依赖全接上（不含 `daily`，见 `daily_env`）。

    ⚠️ **锁的路径由 `conftest.py` 的 autouse 夹具统一挪到 `tmp_path`**
    （三个 `cmd_*` 现在各上一次锁，见 M4 审 F4）。本夹具不再自己换一次——
    换的是**路径**不是锁本身，真 `SingleInstance` 的拿锁/放锁逻辑照跑。

    ⚠️ **`load_llm_key` 两处都要接**（M6 之后修）：`cli.py` 用
    `from .config import load_llm_key` **在模块级**绑定了自己的那份引用，
    而 `cmd_refine` / `cmd_digest` / `cmd_repass` 都以**裸名**调它 ⇒ 只打
    `vigil.config.load_llm_key` 够不着它，密钥判空后会 `sys.exit`（那正是这
    批用例此前变红的原因）。打 `cli.load_llm_key` 才是真正生效的那一处；
    再打一份 `vigil.config` 是为了不依赖"别的夹具恰好还在"（与 `daily_env` 同）。
    """
    cfg = _config(tmp_path)
    monkeypatch.setattr(cli, "_load_or_die", lambda: (cfg, "k-db"))
    monkeypatch.setattr(cli, "_load_config_only", lambda: cfg)
    monkeypatch.setattr(cli, "_require_export_db", lambda config: tmp_path / "export.db")
    monkeypatch.setattr(cli, "load_llm_key", lambda *a, **k: "k-llm")
    monkeypatch.setattr("vigil.config.load_llm_key", lambda *a, **k: "k-llm")
    return tmp_path


@pytest.fixture
def daily_env(monkeypatch, tmp_path):
    """把 `cmd_daily` 的外部依赖全接上——**不碰真实日志目录、不真加锁**。

    ⚠️ 配置与两个密钥**都要接**：`cmd_daily` 在加锁**之前**就要它们齐备，
    缺一个就会提前 `return 1`——于是那两条锁/失败的用例测到的其实是
    「没有密钥」，而不是它名字里说的那件事（而这台机器上 `.env` 恰好齐备时，
    测试又会悄悄地变成依赖真实环境）。
    """
    monkeypatch.setattr(cli, "load_config", lambda *a, **k: _config(tmp_path))
    monkeypatch.setattr(cli, "load_key", lambda *a, **k: "k-db")
    monkeypatch.setattr(cli, "load_llm_key", lambda *a, **k: "k-llm")
    monkeypatch.setattr("vigil.config.load_llm_key", lambda *a, **k: "k-llm")
    monkeypatch.setattr(cli.logs, "setup", lambda **kw: None)
    monkeypatch.setattr(cli.logs, "clear_last_error", lambda: None)
    monkeypatch.setattr(cli.logs, "prune_old_logs", lambda **kw: [])
    monkeypatch.setattr(cli.logs, "write_last_error", lambda *a, **kw: None)
    monkeypatch.setattr(cli.lock, "SingleInstance", _null_ctx)
    return tmp_path


# ── export：整群读不到 ⇒ 非零 ────────────────────────────────────


def test_export_returns_nonzero_when_a_group_could_not_be_read(cli_env, monkeypatch,
                                                               capsys):
    """⭐ export 有整群读不到 ⇒ 非零。

    实测旧实现（M4 规划期）：`cmd_export` 结尾无条件 `return 0`，
    即使它自己刚打了 `[失败] N 个群整群读不到`。任务计划看到的是成功。

    变异 M1（删掉 `if stats.failed_groups: return 1`）→ 本条红。
    """
    from vigil.ingest import IngestStats as ExportStats

    stats = ExportStats(
        per_group=[(100, "班级群", 12)],
        failed_groups=[(200, "新生群")],
    )
    monkeypatch.setattr("vigil.ingest.ingest", lambda *a, **kw: stats)

    rc = cli.main(["export"])

    assert rc != 0
    assert "失败" in capsys.readouterr().out


def test_export_returns_zero_when_every_group_was_read(cli_env, monkeypatch):
    """阳性对照：全都读到了就必须是 0。

    没有这一条的话，「无条件 `return 1`」也能让上面那条全绿——而它意味着
    每次成功导出都在报警，报警器很快就会被无视。
    """
    from vigil.ingest import IngestStats as ExportStats

    stats = ExportStats(per_group=[(100, "班级群", 12), (200, "新生群", 8)])
    monkeypatch.setattr("vigil.ingest.ingest", lambda *a, **kw: stats)

    assert cli.main(["export"]) == 0


def test_export_returns_zero_when_only_bad_pages(cli_env, monkeypatch):
    """坏页（`skipped_by_group`）**不算失败**——别把它并进失败里。

    那是 QQ 库的物理损坏：已知、非致命、同群其余消息不受影响，输出里
    已经照实报了。把它算成失败，等于让每一天都是"失败"。
    """
    from vigil.ingest import IngestStats as ExportStats

    stats = ExportStats(
        per_group=[(100, "班级群", 12)],
        skipped_by_group=[(200, "新生群", 3)],
    )
    monkeypatch.setattr("vigil.ingest.ingest", lambda *a, **kw: stats)

    assert cli.main(["export"]) == 0


def test_export_output_goes_through_logs_emit(cli_env, monkeypatch):
    """管线输出必须走 `logs.emit`（T1 契约：先 print 原文、再写日志）。

    变异「`logs.emit(` → `print(`」会让本条红——而现实中它意味着这几行只到
    stdout：任务计划下 stdout **没有落点**，跑完一夜等于什么都没说过。
    """
    from vigil.ingest import IngestStats as ExportStats

    seen: list[str] = []
    monkeypatch.setattr(cli.logs, "emit", seen.append)
    monkeypatch.setattr(
        "vigil.ingest.ingest",
        lambda *a, **kw: ExportStats(per_group=[(100, "班级群", 12)]),
    )

    cli.main(["export"])

    assert any("完成：1 个群，共 12 条消息" in m for m in seen)


# ── refine：有批次出错 ⇒ 非零（且汇总行仍在）────────────────────


def _refine_stats(**kw):
    from vigil.refine import RefineStats

    base = dict(
        scanned=100, discarded_local=60, candidates=44, sent_messages=40,
        batches_planned=2, batches=2, items_saved=7,
    )
    base.update(kw)
    return RefineStats(**base)


def test_refine_returns_nonzero_when_batches_errored(cli_env, monkeypatch):
    """⭐ refine 有批次出错 ⇒ 非零。

    变异 M2（`return 1 if stats.errors else 0` → `return 0`）→ 本条红。
    """
    stats = _refine_stats(errors=["批 1：HTTP 503", "批 2：超时"])
    monkeypatch.setattr("vigil.refine.refine", lambda *a, **kw: stats)

    assert cli.main(["refine"]) != 0


def test_refine_returns_zero_when_no_batch_errored(cli_env, monkeypatch):
    """阳性对照：一批都没错 ⇒ 0（挡「无条件 return 1」）。"""
    monkeypatch.setattr("vigil.refine.refine", lambda *a, **kw: _refine_stats())

    assert cli.main(["refine"]) == 0


def test_refine_still_prints_summary_before_returning_nonzero(cli_env, monkeypatch,
                                                              capsys):
    """汇总行必须在 `return` 之前——否则失败时日志里没有本次的计数。

    只断言汇总段的**稳定前缀**，不碰词表：词表的判据在别处（见 brief 的
    M5 阳性对照——那句文案目前零覆盖）。
    """
    stats = _refine_stats(errors=["批 1：HTTP 503"])
    monkeypatch.setattr("vigil.refine.refine", lambda *a, **kw: stats)

    rc = cli.main(["refine"])

    out = capsys.readouterr().out
    assert rc != 0
    assert "完成：扫描" in out      # 汇总行仍在
    assert "产出条目" in out        # 汇总段确实走完了，不是半路 return
    assert "[失败] 1 批出错" in out


def test_refine_summary_states_the_discard_counts_as_peers_not_a_subset(
    cli_env, monkeypatch, capsys
):
    """⭐⭐ 汇总行里「本地筛掉」与「规则硬丢弃」是**并列**关系，不许写成包含（「其中」）。

    计划 §三 3.6 的第 2 条禁令。M2-4 修的正是这句假话：`prefilter.expand_context`
    取 ±2 邻居时**不看**那条消息有没有被硬规则判死，所以被判死的消息照样可能作为
    上下文出网——「本地筛掉」里**不包含**「规则硬丢弃」，写成「（其中硬规则丢弃 N 条）」
    就是在说两者是父集/子集，在真库上是假话。

    ⚠️ M4 把它改对了，却**没给「它不再回来」上保险**：终审复核实测，把汇总行改回
    「（其中硬规则丢弃 N 条）」→ **原 404 条测试全绿**。这条断言就是那个保险。

    ⚠️ 断言**只落在 refine 的汇总行上**，不全局搜「其中」——`cli.py` 别处另有一处
    **合法**的「其中 N 行是程序补的」（digest 的补行计数，那是真的包含关系），
    全局搜会误伤它。

    变异「`（候选 … 条；规则硬丢弃 … 条）` → `（其中硬规则丢弃 … 条）`」→ 本条红。
    """
    monkeypatch.setattr("vigil.refine.refine", lambda *a, **kw: _refine_stats())

    assert cli.main(["refine"]) == 0

    lines = [ln for ln in capsys.readouterr().out.splitlines()
             if ln.startswith("完成：扫描")]
    assert len(lines) == 1, f"refine 的汇总行应当恰好一行，实际：{lines!r}"
    summary = lines[0]
    assert "本地筛掉" in summary, summary
    assert "其中" not in summary, (
        "「其中」把「规则硬丢弃」说成了「本地筛掉」的子集——那不是实情："
        f"{summary}"
    )


def test_cli_refine_reports_backdated_deadlines(cli_env, monkeypatch, capsys):
    """⭐ M5 遗留 #4 的**可见性**那一半：计数必须真的打印出来。

    `RefineStats.deadlines_backdated` 有测试（`test_refine.py` 那条端到端），
    而「CLI 有没有把它印出来」是**另一件事**——`cli.py` 里那段没守住的话，
    计数进了 stats 也没人看得见，而 #4 的全部理由就是「运维要看得见」。
    同族的既有先例：`test_digest.py::test_cli_digest_reports_dropped_deadlines`。

    ⚠️ 与「找不到字面依据」那句**必须是两句话**：两者的处置不同
    （前者查模型/提示词，后者查提示词或数据源），合成一句就是报了一个
    说不清是什么的数。

    变异反证：删掉 `cmd_refine` 里那段 `if stats.deadlines_backdated:` 的打印
    ⇒ 本条红。
    """
    monkeypatch.setattr(
        "vigil.refine.refine",
        lambda *a, **kw: _refine_stats(deadlines_backdated=3, deadlines_dropped=0),
    )

    assert cli.main(["refine"]) == 0

    out = capsys.readouterr().out
    assert "[注意] 3 条截止日**早于源消息时间**" in out, (
        f"时序倒挂的条数没有出现在 CLI 输出里——#4 要的可见性这一半丢了：{out}"
    )
    assert "找不到字面依据" not in out, (
        "两句告警混成了一句：两个数的运维含义不同，不许互相冒充"
    )


def test_refine_dry_run_returns_zero(cli_env, monkeypatch):
    """`--dry-run` 仍然是 0：它本来就不该有错误，且明确标注了没调模型。"""
    monkeypatch.setattr("vigil.refine.refine", lambda *a, **kw: _refine_stats())

    assert cli.main(["refine", "--dry-run"]) == 0


def test_pipeline_stages_receive_logs_emit_as_on_progress(cli_env, monkeypatch):
    """三个阶段的进度回调必须是 `logs.emit`。

    阶段内部那些进度行（每群几行、每批几行）默认走内建 `print`——只到
    stdout。`daily` 跑完后排障时要靠日志回答"跑到哪了、哪个群卡住了"，
    回调没接上，日志里就是一段空白。
    """
    from vigil.digest import DigestStats
    from vigil.ingest import IngestStats as ExportStats

    got: dict[str, object] = {}

    def fake_export(config, key, *, on_progress=print):
        got["export"] = on_progress
        return ExportStats()

    def fake_refine(config, **kw):
        got["refine"] = kw.get("on_progress")
        return _refine_stats()

    def fake_digest(config, **kw):
        got["digest"] = kw.get("on_progress")
        return DigestStats(day="2026-09-13")

    monkeypatch.setattr("vigil.ingest.ingest", fake_export)
    monkeypatch.setattr("vigil.refine.refine", fake_refine)
    monkeypatch.setattr("vigil.digest.digest", fake_digest)

    assert cli.main(["export"]) == 0
    assert cli.main(["refine"]) == 0
    assert cli.main(["digest", "--date", "2026-09-13"]) == 0

    assert got["export"] is cli.logs.emit
    assert got["refine"] is cli.logs.emit
    assert got["digest"] is cli.logs.emit


# ── digest：失败也要走完汇总 ───────────────────────────────────


def test_digest_prints_summary_when_a_batch_failed(cli_env, monkeypatch, capsys):
    """⚠️ 失败**不再提前 return**：提前返回会让日志里只有一句「[失败]」，
    而没有本次的计数——事后根本判断不出这轮跑到哪、抓到几条。

    变异「把 `return 1` 挪回汇总行之前」会让本条红。
    """
    from vigil.digest import DigestStats

    stats = DigestStats(
        day="2026-09-13", groups=2, messages=30, items=5, lines=4,
        errors=["批 1：HTTP 503"],
    )
    monkeypatch.setattr("vigil.digest.digest", lambda *a, **kw: stats)

    rc = cli.main(["digest", "--date", "2026-09-13"])

    out = capsys.readouterr().out
    assert rc == 1
    assert "[失败] 批 1：HTTP 503" in out
    assert "完成：2026-09-13 窗口内 2 个群 30 条消息" in out


# ── daily：退出码 0 / 1 / 2 ────────────────────────────────────


def test_daily_returns_2_when_another_instance_holds_the_lock(daily_env, monkeypatch):
    """⭐ 已有实例在跑 ⇒ 退出码 **2**（§三 3.4 冻结）。

    2 不是 1：无人值守下「上一次还没跑完」只要等着就行，「这次跑失败了」
    要人来看。用同一个码会把这两件事混成一件。

    变异 M3（`return 2` → `return 1`）→ 本条红。
    """
    from vigil import lock

    def boom(*a, **kw):
        raise lock.AlreadyRunning("已有实例在跑（锁：x；上次写入：pid=123）")

    monkeypatch.setattr(cli.lock, "SingleInstance", boom)

    assert cli.main(["daily"]) == 2


def test_daily_writes_last_error_and_returns_1_on_failure(daily_env, monkeypatch):
    """⭐ 有阶段失败 ⇒ 写 LAST-ERROR.txt + 退出码 1。

    变异 M4（删掉 `logs.write_last_error(report.last_error)`）→ 本条红。
    """
    written: dict[str, str] = {}
    monkeypatch.setattr(
        cli.logs, "write_last_error",
        lambda msg, **kw: written.setdefault("msg", msg),
    )

    from vigil.daily import RunReport, StageResult

    monkeypatch.setattr(
        "vigil.daily.run",
        lambda **kw: RunReport(
            stages=(StageResult("export", False, "2 个群整群读不到"),),
            last_error="export: 2 个群整群读不到",
            deadline="2026-09-16",
        ),
    )

    rc = cli.main(["daily"])

    assert rc == 1
    assert "2 个群整群读不到" in written["msg"]


def test_daily_returns_0_on_success(daily_env, monkeypatch):
    """成功 ⇒ 0。也是 `RunReport.ok` 这条契约在 CLI 侧的锚点。"""
    from vigil.daily import RunReport

    monkeypatch.setattr(
        "vigil.daily.run", lambda **kw: RunReport(deadline="2026-09-16")
    )

    assert cli.main(["daily"]) == 0


def test_daily_clears_last_error_before_running(daily_env, monkeypatch):
    """开跑先清 LAST-ERROR ⇒ 「文件存在」⟺「本次跑失败过」（T1 的语义）。

    顺序也要紧：清了之后再跑，跑失败时写进去的就是**本次**的错误。
    """
    calls: list[str] = []
    monkeypatch.setattr(cli.logs, "clear_last_error", lambda: calls.append("clear"))

    from vigil.daily import RunReport

    def fake_run(**kw):
        calls.append("run")
        return RunReport()

    monkeypatch.setattr("vigil.daily.run", fake_run)

    assert cli.main(["daily"]) == 0
    assert calls == ["clear", "run"]


# ── F4：人工命令也要上锁（M4 独立审查）─────────────────────────
#
# 审查 AST 核过：全文件此前**只有 `cmd_daily` 引用 `lock`** ⇒ 人工跑
# `export`/`refine`/`digest` 与任务计划撞车时完全不受保护。而
# `docs/SETUP-自动化.md` §七 给退出码 2 写的理由**正是**这个场景
# （「用户手动又跑一次」），spec §4.8 的「幂等可重入」也只堵了一半：
# 两个 refine 并发会各读同一批待处理消息 ⇒ 抽两遍、烧两份 token。


def test_export_returns_2_when_another_instance_holds_the_lock(cli_env, monkeypatch):
    """⭐ 人工 `export` 撞上正在跑的实例 ⇒ **2**，且管线一行没跑。

    变异「去掉 `cmd_export` 的 `with lock.SingleInstance():`」→ 本条红。
    """
    from vigil import lock
    from vigil.ingest import IngestStats as ExportStats

    ran: list[str] = []
    monkeypatch.setattr(
        "vigil.ingest.ingest",
        lambda *a, **kw: ran.append("export") or ExportStats(),
    )

    with lock.SingleInstance(lock.LOCK_PATH):   # 「另一个实例」持着同一把锁
        assert cli.main(["export"]) == 2
    assert ran == []                                      # 没真的跑管线
    assert cli.main(["export"]) == 0                      # 锁放掉后照常


def test_refine_returns_2_when_another_instance_holds_the_lock(cli_env, monkeypatch):
    """⭐ 人工 `refine` 撞车 ⇒ **2**，且没调模型、没写库。

    变异「去掉 `cmd_refine` 的 `with lock.SingleInstance():`」→ 本条红。
    """
    from vigil import lock

    ran: list[str] = []
    monkeypatch.setattr(
        "vigil.refine.refine",
        lambda *a, **kw: ran.append("refine") or _refine_stats(),
    )

    with lock.SingleInstance(lock.LOCK_PATH):   # 与代码**同一把锁**
        assert cli.main(["refine"]) == 2
    assert ran == []
    assert cli.main(["refine"]) == 0


def test_digest_returns_2_when_another_instance_holds_the_lock(cli_env, monkeypatch):
    """⭐ 人工 `digest` 撞车 ⇒ **2**，且没生成日报。

    变异「去掉 `cmd_digest` 的 `with lock.SingleInstance():`」→ 本条红。
    """
    from vigil import lock
    from vigil.digest import DigestStats

    ran: list[str] = []
    monkeypatch.setattr(
        "vigil.digest.digest",
        lambda *a, **kw: ran.append("digest") or DigestStats(day="2026-09-13"),
    )

    with lock.SingleInstance(lock.LOCK_PATH):   # 与代码**同一把锁**
        assert cli.main(["digest", "--date", "2026-09-13"]) == 2
    assert ran == []
    assert cli.main(["digest", "--date", "2026-09-13"]) == 0


def test_lock_is_released_when_a_locked_command_exits_via_sys_exit(cli_env, monkeypatch,
                                                                   capsys):
    """`sys.exit`（**SystemExit**）也必须释放锁。

    三个 `cmd_*` 的配置/参数错误分支都是 `sys.exit(...)`，而它们现在都在
    `with lock.SingleInstance():` 里面。**SystemExit 不是 Exception 是
    BaseException**——`with` 仍会调 `__exit__`，但这条链必须实测：判错了就是
    「一次参数错误把锁永远攥在自己手里」，此后所有人工命令与任务计划
    全会拿到 2，而原因完全看不出来。

    变异「`__exit__` 里不解锁 / 只在 `except Exception` 上解锁」→ 本条红。
    """
    # `--since` 非法 ⇒ `reader._to_epoch` 抛 ValueError ⇒ `cmd_refine` 的 sys.exit
    with pytest.raises(SystemExit):
        cli.main(["refine", "--since", "2026-13-45"])

    monkeypatch.setattr("vigil.refine.refine", lambda *a, **kw: _refine_stats())
    assert cli.main(["refine"]) == 0        # 锁已放掉：能跑进管线（0），不是 2


# ── §8.5：能吞异常的只有「打扫类」失败 ─────────────────────────


def test_daily_unexpected_exception_returns_nonzero(daily_env, monkeypatch):
    """§8.5 第 2 条：`cmd_daily` 的顶层 `except Exception` **必须返回非零**。

    绝对不许写成 `except Exception: logs.emit(...); return 0` 之类的
    「降级成功」——现实里它意味着「daily 崩了，任务计划看到成功」。
    """
    def boom(**kw):
        raise RuntimeError("daily 内部炸了")

    monkeypatch.setattr("vigil.daily.run", boom)

    assert _exit_code_of(["daily"]) != 0


def test_daily_log_write_failure_never_ends_in_success(daily_env, monkeypatch):
    """§8.5 第 1/4 条：从 `logs.emit` 冒出来的异常**不许被记成成功**。

    ⚠️ 这里钉的是「**冒出来的异常不许被降级成 exit 0**」这条契约，
    **不是**在模拟真实磁盘满（计划 §8.5 第 4 条明说它不证明那件事）。

    ⚠️ 而且「日志写失败」并**不是**一条能到得了本层的事件：`logs.emit` 的
    「写不进就抛」只对**跨零点换文件**那一段成立；普通写失败（磁盘满）被
    `logging.StreamHandler.emit` 自带的 `try/except` 吞掉、只调
    `handleError` 往 stderr 打一份——而任务计划**丢弃 stderr**。
    ⇒ 那条路径要靠在 `logs.py` 的 `handleError` 上兜（T1 的文件，由 controller
    另行处置），**不是**本条能覆盖的。本条只保证：万一异常真的冒上来了，
    `cmd_daily` 不会把它变成成功。
    """
    from vigil.config import ConfigError

    seen: list[str] = []

    def boom(msg):
        seen.append(msg)
        raise OSError("日志写不进去")

    def config_dies(*a, **kw):
        raise ConfigError("配置读不出来")

    monkeypatch.setattr(cli.logs, "emit", boom)
    # 用一个**必然经过 emit** 的失败分支（配置错误）把 emit 推到台前：
    # 否则 emit 根本没被调用，这条测试看起来绿、其实什么都没测。
    monkeypatch.setattr(cli, "load_config", config_dies)

    assert _exit_code_of(["daily"]) != 0
    # ⚠️ 这两半都要钉（M4 审 F2）。只断言「抛了不报成功」是不够的：把该分支的
    # `logs.emit(...)` 换成 `pass`，那条断言**照样全绿**——它对这个分支到底有没有
    # 留痕不敏感。所以这里同时断言 emit 真的被调用过、且带的是配置错误那句话。
    assert seen, "该失败分支必须经过 logs.emit"
    assert "配置" in seen[0]


def test_daily_last_error_write_failure_never_ends_in_success(daily_env, monkeypatch):
    """§8.5 第 3 条：`write_last_error` 在磁盘满时**自己也会抛**。

    那条链可以断（异常继续往上走 ⇒ 退出码仍非零），但**不许断在"成功"上**：
    要么不包，要么包了之后仍然返回非零。
    """
    from vigil.daily import RunReport, StageResult

    def boom(*a, **kw):
        raise OSError("磁盘满")

    monkeypatch.setattr(cli.logs, "write_last_error", boom)
    monkeypatch.setattr(
        "vigil.daily.run",
        lambda **kw: RunReport(
            stages=(StageResult("export", False, "整群读不到"),),
            last_error="export: 整群读不到",
        ),
    )

    assert _exit_code_of(["daily"]) != 0


# ── 边界：改动的分界线（人工查询命令不进日志）───────────────────


@pytest.mark.parametrize(
    "argv, reader_func",
    [
        (["read"], "read_messages"),
        (["who"], "sender_stats"),
        (["media"], "local_images"),
    ],
)
def test_query_commands_do_not_write_to_the_log(monkeypatch, tmp_path, argv,
                                                reader_func):
    """`read` / `who` / `media`（以及 `groups` / `deadline-audit` / `serve`）
    是**人工即时查询**，永不无人值守。

    它们往日志里灌只会把日志淹掉——本任务只把四个管线命令接上 `logs.emit`。
    变异「顺手把查询命令也换掉」会让本条红。
    """
    from vigil import reader

    monkeypatch.setattr(cli, "_load_config_only", lambda: _config(tmp_path))
    monkeypatch.setattr(cli, "_require_export_db", lambda config: tmp_path / "export.db")
    monkeypatch.setattr(reader, reader_func, lambda *a, **kw: [])

    def boom(*a, **kw):
        raise AssertionError("人工查询命令不该写日志")

    monkeypatch.setattr(cli.logs, "emit", boom)

    assert cli.main(argv) == 0


# ── C-1：真的 `ingest()` 读不到群 ⇒ 真的 `cmd_ingest` 非零（M4 终审）────
#
# ⚠️ 上面那些 export 判据（`:98` 起）用的全是**手造的 `IngestStats`**：它们
# 证明的是「`failed_groups` 非空 ⇒ 退 1」，**证明不了**「读不到的群会进
# `failed_groups`」。而终审 C-1 的洞恰恰在后者——源实现在某条查询失败时返回
# 空的 `GroupRead`，与「这个群本来就没消息」**完全同形** ⇒ 一个群整群读不到
# 时 `cmd_ingest` 退 0、`daily` 照写日报。
#
# 数据源层重构后，这个洞被**搬进契约**了：`source.Source.read_group` 的
# docstring 明文要求「读不到必须抛异常，不许返回空 `GroupRead`」。于是守护它
# 的正确位置也从「打完桩的 qqdb」变成了**源与 ingest 的交接处**——即下面那
# 个假源。这一条现在跑的是**真的** `ingest_mod.ingest`，只把"源是谁"换掉。


class _FakeSource:
    """满足 `source.Source` 协议的最小假源：固定一份群，不碰真 QQ 库。

    ⚠️ 为什么是假源、而不用仓库里现成的 `synth.SyntheticSource`：后者是**真
    实现**，它从一整份合成语料里挑群——读哪个群、读到几条都随 seed 变。而这些
    用例要钉的恰恰是「某一个群**整群读不到**」这个**精确的失败形状**，只有在
    假源里把"读哪个群、抛不抛"写死，才能把「整群读不到」与「本来就没消息」
    对照出来——那正是这条守卫的全部意义。
    """

    def __init__(self, *, unreadable: tuple[int, ...] = ()) -> None:
        # `unreadable` 里列出的群，`read_group` 抛「库损坏」；空 = 全部读得到。
        self._unreadable = set(unreadable)

    def list_groups(self) -> list[tuple[int, int, int]]:
        return [(100, 7, 1_700_000_000)]

    def group_names(self) -> dict[str, str]:
        return {"100": "班级群"}

    def read_group(self, gid: int):
        import sqlite3

        from vigil.source import GroupRead

        if gid in self._unreadable:
            # 与真源读不到时抛的是**同一类**异常（库/查询级故障），不是随便
            # 什么错——ingest 的失败契约只认"源抛了"，异常类型无关紧要，但
            # 用真形状能让这条测试在读起来时不像在测"任意异常会冒泡"。
            raise sqlite3.OperationalError("database disk image is malformed")
        return GroupRead(messages=[])

    def sender_names(self) -> dict:
        return {}

    def media_index(self) -> dict:
        return {}


def test_ingest_returns_nonzero_when_the_real_ingest_cannot_read_a_group(
    cli_env, monkeypatch, capsys
):
    """⭐⭐ 真的 `ingest()` 读不到一个群 ⇒ 真的 `cmd_ingest` 退 **1**。

    终审 C-1 的完整链路是「整群读不到 ⇒ `failed_groups` 里没有它 ⇒ 退 0 ⇒
    `daily` 照写日报」。本条的判据一路从**源的 `read_group`** 走到退出码，
    中间**不许有任何替身**——`ingest` 是真的，只把"源是谁"换成假源。

    对照组（同一个假源、只是不抛）：退 **0**。没有这一半，「任何情况都退 1」
    也能让第一条绿。

    ⚠️ 它守的是**「整群读不到 ≠ 这个群没消息」**，不是「异常会冒泡」：假源里
    群 100 与「一个没有任何消息的群」在**返回空 `GroupRead`** 这一点上完全
    同形，唯一把它们分开的就是"抛不抛"。所以对照组的假源返回的正是**空**
    `GroupRead`——若哪天有人把契约改成"读不到返回空 `GroupRead`"，第一条会
    静默退化回退 0（正是那个破洞），而这个对照组的退 0 会**看不出**差别，
    修改者必须回来读这段注释才明白为什么不能那么改。

    变异「源把 `raise` 改回 `return GroupRead([])`」→ 第一条红。
    """
    from vigil import lock  # noqa: F401  —— 只为与下面那条锁用例同款地说明锁

    monkeypatch.setattr(
        cli, "_open_source", lambda config: _FakeSource(unreadable=(100,))
    )

    assert cli.main(["export"]) == 1, (
        "整群读不到的 ingest 报了成功——任务计划看到 exit 0，而那个群一条都没读进来"
    )
    assert "[失败] 1 个群整群读不到" in capsys.readouterr().out

    # ── 对照组：同一个假源、只是不抛 ⇒ 退 0 ──────────────────────────
    monkeypatch.setattr(cli, "_open_source", lambda config: _FakeSource())
    assert cli.main(["export"]) == 0


# ── F4 的第四条写库命令：`deadline-audit --apply` 也要上锁（终审 U-3/M-1）──
#
# F4 的裁定是「人工命令也要上锁」（R71），但实现的是**按名字枚举**的三个
# （export/refine/digest）。写库路径还有**第四条**：`cmd_deadline_audit` →
# `store.clear_deadlines`（它自己 commit）。终审实测（锁被持有时）：
# `export` rc=2（被保护），`deadline-audit --apply` rc=**0** 且库**仍被改**。


def _seed_unverifiable_deadline(db) -> int:
    """造一个导出库，里面有一条「截止日在源文里找不到依据」的 item。

    返回那个 `deadline_ts`——「库被改没被改」就靠它判。
    """
    import datetime as dt
    import sqlite3

    from vigil import store
    from vigil.store import ExtractedItem

    db.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(db))
    store.ensure_schema(conn)
    # `messages` 表按 web/export 产出的形状建（ensure_schema 不管它）
    conn.executescript(
        "CREATE TABLE IF NOT EXISTS messages (msg_id INTEGER PRIMARY KEY,"
        " group_id INTEGER NOT NULL, ts INTEGER NOT NULL, sender_uid TEXT,"
        " content TEXT NOT NULL);"
    )
    ts = int(dt.datetime(2026, 9, 26).timestamp())
    with store.transaction(conn):
        store.save_items(
            conn,
            [ExtractedItem(
                kind="notice", title="某通知", detail=None, event_ts=ts,
                deadline_ts=ts, group_id=100, actor_uid=None, place=None,
                links=(), amount=None, confidence=0.9, src_msg_ids=(1,),
            )],
            model="m", prompt_ver="v2", commit=False,
        )
        conn.execute(
            "INSERT INTO messages (msg_id, group_id, ts, sender_uid, content)"
            " VALUES (1, 100, ?, 'u1', '这条正文里没有任何日期')",
            (ts,),
        )
    conn.close()
    return ts


def _deadline_ts_of(db, item_id: int = 1):
    import sqlite3

    conn = sqlite3.connect(str(db))
    try:
        return conn.execute(
            "SELECT deadline_ts FROM items WHERE item_id = ?", (item_id,)
        ).fetchone()[0]
    finally:
        conn.close()


def test_deadline_audit_apply_returns_2_and_leaves_the_db_alone_when_locked(
    cli_env, monkeypatch, capsys
):
    """⭐ `deadline-audit --apply` 撞车 ⇒ **2**，且**库一个字没动**。

    终审实测（锁被持有时）：`export` rc=2（被保护），而 `deadline-audit --apply`
    rc=**0** 且 `items.deadline_ts` 被置 NULL——「所有写库命令都上锁」这条 M4
    承诺此前只覆盖**按名字枚举**的三个。而它对库的写**不可逆**（核验判错就
    把真截止日抹了），所以「锁在保护、它照写」是这条里最重的一半。

    变异「去掉 `cmd_deadline_audit` 里的 `with lock.SingleInstance():`」→
    本条红，且报告里**两个数一起现形**（实测 `(0, None) == (2, ts)`）：
    退出码 0 而不是 2，**并且库已经被改了**。
    """
    from vigil import lock

    db = cli_env / "export.db"
    ts = _seed_unverifiable_deadline(db)
    monkeypatch.setattr(cli, "_load_config_only", lambda: _config(cli_env))
    monkeypatch.setattr(cli, "_require_export_db", lambda config: db)

    assert _deadline_ts_of(db) == ts, "造场景失败：这条的截止日一开始就该在"

    with lock.SingleInstance(lock.LOCK_PATH):     # 「另一个实例」持着同一把锁
        rc = cli.main(["deadline-audit", "--apply"])
    after = _deadline_ts_of(db)

    # ⚠️ 两个事实**合成一条断言**：分成两条的话，前一条一红后一条就再也不跑，
    # 而「库被改了没」恰恰是这里最重的一半（deadline_ts 置 NULL 不可逆）。
    assert (rc, after) == (2, ts), (
        f"锁被持有时 rc={rc}（应当是 2），deadline_ts={after}（应当是 {ts}，"
        f"即**一个字都没改**）——退 0 + 库被改说明这把锁根本没保护到它"
    )
    # 退出码是给任务计划的，人还要看得见「为什么没跑」——与其它三个写库
    # 命令同一句话术，且**不许**静默退 2
    assert "[跳过]" in capsys.readouterr().out

    # 锁放掉后照常：真的写，且报出改了几条
    assert cli.main(["deadline-audit", "--apply"]) == 0
    assert _deadline_ts_of(db) is None


def test_deadline_audit_dry_run_is_read_only_so_it_does_not_take_the_lock(
    cli_env, monkeypatch
):
    """默认的 `deadline-audit`（dry-run）**不上锁**——这是判断，不是遗漏。

    它是 `read`/`who`/`media`/`groups` 那一族的**人工即时查询**：只有 SELECT
    和 print。`lock.SingleInstance` 的语义是「同一时刻只许一个**写库**的
    vigil 进程」（见 `lock.py` 的模块 docstring）；给只读检查上锁会凭空造出
    一个**假拒绝**（rc=2），而 2 的约定含义是「已经有实例在写」。

    所以锁**只在 `--apply` 那一刻**取，理由写在 `cmd_deadline_audit` 里。

    变异「把 `with lock.SingleInstance():` 提到函数开头（dry-run 也上锁）」
    → 本条红（rc 会变成 2）。
    """
    from vigil import lock

    db = cli_env / "export.db"
    ts = _seed_unverifiable_deadline(db)
    monkeypatch.setattr(cli, "_load_config_only", lambda: _config(cli_env))
    monkeypatch.setattr(cli, "_require_export_db", lambda config: db)

    with lock.SingleInstance(lock.LOCK_PATH):
        assert cli.main(["deadline-audit"]) == 0, (
            "只读的核验被锁挡住了——那不是「有人在写」，是假拒绝"
        )
    assert _deadline_ts_of(db) == ts, "dry-run 一个字都不许改库"


def test_soft_deleted_item_still_shows_up_in_deadline_audit(cli_env, monkeypatch, capsys):
    """⑤ `deadline-audit` **刻意不接 overlay**——这个决定必须有守卫（T5 审查的结论）。

    它是**对底层数据的核验**（「这条 item 的截止日在源文里有没有依据」），不是视图：
    它就该看真实的 `items`——包括已软删的那些。软删只是「界面上别显示」，
    源文与截止日的矛盾**照样存在**，核验命令看不到它才是错的。

    ⚠️ 这条守卫还替一个**更重的**事实站岗：`cli.py` 里那个
    `SELECT title FROM items WHERE item_id = ?` 后面直接是 `.fetchone()[0]`。
    T5 审查实测：**给 `items_with_deadline` 接上 overlay 反而会崩**——
    （T8 fix loop **复跑确认**了这条：把标题查询接上 overlay 并过滤软删后，
    本条红在 `TypeError: 'NoneType' object is not subscriptable`，
    `vigil/cli.py:525`——就是下面那句 `.fetchone()[0]`。见 task-8-report.md 的 E5。）
    软删之后这条查询返回 `None`，`[0]` 抛 `TypeError`。
    所以「不接」不是遗漏，是判断；而判断需要有东西钉住。

    变异「在 `cmd_deadline_audit` 的查询里过滤软删」→ 本条红
    （标题不再出现在输出里）。
    """
    import sqlite3 as _sq

    from vigil import overrides, store

    db = cli_env / "export.db"
    _seed_unverifiable_deadline(db)
    monkeypatch.setattr(cli, "_load_config_only", lambda: _config(cli_env))
    monkeypatch.setattr(cli, "_require_export_db", lambda config: db)

    overrides.ensure_schema()
    w = overrides.connect()
    try:
        overrides.delete_item(w, item_id=1, msg_id=1, actor="test")
    finally:
        w.close()

    # **效果对照**：软删在视图层确实生效——否则下面那句「照旧出现」证明不了因果。
    # ⚠️ 用 `window_items`（日报的读路径，overlay 感知）而不是 `get_item`：
    # 后者会 JOIN `sender_names`，而 `_seed_unverifiable_deadline` 只建了 `messages`
    # （那是 export.py 的产物）——那会变成一条与本条要守的东西无关的红。
    ro = _sq.connect(f"file:{db.as_posix()}?mode=ro", uri=True)
    try:
        visible = [w.item_id for w in store.window_items(ro, since=0, until=2**31)]
    finally:
        ro.close()
    assert 1 not in visible, "软删没生效——这条守卫是空的"

    assert cli.main(["deadline-audit"]) == 0
    out = capsys.readouterr().out
    assert "某通知" in out, (
        "deadline-audit 看不到被软删的条目了。它刻意不接 overlay（对底层数据的核验）："
        "软删只是「界面别显示」，源文与截止日的矛盾照样存在"
    )


# ── repass：存量重判（默认只出清单；--apply 才写库）────────────────
#
# ⚠️ 这一组守的是**退出码与锁的契约**，不是重判逻辑本身
# （逻辑在 `tests/test_repass.py`）。理由与 export/refine/digest：退出码是
# 无人值守下唯一还活着的信号，而它是**接线**属性——重判算得再对，
# 命令返回 0 就等于没报警。


def _repass_result(*, errors=(), to_delete=(1, 2), to_keep=(3,)):
    from vigil.repass import RepassPlan, RepassStats

    return (
        RepassPlan(to_delete=list(to_delete), to_keep=list(to_keep)),
        RepassStats(scanned_msgs=10, batches=1, errors=list(errors)),
    )


def test_repass_dry_run_takes_no_lock_and_passes_apply_false(
    cli_env, monkeypatch, capsys
):
    """默认（不 `--apply`）**只出清单**：不上锁，且明确传 `apply=False`。

    ⚠️ 与 `deadline-audit` 的 dry-run 同一取舍：它是人工即时查询那一族的，
    给只读检查上锁会凭空造出一个**假拒绝**（rc=2），而 2 的约定含义是
    「已经有实例在写」。

    变异「把 `with lock.SingleInstance():` 提到函数开头（dry-run 也上锁）」
    → 本条红；变异「默认 `apply=True`」→ 本条红（`seen["apply"]` 是 True）。
    """
    from vigil import lock

    seen: dict = {}

    def fake(config, **kw):
        seen.update(kw)
        return _repass_result()

    monkeypatch.setattr("vigil.repass.repass", fake)

    with lock.SingleInstance(lock.LOCK_PATH):   # 「另一个实例」持着同一把锁
        rc = cli.main(["repass"])

    assert rc == 0, "只出清单的重判被锁挡住了——那不是「有人在写」，是假拒绝"
    assert seen["apply"] is False, "默认必须**只出清单**：存量重判不可逆"
    out = capsys.readouterr().out
    assert "item 1" in out and "item 2" in out, "清单要真的打出来给人看"


def test_repass_apply_returns_2_and_never_runs_when_the_lock_is_held(
    cli_env, monkeypatch, capsys
):
    """⭐ `repass --apply` 撞车 ⇒ **2**，且**根本没有跑**（更别说写库）。

    ⚠️ 「没跑」和「跑了但没写」是两件事：这里是前者——`repass()` 一次都没被调用。
    拿到锁之后再跑一次是**阳性对照**：证明 2 来自锁，不是来自「这个命令永远退 2」。
    """
    from vigil import lock

    calls: list[dict] = []

    def fake(config, **kw):
        calls.append(kw)
        return _repass_result()

    monkeypatch.setattr("vigil.repass.repass", fake)

    with lock.SingleInstance(lock.LOCK_PATH):
        rc = cli.main(["repass", "--apply"])

    assert (rc, calls) == (2, []), (
        f"锁被持有时 rc={rc}（应当是 2）、repass() 调用 {len(calls)} 次"
        f"（应当是 0）——退 0 或照样跑说明这把锁没保护到它"
    )
    assert "[跳过]" in capsys.readouterr().out, "为什么没跑要看得见，不许静默退 2"

    assert cli.main(["repass", "--apply"]) == 0
    assert calls and calls[0]["apply"] is True


def test_repass_plan_prints_deferred_and_downgrades_as_not_executed(
    cli_env, monkeypatch, capsys
):
    """⭐ `--subset` 挡下的条目与「本轮不执行的降级」都必须**显式打出来**。

    混着不报的话，「只删了 46 条」会被读成「只有 46 条该删」，
    而「降级 4 条」会被读成「降级做了」——两者都是假话。

    变异：删掉 `if plan.deferred:` 那一段 ⇒ 本条红。
    """
    plan, stats = _repass_result()
    plan.deaths = {1: "a2", 2: "a0"}
    plan.deferred = [2]
    plan.downgrades = [(3, None, None)]
    stats.downgrades_deferred = 1
    stats.tombstones = 1
    seen: dict = {}

    def fake(config, **kw):
        seen.update(kw)
        return plan, stats

    monkeypatch.setattr("vigil.repass.repass", fake)

    assert cli.main(["repass", "--subset", "a2"]) == 0

    out = capsys.readouterr().out
    assert "死法拆分" in out and "a2 1 条" in out and "a0 1 条" in out
    assert "本轮不动" in out and "item 2" in out
    assert "本轮不执行" in out and "字段降级" in out
    # ⚠️ **接线**也要钉：`--subset` 必须真的传进 `repass()`。
    # 只断言打印的话，`subset=args.subset` → `subset=None` 这种变异**全绿**
    # ——而那意味着 a0 的 53 条会一起被删（安全相关的静默失效）。
    assert seen["subset"] == "a2", "--subset 没接进 repass()：过滤开关是死的"
    assert seen["audit_path"], "默认也要落审计文件"


def test_repass_returns_nonzero_when_a_batch_failed(cli_env, monkeypatch):
    """⭐ 有批次失败 ⇒ 非零：那一批这一轮**没有取得判定**（勘误 E5），
    清单因此是**不完整**的——这件事需要人来看，不许报成功。

    变异「`return 1 if stats.errors else 0` → `return 0`」→ 本条红。
    """
    monkeypatch.setattr(
        "vigil.repass.repass",
        lambda *a, **kw: _repass_result(errors=["第 1 批失败: HTTP 503"]),
    )
    assert cli.main(["repass"]) != 0


def test_repass_returns_zero_when_no_batch_failed(cli_env, monkeypatch):
    """阳性对照：一批都没错 ⇒ 0（挡「无条件 return 1」——报警器被无视的第一个原因）。"""
    monkeypatch.setattr("vigil.repass.repass", lambda *a, **kw: _repass_result())
    assert cli.main(["repass"]) == 0


def test_repass_plan_reports_the_three_safety_buckets_separately(
    cli_env, monkeypatch, capsys
):
    """三只「不删」的桶必须**分开**报出来。

    混成一句「保留 N 条」，读的人就分不清「判过了、该留」与「根本没判」——
    而后者意味着这次重判的结果**不能全信**（勘误 E5）。

    变异「把 `plan.unjudged` 并进 `to_keep` 的计数里」→ 本条红。
    """
    plan, stats = _repass_result()
    plan.skipped_referenced = [7]
    plan.skipped_soft_deleted = [8]
    plan.unjudged = [9]
    monkeypatch.setattr("vigil.repass.repass", lambda *a, **kw: (plan, stats))

    cli.main(["repass"])

    out = capsys.readouterr().out
    for iid in (7, 8, 9):
        assert f"item {iid}" in out, f"item {iid} 没被单独报出来——三只桶被并了"
    assert "没判成" in out and "软删" in out and "日报引用" in out


# ── M5 终审 Important-3：字段降级路径（`vigil downgrade`）──────────────
#
# `store.downgrade_item_fields` 此前**零调用方**，于是 M5 出口判据 3
# （「不再存在 `deadline_ts < event_ts` 的条目」）没有所有者：真库 item 83
# 一直是「event 2026-08-05 / deadline 2026-05-06」，`ItemCard` 在 8 月的
# 条目上渲染「⏰ 05-06 截止」。


def _seed_downgradable(db) -> dict:
    """造一个导出库，里面四种条目各一条（判据要能分开看）。

    * `1` 倒挂的截止日 + 源文里找不到依据的 place ⇒ **两条都该降级**
    * `2` 正常的截止日（源文里有 `9月26日`）+ 有依据的 place ⇒ **一个字都不许动**
    * `3` 被软删、且同样倒挂 ⇒ **跳过**（用户自己隐藏的行，不替他改）
    * `4` 没有源消息、deadline 正常 ⇒ **不许动**（无来源只影响 place 判据）
    """
    import datetime as dt
    import sqlite3

    from vigil import store
    from vigil.store import ExtractedItem

    db.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(db))
    store.ensure_schema(conn)
    conn.executescript(
        "CREATE TABLE IF NOT EXISTS messages (msg_id INTEGER PRIMARY KEY,"
        " group_id INTEGER NOT NULL, ts INTEGER NOT NULL, sender_uid TEXT,"
        " content TEXT NOT NULL);"
    )
    event = int(dt.datetime(2026, 9, 20).timestamp())
    past = int(dt.datetime(2026, 5, 6).timestamp())
    with store.transaction(conn):
        store.save_items(
            conn,
            [
                ExtractedItem(
                    kind="notice", title="档案袋封口时间：5 月 6 日", detail=None,
                    event_ts=event, deadline_ts=past, group_id=100, actor_uid=None,
                    place="立德楼", links=(), amount=None, confidence=0.9,
                    src_msg_ids=(1,),
                ),
                ExtractedItem(
                    kind="notice", title="报名截止", detail=None,
                    event_ts=event, deadline_ts=event, group_id=100, actor_uid=None,
                    place="宿舍", links=(), amount=None, confidence=0.9,
                    src_msg_ids=(2,),
                ),
                ExtractedItem(
                    kind="notice", title="软删掉的倒挂条目", detail=None,
                    event_ts=event, deadline_ts=past, group_id=100, actor_uid=None,
                    place=None, links=(), amount=None, confidence=0.9,
                    src_msg_ids=(3,),
                ),
                ExtractedItem(
                    kind="notice", title="没有来源的条目", detail=None,
                    event_ts=event, deadline_ts=event, group_id=100, actor_uid=None,
                    place=None, links=(), amount=None, confidence=0.9,
                    src_msg_ids=(),
                ),
            ],
            model="m", prompt_ver="v3", commit=False,
        )
        conn.executemany(
            "INSERT INTO messages (msg_id, group_id, ts, sender_uid, content)"
            " VALUES (?, 100, ?, 'u1', ?)",
            [
                (1, event, "档案袋封口时间：5 月 6 日"),
                # 这一条的源文里既有 `9月26日`（给 deadline 背书）也有 `宿舍`
                (2, event, "宿舍报名 9月26日 截止"),
                (3, event, "软删条的正文"),
            ],
        )
    conn.close()

    from vigil import overrides

    ov = overrides.connect()
    try:
        overrides.delete_item(ov, item_id=3, msg_id=3)
    finally:
        ov.close()
    return {"event": event, "past": past}


def _item_row(db, item_id: int):
    import sqlite3

    conn = sqlite3.connect(str(db))
    try:
        return conn.execute(
            "SELECT place, deadline_ts FROM items WHERE item_id = ?", (item_id,)
        ).fetchone()
    finally:
        conn.close()


def test_downgrade_dry_run_lists_changes_and_does_not_write(cli_env, monkeypatch, capsys):
    """默认**只出清单**（不可逆操作事前可见——`repass` / `deadline-audit` 的同一惯例）。

    ⚠️ 只读的清单**不上锁**：给只读检查上锁会凭空造出一个假拒绝（rc=2），
    而 2 的约定含义是「已经有实例在写」（与 `deadline-audit` 同一条判断）。
    """
    from vigil import lock

    db = cli_env / "export.db"
    seen = _seed_downgradable(db)
    monkeypatch.setattr(cli, "_load_config_only", lambda: _config(cli_env))
    monkeypatch.setattr(cli, "_require_export_db", lambda config: db)

    with lock.SingleInstance(lock.LOCK_PATH):
        assert cli.main(["downgrade"]) == 0, "只读的清单被锁挡住了——那是假拒绝"

    out = capsys.readouterr().out
    assert "item 1" in out and "倒挂" in out
    assert "item 3" in out and "软删" in out, "被跳过的软删条目必须报出来，不许静默略过"
    assert _item_row(db, 1) == ("立德楼", seen["past"]), "dry-run 一个字都不许改库"


def test_downgrade_apply_clears_the_inverted_deadline_and_reports_it(
    cli_env, monkeypatch, capsys
):
    """⭐ `--apply` 真写：倒挂的 deadline 置 NULL，**没问题的条目不碰**。

    变异「把 `store.downgrade_item_fields` 换成 `store.clear_deadlines`」⇒
    本条红（place 那一列不会跟着降级）；变异「把 `scope` 里的软删过滤去掉」⇒
    软删那条会被改，而下面那句断言它**没被改**。

    ⚠️ **显式带 `--fields both`**（M5 收尾 A1 之后）：命令行默认值已收窄成
    `deadline`，而本条要守的正是「两列一起降级」这条路径 ⇒ 范围必须写出来。
    断言一个字未改（`(None, None)` 那条仍钉着 place 也被降级）。
    """
    db = cli_env / "export.db"
    seen = _seed_downgradable(db)
    monkeypatch.setattr(cli, "_load_config_only", lambda: _config(cli_env))
    monkeypatch.setattr(cli, "_require_export_db", lambda config: db)

    # **效果对照**：软删在视图层确实生效（否则「item 3 没被改」证明不了因果）
    import sqlite3 as _sq

    from vigil import store as _store

    ro = _sq.connect(f"file:{db.as_posix()}?mode=ro", uri=True)
    try:
        visible = [w.item_id for w in _store.window_items(ro, since=0, until=2**31)]
    finally:
        ro.close()
    assert 3 not in visible and 1 in visible, "造场景失败：软删没生效，这条守卫是空的"

    assert cli.main(["downgrade", "--fields", "both", "--apply"]) == 0
    out = capsys.readouterr().out

    assert _item_row(db, 1) == (None, None), "倒挂的 deadline 该被降级（place 也在内）"
    assert _item_row(db, 2) == ("宿舍", seen["event"]), "过了闸门的条目被误改"
    assert _item_row(db, 3) == (None, seen["past"]), "被软删的条目被改了（D15：不替用户改）"
    assert _item_row(db, 4) == (None, seen["event"]), "没有来源的条目被误改（deadline 是好的）"
    # 判据自负：命令自己要报出复核结果，而不是等人去查
    assert "剩 1 条" in out, out
    # 复核：判据 SQL 数的是**全部** items（含软删）⇒ 还剩 item 3 那一条
    import sqlite3

    conn = sqlite3.connect(str(db))
    try:
        left = conn.execute(
            "SELECT COUNT(*) FROM items"
            " WHERE deadline_ts IS NOT NULL AND deadline_ts < event_ts"
        ).fetchone()[0]
    finally:
        conn.close()
    assert left == 1, "非软删的倒挂条目应当清零（剩的那条是被软删、刻意不动的）"


def test_downgrade_apply_returns_2_and_leaves_the_db_alone_when_locked(
    cli_env, monkeypatch, capsys
):
    """撞车 ⇒ **2** + 库一个字没动（与 `deadline-audit --apply` 同一套契约）。

    变异「去掉 `cmd_downgrade` 里的 `with lock.SingleInstance():`」⇒ 本条红，
    且两个数一起现形（rc 0 而不是 2，**且库已经被改**）。
    """
    from vigil import lock

    db = cli_env / "export.db"
    seen = _seed_downgradable(db)
    monkeypatch.setattr(cli, "_load_config_only", lambda: _config(cli_env))
    monkeypatch.setattr(cli, "_require_export_db", lambda config: db)

    with lock.SingleInstance(lock.LOCK_PATH):
        rc = cli.main(["downgrade", "--apply"])
    after = _item_row(db, 1)

    assert (rc, after) == (2, ("立德楼", seen["past"])), (
        f"锁被持有时 rc={rc}（应当是 2）、item 1={after}——退 0 + 库被改说明锁没保护到它"
    )
    assert "[跳过]" in capsys.readouterr().out


def test_downgrade_fields_deadline_leaves_place_alone(cli_env, monkeypatch):
    """`--fields deadline` 只动那一列（真库执行时用的就是这个范围）。"""
    db = cli_env / "export.db"
    _seed_downgradable(db)
    monkeypatch.setattr(cli, "_load_config_only", lambda: _config(cli_env))
    monkeypatch.setattr(cli, "_require_export_db", lambda config: db)

    assert cli.main(["downgrade", "--fields", "deadline", "--apply"]) == 0
    assert _item_row(db, 1) == ("立德楼", None), "只该降 deadline，place 不许动"


# ── M5 收尾 scoped re-review A1：`--fields` 的破坏性默认值 ──────────────
#
# 修复前默认 `both`：照 `--apply` 的惯例敲一遍、**不带 flag**，就会连带把
# place 也置 NULL——而用户 2026-09-17 只批准了 deadline 那一次。
# 真库操作不可逆，这种「顺手多清一列」没有任何一步会报错。


def test_downgrade_fields_default_is_deadline_not_both(cli_env, monkeypatch):
    """⭐ 解析层：`--fields` 的默认值逐字 == `deadline`。

    ⚠️ 守的是**默认值本身**，不是某次运行的结果——用 `cmd_downgrade` 的替身
    把解析出来的 `args.fields` 抓出来。变异「把 `default=` 改回 `"both"`」⇒ 本条红。
    ⚠️ 与下面那条行为用例是**两层**：这条钉死解析结果、那条钉死「这个范围
    真的写不进 place」。只有这条的话，「解析器默认对了、代码里却把 `fields`
    又合并成 `both`」这种改法照样全绿。
    """
    seen = {}

    def spy(args) -> int:
        seen["fields"] = args.fields
        return 0

    monkeypatch.setattr(cli, "cmd_downgrade", spy)
    assert cli.main(["downgrade"]) == 0, "不带任何 flag 的干跑必须仍然可用"
    assert seen["fields"] == "deadline", (
        f"`--fields` 的默认值是 {seen['fields']!r}——"
        "默认必须是收窄的那一侧：不带 flag 跑 --apply 不可能意外清掉 place"
    )


def test_downgrade_apply_without_fields_never_clears_place(cli_env, monkeypatch, capsys):
    """⭐ 行为层（A1 的判据）：**不带 `--fields`** 跑 `--apply` 清不掉 place。

    判据分两步走，缺一不可：
      ① 不带 flag ⇒ item 1 的 place 一个字不动（只有 deadline 被降）；
      ② **阳性对照**：同一条目在 `--fields both` 下确实会被降 place
         ——没有 ②，这条在「place 判据整个失效」时也照样绿（空守卫）。
    变异「把默认值改回 `both`」⇒ ① 红；变异「place 闸门失效」⇒ ② 红。
    """
    db = cli_env / "export.db"
    _seed_downgradable(db)
    monkeypatch.setattr(cli, "_load_config_only", lambda: _config(cli_env))
    monkeypatch.setattr(cli, "_require_export_db", lambda config: db)

    # ① 不带 --fields
    assert cli.main(["downgrade", "--apply"]) == 0
    out = capsys.readouterr().out
    assert _item_row(db, 1) == ("立德楼", None), (
        "不带 --fields 的 --apply 动了 place——默认值是破坏性的那一侧（A1 原样复发）"
    )
    assert "降级字段：deadline" in out, (
        f"范围没有打给用户看，清单与执行的口径无从对照：{out!r}"
    )

    # ② 阳性对照：这条目**确实**过不了 place 闸门（源文「档案袋封口时间：5 月 6 日」
    #    里没有「立德楼」）⇒ ① 的「没被改」是默认值挡住的，不是判据本来就动不了它。
    assert cli.main(["downgrade", "--fields", "both", "--apply"]) == 0
    assert _item_row(db, 1) == (None, None), (
        "阳性对照失败：place 都没有被降级 ⇒ ① 是空守卫（判据可能整个失效了）"
    )


# ── groups：与 export 抢同一个缓存文件 ⇒ 上锁（U-4，M6 T1）────────


def test_groups_refuses_to_run_while_another_instance_holds_the_lock(
    cli_env, monkeypatch, capsys
):
    """⭐ U-4 的另一半：`groups` 撞上正在跑的 export 时**响亮失败**，不静默并发。

    ⚠️ 变异反证：把 `cmd_groups` 里的 `with lock.SingleInstance():` 去掉
    ⇒ 本条**必红**（会真的去读 QQ 库并返回 0）。

    ⚠️ 判据必须是「退出码 2」而不是「抛了 AlreadyRunning」：退出码是
    任务计划与用户唯一能看的东西（`cmd_daily` / `cmd_export` 都是这个契约）。
    ⚠️ 但**只有退出码是不够的**（M6 终审 #4）：同文件里 `repass` / `export` /
    `daily` / `deadline-audit` 的同类用例**两者都钉**（`rc == 2` + 那行
    `[跳过] …`）——"响亮地失败"的**机械形式**正是那行输出，只钉退出码的话，
    一个"退 2 但一个字都不说"的实现照样绿，而用户对着任务计划的
    「上次运行结果：2」无从知道"是有人在跑、不是我坏了"。
    """
    from vigil import lock

    def boom(self):
        raise lock.AlreadyRunning("已有实例在跑（锁：x；上次写入：pid=123）")

    monkeypatch.setattr(lock.SingleInstance, "__enter__", boom)
    rc = cli.main(["groups"])
    assert rc == 2
    assert "[跳过]" in capsys.readouterr().out, "退 2 却一个字都没说：用户无从知道为什么没跑"


# ── groups：末尾顺手写群清单快照（M6 T4；复审 Important-2 补的覆盖）────


def test_groups_writes_the_snapshot_that_the_web_side_reads(cli_env, monkeypatch):
    """⭐ `vigil groups` 必须把群清单快照写到 `groups_snapshot.default_path(config)`。

    ⚠️ 为什么非要有守卫（M6 T4 复审 Important-2）：快照是 Web「配置」页**唯一**
    的只读数据源（spec §1.6：Web 永不接触 QQ 库、不拿密钥），而它只有两个
    生产者（`vigil groups` 与 `vigil ingest`）。把这一段整块删掉 ⇒ 界面永远
    停在「还没有快照，请先跑一次 ingest」——**而全量 663 条没有一条会红**
    （复审已实测）。

    ⚠️ 数据源整块换成 `_FakeSource`（`list_groups` / `group_names` 是它的两个
    固定答案）：本用例测的是**那一小段写快照的代码**，不是"读 QQ 库"——真
    `open_source('qq')` 需要库里没有的私有模块 `qq_source.py`。这里把源换成
    假源，就恰好把那一段**收敛成唯一变量**：掌上拿到的 `counts` / `names` 是
    钉死的，快照内容对不对就只看 `collect()` 拼得对不对。
    """
    from vigil import groups_snapshot

    # ⚠️ 打 `cli._open_source` 而不是 `source.open_source`：`cmd_groups` 是
    # 经 `cli._open_source` 这个中间名取源的，打模块函数够不着那层间接。
    monkeypatch.setattr(cli, "_open_source", lambda config: _FakeSource())

    assert cli.main(["groups"]) == 0

    snap = groups_snapshot.load(cli_env / "groups-snapshot.json")
    assert snap is not None, (
        "`vigil groups` 没写快照 ⇒ Web 的配置页永远停在「还没有快照」"
        "（而快照只有它和 `vigil ingest` 两个生产者）"
    )
    assert snap.groups == (
        groups_snapshot.GroupInfo(
            id=100, name="班级群", messages=7, last_ts=1_700_000_000
        ),
    ), "快照内容与 `collect()` 的拼装不符"


def test_groups_survives_a_snapshot_failure_that_is_not_an_oserror(
    cli_env, monkeypatch, capsys
):
    """⭐ 「快照写失败只警告不中断」必须覆盖**非磁盘类**失败（复审 Minor-2）。

    ⚠️ 原实现只捕 `OSError`（实测：`collect()` 抛 `TypeError` 会**冒出**
    `_cmd_groups_locked`、整条命令中断）——而 `except OSError` 与 `ingest()` 的
    `except Exception` 宽窄不一致，本文件已经把后者写成了规矩。
    对用户来说「磁盘满」与「counts 形状不对」是同一条处置：**表格照打、退出码 0**。

    ⚠️ 判据三条一起：rc=0、**表格还在**（`stdout` 非空——复审实测过坏形状下
    stdout 为空）、并且**说出了那句警告**（静默降级在本项目是不许的）。
    """
    from vigil import groups_snapshot

    # 源照旧用 `_FakeSource`：本条要的是「快照写失败」，不是「读库失败」——
    # 读库那条路必须**正常走通**，否则表格打不出来，"表格照打"这条判据就悬空了。
    monkeypatch.setattr(cli, "_open_source", lambda config: _FakeSource())

    def boom(counts, names):
        raise TypeError("counts 的形状不对")

    monkeypatch.setattr(groups_snapshot, "collect", boom)

    assert cli.main(["groups"]) == 0, "非磁盘类失败不许把整条 `vigil groups` 中断"
    captured = capsys.readouterr()
    assert "消息数" in captured.out, "表格必须照打（快照失败不该影响主业）"
    assert "[警告] 群清单快照写不出去" in captured.err, (
        "失败必须说出来——静默降级等于把「快照没写成」藏起来"
    )


# ── M6 T7：`vigil config export / import` ───────────────────────────


@pytest.fixture
def pack_env(monkeypatch, tmp_path):
    """把 `vigil config export/import` 的三个落点全挪进 tmp 根。

    ⚠️ **三个都要挪，一个都不能省**（`cmd_config_import` 会**写**它们）：
      · `cli.REPO_ROOT` —— groups.toml 的落点（`cli.py` 里是 import 期绑定的名字）；
      · `categories.DEFAULT_CATEGORIES` —— `conftest.py` 的 autouse 夹具已经挪过，
        这里**显式再挪一次**是为了不依赖"另一个文件里的夹具恰好还在"；
      · `config.DEFAULT_PERSONS` —— ⛔ 不做这一步，`config import` 会**整体覆盖
        真实的 `config/persons.toml`**（用户真实名单，`data/` 之外，没有备份）。
        `load_persons()` / `save_persons(x, path)` 都是**调用时**查这个模块全局，
        所以 `monkeypatch.setattr(config, "DEFAULT_PERSONS", ...)` 够得着。
    """
    from vigil import categories as cats_mod, config as config_mod

    root = tmp_path / "repo"
    (root / "config").mkdir(parents=True)
    (root / "data").mkdir()
    qq = tmp_path / "qq"
    qq.mkdir()
    cats = root / "config" / "categories.toml"
    cats.write_text(
        '[[categories]]\nslug = "notice"\nlabel = "通知"\ndesc = "正式通知"\n',
        encoding="utf-8",
    )
    persons = root / "config" / "persons.toml"
    monkeypatch.setattr(cli, "REPO_ROOT", root)
    monkeypatch.setattr(cats_mod, "DEFAULT_CATEGORIES", cats)
    monkeypatch.setattr(config_mod, "DEFAULT_PERSONS", persons)
    cfg = config_mod.Config(
        source_dir=qq,
        output_db=root / "data" / "vigil.db",
        groups=(config_mod.Group(id=111, name="甲群"),),
    )
    monkeypatch.setattr(cli, "_load_config_only", lambda: cfg)
    return root


def _pack_with(cats, persons=(), aliases=()):
    """用 `pack_env` 那份本机配置渲一个包文本（导出侧的另一半：手写一个包）。"""
    from vigil import configpack

    return configpack.build(
        config=cli._load_config_only(),
        cats=cats,
        persons=persons,
        aliases=aliases,
    )


def test_config_export_writes_a_pack_that_parses(pack_env):
    """⭐ 导出的是一个**能被自己读回来**的包（三份配置 + 别名）。

    ⚠️ 断言「读回来」，不是「文件存在」：`export` 走的是 `write_pack` →
    `build` → `atomic_write_text`，只断言存在的话，**把 `build` 换成空串也全绿**。
    """
    import pathlib

    from vigil import configpack

    out = pack_env / "pack.toml"
    assert cli.main(["config", "export", "--out", str(out)]) == 0

    pack = configpack.parse(out.read_text(encoding="utf-8"), pathlib.Path("pack.toml"))
    assert [g.id for g in pack.config.groups] == [111]
    assert [c.slug for c in pack.categories] == ["notice"]
    assert pack.aliases == ()


def test_config_export_defaults_the_output_next_to_the_db(pack_env):
    """不给 `--out` 时落在 `output_db.parent` 下（数据目录，不污染仓库）。"""
    import datetime as dt
    import pathlib

    assert cli.main(["config", "export"]) == 0
    expect = pack_env / "data" / f"vigil-config-{dt.date.today().isoformat()}.toml"
    assert pathlib.Path(expect).is_file()


def test_config_import_dry_run_writes_nothing(pack_env):
    """`--dry-run` 只校验，一个文件都不许动（与 `repass` 的默认态同一条判据）。

    ⚠️ 判据是**内容没变**，不是「文件不存在」：`pack_env` 里那份
    `categories.toml` 本来就存在，而 `--dry-run` 的坏法恰恰是**整体覆盖**它。
    """
    from vigil import configpack

    src = pack_env / "pack.toml"
    src.write_text(
        _pack_with((configpack.Category(slug="life", label="生活", desc="生活"),)),
        encoding="utf-8",
    )
    before = (pack_env / "config" / "categories.toml").read_text(encoding="utf-8")

    assert cli.main(["config", "import", str(src), "--dry-run"]) == 0
    assert (pack_env / "config" / "categories.toml").read_text(encoding="utf-8") == before
    assert not (pack_env / "config" / "persons.toml").exists()


def test_config_import_writes_the_pack(pack_env):
    """真的导入：类目 / 人物都落到 `pack_env` 那个根上（不经 `--out` 那条路）。"""
    from vigil import categories as cats_mod
    from vigil import config as config_mod
    from vigil import configpack

    src = pack_env / "pack.toml"
    src.write_text(
        _pack_with(
            (configpack.Category(slug="life", label="生活", desc="生活"),),
            persons=(config_mod.Person(uin=5, label="甲"),),
        ),
        encoding="utf-8",
    )
    assert cli.main(["config", "import", str(src)]) == 0

    assert [c.slug for c in cats_mod.load_categories(cats_mod.DEFAULT_CATEGORIES)] == ["life"]
    assert [p.uin for p in config_mod.load_persons(config_mod.DEFAULT_PERSONS)] == [5]


def test_config_import_refuses_a_broken_pack_without_touching_anything(pack_env):
    """⭐ CLI 侧的「全有或全无」：包里群号形状不对 ⇒ 退出码非零、**一个文件都不写**。"""
    src = pack_env / "pack.toml"
    src.write_text(
        "schema = 1\n"
        "groups = [111, 222]\n"
        "\n"
        "[settings]\n"
        f'source_dir = "{(pack_env / "qq").as_posix()}"\n'
        f'output_db = "{(pack_env / "data" / "vigil.db").as_posix()}"\n'
        "\n"
        "[[categories]]\n"
        'slug = "life"\n'
        'label = "生活"\n'
        'desc = "生活"\n',
        encoding="utf-8",
    )
    before = (pack_env / "config" / "categories.toml").read_text(encoding="utf-8")

    # ⚠️ 判据写 `!= 0` 而不是 `== 1`：`cmd_config_import` 的报错走
    # `sys.exit("...")`（字符串退出码），`_exit_code_of` 会把它原样返回。
    assert _exit_code_of(["config", "import", str(src)]) != 0
    assert (pack_env / "config" / "categories.toml").read_text(encoding="utf-8") == before
    assert not (pack_env / "config" / "persons.toml").exists()


def test_config_export_survives_an_overlay_db_that_predates_the_alias_table(
    pack_env, monkeypatch
):
    """⭐ 冒烟实测的形状：`data/overrides.db` **在**、但 `kind_alias` 表**不在**。

    ⚠️ 真机上就是这个形状（2026-09-18 冒烟，修前）：
    `data/overrides.db` 是 M5 建的，里面只有 `item_edits` / `item_state`，
    而 `kind_alias` 是 T5 新增的表，要等某次 `overrides.ensure_schema()` 跑过才在。
    于是一条**只读**的 `vigil config export` 以 `no such table: kind_alias`
    的裸 traceback 收场——而它的本职与 overlay 毫无关系。

    ⚠️ 判据两条一起，缺一条就是空守卫：
      · **读到的是空表**（库里确实一条别名都没有 ⇒ 不是靠"抛了但被吞"蒙混）；
      · **补完之后表是齐的**（`ensure_schema` 幂等地把新表建起来），
        否则下一次调用还会踩同一颗雷。

    ⚠️ 变异反证：把 `_read_kind_aliases` 里的 `overrides.ensure_schema(target)`
    删掉 ⇒ 本条**必红**（`no such table: kind_alias`）。
    """
    import sqlite3

    from vigil import overrides

    # 造一个 M5 形状的 overlay 库：**先按真实 DDL 建齐、再把 T5 新增的两张表删掉**。
    # ⚠️ 不手抄那两张老表的列定义——抄一份会与 `_TABLES_DDL` 漂移，
    # 而漂移的方向恰好是「测试里的旧库 ≠ 真的旧库」（手抄版第一稿就是
    # `item_edits (edit_id INTEGER PRIMARY KEY)`，缺了 `item_id`，
    # 于是 `ensure_tables` 死在 `CREATE INDEX … ON item_edits(item_id)` 上
    # ——那是一次**假红**：红的是我抄错了表，不是被测的那件事）。
    vintage = pack_env / "data" / "overrides.db"
    overrides.ensure_schema(vintage)
    conn = sqlite3.connect(str(vintage))
    conn.executescript("DROP TABLE config_snapshots; DROP TABLE kind_alias;")
    conn.commit()
    tables = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    conn.close()
    assert tables == {"item_edits", "item_state"}, f"M5 形状没造对：{sorted(tables)}"
    monkeypatch.setattr(overrides, "OVERRIDES_DB", vintage)

    assert cli._read_kind_aliases() == {}, "库里没有别名 ⇒ 空表，不是异常"

    conn = sqlite3.connect(str(vintage))
    tables = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    conn.close()
    assert "kind_alias" in tables, "补表没生效——下一次导出还会踩同一颗雷"


# ── 夹具的判据（M6 T7 Fix loop 1 / I3）──────────────────────────────


def test_cli_repo_root_is_isolated_by_conftest():
    """⭐ **夹具的判据**：`cli.REPO_ROOT` 必须被挪出仓库，而且根下有一份能读通的配置。

    ⚠️ 为什么要有这条：`cli.REPO_ROOT` 是 `cli.py` **自己**的全局名字
    （`from .config import REPO_ROOT` 在 import 期绑定）⇒
    `monkeypatch.setattr(config, "REPO_ROOT", …)` / 挪 `api.REPO_ROOT` 都**够不着它**。
    不隔离时（reviewer 实测 2026-09-18）一条**不用 `pack_env`** 的用例调
    `cli.main(["config", "import", …])` 就会写**仓库根的 `config/groups.toml`**
    ——实测把 15 群改成 1 群、`persons.toml` 抹成 0 人，**而用例全绿**。
    同一个根还挂着 `cmd_repass --audit` 的默认落点（真仓库的 `data/`）。

    ⚠️ 三条断言各挡一种退化：
      · **路径在仓库外** —— 挡"夹具被删掉"（本条也是 I3 的变异反证落点）；
      · **`config/groups.toml` 存在** —— 挡"只挪根不写文件"：那样
        `--keep-settings` 会**静默走回落分支**，一整类用例测的就成了另一支；
      · **那份配置真能读通** —— 挡"写了个坏文件"（存在不等于合法）。

    ⚠️ 本用例**只读**。
    ⚠️ 变异反证：把 `conftest.py` 的 `_isolate_cli_repo_root` 改成空转
    ⇒ 本条**必红**（第一条断言）。只跑本条，**绝不许跑全量**——夹具一撤，
    别的用例就可能写进真仓库。
    """
    from vigil.config import REPO_ROOT, load_config

    root = cli.REPO_ROOT
    assert not root.resolve().is_relative_to(REPO_ROOT.resolve()), (
        f"`cli.REPO_ROOT` 还指着仓库：{root}——`config import` 会写真实的 "
        "config/groups.toml，`repass --audit` 会往真实 data/ 落文件"
    )
    local = root / "config" / "groups.toml"
    assert local.is_file(), (
        f"夹具要连 `{local}` 一起写出来：只挪根的话，`--keep-settings` 会静默"
        "走「读不出来 ⇒ 整份用包里的」那一支，用例测的就不是它名字里那件事"
    )
    assert [g.id for g in load_config(local).groups] == [111], "自带的那份必须真能读通"


def test_config_import_returns_2_when_another_instance_holds_the_lock(pack_env):
    """⭐ Minor①：`config import` 也是**写命令**（它一次写三份配置）⇒ 要上锁。

    ⚠️ 其余写命令（`export` / `refine` / `digest` / `daily` / `repass --apply`）
    **全都取** `lock.SingleInstance`；它是本组里唯一漏掉的。与正在跑的 `daily`
    撞车时，正确行为是**响亮地拒绝**（退出码 2 = 「上一次还没跑完，等着就行」），
    而不是两边各写一遍配置文件。

    ⚠️ 判据两条一起：**退出码 2** + **一个字都没写**（只判退出码的话，
    "先写后报 2" 也能全绿）。

    ⚠️ 变异反证：把 `cmd_config_import` 的 `with lock.SingleInstance():` 去掉
    ⇒ 本条**必红**（第一次调用会真的写，退出码 0）。
    """
    from vigil import configpack, lock

    src = pack_env / "pack.toml"
    src.write_text(
        _pack_with((configpack.Category(slug="life", label="生活", desc="生活"),)),
        encoding="utf-8",
    )
    before = (pack_env / "config" / "categories.toml").read_text(encoding="utf-8")

    with lock.SingleInstance(lock.LOCK_PATH):        # 「另一个实例」持着同一把锁
        assert cli.main(["config", "import", str(src)]) == 2
    assert (pack_env / "config" / "categories.toml").read_text(encoding="utf-8") == before, (
        "撞车时一个字节都不许写——先写后报 2 会留下半新半旧的配置"
    )
    assert cli.main(["config", "import", str(src)]) == 0   # 锁放掉后照常


def test_config_import_reports_a_cross_pack_conflict_readably(pack_env):
    """⭐ FIX 1a（CLI 侧，Fix loop 2）：跨包别名冲突 ⇒ **一行 `[配置错误]`**，不是 traceback。

    ⚠️ reviewer 实测的原文是「同一路径 CLI ⇒ `OverlayError` **直接冒出
    `cli.main`**」：`cmd_config_import` 的 `except ConfigError` 只罩着 `parse`，
    而 `import_pack` 在它外面 ⇒ 用户看到的是一坨 traceback（或者"退出码 1"，
    任务计划只能看见一个数字，看不见"为什么"）。

    ⚠️ 判据两条一起：**退出码是字符串**（`_exit_code_of` 对 `sys.exit(str)` 返回
    那个字符串，对未捕获异常返回 **1**——所以 `isinstance(rc, str)` 正是
    "不是 traceback"的机械形式）+ **一个字都没写**。

    ⚠️ 变异反证：把 `cmd_config_import` 里那圈 `except ConfigError` 拿掉
    ⇒ 本条**必红**（`_exit_code_of` 返回 1，且消息里没有"冲突"）。
    """
    from vigil import configpack

    base = cli._load_config_only()
    first = pack_env / "first.toml"
    second = pack_env / "second.toml"
    first.write_text(
        _pack_with(
            (configpack.Category(slug="notice", label="通知", desc="正式通知"),),
            aliases=(("life_old", "notice"),),
        ),
        encoding="utf-8",
    )
    # ⚠️ 第二份的类目**故意不同**（life）：半写会让下面的内容断言立刻现形
    second.write_text(
        _pack_with(
            (configpack.Category(slug="life", label="生活", desc="生活"),),
            aliases=(("notice", "life_old"),),
        ),
        encoding="utf-8",
    )
    assert base.groups, "夹具里的本机配置得有群（parse 要求 settings 里的目录存在）"

    assert cli.main(["config", "import", str(first)]) == 0
    before = (pack_env / "config" / "categories.toml").read_text(encoding="utf-8")

    rc = _exit_code_of(["config", "import", str(second)])
    assert isinstance(rc, str) and rc.startswith("[配置错误]"), (
        f"不是一行可读的报错（退出码 {rc!r}）——裸 traceback 会让任务计划只看见一个数字"
    )
    assert "冲突" in rc, rc
    assert (pack_env / "config" / "categories.toml").read_text(encoding="utf-8") == before, (
        "冲突时一个字节都不许写"
    )
