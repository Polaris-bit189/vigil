"""每日管线的编排测试。

⚠️ 三个阶段全部被 monkeypatch 成假的——本文件**不调模型、不碰真库**。
测的是编排语义：失败判据、跳过规则、最坏状态、账目。
"""

from __future__ import annotations

import logging
import pathlib

import pytest

from vigil import daily, logs


class _FakeConfig:
    """一个只带 ``output_db`` 的假配置。

    ⚠️ **偏离 brief（见报告 §偏差 D1）**：brief 的测试里写的是
    ``config=object()``，那样**一条都跑不过**——`daily.run()` 要读
    ``config.output_db``（进度文案一处、refine/digest 的 ``db_path`` 两处），
    而 `object()` 没有这个属性；进度文案那一处还在 `try` **外面**，会直接
    AttributeError 掀掉整轮（不是被记成阶段失败）。反过来，用真的 `Config`
    会去读 ``config/*.toml``——那是「测试不碰真库/真配置」要避免的。
    所以用这个两行替身：它证明的是「config 只是被原样传递的道具」。
    """

    output_db = pathlib.Path("unused.db")
    # ⚠️ `source` 也必须给：`daily.run()` 阶段 1 会 `open_source(config.source, …)`，
    # 而那个调用发生在**假 ingest 之前** ⇒ 缺这个属性会让阶段 1 在调假 ingest
    # 之前就 AttributeError，于是 calls 里根本没有 "export"。
    # 给了它，`open_source` 真跑一次（返回合成源，不碰网络、不碰文件），
    # 紧接着被假 ingest 顶掉。
    source = "synthetic"
    source_dir = pathlib.Path("unused_src")
    groups = ()


def _export_stats(**over):
    from vigil.ingest import IngestStats as ExportStats

    s = ExportStats()
    s.per_group = over.get("per_group", [(1, "群甲", 10)])
    s.failed_groups = over.get("failed_groups", [])
    return s


def _refine_stats(**over):
    from vigil.refine import RefineStats

    s = RefineStats()
    s.scanned = over.get("scanned", 100)
    s.sent_messages = over.get("sent", 10)
    s.candidates = over.get("candidates", 10)
    s.errors = over.get("errors", [])
    return s


def _digest_stats(**over):
    from vigil.digest import DigestStats

    s = DigestStats(day="2026-09-16", window_from=0, window_to=1)
    s.errors = over.get("errors", [])
    return s


@pytest.fixture
def wired(monkeypatch):
    """把三阶段换成可编程的假的，并记录调用顺序。"""
    calls: list[str] = []
    plan: dict[str, object] = {}

    def fake_ingest(config, src, *, on_progress=print):
        calls.append("export")
        r = plan.get("export")
        if isinstance(r, BaseException):
            raise r
        return r if r is not None else _export_stats()

    def fake_refine(config, **kw):
        calls.append("refine")
        r = plan.get("refine")
        if isinstance(r, BaseException):
            raise r
        return r if r is not None else _refine_stats()

    def fake_digest(config, **kw):
        calls.append("digest")
        r = plan.get("digest")
        if isinstance(r, BaseException):
            raise r
        return r if r is not None else _digest_stats()

    monkeypatch.setattr(daily.ingest_mod, "ingest", fake_ingest)
    monkeypatch.setattr(daily.refine_mod, "refine", fake_refine)
    monkeypatch.setattr(daily.digest_mod, "digest", fake_digest)
    return calls, plan


def test_happy_path_runs_all_three_in_order(wired):
    calls, _ = wired
    report = daily.run(config=_FakeConfig(), llm_key="l",
                       day="2026-09-16", on_progress=lambda *_: None)
    assert calls == ["export", "refine", "digest"]
    assert report.ok
    assert report.last_error == ""
    assert report.deadline == "2026-09-16"


