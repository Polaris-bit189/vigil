"""日志：控制台原文 + 按日轮转文件 + 失败留痕。

为什么必须有（spec §4.8；Step Zero 0.7 明确移入 M4）：
在此之前全项目**零日志基础设施**——`grep -rn "import logging" vigil/` 零命中，
60 处 `print(` 全在 `cli.py`，三个库模块的进度回调默认值就是内建 `print`。
而 print 到 stdout 在任务计划程序下**没有任何落点**：任务计划不保存任务的
输出，挂机跑一夜，第二天只剩「退出码是多少」这一条信息。

四条设计决定（每条都有代价，写在这里免得日后被当成"可以优化掉"）：

1. **按日一个文件** `vigil-YYYY-MM-DD.log`，不是单文件轮转。按日命名让
   「昨天发生了什么」变成一个**文件名**问题；单文件轮转则要么处理"写到
   一半改名"，要么需要额外的句柄重开逻辑，且昨天的内容会被滚走。

2. **`emit()` 先 print 原文、再写日志**，顺序不能反。stdout 上必须是
   **一字不改的原文**（没有时间戳前缀），因为既有测试用 capsys 读它
   （`tests/test_digest.py` 里约 10 处）。反过来做的代价是 M4 顺带重写
   一整套 CLI 输出测试——那是范围蔓延。

3. **不接管 stderr、不装 excepthook。** Python 未捕获异常的 traceback 由
   解释器直接写 stderr，绕过 logging；硬要接管需要重定向或 excepthook，
   那是另一类脆。所以失败路径**同时**依赖三件东西：`LAST-ERROR.txt`
   （本模块写）、退出码（CLI 返回）、stderr（人工跑时可见，任务计划丢弃）。

4. **文件 handler 每条记录都 flush**——这不是我们写的，是
   `logging.StreamHandler.emit` 自带的行为（它末尾就调 `self.flush()`，
   `FileHandler` 继承它）。所以**不包一层自以为是的子类**，而是写一条
   断言该行为的测试（`test_every_record_is_flushed_immediately`）：
   哪天有人换了 handler 类型，那条测试要能红。为什么在乎——无人值守下
   进程被强杀时，还留在缓冲区里的恰好是唯一能解释崩溃的那几行。

⚠️ `LOG_DIR` 一律**在函数体里现读**，不许在 import 时算好冻进默认参数。
   测试用 `monkeypatch.setattr(logs, "LOG_DIR", tmp_path)` 换掉它；冻住的
   话那些测试会去写**真实的** `data/logs/`，而且是静默地写。
   （`vigil/api.py` 的 `WEB_DIST` 是同一条纪律，那里写着原因。）
"""

from __future__ import annotations

import datetime as dt
import logging
import os
import pathlib
import re

from .config import REPO_ROOT

LOG_DIR = REPO_ROOT / "data" / "logs"


def _today() -> dt.date:
    """今天。**单独一个函数是为了让测试能换掉它**——跨零点换文件那条逻辑
    没法靠等真的等到零点来测，而不测就等于没有（本项目抓过的「空守卫」）。

    ⚠️ 生产路径也一律走它，不要图省事直接写 `dt.date.today()`：两处各写一份
    的话，测试换掉的那个和实际用的那个会不是同一个。
    """
    return dt.date.today()


LAST_ERROR_NAME = "LAST-ERROR.txt"

# 日志保留天数。spec §7 R4 记着「磁盘紧张（C: 7.8 GB / D: 29 GB）」，
# 而按当前体量（每天几千行、每行百字节）30 天是几 MB 量级。
KEEP_DAYS = 30

# LAST-ERROR.txt 里附多少行日志尾部。spec §4.8 的原文要求是「日志尾部」——
# 40 行足够覆盖一次 daily 的全部阶段摘要 + 出错前后的上下文。
TAIL_LINES = 40

_LOGGER_NAME = "vigil"
_configured = False

