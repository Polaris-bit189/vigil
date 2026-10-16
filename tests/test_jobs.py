"""后台 job 注册表 —— M6 T6。"""

from __future__ import annotations

import threading
import time

from vigil import jobs


def _wait(job, timeout=5.0):
    deadline = time.monotonic() + timeout
    while job.state == "running" and time.monotonic() < deadline:
        time.sleep(0.01)
    return job


def test_a_job_runs_and_reports_progress():
    reg = jobs.JobRegistry()

    def work(job):
        job.total = 3
        for _ in range(3):
            job.done += 1
        return "搞定"

    job = reg.submit("reorder", work, actor="reorder:1")
    _wait(job)
    assert job.state == "done"
    assert job.done == 3
    assert job.message == "搞定"
    assert job.actor == "reorder:1"
    assert job.finished_at is not None


def test_a_failing_job_records_the_error_instead_of_dying_silently():
    """⭐ 后台线程里抛出的异常**必须落到 job 上**。

    ⚠️ 这是本模块最重要的一条：线程里没接住的异常会被 Python 打到 stderr
    然后**消失**——而 stderr 属于 uvicorn 的日志，用户点完按钮看到的只有
    「一直转圈」。轮询端点必须能说出「失败了，原因是 X」。
    """
    reg = jobs.JobRegistry()

    def boom(job):
        raise RuntimeError("模型返回了看不懂的东西")

    job = reg.submit("reorder", boom)
    _wait(job)
    assert job.state == "failed"
    assert "看不懂" in job.error


def test_a_base_exception_also_lands_on_the_job():
    """⚠️ **`BaseException` 也必须落进 job**（T6 复审 Minor-3）。

    ⚠️ 只捕 `Exception` 的后果不是"少记一条错误"，而是**卡死**：
    穿透 `except Exception` 的那几类不会落状态，于是 `state` **永远停在 running**
    （而 `finished_at` 已经写了），而 `is_busy()` 看的就是 `state` ⇒ 此后
    **每一次**「起 job」都被 `JobBusy` 挡掉，直到重启服务为止——一条**没有出口**的状态。

    ⚠️ **用 `KeyboardInterrupt` 而不是复审原文举的 `MemoryError`**——
    后者是个**错的例子**，实测：`issubclass(MemoryError, Exception) is True`
    ⇒ 拿它写这条用例，**修之前就是绿的**（一条空的守卫，E7 形态 4）。
    `BaseException` 的直接子类只有五个：`BaseExceptionGroup` / `Exception` /
    `GeneratorExit` / `KeyboardInterrupt` / `SystemExit`，真正会从 worker 里
    冒出来的是 `KeyboardInterrupt`（**本机就是 uvicorn 所在的那个终端**，
    用户 Ctrl-C 一下就会打到正在跑重排的线程上）与 `SystemExit`。

    ⚠️ 变异反证：把 `runner` 里的 `except BaseException` 改回 `except Exception`
    ⇒ 本条**必红**（`job.state` 停在 "running"）。
    """
    reg = jobs.JobRegistry()

    def boom(job):
        raise KeyboardInterrupt("模拟：uvicorn 那个终端被按了 Ctrl-C")

    job = reg.submit("reorder", boom)
    _wait(job)
    assert job.state == "failed"
    assert "KeyboardInterrupt" in job.error
    assert reg.is_busy() is False, (
        "state 卡在 running ⇒ 这个 registry 从此再也起不了 job（只能重启服务）"
    )


def test_a_second_job_is_refused_while_one_is_running():
    """⭐ 同一时刻只许一个 job。

    ⚠️ 两个重排同时跑 = **烧两份 token 得到同一份结果**，而且两份
    `set_kind` 事件会交错落进日志（撤销会变得没法解释）。

    ⚠️ 变异反证：把 `submit` 里那句 `if self.is_busy(): raise` 删掉
    ⇒ 本条**必红**。
    """
    reg = jobs.JobRegistry()
    gate = threading.Event()

    def slow(job):
        gate.wait(5)

    reg.submit("reorder", slow)
    try:
        import pytest
        with pytest.raises(jobs.JobBusy):
            reg.submit("reorder", slow)
    finally:
        gate.set()


def test_cancel_asks_the_worker_to_stop():
    """取消是**协作式**的：置一个 Event，由 worker 自己在批次之间检查。

    ⚠️ 不是 `threading.Thread.kill`（Python 根本没有这个 API）——
    所以 `should_stop` 必须被**每一批之间**检查，漏一处就是"点了取消没反应"。
    """
    reg = jobs.JobRegistry()
    seen: list[bool] = []

    def work(job):
        for _ in range(200):
            if job.cancel.is_set():
                seen.append(True)
                return "已取消"
            time.sleep(0.005)
        seen.append(False)
        return "跑完了"

    job = reg.submit("reorder", work)
    time.sleep(0.05)
    assert reg.cancel(job.job_id) is True
    _wait(job)
    assert seen == [True]
    assert job.state == "cancelled"


def test_the_registry_keeps_a_bounded_history():
    """⚠️ 内存里的 job 表**必须有界**：服务跑一个月，点几百次重排，
    无界增长就是一次慢速内存泄漏。新的在前，只留最近 N 条。"""
    reg = jobs.JobRegistry(max_history=3)
    for i in range(6):
        _wait(reg.submit("reorder", lambda job: f"第{i}次"))
    assert len(reg.list()) == 3
