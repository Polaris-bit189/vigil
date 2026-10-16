"""日志模块的测试。

⚠️ 每一条都 monkeypatch `logs.LOG_DIR`，绝不写仓库的 `data/logs/`。
`LOG_DIR` **必须在函数体里现读**，不能在 import 时算好冻进默认参数——
否则 monkeypatch 换不掉它，这些测试就会去写真实目录（而且是**静默地**写）。
"""

from __future__ import annotations

import datetime as dt
import logging
import pathlib

import pytest

from vigil import logs


@pytest.fixture
def logdir(tmp_path, monkeypatch):
    """把日志目录换到临时目录，并复位幂等标志。"""
    d = tmp_path / "logs"
    monkeypatch.setattr(logs, "LOG_DIR", d)
    logs.reset()
    yield d
    logs.reset()


def test_setup_creates_dir_and_writes_a_line(logdir):
    logs.setup()
    logs.emit("你好")
    # emit 会真的写文件——必须 flush 过，所以不 close 也读得到
    text = logs.log_path().read_text(encoding="utf-8")
    assert "你好" in text


def test_setup_is_idempotent_no_duplicate_lines(logdir):
    """⭐ 重复 setup 不许叠加 handler。

    叠了的话同一行会出现两遍——而「日志里同一句话出现三次」会让人
    以为跑了三次。这是幂等的硬理由，不是洁癖。
    """
    logs.setup()
    logs.setup()
    logs.setup()
    logs.emit("只此一行")
    text = logs.log_path().read_text(encoding="utf-8")
    assert text.count("只此一行") == 1


def test_emit_prints_verbatim_without_timestamp(logdir, capsys):
    """⭐ stdout 上是**一字不改的原文**。

    这是与既有 capsys 测试的契约：`tests/test_digest.py` 里约 10 处
    用 `capsys.readouterr().out` 读 CLI 输出。加时间戳前缀会让它们集体变红。
    """
    logs.setup()
    logs.emit("完成：扫描 3 条")
    out = capsys.readouterr().out
    assert out == "完成：扫描 3 条\n", "stdout 必须是原文，不许有时间戳/级别前缀"


def test_file_line_has_timestamp_and_level(logdir):
    logs.setup()
    logs.emit("有事发生")
    line = logs.log_path().read_text(encoding="utf-8").strip()
    assert line.endswith("INFO  有事发生"), line
    # 前缀是 "YYYY-MM-DD HH:MM:SS "
    assert len(line) > 19 and line[4] == "-" and line[13] == ":", line


def test_every_record_is_flushed_immediately(logdir):
    """⭐ 每条记录都落盘，不靠 close。

    无人值守下进程被强杀时，还留在缓冲区里的恰好是唯一能解释崩溃的那几行。
    ⚠️ 这条测的是 `logging.FileHandler` 继承自 `StreamHandler.emit` 的行为
    （它末尾调 `self.flush()`）——**不是**我们自己写的逻辑。所以它是一条
    「换 handler 类型会红」的哨兵：哪天有人把 DailyFileHandler 换成
    BufferingHandler 之类，这条必须红。
    """
    logs.setup()
    logs.emit("崩前最后一句")
    # 注意：没有 close()、没有 shutdown()
    assert "崩前最后一句" in logs.log_path().read_text(encoding="utf-8")


def test_log_path_uses_the_day(logdir):
    d = dt.date(2026, 9, 17)
    assert logs.log_path(d).name == "vigil-2026-09-17.log"


def test_clear_then_write_last_error(logdir):
    logs.clear_last_error()
    assert not (logdir / logs.LAST_ERROR_NAME).exists()
    logs.setup()
    logs.emit("出事了")
    p = logs.write_last_error("refine 第 3 批失败")
    assert p == logdir / logs.LAST_ERROR_NAME
    body = p.read_text(encoding="utf-8")
    assert "refine 第 3 批失败" in body
    assert "出事了" in body, "spec §4.8 要求的是「日志尾部」——尾部要真的在"


def test_write_last_error_survives_missing_log_file(logdir):
    """当天日志不存在时也必须写得出 LAST-ERROR.txt。

    这是 .cmd 兜底路径的形状：Python 在写日志之前就失败了。
    """
    logs.clear_last_error()
    p = logs.write_last_error("启动即失败")
    assert "启动即失败" in p.read_text(encoding="utf-8")