_LOG_NAME_RE = re.compile(r"^vigil-(\d{4})-(\d{2})-(\d{2})\.log$")


def log_path(day: dt.date | None = None) -> pathlib.Path:
    """当天的日志文件路径。`LOG_DIR` 现读（见模块 docstring 末尾）。"""
    day = day or _today()
    return LOG_DIR / f"vigil-{day:%Y-%m-%d}.log"


class DailyFileHandler(logging.FileHandler):
    """按日命名的文件 handler，**跨零点自动换文件**。

    `logging.FileHandler` 在构造时就把文件名定死了。一个从 23:50 跑到
    00:20 的 `refine` 会把第二天的事件全写进前一天的文件里，于是
    「日志是哪天跑的」这件事就对不上了——而无人值守下日志正是唯一的记录。
    """

    def __init__(self, directory: pathlib.Path) -> None:
        self._dir = directory
        self._day = _today()
        super().__init__(self._current_path(), encoding="utf-8", delay=False)

    def _current_path(self) -> str:
        # ⚠️ 这个方法**不能**叫 `_name`：`logging.Handler.__init__` 会给自己赋
        # `self._name = None`（它是 `set_name` 的存储），同名方法会被实例属性
        # **遮蔽**——构造时还调得动（那一次是在 `super().__init__` **之前**求值），
        # 跨零点再调就成了 `None()` → TypeError。实测踩过：是
        # `test_handler_rolls_over_at_midnight` 一红就红在这一行抓出来的。
        return str(self._dir / f"vigil-{self._day:%Y-%m-%d}.log")

    def emit(self, record: logging.LogRecord) -> None:
        """跨零点就换文件；**写不进去就抛，不许静默继续**。

        ⚠️ 这与 `prune_old_logs` 的「吞」是**刻意的不对称**，不是不一致：

          · 轮转失败 = **打扫**失败，丢的是历史日志 → 吞掉，影响不到本次运行；
          · 这里失败 = **本次运行的记录没了** → **抛**。

        为什么抛：无人值守下退出码是唯一还活着的信号（任务计划丢弃 stderr、
        磁盘满时连 LAST-ERROR.txt 也写不出来）。这时候静默继续，会得到一个
        `exit 0 但日志有洞` 的运行——**正是 M4 要消灭的那个形状**：
        「没人看得见」比「这次没跑完」更糟。代价（磁盘满到连 LAST-ERROR.txt
        都写不出来）是物理约束，接受。

        ⚠️ **「写失败会不会抛」不由本方法决定，由 `handleError` 决定。**
        `logging.StreamHandler.emit`（下面 `super().emit` 调到的那个）自带
        `except Exception: self.handleError(record)`，而**标准实现的
        `handleError` 只往 stderr 打一份 traceback、不重抛**——于是本方法
        这里写的「抛」在覆盖 `handleError` 之前**是空头支票**：真实写失败
        （磁盘满）被吞掉，外面一个字都看不见。详见本类下面的 `handleError`。
        ⚠️ 同一条纪律也适用于「文件 handler 每条记录都 flush」：
        那是 `StreamHandler.emit` 自带的行为，由
        `test_every_record_is_flushed_immediately` 守卫——哪天有人换了
        handler 类型，那条测试要能红。
        """
        today = _today()
        if today != self._day:
            self._day = today
            self.acquire()
            try:
                if self.stream:
                    self.stream.close()
                    self.stream = None
                self.baseFilename = os.path.abspath(self._current_path())
                self.stream = self._open()
            finally:
                self.release()
        super().emit(record)

    def handleError(self, record: logging.LogRecord) -> None:
        """不让 logging 把写失败吞掉——**原样抛出**。

        ⚠️ 为什么要覆盖：`logging.StreamHandler.emit` 自带
        `except Exception: self.handleError(record)`，而**标准实现**的 `handleError`
        只往 stderr 打一份 traceback、**不重抛**。任务计划**丢弃 stderr** ⇒
        磁盘满时「日志写不进去」这件事**外面一个字都看不见**——正是本模块
        `emit` 那条 docstring 说的「`exit 0` 但日志有洞」，也正是 M4 要消灭的形状。

        ⚠️ **与标准库行为相反是刻意的**，理由与轮转失败那条对称：
          · 轮转失败 = **打扫**失败（丢的是历史日志）→ **吞掉**
          · 写失败   = **本次运行的记录没了**          → **抛**
        代价（这一次运行会中断）是**知情的取舍**：**「没人看得见」比「这次没跑完」更糟**。

        ⚠️ 这里用裸 `raise` 重抛当前异常。这是安全的：**本方法只由 handler 的
        `emit` 在 `except` 里调用**（`logging.Handler.handle` 不调它，
        `Logger.callHandlers` 也不调），因此必有活动异常。
        """
        raise