def test_export_failure_skips_digest_but_still_refines(wired):
    """⭐ export 失败 ⇒ **跳过 digest**，但 refine 照跑。

    为什么跳过 digest：export 失败意味着 `messages` 不完整，于是日报那句
    「当天 N 条消息」可能读起来像"这天很安静"，而其实是"没读进来"——
    那是这个项目反复抓到的同一句假话。refine 照跑是因为存量消息仍值得抽。
    """
    calls, plan = wired
    plan["export"] = _export_stats(failed_groups=[(7, "读不到")])
    report = daily.run(config=_FakeConfig(), llm_key="l",
                       day="2026-09-16", on_progress=lambda *_: None)

    assert calls == ["export", "refine"], "digest 必须被跳过"
    assert not report.ok
    assert "export" in report.last_error
    skipped = [s for s in report.stages if s.name == "digest"]
    assert skipped and not skipped[0].ok and skipped[0].skipped_reason


def test_export_raises_is_also_a_failure(wired):
    calls, plan = wired
    plan["export"] = RuntimeError("QQ 库路径没了")
    report = daily.run(config=_FakeConfig(), llm_key="l",
                       day="2026-09-16", on_progress=lambda *_: None)
    assert calls == ["export", "refine"]
    assert not report.ok
    assert "QQ 库路径没了" in report.last_error


def test_refine_failure_does_not_skip_digest(wired):
    """⭐ refine 有错 ⇒ digest **照跑**。

    日报自己的覆盖率检查会说出「仅抽取了 X/Y」——那是它在说自己有资格说的话。
    替它跳过，等于把一个它本来能诚实表达的部分结果也丢掉。
    """
    calls, plan = wired
    plan["refine"] = _refine_stats(errors=["第 2 批失败: 429"])
    report = daily.run(config=_FakeConfig(), llm_key="l",
                       day="2026-09-16", on_progress=lambda *_: None)
    assert calls == ["export", "refine", "digest"]
    assert not report.ok
    assert "429" in report.last_error


def test_digest_failure_is_reported(wired):
    _, plan = wired
    plan["digest"] = _digest_stats(errors=["模型调用失败: 超时"])
    report = daily.run(config=_FakeConfig(), llm_key="l",
                       day="2026-09-16", on_progress=lambda *_: None)
    assert not report.ok
    assert "超时" in report.last_error


def test_default_day_is_yesterday(wired):
    from vigil.digest import yesterday

    report = daily.run(config=_FakeConfig(), llm_key="l",
                       on_progress=lambda *_: None)
    assert report.deadline == yesterday()


def test_progress_goes_through_the_given_sink(wired):
    lines: list[str] = []
    daily.run(config=_FakeConfig(), llm_key="l",
              day="2026-09-16", on_progress=lines.append)
    text = "\n".join(lines)
    assert "export" in text and "refine" in text and "digest" in text
    assert "2026-09-16" in text


# ─────────────────────────────────────────────────────────────────────────
# 以下四条是**补的**（偏离 brief，见报告 §偏差 D2）。
#
# 理由：brief 的 Step 5 变异表 M2 说「把 refine 阶段的 try/except 去掉会
# 变红」，但 brief 的测试文件里**没有任何一条**把 `plan["refine"]` 设成异常
# （只设过 `plan["export"]` 与 `plan["digest"]`）——按 M2 变异，整份测试**全绿**，
# 那是一条会假绿的"反证"。同理 digest 抛异常的路径也没被覆盖。
# 另外 T1 把 `logs.emit` 钉成「写不进去就抛 OSError」，那条契约在编排层
# 的行为也必须钉住（§8.5）。
# ─────────────────────────────────────────────────────────────────────────


def test_refine_raises_is_recorded_and_digest_still_runs(wired):
    """refine **抛异常**（不只是 errors 非空）⇒ 记成阶段失败，digest 照跑。"""
    calls, plan = wired
    plan["refine"] = RuntimeError("预算护栏崩了")
    report = daily.run(config=_FakeConfig(), llm_key="l",
                       day="2026-09-16", on_progress=lambda *_: None)
    assert calls == ["export", "refine", "digest"], "refine 抛异常不该拦下 digest"
    assert not report.ok
    assert "预算护栏崩了" in report.last_error
    stage = [s for s in report.stages if s.name == "refine"]
    assert stage and not stage[0].ok and "预算护栏崩了" in stage[0].detail


