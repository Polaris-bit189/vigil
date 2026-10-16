"""单实例锁：同一时刻只许一个写库的 vigil 进程。

为什么需要（M4 规划期实测）：全仓**没有任何锁、没有 PID 文件、没有
`busy_timeout`**。两个 `vigil refine` 同时跑会各自读到同一批
`pending_messages`（谁都没看见对方的 `refine_runs` 行），于是同一批消息
抽两遍、产出两份重复 items、白烧两份 token。而任务计划**会**双触发：
触发器重叠、上一次还没跑完、用户手动又跑一次——都是现实场景。

⚠️ **为什么用操作系统级字节范围锁，而不用"建锁文件"（`O_CREAT|O_EXCL`）**：
实测（见计划 §二 发现 1）操作系统锁在进程被**强杀**后由内核回收，锁文件
仍在磁盘上但锁已经没了。而"建文件当锁"在强杀后会把文件永远留下，下次
启动误判成"已有实例在跑"，逼你写一套"这个文件是不是陈旧的"判定——
那套判定本质不可靠（PID 会复用、时间戳会被调），而且它**恰好会在最需要
它的场景（断电重启）下给出错误答案**。

⚠️ **不做重入**：同一进程里拿两次会抛 `AlreadyRunning`，这正是我们要的
行为，也是测试覆盖的。需要串联多个写阶段时，由**最外层**加一次锁，
内层直接调库函数而不调 `cmd_*`（`vigil/daily.py` 就是这么做的）。
"""

from __future__ import annotations

import os
import sys
from types import TracebackType

from .config import REPO_ROOT

LOCK_PATH = REPO_ROOT / "data" / "vigil.lock"

# Windows 上锁哪一字节。**刻意不锁第 0 字节**：
# Windows 的字节范围锁不只是"别人锁不上"，它同时让**别人读不了**被锁的
# 那几个字节。最小复现（A 进程持锁，B 进程读同一个锁文件）：
#   锁 [0,1) 时                B 读 pid → PermissionError 13（读不到）
#   锁 [1000000,1000001) 时    B 读 pid → 'pid=…' 正常读出来
# 而锁文件的第一行正是**写给排障的人看的 pid**。锁在第 0 字节上，等于把
# 唯一的线索锁进保险箱：恰恰在你需要它的时候（另一个实例正在跑）读不到，
# `AlreadyRunning` 就只剩一句"已有实例在跑"，说不出是谁在跑。
# 所以锁一个**永远不会有内容的远偏移**（文件只写十几个字节），文本区对
# 任何读者都是可读的。
# ⚠️「锁在百万位会不会把文件撑大」——**不会**：偏移只是个锁区间，
# 不写数据也不分配（实测 `dir` 与 `fstat` 都仍是 11 字节；连续 5 轮
# 拿锁/放锁后大小无累积）。
# 跨进程互斥与「强杀后由内核回收」都已在这个偏移上重新实测过，与
# 发现 1 的行为一致（对比数据：跨进程拿锁 6.0ms；8 个进程同时起最慢 10.7ms）。
# POSIX 侧用 flock，锁的是整个文件，没有这个问题，故不需要偏移。
_LOCK_OFFSET = 1_000_000


class AlreadyRunning(RuntimeError):
    """已有实例在跑。消息里带上是哪个 pid 拿着锁，便于人工查看。"""


def _lock_fd(fd: int) -> None:
    """对 fd 的 `_LOCK_OFFSET` 那一字节加**非阻塞**排他锁。拿不到就抛 OSError。

    ⚠️ 锁的字节范围从**当前文件位置**算起（Windows），所以必须先用
    `os.lseek` 把位置挪到要锁的字节上——位置不对就锁到别处去了
    （实测：锁完之后再写内容会把位置推到文件尾，此时解锁会
    `PermissionError: 13`，因为解的是"没被锁的那一段"）。
    """
    if sys.platform == "win32":
        import msvcrt

        os.lseek(fd, _LOCK_OFFSET, os.SEEK_SET)
        msvcrt.locking(fd, msvcrt.LK_NBLCK, 1)
    else:
        import fcntl

        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)


def _unlock_fd(fd: int) -> None:
    if sys.platform == "win32":
        import msvcrt

        os.lseek(fd, _LOCK_OFFSET, os.SEEK_SET)
        msvcrt.locking(fd, msvcrt.LK_UNLCK, 1)
    else:
        import fcntl

        fcntl.flock(fd, fcntl.LOCK_UN)


def _read_holder(path: os.PathLike[str] | str) -> str:
    """读锁文件里上次写下的 "pid=…" 一行（读不到就返回空串）。"""
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as f:
            return f.read(200).strip()
    except OSError:
        return ""


class SingleInstance:
    """上下文管理器。拿不到锁 → 抛 `AlreadyRunning`（**不等待、不重试**）。

    不等待是刻意的：无人值守下"等待"会变成任务计划里的僵尸进程，而
    「已经有实例在跑」本来就该原样告诉调度器（CLI 映射成退出码 2）。
    """

    def __init__(self, path: os.PathLike[str] | str | None = None) -> None:
        self._path = str(path or LOCK_PATH)
        self._fd: int | None = None

    def __enter__(self) -> "SingleInstance":
        parent = os.path.dirname(self._path)
        if parent:
            os.makedirs(parent, exist_ok=True)

        fd = os.open(self._path, os.O_RDWR | os.O_CREAT)
        try:
            _lock_fd(fd)
        except OSError:
            holder = _read_holder(self._path)
            os.close(fd)
            raise AlreadyRunning(
                f"已有实例在跑（锁：{self._path}；上次写入：{holder or '无'}）\n"
                f"  锁由操作系统持有，文件只是记号：确认没人在跑时删掉它不影响\n"
                f"  下次启动；正被持有时 Windows 会拒绝删除——拒绝本身就是\n"
                f"  「有人在跑」的旁证。"
            ) from None
        self._fd = fd

        # 把 pid 写进去，纯粹为了**人**排障时知道是谁拿着；锁的正确性不靠它。
        try:
            os.lseek(fd, 0, os.SEEK_SET)
            os.truncate(fd, 0)
            os.write(fd, f"pid={os.getpid()}\n".encode("utf-8"))
        except OSError:
            pass          # 写不进去不影响锁的效力
        return self

    def __exit__(self, exc_type: type[BaseException] | None,
                 exc: BaseException | None,
                 tb: TracebackType | None) -> None:
        if self._fd is None:
            return
        try:
            _unlock_fd(self._fd)
        except OSError:
            pass
        finally:
            os.close(self._fd)      # 关句柄本身也会释放锁，双保险
            self._fd = None