def setup(*, level: int = logging.INFO) -> logging.Logger:
    """装好文件 handler，返回 vigil 的 logger。**幂等**。

    幂等的硬理由：`vigil daily` 要先配一次，若它内部调的三个阶段各自再配
    一次，同一行就会在文件里出现三遍——而「日志里同一句话出现三次」会让
    人以为跑了三次。

    只装文件 handler，**不装控制台 handler**：人看的输出归 `emit()` 管
    （见模块 docstring 第 2 条），两条路各写各的才不会有先后与重复问题。
    """
    global _configured

    logger = logging.getLogger(_LOGGER_NAME)
    if _configured:
        return logger

    logger.setLevel(level)
    # 不向 root 冒泡：否则任何第三方库调 basicConfig 都会让我们多印一份。
    logger.propagate = False

    directory = LOG_DIR          # 现读，见模块 docstring 末尾
    directory.mkdir(parents=True, exist_ok=True)

    handler = DailyFileHandler(directory)
    handler.setFormatter(
        logging.Formatter("%(asctime)s %(levelname)-5s %(message)s",
                          datefmt="%Y-%m-%d %H:%M:%S")
    )
    logger.addHandler(handler)

    _configured = True
    return logger


def harden_streams() -> None:
    """把 stdout / stderr 的**编码错误处理**改成 `replace`。

    ⚠️ **这是中文 Windows 上的必修项，不是美化**（实测 2026-10-15）：
    Windows 的 Python **默认按 ANSI 代码页开 stdout**（中文机器上即 **GBK**），
    而本项目的人读输出里有大量 GBK **编不出来**的字符 —— `✓` `✗` `⚠` `⇒` `⏰` `²`…
    后果是程序在**成功路径**上崩：

        UnicodeEncodeError: 'gbk' codec can't encode character '\u2713'

    实测影响：`vigil demo` 退出码 1、库**只建到 329/1237 条**（崩在中途）——
    而 README 写着「第 ① 步在任何装了 Python 3.11+ 的机器上都能跑通」。
    组委会复现时若用中文 Windows，这一条**当场不成立**（赛题第⑦条红线）。

    ⚠️ **为什么是 `errors="replace"` 而不是 `reconfigure(encoding="utf-8")`**：
    后者会让 UTF-8 字节被 GBK 终端解释成**满屏乱码**（中文全毁）；而
    `replace` **保留 GBK**——中文照常显示，只有那几个稀有符号降级成 `?`。
    **中文正常 > 符号漂亮**，且两者都不崩。

    ⚠️ `reconfigure` 只在 `TextIOWrapper` 上有（Py3.7+）。测试里 stdout 常被换成
    `StringIO`（没有这个方法）或已被包装，所以两种异常都要吞：
    `AttributeError`（不是那个类）、`ValueError`（已 close/detach 的流）。
    **这个函数永远不该让程序起不来** —— 它是兜底，不是前置条件。

    ⚠️ **放在 `logs` 而不是 `cli`**（M7 抽出来）：`scripts/bench.py` 也要它 ——
    实测那个脚本自己在中文 Windows 上崩过（`²` 编不出来）。
    留在 `cli` 里的话，每个新脚本都要再抄一遍，而**抄漏的那次会在别人的机器上崩**。
    """
    import sys

    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(errors="replace")
        except (AttributeError, ValueError):
            pass