def test_digest_raises_is_recorded(wired):
    """digest 抛异常（不只是 errors 非空）⇒ 记成阶段失败，不掀掉 run()。"""
    _, plan = wired
    plan["digest"] = RuntimeError("落文件失败：磁盘满")
    report = daily.run(config=_FakeConfig(), llm_key="l",
                       day="2026-09-16", on_progress=lambda *_: None)
    assert not report.ok
    assert "磁盘满" in report.last_error


def test_emit_failure_inside_a_stage_is_a_stage_failure(wired, monkeypatch):
    """§8.5：`emit` 在阶段内部炸 ⇒ 那个阶段记成**失败**，绝不被吞成成功。

    `emit`（= on_progress 或 `logs.emit`）写不进日志时抛 OSError 是 Task 1
    钉死的契约：无人值守下退出码是唯一还活着的信号，「日志有洞但 exit 0」
    正是 M4 要消灭的形状。所以这条测试要证明的是：OSError 传到 `run()`
    的阶段级 `except Exception` 后，`RunReport.ok` 必须为假。
    """
    calls, plan = wired
    boom = "[probe] 这次写日志会炸"

    def exploding_ingest(config, src, *, on_progress=print):
        calls.append("export")
        on_progress(boom)          # ← 模拟 export 内部的一次 emit
        return _export_stats()

    monkeypatch.setattr(daily.ingest_mod, "ingest", exploding_ingest)

    def flaky(msg: str) -> None:
        if msg == boom:
            raise OSError("日志盘满")
        # 其余的 emit 照常——否则阶段失败话术自己也会炸，测试就测不到"记账"

    report = daily.run(config=_FakeConfig(), llm_key="l",
                       day="2026-09-16", on_progress=flaky)

    assert not report.ok, "emit 抛 OSError 被吞成了成功——正是 §8.5 禁止的形状"
    export_stage = [s for s in report.stages if s.name == "export"]
    assert export_stage and not export_stage[0].ok
    assert "日志盘满" in export_stage[0].detail
    assert "日志盘满" in report.last_error, "失败必须留在账上"
    assert calls == ["export", "refine"], "export 失败 ⇒ digest 跳过（与 M1 同一判据）"


def test_broken_emit_never_returns_a_success_report(wired):
    """§8.5：日志从第一行就写不进去 ⇒ `run()` **直接抛**，绝不返回成功账目。

    这是 `run()` docstring 里"任何阶段失败都不抛"的**唯一例外**：emit 坏掉
    不是阶段失败，是"本次运行的记录没了"。这里不许吞（吞掉的话调用方会拿到
    一份 `ok=True` 的账目去写"全部成功"，而磁盘上一个字都没有）——抛出去由
    `cmd_daily` 的顶层兜底变成**非零退出码**，那是无人值守下唯一还活着的信号。
    """
    calls, plan = wired

    def dead_emit(msg: str) -> None:
        raise OSError("日志目录没了")

    with pytest.raises(OSError):
        daily.run(config=_FakeConfig(), llm_key="l",
                  day="2026-09-16", on_progress=dead_emit)
    assert calls == [], "第一行 emit 就炸了，不该有任何阶段开跑"


# ─────────────────────────────────────────────────────────────────────────
# 以下三条 + 两个夹具是**补的**（T7 审查 R65 / F2·F3 点名的覆盖缺口）。
#
# 审查实测：`since/until` 对调、`day_label` 换掉、`api_key`/`db_path`/`prompt_ver`
# 换垃圾值、以及把生产默认 `emit = on_progress or logs.emit` 换成 `or print`——
# **全都 11/11 绿**。共同根因：上面 `wired` 里那三个假函数签名是 `(config, **kw)`，
# **把关键字参数整个吞掉**，于是原有 11 条只钉住了「顺序 + 失败账目」，
# **一个参数都没钉**。对 `daily.run()` 这个无人值守的唯一入口来说，
# 「窗口对调 / 标签错 / 拿错密钥」都是**静默产出错误日报**的形状；
# 最后一条（默认 sink）更重：换掉它 = 整条日志链静默消失。见计划 §8.5。
# ─────────────────────────────────────────────────────────────────────────