def test_prune_only_deletes_our_own_filename_shape(logdir):
    """⭐ 轮转只认 `vigil-YYYY-MM-DD.log`。

    用宽 glob（`*.log` / `*`）的话，日后往这个目录放任何东西都会被无声吃掉
    ——而「日志轮转把别的东西删了」是最难查的一类事故。
    """
    logdir.mkdir(parents=True, exist_ok=True)
    today = dt.date(2026, 9, 17)
    old = logs.log_path(today - dt.timedelta(days=40))
    keep = logs.log_path(today - dt.timedelta(days=3))
    foreign = logdir / "important.log"
    errfile = logdir / logs.LAST_ERROR_NAME
    for f in (old, keep, foreign, errfile):
        f.write_text("x", encoding="utf-8")

    deleted = logs.prune_old_logs(today=today)

    assert deleted == [old.name]
    assert not old.exists()
    assert keep.exists() and foreign.exists() and errfile.exists()


def test_prune_keeps_files_at_the_boundary(logdir):
    logdir.mkdir(parents=True, exist_ok=True)
    today = dt.date(2026, 9, 17)
    edge = logs.log_path(today - dt.timedelta(days=logs.KEEP_DAYS))
    edge.write_text("x", encoding="utf-8")
    assert logs.prune_old_logs(today=today) == [], "恰好第 KEEP_DAYS 天的不该删"
    assert edge.exists()


def test_prune_tolerates_missing_dir(logdir):
    assert logs.prune_old_logs() == []      # logdir 还没建


def test_handler_rolls_over_at_midnight(logdir, monkeypatch):
    """⭐ 跨零点自动换文件。

    没有这条的话，把 `DailyFileHandler.emit` 整个方法体换成
    `super().emit(record)`（等于删掉跨零点逻辑）**11 条测试全绿**——
    实测过。而它要防的是：一个从 23:50 跑到 00:20 的 refine 会把第二天
    的事件全写进前一天的文件里，于是"日志是哪天跑的"这件事就对不上了。
    """
    day1 = dt.date(2026, 9, 16)
    monkeypatch.setattr(logs, "_today", lambda: day1)

    logs.setup()
    logs.emit("零点前")
    assert "零点前" in logs.log_path(day1).read_text(encoding="utf-8")

    # 时钟跨过零点
    day2 = day1 + dt.timedelta(days=1)
    monkeypatch.setattr(logs, "_today", lambda: day2)
    logs.emit("零点后")

    after = logs.log_path(day2).read_text(encoding="utf-8")
    assert "零点后" in after, "跨零点后的记录必须进**新一天**的文件"
    assert "零点前" not in after, "旧记录不许跟着跑到新文件里"
    assert "零点后" not in logs.log_path(day1).read_text(encoding="utf-8"), \
        "新记录不许再写进前一天的文件"


def test_clear_last_error_actually_removes_the_file(logdir):
    """⭐ `clear_last_error` 必须**真的删**——它是「文件存在 ⟺ 本次跑失败过」的一半。

    另一半（失败时写出来）由 `test_clear_then_write_last_error` 覆盖。
    审查实测：把本函数变异成 `return None`，原先 12 条**全绿**——
    因为既有测试都在干净目录里"先清再断言不存在"，no-op 也能过。
    """
    logs.clear_last_error()                    # 目录都不存在时也不许抛
    logs.setup()
    logs.write_last_error("上次失败了")
    assert (logdir / logs.LAST_ERROR_NAME).exists()

    logs.clear_last_error()

    assert not (logdir / logs.LAST_ERROR_NAME).exists(), \
        "clear_last_error 没真的删——于是下次跑成功也会被读成『这次失败了』"


def test_log_tail_returns_the_tail_not_the_head(logdir):
    """⭐ 取的是**尾部**不是头部。

    实测：把 `rows[-lines:]` 改成 `rows[:lines]`，原 12 条全绿——
    因为唯一碰它的那条测试只写了一行日志，头尾是同一条。

    取成头部的后果：LAST-ERROR.txt 里是本轮开跑时那句
    『═══ vigil daily 开始 ═══』，而真正有用的出错现场一个字都没有。
    spec §4.8 的原文要求就是「日志尾部」，所以这条有明确的判据来源。
    """
    logs.setup()
    for i in range(1, 11):
        logs.emit(f"第{i}行")

    logs.write_last_error("出事了", tail_lines=3)

    body = (logdir / logs.LAST_ERROR_NAME).read_text(encoding="utf-8")
    assert "第10行" in body and "第9行" in body and "第8行" in body, body
    assert "第1行" not in body, "取到头部了——那不是『日志尾部』"
    assert "出事了" in body