def emit(message: str) -> None:
    """人看的原文走 stdout，日志留一份带时间戳的。CLI 与 daily 的唯一输出口。

    ⚠️ **先 print 再 log，顺序不能反**（模块 docstring 第 2 条）。
    """
    print(message)
    logging.getLogger(_LOGGER_NAME).info(message)


def reset() -> None:
    """拆掉已装的 handler 并复位幂等标志。**只给测试用**。

    生产代码里没有任何调用点，因为没有「该把日志关了」的时刻。
    """
    global _configured
    logger = logging.getLogger(_LOGGER_NAME)
    for handler in list(logger.handlers):
        logger.removeHandler(handler)
        handler.close()
    _configured = False


def clear_last_error() -> None:
    """开跑前清掉上一次的 LAST-ERROR.txt。

    ⚠️ 必须在**开跑时**清，不是成功时清：成功时才清的话，一个启动即崩
    （连本函数都没跑到）的进程会留下上一次的旧错误，被读成"这次又失败了"。
    开跑时清 ⇒ 「文件存在」⟺「本次运行失败过」。
    """
    try:
        (LOG_DIR / LAST_ERROR_NAME).unlink()
    except FileNotFoundError:
        pass


def _log_tail(lines: int) -> str:
    """当天日志的最后 N 行。读不到就返回空串（不抛）。"""
    try:
        text = log_path().read_text(encoding="utf-8")
    except OSError:
        return ""
    rows = text.splitlines()
    return "\n".join(rows[-lines:])


def write_last_error(message: str, *, tail_lines: int = TAIL_LINES) -> pathlib.Path:
    """把失败摘要 + 日志尾部写进 LAST-ERROR.txt（spec §4.8 的原文要求）。

    ⚠️ 尾部从**当天的日志文件**读，不从内存缓冲读——内存里只有本进程写的行，
    而这条路径**本来就要覆盖"失败发生在别的进程里"**（.cmd 的兜底分支）。
    """
    directory = LOG_DIR          # 现读
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / LAST_ERROR_NAME
    stamp = dt.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    body = f"[{stamp}] {message}\n"
    tail = _log_tail(tail_lines)
    if tail:
        body += f"\n--- 日志尾部（{pathlib.Path(log_path()).name}）---\n{tail}\n"
    path.write_text(body, encoding="utf-8", newline="\n")
    return path


def prune_old_logs(*, keep_days: int = KEEP_DAYS,
                   today: dt.date | None = None) -> list[str]:
    """删掉 `keep_days` 天前的日志文件，返回被删的文件名。

    ⚠️ **只认 `vigil-YYYY-MM-DD.log` 这个形状**，不用宽 glob。用 `*.log`
    的话，日后往这个目录放任何东西都会被无声吃掉——而「日志轮转把别的
    东西删了」是最难查的一类事故。`LAST-ERROR.txt` 同理，不在删除范围。

    边界取「**严格早于** today - keep_days」：恰好第 keep_days 天的保留。
    """
    directory = LOG_DIR          # 现读
    if not directory.is_dir():
        return []
    cutoff = (today or _today()) - dt.timedelta(days=keep_days)

    removed: list[str] = []
    for p in sorted(directory.iterdir()):
        m = _LOG_NAME_RE.match(p.name)
        if not m or not p.is_file():
            continue
        try:
            day = dt.date(int(m.group(1)), int(m.group(2)), int(m.group(3)))
        except ValueError:
            continue          # 文件名像但日期非法（如 2026-13-45），不碰它
        if day < cutoff:
            try:
                p.unlink()
            except OSError:
                continue      # 被别的进程占着就跳过，删日志失败不该掀掉整轮
            removed.append(p.name)
    return removed