@pytest.fixture
def logdir(tmp_path, monkeypatch):
    """把日志目录换到临时目录（与 `tests/test_logs.py` 的同名夹具同形）。

    ⚠️ **必须有**：`logs.emit` 除 `print` 之外那一半会**真的写文件**，而本仓库的
    `data/logs/` 是生产日志目录——测试静默往里写，正是 `logs.py` 模块 docstring
    点名要避免的事（`LOG_DIR` 一律现读就是为了这个）。

    ⚠️ 顺带把 `propagate`/`level` 也还原：`logs.reset()` **不管这两个**（它只摘
    handler、复位幂等标志），而它们是**进程级**状态。不还原的话，本文件会把后面
    `tests/test_logs.py` 的前提悄悄改掉——那正是「单跑红、整跑绿」最爱出没的地方。
    """
    logger = logging.getLogger("vigil")
    before = (logger.propagate, logger.level)
    monkeypatch.setattr(logs, "LOG_DIR", tmp_path / "logs")
    logs.reset()
    yield tmp_path / "logs"
    logs.reset()
    logger.propagate, logger.level = before


def _our_handler() -> logs.DailyFileHandler:
    """取**我们自己的** handler。**绝不能用 `handlers[0]`**。

    与 `tests/test_logs.py:_our_handler` 同款（那里记着完整实测经过）：pytest 9.1.1
    下 `_pytest/logging.py` 会给每个非传播 logger 挂上自己的 `LogCaptureHandler`，
    而它的 `handleError` **就是 `raise`**——把"写必炸的流"挂到它身上，测试会**绿**，
    但绿的是 pytest 的 handler，本项目的代码一行都没走到（实测形态：单跑红、整跑绿）。
    """
    for h in logging.getLogger("vigil").handlers:
        if isinstance(h, logs.DailyFileHandler):
            return h
    raise AssertionError("vigil logger 上没挂 DailyFileHandler——setup() 没生效？")


def _stages_recording_kwargs(monkeypatch) -> dict[str, dict]:
    """像 `wired` 一样换掉三阶段，但**把收到的参数原样记下来**。

    ⚠️ 三个假函数**不调用** `on_progress`：这一组要问的是「谁被传了进来」，
    而不是阶段内部行为（那是 `wired` 那组的事）。所以它们对真 sink 没有任何副作用。
    """
    seen: dict[str, dict] = {}

    def fake_ingest(config, src, *, on_progress=None):
        seen["export"] = {"config": config, "source": src, "on_progress": on_progress}
        return _export_stats()

    def fake_refine(config, **kw):
        seen["refine"] = {"config": config, **kw}
        return _refine_stats()

    def fake_digest(config, **kw):
        seen["digest"] = {"config": config, **kw}
        return _digest_stats()

    monkeypatch.setattr(daily.ingest_mod, "ingest", fake_ingest)
    monkeypatch.setattr(daily.refine_mod, "refine", fake_refine)
    monkeypatch.setattr(daily.digest_mod, "digest", fake_digest)
    return seen


