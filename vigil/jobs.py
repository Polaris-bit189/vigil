"""后台 job：把「按了要等」的活儿（LLM 重排）挪出请求线程。

**为什么要它**（spec §4.2 ④ 明写「328 条 ≈ 11 批 ≈ 1-2 分钟，是"按了要等"的活儿
→ 需要 job 机制」）：一个挂 1–2 分钟的 HTTP 请求在前端看起来就是**卡死**——
用户会刷新、会再点一次，而两次点击就是两份 token。

**形态上刻意从简的三处**（单机自用工具，别过度设计）：
* 状态**只在内存里**——重启服务就没了。这不是妥协：job 的产物（overlay 事件）
  是持久的，丢的只是"进度显示"。
* **不做持久化队列**、不做重试、不做优先级。一次只跑一个。
* **不拿 `lock.SingleInstance`**（跨进程那把）。理由见 `submit` 的 docstring。
"""

from __future__ import annotations

import threading
import time
import uuid
from collections.abc import Callable
from dataclasses import dataclass, field

# ⚠️ 历史保留条数。**必须有界**：服务跑一个月、点几百次重排，无界增长就是
# 一次慢速内存泄漏——而它不会以任何显眼的方式表现出来。
MAX_HISTORY = 20

STATE_RUNNING = "running"
STATE_DONE = "done"
STATE_FAILED = "failed"
STATE_CANCELLED = "cancelled"


class JobBusy(RuntimeError):
    """已有 job 在跑。API 侧映成 **409**（请求合法，与**当前状态**冲突）。"""


@dataclass
class Job:
    """一个后台活儿。字段是**可变**的（worker 线程会一直改它）⇒ 用 `list()` 取快照。"""

    job_id: str
    kind: str
    actor: str = ""
    state: str = STATE_RUNNING
    total: int = 0
    done: int = 0
    message: str = ""
    error: str = ""
    started_at: int = field(default_factory=lambda: int(time.time()))
    finished_at: int | None = None
    # ⚠️ 取消是**协作式**的：Python 没有办法从外面杀掉一个线程。
    # worker 必须在批次之间自己检查它（`reorder.plan_reorder` 的 `should_stop`）。
    # 漏掉检查点 = 点了取消没反应 = 用户以为按钮坏了。
    cancel: threading.Event = field(default_factory=threading.Event)


class JobRegistry:
    def __init__(self, max_history: int = MAX_HISTORY) -> None:
        self._lock = threading.Lock()
        self._jobs: dict[str, Job] = {}
        self._order: list[str] = []
        self._max = max_history

    def is_busy(self) -> bool:
        with self._lock:
            return any(j.state == STATE_RUNNING for j in self._jobs.values())

    def submit(self, kind: str, work: Callable[[Job], str], *, actor: str = "") -> Job:
        """起一个 job。已有 job 在跑 ⇒ 抛 `JobBusy`。

        ⚠️ **刻意不拿 `lock.SingleInstance`**（跨进程那把）。它看起来更稳妥，
        实际会造成一个具体的伤害：那把锁是**进程级、不重入、不等待**的，
        服务进程持着它跑 2 分钟，**08:00 的任务计划就会拿到退出码 2 并跳过
        当天的 export+refine+digest**——为了一个手动点的按钮，赔掉一整天的日报。

        ⚠️ 代价要**说清**：若 daily 恰好在跑，两边会各烧一份 token。这是
        可接受的（用户手点的操作、单用户自用），而上面那个伤害不可接受。
        """
        with self._lock:
            if any(j.state == STATE_RUNNING for j in self._jobs.values()):
                raise JobBusy("已经有一个后台任务在跑了，等它结束再试")
            job = Job(job_id=uuid.uuid4().hex[:12], kind=kind, actor=actor)
            self._jobs[job.job_id] = job
            self._order.append(job.job_id)
            while len(self._order) > self._max:
                self._jobs.pop(self._order.pop(0), None)

        def runner() -> None:
            try:
                job.message = work(job) or ""
                if job.cancel.is_set() and job.state == STATE_RUNNING:
                    job.state = STATE_CANCELLED
                elif job.state == STATE_RUNNING:
                    job.state = STATE_DONE
            except BaseException as exc:  # noqa: BLE001 — **必须落进 job**，见下
                # ⚠️ 后台线程里没接住的异常会被 Python 打到 stderr 然后**消失**，
                # 而 stderr 属于 uvicorn 的日志。用户点完按钮看到的只有
                # 「一直转圈」——轮询端点必须能说出「失败了，原因是 X」。
                #
                # ⚠️ 捕 `BaseException` 而不是 `Exception`（T6 复审 Minor-3）：
                # 只捕 `Exception` 时，`KeyboardInterrupt` / `SystemExit` 会**穿透**
                # 这里 ⇒ `state` 永远停在 `running`（而 `finished_at` 已经写了）
                # ⇒ `is_busy()` 此后恒为真，**每一次**「起 job」都被 `JobBusy`
                # 挡掉，直到重启服务——一条**没有出口**的状态。
                # （⚠️ 别拿 `MemoryError` 当例子：它是 `Exception` 的子类，
                #  实测 `issubclass(MemoryError, Exception) is True`，拿它做测例
                #  是一条**修不修都绿**的空守卫。）
                #
                # ⚠️ 不 re-raise：worker 是 daemon 线程，主线程该收到的 Ctrl-C
                # 照样收到（它不经过这里）；这里只负责把状态**落下来**。
                job.state = STATE_FAILED
                job.error = f"{type(exc).__name__}: {exc}"
            finally:
                job.finished_at = int(time.time())

        threading.Thread(target=runner, name=f"vigil-{kind}", daemon=True).start()
        return job

    def get(self, job_id: str) -> Job | None:
        with self._lock:
            return self._jobs.get(job_id)

    def list(self) -> list[Job]:
        """**新的在前**。"""
        with self._lock:
            return [self._jobs[i] for i in reversed(self._order) if i in self._jobs]

    def cancel(self, job_id: str) -> bool:
        with self._lock:
            job = self._jobs.get(job_id)
        if job is None or job.state != STATE_RUNNING:
            return False
        job.cancel.set()
        return True


# 模块级单例：`api.py` 用它。**测试不要共享它**——每个用例自己 new 一个，
# 否则一个用例残留的 running job 会让下一个用例撞 `JobBusy`，
# 而那是一条与环境相关的假红（本项目在 `data/vigil.lock` 上吃过这个亏，
# 见 `tests/conftest.py::_isolate_lock_path`）。
registry = JobRegistry()
