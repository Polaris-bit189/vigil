"""单实例锁的测试。

⚠️ 跨进程锁**只能用两个真进程测**。同进程内拿两次锁，`msvcrt.locking`
按文件句柄判定，测出来的是"同一个进程能不能锁两次"——那正是我们要禁止
的用法，却**测不出**真正的并发保护。所以下面用 subprocess 起真子进程。

⚠️ 全部用 tmp_path 里的锁文件，绝不碰 `data/vigil.lock`。
"""

from __future__ import annotations

import os
import subprocess
import sys
import time
import pathlib

import pytest

from vigil import lock
from vigil.config import REPO_ROOT


@pytest.fixture
def lockfile(tmp_path):
    return tmp_path / "t.lock"


def test_second_instance_raises_already_running(lockfile):
    """同一进程里拿两次 → 第二次必须抛（且**不等待**）。

    「不等待」是刻意的：无人值守下等待会变成任务计划里的僵尸进程，
    而"已经有实例在跑"这件事本来就该原样告诉调度器。
    """
    with lock.SingleInstance(lockfile):
        # ⚠️ 取 N 次的**最小值**，而不是单次测量。
        # 单次会被瞬时负载污染（杀软扫新建文件、磁盘 I/O 争用）：实测在并发跑多个
        # agent 的机器上，干净树 3 次跑红了 1 次——而这条断言常态是**毫秒级**
        # （整份 test_lock.py 跑 5 遍都是 1.1–1.2s）。min 对瞬时尖峰最稳健，
        # 且它测的**仍是同一件事**：拿不到锁时**立刻**失败。
        waits = []
        for _ in range(5):
            t0 = time.monotonic()
            with pytest.raises(lock.AlreadyRunning) as ei:
                with lock.SingleInstance(lockfile):
                    pass
            waits.append(time.monotonic() - t0)
        assert str(lockfile) in str(ei.value)

    # ⚠️「不等待」得**能量出来**才算数：Windows 上 msvcrt 的**阻塞**版
    # `LK_LOCK` 在放弃前会重试约 10 次（实测 9.1s），最后抛的**是同一个
    # OSError** —— 而 `__enter__` 把它转成 `AlreadyRunning`，于是单看异常
    # 类型，阻塞与不阻塞**完全同形**（实测：把 `LK_NBLCK` 换成 `LK_LOCK`，
    # 当时本文件 4 条判据**全绿**，只是整份慢了，19.34s）。所以这条时间
    # 上限不是点缀，它是"非阻塞"这个冻结语义**唯一**的可观测证据。
    # 上限 2s：正确路径是毫秒级（本文件 5 条跑完约 1s），阻塞路径 9s 起。
    assert min(waits) < 2.0, (
        f"第二次尝试最快也等了 {min(waits):.2f}s"
        f"（5 次分别为 {[round(w, 3) for w in waits]}）"
        f" —— 锁是阻塞的，无人值守下会变成僵尸进程"
    )


def test_message_names_the_holder(lockfile):
    """冻结要求（计划 §三 3.2）：消息里必须带上是哪个 pid 拿着锁。

    ⚠️ 这条判据背后有个**看不见的坑**：Windows 的字节范围锁不只让人锁不上，
    它还让另一个句柄**读不了被锁的那几个字节**。锁若在第 0 字节上，排障的人
    恰好在这时读不到 pid（实测：`open(锁文件).read(200)` → PermissionError 13），
    消息就永远只剩一句"已有实例在跑"，说不出是谁在跑——而那正是它存在的理由。
    """
    with lock.SingleInstance(lockfile):
        with pytest.raises(lock.AlreadyRunning) as ei:
            with lock.SingleInstance(lockfile):
                pass
    assert f"pid={os.getpid()}" in str(ei.value)


def test_missing_parent_directory_is_created(tmp_path):
    """父目录不存在**不是**假想边界：默认锁路径是 `data/vigil.lock`，而
    `data/` 在 .gitignore 里**不进 git**——新克隆的机器上它根本不存在。

    少了 `makedirs`，新机器上第一次跑 `vigil daily` 会死在
    `FileNotFoundError` 上，而任务计划只会把它记成"跑失败了"，没人看得懂。
    """
    deep = tmp_path / "not" / "yet" / "there" / "t.lock"
    with lock.SingleInstance(deep):
        assert deep.parent.is_dir()
    with lock.SingleInstance(deep):      # 第二次也要能拿到（目录已存在）
        pass


def test_lock_released_after_exit(lockfile):
    with lock.SingleInstance(lockfile):
        pass
    with lock.SingleInstance(lockfile):   # 必须能拿到
        pass


def test_lock_released_on_exception(lockfile):
    with pytest.raises(ValueError):
        with lock.SingleInstance(lockfile):
            raise ValueError("模拟中途失败")
    with lock.SingleInstance(lockfile):
        pass


def test_lock_released_when_holder_is_force_killed(lockfile):
    """⭐⭐ 这是本任务存在的那条判据：**持锁进程被强杀后，锁必须自动释放。**

    它是 M4 锁设计的唯一事实依据（计划 §二 发现 1）。用 PID 文件 + 陈旧
    时间戳那套方案会在这一步失败——强杀不会执行任何清理代码，锁文件会
    永远留在磁盘上，下次启动误判成"已有实例在跑"。

    操作系统级字节范围锁由内核在进程终止时回收，所以这里必须绿。
    ⚠️ 杀进程**按 PID**，禁止 `taskkill /F /IM`（会误杀别的进程）。
    """
    holder = subprocess.Popen(
        [sys.executable, "-c",
         "import sys; sys.path.insert(0, r'%s');"
         "from vigil import lock;"
         "ctx = lock.SingleInstance(r'%s'); ctx.__enter__();"
         "print('held', flush=True);"
         "import time; time.sleep(60)" % (str(REPO_ROOT), str(lockfile))],
        stdout=subprocess.PIPE, text=True,
    )
    try:
        assert holder.stdout.readline().strip() == "held"

        # 持锁期间：另一个进程拿不到
        with pytest.raises(lock.AlreadyRunning):
            with lock.SingleInstance(lockfile):
                pass

        # 强杀（不执行任何 finally / atexit / __exit__）
        subprocess.run(
            ["taskkill", "/F", "/T", "/PID", str(holder.pid)],
            capture_output=True, check=False,
        )
        holder.wait(timeout=10)
        time.sleep(0.5)          # 给内核一点回收时间

        # ⭐ 内核已回收 → 必须能拿到
        with lock.SingleInstance(lockfile):
            pass
    finally:
        if holder.poll() is None:
            subprocess.run(
                ["taskkill", "/F", "/T", "/PID", str(holder.pid)],
                capture_output=True, check=False,
            )