def test_stage_arguments_are_wired_from_the_day_window(monkeypatch):
    """⭐ 三个阶段拿到的参数**必须**来自同一条窗口，不许对调、不许写死。

    审查实测（R65）：`since/until` 对调、`day_label` 换掉、`api_key`/`db_path`/
    `prompt_ver` 换垃圾值——原先 **11/11 全绿**，也就是说这些接线**没有任何判据**。

    ⚠️ 判据读的是**假函数收到的 kwargs**，不是 `daily.py` 的源码文本：
    文本判据（"看一眼写对没有"）会在有人把调用改成别的形状时静默失效。
    """
    day = "2026-09-16"
    llm_key = "llm-密钥-只该进模型"

    class _Cfg:
        """只带 `output_db` 的假配置（与 `_FakeConfig` 同款，但**值特意不同**：
        这样"把 db_path 写死成别的路径"也会红）。"""

        output_db = pathlib.Path("wired-output.db")
        # `daily.run()` 阶段 1 会 `open_source(config.source, …)`；缺它会在
        # 调假 ingest 之前 AttributeError（同 `_FakeConfig` 的理由）。
        source = "synthetic"
        source_dir = pathlib.Path("unused_src")
        groups = ()

    seen = _stages_recording_kwargs(monkeypatch)
    cfg = _Cfg()
    sink = lambda _msg: None            # noqa: E731 —— 只为验「sink 有没有被原样传下去」
    report = daily.run(config=cfg, llm_key=llm_key, day=day, on_progress=sink)

    assert report.ok
    assert sorted(seen) == ["digest", "export", "refine"], "三个阶段都得跑到，且只有它们"

    # ── 窗口：必须与 `daily` 自己用的那一个 `day_window(day)` 逐字一致 ──
    window = daily.digest_mod.day_window(day)
    dst = seen["digest"]
    assert (dst["since"], dst["until"]) == window, (
        f"digest 拿到的窗口是 {dst['since']}..{dst['until']}，而 "
        f"day_window({day}) = {window}——对调或写死都会在这里红"
    )
    assert dst["since"] < dst["until"], "窗口反了：since 比 until 还晚（对调的正身）"
    assert dst["day_label"] == day, "日报的日期标签不是这一天——日报会标错日子"

    # ── 密钥：只有 refine/digest 拿 LLM 密钥（export 那一步不该碰它）──
    #
    # ⚠️ 早年这里还有一条"export 拿 DB 密钥、两把不许混" —— DB 密钥是 QQ 源的
    # 概念（解密本地库用），开源版默认源是合成语料、不需要密钥，那条随之消失。
    # 留下的是**更本质**的那半条：模型密钥只出现在真正要调模型的两处。
    assert "api_key" not in seen["export"], (
        "ingest 不该碰 LLM 密钥——它只落数据，不调模型"
    )
    for stage in ("refine", "digest"):
        assert seen[stage]["api_key"] == llm_key, (
            f"{stage} 拿到的不是 LLM 密钥"
        )

    # ── db_path：refine/digest 都得指向 config.output_db ────────────────
    for stage in ("refine", "digest"):
        assert seen[stage]["db_path"] == _Cfg.output_db

    # ── 提示词版本与模型：不许写死成别的（审查的 M15 换的就是这两个） ────
    assert seen["refine"]["prompt_ver"] == daily.refine_mod.PROMPT_VERSION
    assert seen["refine"]["model"] == daily.refine_mod.DEFAULT_MODEL
    assert seen["digest"]["model"] == daily.digest_mod.DEFAULT_MODEL

    # ── config 与 sink：原样传递，不许被换成别的东西 ─────────────────────
    for stage in ("export", "refine", "digest"):
        assert seen[stage]["config"] is cfg
        assert seen[stage]["on_progress"] is sink, f"{stage} 的进度回调被换掉了"