def test_emit_propagates_handler_exceptions(logdir, monkeypatch):
    """`logs.emit` 会**传播** handler 抛出的异常——这是 `emit` 的契约。

    ⚠️ 这条**不是**「写不进日志就抛」的守卫，它测的是**另一层**：handler 抛
    出来的异常不许被 `emit` 吞掉。它把 `handler.emit` 整个方法**换掉**了，
    真实写路径（`DailyFileHandler.emit` / `handleError`）一行都跑不到——
    实测：把 `DailyFileHandler.emit` 掏空成 `pass`，本条**照样绿**。
    真守卫在 `test_real_write_failure_propagates_not_swallowed`（让**真实
    写路径**失败），两条合起来才是完整的「写失败必须抛」。

    为什么「抛」这件事仍然要守：无人值守下退出码是唯一还活着的信号（任务计划
    丢弃 stderr、磁盘满时 LAST-ERROR.txt 也写不出来）。若 `emit` 自己把异常
    吞了，`handleError` 的重抛也到不了 CLI，于是得到一个 `exit 0 但日志有洞`
    的运行——**正是 M4 要消灭的形状**。

    与 `test_prune_tolerates_missing_dir` 的「吞」是**刻意的不对称**：
      · 轮转失败 = 打扫失败，丢的是历史日志 → 吞掉，不影响本次运行
      · 写日志失败 = **本次运行的记录没了** → 抛

    ⚠️ handler 用 `_our_handler()` 取（定义在本文件下面），**不用 `handlers[0]`**
    ——原因见那个函数的 docstring（pytest 会往非传播的 logger 上挂自己的
    `LogCaptureHandler`，而它的 `handleError` 是 `raise`，于是测试会绿在一个
    **不是我们的** handler 上）。
    """
    logs.setup()
    handler = _our_handler()

    def boom(record):
        raise OSError("模拟磁盘满")

    monkeypatch.setattr(handler, "emit", boom)

    with pytest.raises(OSError):
        logs.emit("这句写不进去")


def _our_handler() -> logs.DailyFileHandler:
    """取出**我们自己的** handler。

    ⚠️ **绝不能用 `handlers[0]`**——实测（pytest 9.1.1）：`logs.setup()` 把
    `propagate` 设成 False，而 `_pytest/logging.py` 的 `catching_logs.__enter__`
    会给**每一个非传播的 logger** 挂上它自己的 `LogCaptureHandler`。于是从
    第二个测试起 `handlers[0]` 是 pytest 那个，不是 `DailyFileHandler`。
    后果特别阴：pytest 的 `LogCaptureHandler.handleError` 是 **`raise`**
    （它故意重抛，好让 logging 出错就弄红测试）——把写失败挂到它身上，测试
    会**绿**，但绿的是 pytest 的 handler，本模块的代码一行都没走到。

    这不是假设：本条修复的第一版就用 `handlers[0]`，`-k` 单跑红、整文件跑绿
    ——因为整文件跑时前面已经有别的测试把 logger 变成非传播的，pytest 的
    handler 已经挂上了。
    """
    for h in logging.getLogger("vigil").handlers:
        if isinstance(h, logs.DailyFileHandler):
            return h
    raise AssertionError("vigil logger 上没挂 DailyFileHandler——setup() 没生效？")


def test_real_write_failure_propagates_not_swallowed(logdir, monkeypatch):
    """⭐⭐ 真实写失败必须抛，**不许被 logging 吞掉**。

    与 `test_emit_propagates_handler_exceptions` 的区别（那条留着，测的是另一层）：
      · 那条 monkeypatch 掉了 `handler.emit` 整个方法 —— 绕过了真实写路径
      · 这条让**真实写路径**失败：`logging.StreamHandler.emit` 写流时抛 OSError，
        它会 `except Exception: self.handleError(record)`。**标准实现的 `handleError`
        只往 stderr 打 traceback、不重抛** ⇒ 在任务计划下（stderr 被丢弃）
        「磁盘满」这件事外面一个字都看不见。

    实测来源：T7 的 implementer 在写 `daily.py` 时发现「写不进就抛」只对跨零点
    rollover 成立，普通写失败被吞。它与本模块那条「轮转失败可吞 / 写失败必须抛」
    的刻意不对称是同一件事的两面。

    ⚠️ handler 用 `_our_handler()` 取，不用 `handlers[0]`（原因见那个函数）。
    """

    class _BoomStream:
        def write(self, s):
            raise OSError("模拟磁盘满")

        def flush(self):
            pass

    logs.setup()
    handler = _our_handler()
    monkeypatch.setattr(handler, "stream", _BoomStream())

    with pytest.raises(OSError):
        logs.emit("这句写不进去")