def test_default_progress_sink_is_logs_emit(monkeypatch, logdir, capsys):
    """⭐⭐ 缺省 sink 必须是 `logs.emit`——**不许被换成 `print`**。

    审查实测（R65 的 M17）：把生产默认 `emit = on_progress or logs.emit` 换成
    `or print`，**11/11 全绿**。而 M4 的核心承诺是「无人值守下失败看得见」，
    靠的就是 `logs.emit`「写不进就抛」这条契约（`DailyFileHandler.handleError`）
    由 `daily.run()` 的缺省 sink 接住。换成 `print` ⇒ **整条日志链静默消失**
    （任务计划丢弃 stdout），却没有任何测试会红——正是本轮反复抓到的「空守卫」形状。

    ⚠️ **偏离派发单，写在明处**：派发单要求对 `inspect.signature(daily.run)` 的
    默认值**裸判** `is logs.emit`。但实现冻的是 `on_progress=None` + 函数体里
    **现读** `logs.emit`（`emit = on_progress or logs.emit`），所以那条裸判在
    **基线就红**——与「阳性对照 M4 必须全绿」直接冲突。这里拆成三条判据，合起来
    覆盖同一个缺口，且对两种退化形状（改签名默认值 / 改 `or` 的右边）都有效：

      A. 签名默认值只允许是 `None` 或 `logs.emit` 本身；
      B. **缺省调用**时三个阶段拿到的 sink `is logs.emit`（同一对象，不是"长得像"），
         而且它真的把日志**写进了文件**（端到端：`print` 到不了文件）；
      C. 把 `logs.emit` 换成探针后，缺省调用必须打到**探针**，且 stdout 一个字都没有
         ——`or print` 的形状在 C 上必红。

    B 与 C 只差「sink 有没有被换掉」，合起来正好把「走 logs.emit」与「走 print」
    这条线钉死（C 的 stdout 断言就是阴性对照：真走 logs.emit 时 print 不该被调用）。
    """
    import inspect                      # 只在本条测试里用

    default = inspect.signature(daily.run).parameters["on_progress"].default
    assert default is None or default is logs.emit, (
        f"on_progress 的签名默认值是 {default!r}——只允许 `None`（现读 logs.emit）"
        "或 `logs.emit` 本身；`print` 是审查点名的退化形状"
    )

    # ── B：缺省调用 ⇒ sink 是 logs.emit 这个对象，且真的落到文件 ──────────
    seen = _stages_recording_kwargs(monkeypatch)
    logs.setup()
    report = daily.run(config=_FakeConfig(), llm_key="l", day="2026-09-16")
    assert report.ok

    for stage in ("export", "refine", "digest"):
        assert seen[stage]["on_progress"] is logs.emit, (
            f"{stage} 拿到的缺省 sink 不是 `logs.emit` 本身"
            f"（是 {seen[stage]['on_progress']!r}）——换掉它，整条日志链会静默消失"
        )
    text = logs.log_path().read_text(encoding="utf-8")
    assert "vigil daily 开始" in text, (
        "缺省 sink 没把开场横幅写进日志文件——「无人值守下失败看得见」靠的就是它"
    )

    # ── C：换成探针 ⇒ 缺省调用必须打到探针，且 stdout 一个字都没有 ────────
    capsys.readouterr()                 # 清掉 B 段真 logs.emit 打到 stdout 的那些行
    heard: list[str] = []
    monkeypatch.setattr(logs, "emit", heard.append)
    daily.run(config=_FakeConfig(), llm_key="l", day="2026-09-16")

    assert heard, "缺省调用一次都没走到 logs.emit——默认 sink 被换掉了"
    assert any("vigil daily 开始" in m for m in heard), "开场横幅没走 logs.emit"
    assert "全部成功" in "\n".join(heard), "小结没走 logs.emit——账目也一起丢了"
    assert capsys.readouterr().out == "", (
        "缺省 sink 绕过 logs.emit 直接 print 了——生产默认必须是 logs.emit"
    )


def test_broken_real_emit_never_yields_an_ok_report(wired, logdir, monkeypatch, capsys):
    """⭐ 用**真的** `logs.emit` 接一条写必炸的流，断言 `run()` 不给 `ok=True`。

    这是 §8.5 那条链的**端到端接缝**（真 `logs.emit`，不是替身）。T7 的审查用同一形状
    实测过：**修 `logs.py` 之前**（标准库 `handleError` 把写失败吞掉），`run()` 返回
    `ok=True` / `last_error=''` / 印「全部成功」⇒ `cmd_daily` 会 `return 0`、而日志
    一个字都没写成——**正是 M4 要消灭的形状**。`94a9b54` 让
    `DailyFileHandler.handleError` 原样重抛之后，这里必须**抛**。

    与 `test_broken_emit_never_returns_a_success_report` 的分工：那条用**假 sink**
    （测 `run()` 自己怎么处置"抛出"），这条用**真 logs.emit + 真 logging 写路径 +
    真 handler**——只有这条能证明「写失败真的一路走到 `run()`」这个**前提**本身成立。
    前提不成立时，假 sink 那条照样是绿的，而生产链是断的。

    ⚠️ handler 按**类型**取，不用 `handlers[0]`（原因见 `_our_handler`）：
    挂错对象（pytest 的 `LogCaptureHandler`，它的 `handleError` 就是 `raise`）会让
    这条在整文件跑时**假绿**——实测形态正是「单跑红、整跑绿」。

    ⚠️ 三个阶段仍然用假货：退化（写失败被吞）时它们**会**被跑到，那时若接的是真阶段，
    这条测试会去碰真库、真模型——测试本身绝不许有这种副作用。
    """
    class _BoomStream:
        def write(self, s):
            raise OSError(28, "No space left on device（模拟磁盘满）")

        def flush(self):
            pass

    logs.setup()
    monkeypatch.setattr(_our_handler(), "stream", _BoomStream())

    calls, _ = wired
    raised = False
    try:
        # ⚠️ 不传 on_progress：逼它走缺省 = 真 logs.emit（生产形状）
        report = daily.run(config=_FakeConfig(), llm_key="l", day="2026-09-16")
    except OSError:
        raised = True                   # 合法结局之一：抛 ⇒ cmd_daily 顶层兜底 ⇒ exit 1

    if not raised:
        assert not report.ok, (
            "真 logs.emit 写不进去，run() 却给了 ok=True 的账目 ⇒ cmd_daily 会 return 0"
            "而日志有洞——正是 M4 要消灭的形状"
        )
        assert report.last_error, "失败没留在账上"

    assert calls == [], "记录根本写不出去的一轮，不该把阶段跑下去"
    assert "全部成功" not in capsys.readouterr().out, (
        "日志写不进去却印了「全部成功」——这正是审查实测到的修前行为"
    )


# ─────────────────────────────────────────────────────────────────────────
# 无人值守路径的 token 预算（M4 终审 I-3）
#
# `vigil refine` 的 `--budget` 只存在于 CLI，而 `daily.run` 是**没人看着跑**
# 的那条路。不传 ⇒ `refine()` 的预算护栏（只在 `budget_tokens is not None`
# 时生效）形同不存在 ⇒ 首次挂机或断更积压时，08:00 那次会在无人看管下把
# 全部积压跑完，时长与花费都无上界（`StartWhenAvailable` 还会把错过的补跑）。
# ─────────────────────────────────────────────────────────────────────────


def test_unattended_refine_gets_a_token_budget(monkeypatch):
    """⭐⭐ `daily.run` 传给 `refine` 的 `budget_tokens` **不许是 None**。

    断言两件，缺一不可：

    * **不是 None** —— 护栏真的能生效（`refine()` 的闸是
      `if budget_tokens is not None and stats.total_tokens >= budget_tokens`）；
    * **就是那个具名常量** —— 不许顺手写死另一个数；取值依据（实测锚点、
      正常日/爆量日/全史积压各是多少）写在 `daily.DAILY_BUDGET_TOKENS` 上面那段。

    变异「删掉 `budget_tokens=DAILY_BUDGET_TOKENS` 这个实参」→ 第一条断言红。
    变异「把它改成 `refine_mod.DEFAULT_MODEL` 之类的别的值」→ 第二条红。
    """
    seen = _stages_recording_kwargs(monkeypatch)
    report = daily.run(config=_FakeConfig(), llm_key="l",
                       day="2026-09-16", on_progress=lambda *_: None)

    assert report.ok
    budget = seen["refine"].get("budget_tokens")
    assert budget is not None, (
        "无人值守那条路没有预算上限：`refine()` 的护栏只在 budget_tokens 不是 None "
        "时生效，不传 = 首次挂机/断更积压会在无人看管下把全部积压跑完"
    )
    assert budget == daily.DAILY_BUDGET_TOKENS

    # 下界（不是把 300,000 这个选择钉死，只防「有人悄悄改小到卡死日常」）：
    # 已验收的三篇日报是 151 / 514 / 536 条，按每条扫描消息 ≈15 token
    # （M1 的 1,050 条窗口实跑 13,520 输入 token + 输出折算）⇒ 最吵的那天
    # ≈8,000 token。留 10 倍余量。
    busiest_recorded_day = 536 * 15
    assert budget >= busiest_recorded_day * 10, (
        f"预算 {budget:,} 离「最吵的一天」{busiest_recorded_day:,} token 太近——"
        f"宁可宽松也不要卡死日常（依据见 daily.DAILY_BUDGET_TOKENS 的注释）"
    )