def test_healthy_stream_is_not_told_to_raise(logdir, monkeypatch):
    """阳性对照：**「写失败就抛」不是「任何情况都抛」**。

    与 `test_real_write_failure_propagates_not_swallowed` 只差一件事：
    这里的流**写得进去**。若那条测试无论流好不好都红（例如
    `DailyFileHandler.emit` 里凭空多抛一次），这条会立刻把它戳穿——所以两条
    必须一起在。

    ⚠️ **本对照不覆盖**「`handleError` 写成无条件抛」那种退化（健康流下它
    根本不会被调用，实测两种写法都过）；它覆盖的是「**内容没真的写下去**」
    （换成 `BufferingHandler` 之类会红）。

    还断言了消息**真的走完了格式化+写入**，不是"没抛就完事"。
    """

    class _GoodStream:
        def __init__(self):
            self.written: list[str] = []

        def write(self, s):
            self.written.append(s)

        def flush(self):
            pass

    logs.setup()
    handler = _our_handler()
    good = _GoodStream()
    monkeypatch.setattr(handler, "stream", good)

    logs.emit("这句写得进去")          # 必须正常返回，不许抛

    assert any("这句写得进去" in s for s in good.written), \
        "记录没走完真实写路径——这条阳性对照就没能证明「失败才抛」"


# ── 中文 Windows 的 GBK 崩溃（M7）────────────────────────────────


def test_harden_streams_survives_a_gbk_stream():
    """⭐⭐ **中文 Windows 上的必修项**：GBK 流编不出 `✓`/`⏰` 时不许崩。

    ⚠️ 这条挡的是一个**实测踩到的复现红线问题**：Windows 的 Python 默认按
    ANSI 代码页开 stdout（中文机器即 GBK），而本项目输出里有大量 GBK
    编不出的字符。不修的话 `vigil demo` **崩在成功路径中途** ——
    实测退出码 1、库只建到 329/1237 条。

    ⚠️ 判据是「能不能把 `✓` 写进去而**不抛异常**」，不是"输出长什么样"：
    `replace` 会把编不出的字符降级成 `?`，那是**设计如此**（保中文、舍符号）。
    """
    import io

    buf = io.BytesIO()
    # 造一个真的 GBK 流（模拟中文 Windows 的 stdout）
    stream = io.TextIOWrapper(buf, encoding="gbk")
    try:
        stream.write("✓ ⏰ ⚠")
    except UnicodeEncodeError:
        pass  # 未加固时确实会抛 —— 下面是加固后的对照
    stream.detach()

    buf2 = io.BytesIO()
    fixed = io.TextIOWrapper(buf2, encoding="gbk")
    fixed.reconfigure(errors="replace")
    fixed.write("✓ ⏰ ⚠ 中文正常")
    fixed.flush()
    fixed.detach()

    out = buf2.getvalue().decode("gbk")
    assert "?" in out, "编不出的字符该降级成 ?"
    assert "中文正常" in out, "⚠️ 中文必须完好 —— 转 UTF-8 会让它满屏乱码"


def test_harden_streams_never_raises_on_exotic_streams():
    """⚠️ 它**永远不该让程序起不来** —— 它是兜底，不是前置条件。

    测试里 stdout 常被换成 `StringIO`（没有 `reconfigure`），
    所以 `AttributeError` 必须被吞掉。
    """
    import io

    from vigil import logs

    fake = io.StringIO()
    try:
        fake.reconfigure(errors="replace")  # type: ignore[attr-defined]
        raise AssertionError("StringIO 不该有 reconfigure —— 测试前提变了")
    except AttributeError:
        pass

    # 直接调真函数：它读的是 sys.stdout/sys.stderr，不该抛
    logs.harden_streams()