@pytest.mark.parametrize(
    ("toml_thinking", "expected"),
    [
        ("enable_thinking = true\n", True),
        ("", None),
    ],
    ids=["文件写了 true", "文件没写该键（= 不发）"],
)
def test_daily_passes_the_configured_llm_endpoint(
    monkeypatch, tmp_path, toml_thinking, expected
):
    """⭐⭐ `daily` 必须把 `config/llm.toml` 里那三样**真的传下去**。

    ⚠️ 这条挡的是一个具体的形状：`daily` 是**无人值守**路径，没有命令行
    可接，很容易在改造时被漏掉——而它漏掉的后果是"网页上改了端点、
    日报却还在打老地址"，且**只有等到有人看日志才发现**。

    ⚠️ **`enable_thinking` 这两档是 re-review 补的（Important-2）**，原来的
    参数是 `false`——而 `false` **与「硬写回 `False`」不可区分**：
    把 `daily.py` 里那行换成 `enable_thinking=False`，全量照样 645 passed。
    一个值域只有 {硬编码值} 的夹具，测的是它自己。所以：

      · 写 `true` ⇒ 必须传 `True`（**任何形式的"我们替你关掉"都会在这里红**）
      · 不写该键 ⇒ 必须传 `None`（**"不发"这一态，R12**；写成 `False` 也红）
      · 反过来**不测** `false` ⇒ 那一档由"出厂文件 + 三处默认值 `False`"覆盖，
        而它对变异是瞎的（上面那句实测）。**测有判别力的那一档。**

    ⚠️ 变异反证：把 `daily.py` 里 `enable_thinking=llm_settings.enable_thinking`
    写成 `enable_thinking=False` ⇒ **两档都红**。
    """
    from vigil import config

    monkeypatch.setattr(
        config, "DEFAULT_LLM_TOML",
        tmp_path / "llm.toml",
    )
    (tmp_path / "llm.toml").write_text(
        '[llm]\nbase_url = "https://my-host/v1/chat/completions"\n'
        f'model = "my-model"\n{toml_thinking}',
        encoding="utf-8",
    )

    seen: dict[str, object] = {}

    def fake_ingest(config, src, *, on_progress=print):
        return daily.ingest_mod.IngestStats(per_group=[(1, "群", 0)])

    def fake_refine(cfg, **kw):
        seen["refine"] = kw
        return daily.refine_mod.RefineStats()

    def fake_digest(cfg, **kw):
        seen["digest"] = kw
        return daily.digest_mod.DigestStats()

    monkeypatch.setattr(daily.ingest_mod, "ingest", fake_ingest)
    monkeypatch.setattr(daily.refine_mod, "refine", fake_refine)
    monkeypatch.setattr(daily.digest_mod, "digest", fake_digest)
    monkeypatch.setattr(daily.digest_mod, "day_window", lambda day: (0, 1))

    daily.run(
        config=_FakeConfig(), llm_key="lk",
        day="2026-09-16", on_progress=lambda _: None,
    )

    assert seen["refine"]["base_url"] == "https://my-host/v1/chat/completions"
    assert seen["refine"]["model"] == "my-model"
    assert seen["refine"]["enable_thinking"] is expected
    assert seen["digest"]["base_url"] == "https://my-host/v1/chat/completions"
    assert seen["digest"]["enable_thinking"] is expected, (
        "digest 那一处也要跟文件走——brief 点名「daily 是唯一行为真的变了的地方」，"
        "而它改的是**两处**调用"
    )
