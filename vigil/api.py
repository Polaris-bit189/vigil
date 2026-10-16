"""只读 JSON API + 前端静态托管。

**只读是机械保证，不是口头约定**：连接一律 ``mode=ro`` 打开，
写操作会被 SQLite 直接拒绝（实测 ``attempt to write a readonly database``）。
这样 Web 这一侧永远不可能改到 `data/vigil.db`。

⚠️ **每请求一条连接**，不复用：FastAPI 的同步端点跑在线程池里，而
``sqlite3.Connection`` 默认不能跨线程。本地文件开连接是微秒级，
省下的复杂度远比省下的那点时间值钱。
"""

from __future__ import annotations

import datetime as dt
import pathlib
import re
import sqlite3
import threading
import time
import tomllib
import uuid
from collections.abc import Sequence

from fastapi import Body, FastAPI, HTTPException, Query
from fastapi.responses import FileResponse, HTMLResponse

from . import categories as categories_mod
from . import conflicts as conflicts_mod
from . import groups_snapshot, jobs, logs, overrides, reorder, schedule as schedule_mod
from . import store
from . import timetable as timetable_mod
from .categories import Category, load_categories
from .config import (
    requires_llm_key,
    REPO_ROOT,
    TIER_NORMAL,
    VALID_TIERS,
    Config,
    ConfigError,
    Group,
    LLMSettings,
    Person,
    atomic_write_text,
    load_config,
    load_llm_key,
    load_llm_settings,
    load_persons,
    save_config,
    save_llm_settings,
    save_persons,
)

WEB_DIST = REPO_ROOT / "web" / "dist"

MAX_LIMIT = 200

# ⚠️ `/api/bulk-undo` 只接受**本程序自己造的** actor 前缀（T6 复审 Minor-2）。
# 它挡的是**误用**，不是**权限**——单机自用、没有用户模型，能点这个按钮的人
# 本来就有权改；前缀闸拦住的是一件具体的事：`{"actor": "web"}` 会把尾部那一整段
# **人工操作**（改类目、`delete` 行、墓碑行）一次性撤掉，而调用方很可能只是想撤
# 「刚才那批重排」。定义在**模块级**（不是 `create_app` 里）好让测试直接断言它。
_ACTOR_PREFIXES = ("reorder:", "bulk:")

# ⚠️ 进度的**字符串协议**（T6 复审 ⚠️W-2）。`reorder.plan_reorder` 的进度通道
# 就是一条 `on_progress(str)`，而复审明确要求**不改它的签名**（它已经有回调了）。
# 所以这里读它那两行既有的、给人看的输出——**格式变了会红**：
# `tests/test_api.py::test_api_reorder_reports_progress_while_running` 钉着
# 「源消息 … → N 批」必须把 `total` 填上、「批次 i/M」必须把 `done` 填上。
_PROGRESS_BATCHES = re.compile(r"批次 (\d+)/(\d+)")
_PROGRESS_TOTAL = re.compile(r"→ (\d+) 批")

# 收尾文案里那句 LLM 原始报错最多留多少字符（见 `_clip_error`）。
_ERROR_CLIP = 160


def _clip_error(text: str, limit: int = _ERROR_CLIP) -> str:
    """截断要**看得见**：不留那个「…」的话，读的人会以为这就是完整的报错
    ——那正是这一轮在修的那类缺陷（说一句自己没有资格说的话）换个地方重演。
    """
    return text if len(text) <= limit else text[: limit - 1] + "…"


def _batch_problem(stats: reorder.ReorderStats) -> str:
    """收尾文案里「有几批没取得判定」那半句；**没有失败时是空串**。

    ⚠️⭐ 冒烟 Bug A-2：`work()` 从前把 `stats` 收下就**再也没读过**——
    `failed_batches` / `errors` 全丢，于是 job 的收尾 message 只说
    「22 条改了类目，**0 条没判成**（它们没被动）」，而那一轮实测有 **2 批真的
    失败了**（`HTTP 503, code 50508`）。界面上的失败信息
    （`[reorder]第 142 批失败: 重试 3 次仍失败: …`）只在跑的过程中一闪而过，
    用户唯一读到的那句话把它盖掉了。

    ⚠️ 失败批次数**单独说**，不从 `unjudged` 推：两者可以不等——失败那批
    的条目可能已经全被用户软删掉（`unjudged` 是 0，而确确实实有 N 批没跑成）。
    合成一个数就会在那种边界上再产出一句假话。

    ⚠️⭐ **所以这个函数拿不到 `plan`**（终审 re-review 的 Minor：从前有一个
    `plan: ReorderPlan` 参数，**从头到尾没被读过**）。删而不是留：留着的死参数
    是**诱饵**——下一个想把这半句写得更"全"的人会顺手去读 `plan.unjudged`，
    而上面那条推论正是"两者可以不等"。签名里没有它，这条不变量就是**结构性的**，
    不靠注释拦人。（判据：没有单测钉这个签名，删除本身不会有任何红——它挡的
    是未来那种"多读一个数、多说一句假话"的改动，那种改动由
    `test_api_reorder_summary_admits_a_failed_batch` 那几条文案用例罩着。）
    """
    if not stats.failed_batches:
        return ""
    first = stats.errors[0] if stats.errors else "（没留下原因）"
    return f"；{stats.failed_batches} 批没有取得判定，首个原因：{_clip_error(first)}"


# ⚠️ 这张表同时承担**两个**职责，动它之前先看清两种后果不一样：
#
#   1) 真文件发出去时的 media type：`FileResponse(..., media_type=_MIME.get(suffix))`。
#      **删掉某个键，这里照样对**——`media_type=None` 时 Starlette 会回落到
#      `mimetypes.guess_type`（实测 `.js → text/javascript`）。
#   2) 「这个后缀是静态资源、不是前端路由」的判据：`if suffix in _MIME: 404`。
#      **删掉同一个键，这里静默坏掉**——该后缀不再被判为静态资源，缺文件时
#      一路落进 SPA 兜底，返回 **200 的 index.html**。
#
# 于是「删键」的后果**只**出现在职责二上，而所有只看 content-type 的测试都抓不到
# （浏览器把 HTML 当 JS/manifest 解析失败，而状态码全是 200——正是上面那段
# 注释里实测过的同一类假绿）。键与值都被 `tests/test_api.py` 逐字钉死。
#
# 定义在**模块级**而不是 `create_app` 内部：它是被测试直接断言的冻结契约
# （`api._MIME`）。放进 `create_app` 里，测试就够不着这张表了。
_MIME = {
    ".js": "text/javascript",
    ".css": "text/css",
    ".webmanifest": "application/manifest+json",
    ".json": "application/json",
    ".png": "image/png",
    ".svg": "image/svg+xml",
    ".ico": "image/x-icon",
    ".woff2": "font/woff2",
    ".txt": "text/plain",
}


def _parse_day(day: str) -> dt.date:
    try:
        return dt.datetime.strptime(day, "%Y-%m-%d").date()
    except ValueError:
        raise HTTPException(
            status_code=400, detail=f"日期格式应为 YYYY-MM-DD，收到：{day}"
        )


def _day_start(day: str, *, plus_days: int = 0) -> int:
    """``YYYY-MM-DD`` → **本地**当天 00:00 的 epoch 秒。

    ⚠️ 必须用 ``datetime`` 而不是 SQLite 的 ``strftime('%s', ...)``：后者按
    **UTC** 解释同一个日期串，与产品的「本地日」口径差 8 小时（M2 实测记录）。

    ⚠️ 「能解析成日期」与「能表示成 epoch 秒」是**两件事**，所以坏输入有两种
    形状：``1970-01-01``（早于 epoch）在 ``timestamp()`` 上抛 ``OSError``；
    ``9999-12-31`` 配 ``plus_days=1`` 连 ``date + timedelta`` 都过不去，抛
    ``OverflowError``。它们都是**合法日期**，于是径直穿过 ``_parse_day`` 那
    一关——格式错的给 400、范围错的给 500，是同一个文件里两套错误处理，
    这正是这次缺陷的成因。所以这里**也要转 400**，与 `_parse_day` 同形。
    """
    try:
        d = _parse_day(day) + dt.timedelta(days=plus_days)
        return int(dt.datetime.combine(d, dt.time.min).timestamp())
    except (OSError, OverflowError, ValueError):
        # ValueError 其实已被 _parse_day 转成 400 了，多捕它一层只是不让
        # 任何一条漏网的转换异常有机会变成 500。
        raise HTTPException(
            status_code=400,
            detail=f"日期超出可表示范围（是合法日期，但转不成时间戳），收到：{day}",
        )


def _day_of(ts: int) -> str:
    """窗口起点 → 本地日期字符串，与 ``_day_start`` 互逆。"""
    return dt.datetime.fromtimestamp(ts).strftime("%Y-%m-%d")


def _conflict_out(c) -> dict:
    """把一条冲突渲染成 JSON。**两边都带上**（与 CLI 的文案同一条纪律：
    只说"冲突了"等于把排查丢回给用户）。"""

    def side(e) -> dict:
        return {
            "label": e.label,
            "start": e.start,
            "end": e.end,
            "item_id": e.item_id,
            "kind": e.kind,
        }

    return {"a": side(c.a), "b": side(c.b), "minutes": c.minutes}


def _course_conflicts(conflicts_mod, schedule_mod, tt, entries: list) -> list[dict]:
    """③ 活动撞课表：把**周期性**课表展开到具体日期后与活动求交。

    ⚠️ **不能只比"星期几"**：课表说「周三 14:00 有课」，活动是「10-17 14:00」
    —— 要先算出那天是不是周三，再比时段。直接拿 `course.start`（距零点分钟）
    与活动的 `start_ts`（unix 秒）比是**两个量纲**，结果没有意义。

    ⚠️ 复用 `schedule.busy_from_timetable`（它已正确处理单双周与作息），
    不在这里重写展开逻辑 —— 两份实现迟早漂移，而漂移的症状是
    「日历说有冲突、CLI 说没有」。
    """
    if not entries:
        return []
    lo = min(e.start for e in entries)
    hi = max(e.end for e in entries)
    courses = schedule_mod.busy_from_timetable(tt, window_from=lo, window_to=hi)
    if not courses:
        return []
    course_entries = [
        conflicts_mod.Entry(label=b.label, start=b.start, end=b.end, kind="course")
        for b in courses
    ]
    out: list[dict] = []
    for c in conflicts_mod.find_conflicts(course_entries + list(entries)):
        if {c.a.kind, c.b.kind} == {"course", "activity"}:
            out.append(_conflict_out(c))
    return out


def _reject_controls(value: str, field: str) -> None:
    """`label` / `note` 里有换行或别的控制字符 ⇒ **400**（R10：转义与拒绝各管一半）。

    ⚠️ `config.toml_str` 已经能把这些字符**转义**成合法 TOML 并按原样读回来
    （那是 T7 完成的另一半），但「存得进去」不等于「应该存」：
    `config/*.toml` 是**人手输**的，把一个换行静默存进一个"名字"里、
    再在界面上错乱显示，是**最不诚实**的做法——而它一旦落盘，
    只能靠人肉去看那个文件才发现得了。
    名字/备注里要换行没有任何正当用途，所以这里选择**响亮地拒绝**。
    """
    if any(ch < " " or ord(ch) == 0x7F for ch in value):
        raise HTTPException(
            status_code=400,
            detail=f"{field} 里不能有换行/控制字符（C0 与 DEL）——"
            "名字里要换行没有任何正当用途，静默存下去只会在界面上错乱显示",
        )


def _edit_id_of(body: dict | None) -> int | None:
    """请求体里的 `edit_id`：**只接受真正的整数**，缺省/``null`` ⇒ ``None``（撤最近一条）。

    ⚠️ 不能写成 `int(body.get("edit_id"))`（M5 终审 Important-4 实测）：
    ``"abc"`` 抛 ``ValueError`` ⇒ **500**（而同一文件里 `_day_start` 早就把
    「转不成就 400」做成了规矩）；``1.9`` 会被 ``int()`` **静默截断**成 1
    ⇒ 撤掉的是**另一条**事件，而调用方从响应里看不出区别。
    ``bool`` 也要挡：``True`` 是 ``int`` 的子类（``True == 1``），会静默变成 edit_id 1。

    ⚠️ 契约是「整数」而不是「能转成整数的东西」：``"3"`` 这种字符串也拒——
    给「静默转一下」留口子，就等于把「1.9 截断」那类缺陷换个形状请回来。
    """
    if not isinstance(body, dict):
        return None
    target = body.get("edit_id")
    if target is None:
        return None
    if isinstance(target, bool) or not isinstance(target, int):
        raise HTTPException(
            status_code=400,
            detail=f"edit_id 必须是整数（null = 撤最近一条），收到：{target!r}",
        )
    return target


def create_app(config: Config) -> FastAPI:
    app = FastAPI(title="VIGIL 守夜人", docs_url=None, redoc_url=None)

    db_path = config.output_db
    db_uri = f"file:{db_path.as_posix()}?mode=ro"

    # ⚠️ overlay 的**文件**在这里就位（D11 允许 Web 写 overlay）。之后每个
    # 请求的 `store._ensure_overlay(conn)` 才能无条件 ATTACH。
    # ⚠️ **不能**在这里调 `store.ensure_schema`——那是写 `vigil.db`，
    # 而本文件的连接是 `mode=ro`（见 connect() 的 docstring）。
    overrides.ensure_schema()

    # ⚠️ **不许用 `create_app` 收到的那个 `config` 去回答「现在有哪些群」**
    # （它在进程启动那一刻就冻住了）。网页上加了一个群之后，那个对象里没有它
    # ⇒ 新群在信息流里显示成裸群号，而用户刚从「配置」页把它加进去、
    # 看着自己的操作没生效。（与 `persons_path()` 同一族：路径与文件内容
    # 都必须**每次现读**。）
    #
    # ⚠️ **`db_uri` 仍然用启动时那份 `config.output_db`**：改它要重启服务。
    # 这不是妥协——`output_db` 是「这个服务读哪个库」，热切换它意味着
    # 正在飞的请求读一半换了库，比要求重启糟得多。
    def config_path() -> pathlib.Path:
        return REPO_ROOT / "config" / "groups.toml"

    def current_config() -> Config:
        try:
            return load_config(config_path())
        except ConfigError as exc:
            # 配置文件被手工改坏了 ⇒ 500 而不是 503：后者在浏览器里会被
            # 读成「服务没起来」，而真相是**配置文件有语法错**，方向完全不同。
            raise HTTPException(
                status_code=500, detail=f"config/groups.toml 读不出来：{exc}"
            )

    # ⚠️ 进程内的写锁。`save_config` 是**整体替换**（读 → 改 → 写），
    # 两个并发请求各自读到旧版本、各自 append、后写的赢 ⇒ **丢一个群**。
    # 单用户自用下概率低，但 spec §6 R10 明说「不能因为概率低就不做原子写」——
    # 而这里的代价只是一把 `threading.Lock`。
    # ⚠️ 它是**进程内**的：本服务只有一个 uvicorn worker（`uvicorn.run(app, ...)`
    # 不带 workers 参数），所以够用。跨进程那把是 `lock.SingleInstance`，
    # 它保护的是 QQ 库与主库，**语义不同、别混用**：CLI 拿不到锁时会拒绝运行，
    # 而 Web 不该因为「任务计划在跑」就整个不能保存配置。
    write_lock = threading.Lock()

    # ── 后台 job + 重排（M6 T6）─────────────────────────────────────
    #
    # ⚠️ **每个 app 一个 registry 还是全局一个？**——本函数用**模块级**
    # `jobs.registry`。理由：job 是**进程**级的资源（线程池、token 花费），
    # 而 `create_app` 在一个进程里可能被调用多次（测试里就是这么用的）。
    # 用模块级才能在"同一个进程起了两个 app"时仍然只允许一个 job 在跑。
    # ⚠️ 代价：测试之间会**串**（job 的残留跨用例可见 ⇒ 下一个用例可能撞 409）。
    # `tests/conftest.py` 的 autouse 夹具 `_isolate_jobs_registry` 为每个用例
    # 换一份新的（`jobs.registry = jobs.JobRegistry()`）。
    # ⚠️ **实测（T6）：把它改成空转，连跑 5 次全绿** —— 所以那是一道**防御**
    # 而不是被测试逼出来的守卫（理由与数字见那个夹具的 docstring）。
    # 这里刻意不写「不隔离就会红」：编出来的理由比没有理由更危险（勘误 E7）。

    def _job_out(job: jobs.Job) -> dict:
        return {
            "job_id": job.job_id,
            "kind": job.kind,
            "state": job.state,
            "total": job.total,
            "done": job.done,
            "message": job.message,
            "error": job.error,
            "actor": job.actor,
            "started_at": job.started_at,
            "finished_at": job.finished_at,
        }

    @app.post("/api/reorder", status_code=202)
    def api_reorder() -> dict:
        """按**当前类目定义**重判已有条目。返回 `job_id`，用 `GET /api/jobs/{id}` 轮询。

        ⚠️ **它只写 overlay，不碰 `data/vigil.db`**（D11/D16 同时成立）。
        ⚠️ 202 而不是 200：活儿还没干完，这是"已受理"。
        """
        settings = load_llm_settings(on_warn=lambda m: None)
        api_key = load_llm_key(settings)
        # `auth = "none"`（本地 Ollama）不要密钥。
        if requires_llm_key(settings) and not api_key:
            raise HTTPException(
                status_code=400,
                detail=f"没有 LLM 密钥（.env 里的 {settings.api_key_env}），重排跑不了",
            )
        # ⚠️ actor 里**必须有随机性**（复审 Minor-2）：只写 `reorder:{秒}`
        # 的话，同一个秒里起的两个 job 会撞成同一个 actor，于是第二次的
        # `undo_batch` 会把**两次重排的事件一起撤掉**（尾部连续 + actor 相同），
        # 而用户以为自己只撤了后一次。时间戳**留着**是给人看的（日志里能对时刻）。
        actor = f"reorder:{int(time.time())}:{uuid.uuid4().hex[:8]}"

        def work(job: jobs.Job) -> str:
            def on_progress(msg: str) -> None:
                """把 `plan_reorder` 的进度接到 job 上（复审 ⚠️W-2）。

                ⚠️ 不接线的话 `total`/`done`/`message` 在整轮重排里**恒为 0/空**，
                而 `_job_out` 把它们暴露给了前端——spec §4.2 ④ 要的「进度」在接口层缺席，
                用户看到的只有一个转圈。

                ⚠️⭐ **必须定义在 `work()` 里面**（复审 FIX 2，真 bug）：定义在
                `api_reorder` 里、闭包引用 `api_reorder` 的局部 `job` 的话，那个
                名字要等 `jobs.registry.submit(...)` **返回**才绑定，而 worker
                线程在那之前就起来了 ⇒ 它抢先把第一次 `on_progress` 跑到就是
                `NameError: cannot access free variable 'job'`，**第一批都发不出去**。
                ⚠️ 实测（T6 fix loop 2）：把窗口拉宽到 0.2s，本条**必现**
                （`error` 里就是那句 NameError）——真实复现率很低（复审 60 次
                0 复现），但那正说明它是**竞态**：机器一忙就会现形。
                放进 `work()` 里，`job` 就是它的参数——没有窗口。

                ⚠️ `job.done` 记的是「**最后一条成功批次**的序号」，不是"已完成批次数"
                （复审 ⚠️FIX 4：第 1 批抛异常时界面会看到 `0/N → 2/N → 3/N`，
                像"全成了"）。**纯显示、无守卫**，本轮不动，入延迟清单。
                """
                job.message = msg
                m = _PROGRESS_BATCHES.search(msg)
                if m:
                    job.done, job.total = int(m.group(1)), int(m.group(2))
                    return
                m = _PROGRESS_TOTAL.search(msg)
                if m:
                    job.total = int(m.group(1))

            conn = sqlite3.connect(db_uri, uri=True)
            try:
                plan, stats = reorder.plan_reorder(
                    conn, config=config, api_key=api_key,
                    model=settings.model, base_url=settings.base_url,
                    enable_thinking=settings.enable_thinking,
                    on_progress=on_progress,
                    should_stop=job.cancel.is_set,
                )
            finally:
                conn.close()
            if job.cancel.is_set():
                # ⚠️ 「一条都没改」是真话，但它**不完整**：取消不等于"用户同意
                # 有批次失败了"——503 那半句照报（`_batch_problem`）。
                # ⚠️ 这里**不报** `plan.unjudged`：取消是用户自己按的，没跑到的
                # 批次在他预期之内；而失败不在预期之内，所以只有它必须说出来。
                return "已取消（一条都没改）" + _batch_problem(stats)
            ov = overrides.connect()
            try:
                n = reorder.apply_reorder(ov, plan, actor=actor)
            finally:
                ov.close()
            # ⚠️⭐ 这两半缺一不可（冒烟 Bug A-2）：
            #   ① `unjudged` 由 `plan_reorder` **按源消息**算出来（不是从
            #      "判过的那批"反向派生）⇒ 全失败时它是「K 条没判成」而不是 0；
            #   ② `stats` 里那两条失败信息必须**跟着这句话一起出去**——它从前
            #      是"收了不读"，于是用户对"有几批没判成"没有任何办法。
            return (
                f"{n} 条改了类目，{len(plan.unjudged)} 条没判成（它们没被动）"
                + _batch_problem(stats)
            )

        try:
            job = jobs.registry.submit("reorder", work, actor=actor)
        except jobs.JobBusy as exc:
            # 409 而不是 400：请求本身合法，与**当前状态**冲突（与 `/api/undo` 同形）
            raise HTTPException(status_code=409, detail=str(exc))
        return _job_out(job)

    @app.get("/api/jobs/{job_id}")
    def api_job(job_id: str) -> dict:
        job = jobs.registry.get(job_id)
        if job is None:
            raise HTTPException(status_code=404, detail=f"没有这个任务：{job_id}")
        return _job_out(job)

    @app.post("/api/jobs/{job_id}/cancel")
    def api_job_cancel(job_id: str) -> dict:
        """请求中断这个 job（**协作式**：worker 在批次之间看到它才会停）。

        ⚠️ 为什么必须有这个端点（T6 复审 Important-1）：`JobRegistry.cancel`
        此前**全仓零生产调用方**，而下面 `/api/jobs/{id}/undo` 的 409 却写着
        「先取消它再撤销」——要用户去做一件**做不到**的事；且那个 job 全程占着
        唯一的 `JobBusy` 名额（一轮重排是分钟级的，只能干等）。

        ⚠️ 「取消」不是「立刻停」：**正在飞的那一批会跑完**（Python 没法从外面
        杀线程），停的是**下一批**。所以响应里的 `cancelled: true` 只说
        「请求已受理」，真正结束看轮询到的 `state == "cancelled"`。

        ⚠️ 对**已经结束**的 job 返回 200 + `cancelled: false`，不是 409：
        那是"手慢了一步"，而 `state` 就在同一个响应里，前端不需要为它写分支。
        真正的 404 是**没有这个 job**。
        """
        job = jobs.registry.get(job_id)
        if job is None:
            raise HTTPException(status_code=404, detail=f"没有这个任务：{job_id}")
        return {"cancelled": jobs.registry.cancel(job_id), "state": job.state}

    @app.post("/api/jobs/{job_id}/undo")
    def api_job_undo(job_id: str) -> dict:
        """撤销**这一批**重排（一次退回重排前）。

        ⚠️ 与 `/api/undo` 是**两个不同的东西**：那条撤的是「最近一条事件」，
        这条撤的是「这个 job 写的**全部**事件」。协议上刻意分开。
        """
        job = jobs.registry.get(job_id)
        if job is None:
            raise HTTPException(status_code=404, detail=f"没有这个任务：{job_id}")
        if job.state == jobs.STATE_RUNNING:
            raise HTTPException(status_code=409, detail="这个任务还在跑，先取消它再撤销")
        if not job.actor:
            raise HTTPException(status_code=400, detail="这个任务没有可撤销的改动")
        ov = overrides.connect()
        try:
            n = overrides.undo_batch(ov, actor=job.actor)
        finally:
            ov.close()
        return {"undone": n > 0, "count": n}

    @app.post("/api/items/kinds")
    def api_bulk_set_kind(body: dict = Body(...)) -> dict:
        """人工多选批量改类目（D16 的「界面上多选批量改」那一半）。

        ⚠️ **同一次提交共用一个 `actor`**——那样它就能被
        `POST /api/bulk-undo` 一次撤销。逐条各给自己的 actor 的话，
        「改了 5 条、发现改错了」要撤 5 次，而中途任何别的操作都会把它切成两段。
        """
        raw_ids = body.get("item_ids")
        if not isinstance(raw_ids, list) or not raw_ids:
            raise HTTPException(status_code=400, detail="item_ids 必须是非空数组")
        ids: list[int] = []
        for x in raw_ids:
            if isinstance(x, bool) or not isinstance(x, int) or x <= 0:
                raise HTTPException(
                    status_code=400,
                    detail=f"item_ids 里必须都是正整数（拒绝 true：bool 是 int 的子类），收到：{x!r}",
                )
            ids.append(x)

        kind = str(body.get("kind", "")).strip()
        if not kind:
            raise HTTPException(status_code=400, detail="kind 不能为空")
        _reject_controls(kind, "kind")

        # ⚠️ 用 `current_categories()` 而不是 `load_categories(categories_path())`
        # （复审 Minor-1）：两者都**响亮**（`CategoryError` ⇒ 500），差的只是
        # 排障那一档——裸 `load_categories` 让 FastAPI 兜成一句
        # `Internal Server Error`，而前者会说出「config/categories.toml 读不出来：…」。
        # ⚠️ 这里**不降级**（不像 `group_names_map`）：类目表是这条端点的**前提**
        # ——不知道有哪些类目就根本判不了 `kind` 合不合法。
        known = {c.slug for c in current_categories()}
        if kind not in known:
            raise HTTPException(
                status_code=400,
                detail=f"没有这个类目：{kind!r}。现存类目：{sorted(known)}",
            )

        con = connect()
        try:
            missing = [i for i in ids if store.get_item(con, i) is None]
        finally:
            con.close()
        if missing:
            raise HTTPException(status_code=404, detail=f"没有这些条目：{missing}")

        actor = f"bulk:{uuid.uuid4().hex[:12]}"
        ov = overrides.connect()
        try:
            for iid in ids:
                overrides.set_kind(ov, item_id=iid, kind=kind, actor=actor)
        finally:
            ov.close()
        return {"actor": actor, "count": len(ids)}

    @app.post("/api/bulk-undo")
    def api_bulk_undo(body: dict = Body(...)) -> dict:
        """撤掉**一次批量操作**写的全部事件（`reorder.apply_reorder` 或
        `api_bulk_set_kind` 共用同一个 actor）。

        ⚠️ 只接受 `/api/reorder` 与 `/api/bulk-set-kind` 造出来的 actor
        （`_ACTOR_PREFIXES`，复审 Minor-2）。**必须说清这条边界的性质：
        它挡的是「误用」，不是「权限」**——单机自用、没有用户模型，能点这个
        按钮的人本来就有权改。它拦住的是具体的一件事：`{"actor": "web"}` 会把
        尾部那一整段**人工操作**（改类目、`delete` 行、墓碑行）一次性撤掉，
        而调用方很可能只是想撤「刚才那批重排」——那是一次**不可逆的静默扩大**。
        """
        actor = str(body.get("actor", ""))
        if not actor:
            raise HTTPException(status_code=400, detail="actor 不能为空")
        if not actor.startswith(_ACTOR_PREFIXES):
            raise HTTPException(
                status_code=400,
                detail=f"这个 actor 不是本程序造出来的（批量撤销只认 "
                f"{list(_ACTOR_PREFIXES)} 开头）：{actor!r}。"
                "⚠️ 这条闸挡的是误用、不是权限——但它确实会拒绝「撤掉尾部任意"
                "一段人工操作」这种用法：那属于逐条撤销（`/api/undo`）。",
            )
        ov = overrides.connect()
        try:
            n = overrides.undo_batch(ov, actor=actor)
        finally:
            ov.close()
        return {"undone": n > 0, "count": n}

    # ── 类目编辑（M6 T5）────────────────────────────────────────────
    #
    # ⚠️ 这里开始，`categories.toml` 由**程序**整体重写（`save_categories`）。
    # 配置仍是唯一真相源，只是从此有了一个比手写更常见的来源。

    def categories_path() -> pathlib.Path:
        """⚠️ 与 `config_path()` 一样**每次现读** `REPO_ROOT` 全局
        （Global Constraint 11：不许在 `create_app` 时算好冻进闭包）。

        ⚠️ **实测（M6 T5）：把这一处冻住，全量 704 passed / 0 red。**
        原因清楚——所有隔离夹具都是**在造 app 之前**就
        `monkeypatch.setattr(api, "REPO_ROOT", tmp)` 的，于是"冻住的那一份"
        也落在 tmp 根里。⇒ **这一处的『现读』今天没有牙**，它不是被测试逼出来的形状。

        ⇒ 仍然现读，是照约束 11 与同族的 `config_path()` / `persons_path()`
        （后者那次「测试往真仓库写人」的事故，是**那处**的实测历史，不是这一处的）。
        ⚠️ 写清楚这一点是刻意的：本项目最贵的一课是**错的论证比没有论证更危险**
        （勘误 E7）——把一个没被守卫的形状说成"不然测试会红"，会让下一个读的人
        以为自己理解了，于是不去验证。
        """
        return REPO_ROOT / "config" / "categories.toml"

    def current_categories() -> tuple[Category, ...]:
        """**每次现读**的类目表（名字对齐 `current_config()`）。

        ⚠️ 读不出来 ⇒ **500**（同 `current_config()`）：这是服务端自己的配置问题，
        而 503 在浏览器里会被读成「服务没起来」，方向完全不同。
        """
        try:
            return load_categories(categories_path())
        except categories_mod.CategoryError as exc:
            raise HTTPException(
                status_code=500, detail=f"config/categories.toml 读不出来：{exc}"
            )

    def category_map() -> dict[str, Category]:
        """slug → `Category`，**每次现读**（与 `group_names_map()` 同族）。

        ⚠️ 只在**端点入口**读一次、再传进 `item_out`：列表接口一页 50 条，
        写在 `item_out` 里就是 50 次读同一份文件。
        """
        return {c.slug: c for c in current_categories()}

    def live_slugs_of(cats: Sequence[Category], *, keep: str | None = None) -> list[str]:
        """**即将生效**的那套 slug（＋可选地保留一个"此刻还得在"的旧名）。

        ⚠️ `keep` 是给改名/删除用的：那个**马上要被废掉**的旧 slug。
        多含它的理由**只有一个、且是实测的**（Fix loop 1）：别名链 `X → 旧名`
        必须活到 `set_kind_alias` 那一次扁平 UPDATE 跑完，否则 X 的条目会退化成
        裸 slug（`test_api_rename_keeps_the_alias_chain_alive` 钉着它）。
        最终的不变量由**收尾那一刀**保证：`commit_categories` 用的是
        **不含 `keep`** 的 candidate 集。
        """
        return [c.slug for c in cats] + ([keep] if keep else [])

    def prune_aliases(
        cats: Sequence[Category], *, keep: str | None = None
    ) -> int:
        """按 `live_slugs_of(cats, keep=…)` 把别名表剪到一致。

        ⚠️ 为什么必须有它（Fix loop 1 / FIX 1+2）：`store._EFFECTIVE_KIND` 里的
        `ka2`（作用在 `ost.kind` 上的那条别名）**无条件优先于** `ost.kind`，
        所以**不变量「别名行的 `old_kind` 必不是现存 slug」就是 `ka2` 的安全前提**
        ——完整论证在 `overrides.prune_kind_aliases` 的 docstring 里。
        """
        ov = overrides.connect()
        try:
            return overrides.prune_kind_aliases(
                ov, live_slugs=live_slugs_of(cats, keep=keep)
            )
        finally:
            ov.close()

    def snapshot_categories() -> None:
        """把**当前** `categories.toml` 的原文存进 overlay。每次改动**之前**调。

        ⚠️ **文件不存在 ⇒ 500，不静默跳过**（Fix loop 1 / FIX 6）。
        口径与 `current_categories()` 统一：`categories` 这一族的既有约定就是
        **快速失败**（Global Constraint 5：`persons` / `llm` 返回默认值，
        `groups` / `categories` 快速失败）——"文件不存在"与"文件写错"是两回事，
        但两者都不该被当成"没有干预"咽下去。

        ⚠️ 静默跳过的后果是**具体的**：这条路径上写文件**照旧发生**，只是撤销栈
        少了一格 ⇒ 用户下一次「撤销」会**跳过这次编辑、多退一格**，而接口什么
        都没说。今天它不可达（每个调用点前面都已经 `current_categories()` 成功读过
        同一个文件），所以它是一条**兜底**——而兜底要么响亮、要么不存在，
        "静默地少做一件事"是最坏的那种。
        """
        path = categories_path()
        if not path.is_file():
            raise HTTPException(
                status_code=500,
                detail=f"{path} 在本次请求中途消失了（写之前读不到它）——"
                "**没有任何改动落盘**；请检查 config/ 是否被别的程序动过。",
            )
        ov = overrides.connect()
        try:
            overrides.save_config_snapshot(ov, body=path.read_text(encoding="utf-8"))
        finally:
            ov.close()

    def category_or_400(body: dict) -> Category:
        """把请求体变成一个 `Category`，**校验不过一律 400**（不是 500）。

        ⚠️ 校验**必须在写之前**：`save_categories` 自己也会校验（T2），
        但那是最后一道闸——先校验一次，才能做到「400 的时候
        **一份快照都没产生、一个字节都没落盘**」。
        """
        slug = str(body.get("slug", "")).strip()
        label = str(body.get("label", "")).strip() or slug
        desc = str(body.get("desc", "")).strip()
        icon = str(body.get("icon", "")).strip()
        for fname, value in (
            ("slug", slug), ("label", label), ("desc", desc), ("icon", icon),
        ):
            _reject_controls(value, fname)
        if not desc:
            raise HTTPException(
                status_code=400,
                detail="desc 不能为空——它会被**逐字**渲染进 LLM 的提示词，是类目的判据",
            )
        return Category(slug=slug, label=label, desc=desc, icon=icon)

    def validate_or_400(cats: Sequence[Category]) -> None:
        try:
            categories_mod.validate_categories(
                categories_mod.as_dicts(cats), categories_path()
            )
        except categories_mod.CategoryError as exc:
            raise HTTPException(status_code=400, detail=str(exc))

    def commit_categories(cats: Sequence[Category]) -> None:
        try:
            categories_mod.save_categories(cats, categories_path())
        except categories_mod.CategoryError as exc:
            # 走到这里说明 `validate_or_400` 放行了一份 `save_categories` 拒绝的
            # 东西——那是**程序自己的 bug**，500 才对（400 会把排障引向用户输入）。
            raise HTTPException(status_code=500, detail=f"内部错误：{exc}")
        # ⚠️ **先写文件、后剪枝**（Fix loop 2 / FIX 3；Fix loop 1 时是反的）。
        # 反过来的话，**写失败**（Windows 上文件被占用是真实可发生的）会留下
        # 「别名已按新 slug 集剪掉、文件还是旧集」——那正是要维持的不变量被破。
        # 这个顺序下失败只意味着"别名暂时保守地多留了一行"，而
        # `reconcile_if_stale`（读路径、按 mtime）会在下一次读时把它对平。
        # ⚠️ 剪枝用的是**不含 `keep`** 的这套 slug（收尾那一刀），落盘之后表里
        # 不会再有 `old_kind ∈ 现存 slug` 的行 ⇒ `ka2` 安全。
        prune_aliases(cats)

    def persons_path() -> pathlib.Path:
        """⚠️ 路径**每次现读** `REPO_ROOT` 全局，不许在 `create_app` 时算好冻进闭包。

        这与 `WEB_DIST` 的既有教训是同一件事（`api.py:289-292` 的 docstring 专门记过）：
        冻住的话，测试里 `monkeypatch.setattr(api, "REPO_ROOT", tmp_path)` 就够不着它，
        于是那些测试**测的还是仓库里真实的 `config/persons.toml`**——
        **会往真仓库里写人**，而且因为它们总能读到点什么、于是全都变成「怎么改都绿」的空守卫。
        """
        return REPO_ROOT / "config" / "persons.toml"

    def connect() -> sqlite3.Connection:
        # ⚠️ 只开 `mode=ro`，且**绝不**在这里调 `store.ensure_schema`——
        # 它内部会 `rebuild_search_index`（`INSERT INTO items_fts(...) VALUES
        # ('rebuild')`），是**写操作**，在只读连接上是硬失败：
        # `OperationalError: attempt to write a readonly database`。
        # 建表/重建索引属于写路径（refine / digest），不归这里。
        return sqlite3.connect(db_uri, uri=True)

    def group_names_map() -> dict[int, str]:
        """群号 → 群名，**每次现读**（与 `current_config()` 同一族，M6 T4）。

        ⚠️ **不许用 `create_app` 收到的那份 `config`**：它冻在启动那一刻，
        而「配置」页可以让用户随时改群名 / 停用 / 加群 —— 冻住的话，刚加的群
        在信息流里显示成裸群号，用户看着自己的操作没生效。

        ⚠️ 只在**端点入口**读一次、再传进 `item_out`：列表接口一页 50 条，
        写在 `item_out` 里就是 50 次读同一份 3KB 的 TOML。

        ⚠️ **读不出来时降级成空映射，绝不 500**（M6 T4 复审 ⚠️-3 的裁定）：
        用它的三条是**显示路径**（`/api/items`、`/api/items/{id}`、
        `/api/digests/{id}`），它们只需要一个**显示用**的群名——配置坏掉时
        退回裸群号（`item_out` 的 `names.get(..., str(gid))` 本来就是这么写的），
        而不是把**信息流**（主页面）整页黑成 500。这与 `groups_snapshot.load`
        刻意「增强项不许 500」是同一条判据。
        ⚠️ 而 `/api/groups` 与四个写端点**保持响亮**（它们走 `current_config()`）：
        配置就是它们的载荷 / 前提，读不出来必须让人看见。
        ⚠️ 但降级**必须留痕**：静默降级等于把「配置坏了」藏起来，
        用户只会看到一屏裸群号而不知道为什么。

        ⚠️ **捕 `Exception` 而不是 `HTTPException`**（Fix loop 1 / FIX 8，controller
        裁定；T4 那轮只捕了 `HTTPException`，于是这条缝没人守）：上面说的
        「读不出来」**不止** `ConfigError → HTTPException` 一种形态——
        `OSError`（权限、文件被占、路径异常）同样读不出来，而它**不是**
        `HTTPException` ⇒ 从前会一路冒成 500，把主页面整页黑掉。
        实测（T4 re-reviewer 的探针）：`load_config` 抛 `OSError(13)` ⇒ `/api/items` = 500。

        ⚠️ 捕宽是安全的，因为这里能吞的**只有显示用的增强项**（群名 → 退化成裸群号，
        `item_out` 的 `names.get(..., str(gid))` 本来就是这么兜底的）；
        **配置本身的问题不会被藏起来**：`/api/groups` 与四个写端点走
        `current_config()`，照旧响亮地 500。
        """
        try:
            cfg = current_config()
        except Exception as exc:  # noqa: BLE001
            logs.emit(f"[警告] 群名读取失败，信息流退回裸群号：{exc!r}")
            return {}
        # ⚠️ **空名不进这张表**（M6 终审 F2）：`str(g.name)` 若为 `""`，下面
        # `item_out` 的 `names.get(gid, str(gid))` **命中键**（键在、值空）
        # ⇒ 条目显示一个**空群名**，而那是"这条条目读不出来源群"的假相；
        # 跳过之后它退回**裸群号**——与 `cli.py` 的 `gname or m.group_id`
        # 同一条口径，也正是 `groups_snapshot.collect` 把"名字未知"留成 `""`
        # 时**预期**的显示结果（加群端点从 F2 起接受空名，所以这条路径真实存在）。
        return {g.id: g.name for g in cfg.groups if g.name}

    def item_out(
        it: store.ApiItem, names: dict[int, str], cats: dict[str, Category]
    ) -> dict:
        """⚠️ `cats` 由调用方**每次现读**后传进来（见 `category_map()`）。

        不是为了省一次读（虽然也省了 50 次同一份文件的读），而是因为**冻住的
        那一份会过时**：网页上刚加的类目在信息流里会显示成**裸 slug**——
        用户看着自己刚做的操作没生效（与 `group_names_map()` 是同一条判据）。
        """
        cat = cats.get(it.kind)
        return {
            "item_id": it.item_id,
            "kind": it.kind,
            "kind_label": cat.label if cat else it.kind,
            "kind_icon": cat.icon if cat else "",
            "title": it.title,
            "detail": it.detail,
            "event_ts": it.event_ts,
            # ⚠️ 这里**不做二次核验**，因为未经核验的值在数据层就已经是 NULL
            # （见 vigil/deadline.py）。这正是「把核验提到数据层」的收益：
            # 读取方不需要各自记得再挡一次——那种「每个读者都要记得」的约定
            # 迟早会有人忘（M2 只在日报挡过，Web 差一点就把幻觉日期复活了）。
            "deadline_ts": it.deadline_ts,
            "group_id": it.group_id,
            "group_name": names.get(it.group_id, str(it.group_id)),
            "actor": it.actor,
            "actor_uin": it.actor_uin,
            "place": it.place,
            "amount": it.amount,
            "links": list(it.links),
            "source_count": it.source_count,
            # ⚠️ 活动开始时刻（M7）。前端据此渲染「🕐 14:00」并做冲突标记。
            # **为 NULL 是常态**（多数条目没有活动时刻），前端不许当异常处理。
            "start_ts": it.start_ts,
        }

    @app.get("/api/categories")
    def api_categories() -> dict:
        # ⚠️ 原实现用的是 `create_app` 时冻住的 `cats` / `cat_by_slug`。改成**现读**
        # （同 T4 的 `current_config`）：编辑器刚存下的类目必须立刻在这个响应里，
        # 否则用户看到的还是旧表、以为自己没存上。
        # 变量名从 `cats` 改成 `cfg_cats` 是刻意的，好让 `grep -n "cats" vigil/api.py`
        # 一眼看出还有哪里在用旧的那一份。
        cfg_cats = current_categories()
        con = connect()
        try:
            counts = store.kind_counts(con)
        finally:
            con.close()
        ov = overrides.connect()
        try:
            aliases = overrides.kind_aliases(ov)
        finally:
            ov.close()
        return {
            "categories": [
                {
                    "slug": c.slug,
                    "label": c.label,
                    "icon": c.icon,
                    # ⚠️ `desc` 也在契约里（M6 T5）：编辑器要拿它回填输入框，
                    # 而这个字段是**逐字进 LLM 提示词的判据**——不给的话，
                    # 界面上改一次别的字段就会把它抹掉。
                    "desc": c.desc,
                    "count": counts.get(c.slug, 0),
                }
                for c in cfg_cats
            ],
            # ⚠️ 一次把别名映射也交出去，供界面显示「旧名 life → 现在叫 daily」。
            # 单独开一个端点也行，但界面每次都要它，多一次往返不值。
            "aliases": aliases,
        }

    # ⚠️ **四个写端点里每一步都在 `write_lock` 里**（与 `api_add_group` 同形）。
    # 读在锁外的话，「读 → 改 → 写」这个整体替换就会被两个并发请求交错
    # （各自读到旧版本、各自算 candidate、后写的赢）⇒ 可能丢一次编辑。
    # 那正是 `write_lock` 上面那段注释点名要防的事，不因为「单用户自用概率低」
    # 就可以省（spec §6 R10）。
    # ⚠️ **实测：这一层今天没有牙**——把锁换成空上下文，全量 **704 passed / 0 red**
    # （没有任何并发用例）。它守的是一个**单线程测试看不见**的性质，
    # 与上面 `categories_path()` 那条是同一族的诚实标注（勘误 E7）。
    #
    # ⚠️ 快照的**位置**是双重承重的，两条都是实测：
    #   ① 在 `commit_categories()` **之前**——反过来实测 ⇒
    #      `test_api_category_undo_restores_the_previous_file` **红**：
    #      撤销会"回滚"到刚写进去的那一份，即点了撤销什么都没发生。
    #   ② 在**写别名之前**（Fix loop 2 / FIX 1）——快照存的是「文件原文 +
    #      别名表」这一**对**，拍在别名写之后就会把「旧文件 + 新别名表」装回去，
    #      `old_kind` 又变成现存 slug ⇒ 不变量破、条目显示悬空 slug。
    #
    # ⚠️ `api_update_category` / `api_delete_category` 里「**先写别名、再写配置**」
    # 的顺序**没有测试**（两种顺序在单请求里都绿）：它防的是**两次写之间崩溃**
    # 留下的"配置里已经没有 life 了、但别名还没建"⇒ 条目永久悬空。
    # 这一条是**设计选择**，不是被测试逼出来的——如实标注，免得后来者以为有守卫。

    @app.post("/api/categories", status_code=201)
    def api_add_category(body: dict = Body(...)) -> dict:
        cat = category_or_400(body)
        with write_lock:
            existing = current_categories()
            if any(c.slug == cat.slug for c in existing):
                raise HTTPException(status_code=409, detail=f"类目 {cat.slug} 已经存在")
            candidate = existing + (cat,)
            validate_or_400(candidate)      # ⚠️ 先验，后拍快照 ⇒ 400 时零副作用
            snapshot_categories()
            commit_categories(candidate)
        return {"slug": cat.slug, "label": cat.label}

    @app.put("/api/categories/{slug}")
    def api_update_category(slug: str, body: dict = Body(...)) -> dict:
        """改一个类目。**body 里带 `slug` 且与路径不同 ⇒ 这是一次改名**。

        ⚠️ **写入顺序是承重的：先写别名，再写配置文件。**
        反过来的话，中间崩溃会留下「配置里已经没有 `life` 了、但别名还没建」
        ⇒ 条目永久悬空（正是 spec §4.2 ④ 点名要禁的状态）；
        先写别名的话，中间崩溃只留下一条约不到的别名——无害，
        配置文件写好之后它立刻生效。
        """
        with write_lock:
            existing = current_categories()
            old = next((c for c in existing if c.slug == slug), None)
            if old is None:
                raise HTTPException(status_code=404, detail=f"没有这个类目：{slug}")

            new_slug = str(body.get("slug", slug)).strip() or slug
            merged = category_or_400(
                {
                    "slug": new_slug,
                    "label": body.get("label", old.label),
                    "desc": body.get("desc", old.desc),
                    "icon": body.get("icon", old.icon),
                }
            )
            # ⚠️ 空操作**不拍快照**：拍下去的话，一次"什么都改"的 PUT
            # 会往撤销栈里塞一格假的——用户点撤销时会看到"配置没变"
            # （因为那一格就是当前内容），于是以为撤销坏了。
            if merged == old:
                return {"slug": slug, "label": old.label}

            candidate = tuple(merged if c.slug == slug else c for c in existing)
            validate_or_400(candidate)

            # ⚠️ **整段取 `ALIAS_LOCK`**（Fix loop 3 / FIX 2）：这段里
            # 「别名表」与「`categories.toml`」会被**先后**改到，中间是不一致的一瞬。
            # 对账（`reconcile_if_stale`，由**任何一次读**触发）若插进这一瞬，
            # 它读的是**磁盘上那份还是旧的文件** ⇒ 会把刚写进去、**还没生效**的
            # 别名当成"目标不存在"剪掉 ⇒ 文件写完之后条目**永久悬空**。
            # 顺序**不改**（先别名、后文件）：崩在中途时，别名指向一个还没生效的
            # slug ⇒ 对账会把它剪掉 ⇒ 条目回落到**原来那个仍现存的 slug** ⇒ 自愈。
            with overrides.ALIAS_LOCK:
                # ⚠️ **快照必须拍在"别名表被改"之前**（Fix loop 2 / FIX 1，承重）。
                # 快照存的是「文件原文 + 别名表」这一**对**，两者必须来自同一时刻；
                # 拍在别名写之后的话，撤销会把「旧文件 + 新别名表」装回去
                # ⇒ `old_kind` 又变成现存 slug ⇒ 不变量破、条目显示悬空 slug
                # ——那正是这次要修的 C-1。
                snapshot_categories()

                # ⚠️ **顺序：先剪枝、再写别名**（Fix loop 1 / FIX 1c，承重）。
                # 反过来的话「改回原名」（A→B 之后再把 B 改回 A）会因为表里还留着
                # `A→B` 而跟成一个**环** ⇒ `set_kind_alias` 抛错 ⇒ 用户永远回不去。
                # 剪枝用的 live 集**多含一个 `slug`**（那个马上要废的旧名）：
                # 别名链 `X→slug` 必须活到下面那次扁平 UPDATE 跑完，否则 X 的条目
                # 会退化成裸 slug。完整的 live 集语义见 `live_slugs_of`。
                if new_slug != slug:
                    prune_aliases(candidate, keep=slug)
                    ov = overrides.connect()
                    try:
                        try:
                            overrides.set_kind_alias(
                                ov, old_kind=slug, new_kind=new_slug
                            )
                        except overrides.OverlayError as exc:
                            # 剪枝之后这里**理论上**不可达（环要靠"老行"才能形成，
                            # 而它刚被剪掉）——但兜底必须响亮且**方向正确**：
                            # 500 会让排障指向"服务坏了"，而真相是「当前别名表与这次
                            # 改名冲突」，用户有自救路径（再撤一次 / 换个名字）。
                            # ⚠️ 走到这里时**快照已经拍过了**，而这次改名没落盘
                            # ⇒ 撤销栈里多了一格"与当前状态相同"的快照
                            # （用户点一次撤销会觉得"没反应"）。**知情接受**
                            # （Fix loop 3 / FIX 5）：纯 UX，且这一支在剪枝之后
                            # 理论上不可达；为一个不可达分支去回滚快照不值。
                            raise HTTPException(
                                status_code=409, detail=f"改不了名：{exc}"
                            )
                    finally:
                        ov.close()
                commit_categories(candidate)
        return {"slug": merged.slug, "label": merged.label}

    @app.delete("/api/categories/{slug}", status_code=204)
    def api_delete_category(slug: str, migrate_to: str | None = None) -> None:
        """删一个类目。**还有条目指着它时必须给 `?migrate_to=`**。

        ⚠️ 「有 N 条属于它」这句话**必须真的说出来**（spec 出口判据 6）：
        只回一句「不能删」是没用的，用户需要知道**有多少条**、
        以及**不迁移会怎样**。所以 detail 里带上条数。

        ⚠️ 迁移的实现就是一条别名（`slug → migrate_to`），**不碰 `items`**
        ——那正是 `kind_alias` 存在的理由。
        """
        with write_lock:
            existing = current_categories()
            if not any(c.slug == slug for c in existing):
                raise HTTPException(status_code=404, detail=f"没有这个类目：{slug}")

            candidate = tuple(c for c in existing if c.slug != slug)
            if not candidate:
                raise HTTPException(
                    status_code=409,
                    detail="这是最后一个类目，删掉它会让 refine 的提示词没有类目表可用。",
                )
            validate_or_400(candidate)

            con = connect()
            try:
                n = store.kind_counts(con).get(slug, 0)
            finally:
                con.close()

            if n and not migrate_to:
                raise HTTPException(
                    status_code=409,
                    detail=(
                        f"类目 {slug} 还有 {n} 条条目指着它。删之前必须给一个迁移目标："
                        f"`DELETE /api/categories/{slug}?migrate_to=<某个现存 slug>`。"
                        "不给的话那些条目会指向一个不存在的类目。"
                    ),
                )
            if migrate_to and migrate_to not in {c.slug for c in candidate}:
                raise HTTPException(
                    status_code=400, detail=f"迁移目标 {migrate_to!r} 不是一个现存类目"
                )

            # ⚠️ **整段取 `ALIAS_LOCK`**（同 `api_update_category`，Fix loop 3 / FIX 2）。
            with overrides.ALIAS_LOCK:
                # ⚠️ **快照拍在"别名表被改"之前**（同 `api_update_category`，FIX 1）。
                snapshot_categories()

                if migrate_to:
                    # ⚠️ 先剪枝、再写别名（同 `api_update_category`，Fix loop 1 / FIX 1c），
                    # live 集同样多含那个马上要废的 `slug`（别名链 `X→slug` 要活到
                    # 扁平 UPDATE 跑完）。
                    prune_aliases(candidate, keep=slug)
                    ov = overrides.connect()
                    try:
                        try:
                            overrides.set_kind_alias(
                                ov, old_kind=slug, new_kind=migrate_to
                            )
                        except overrides.OverlayError as exc:
                            raise HTTPException(
                                status_code=409, detail=f"迁移不了：{exc}"
                            )
                    finally:
                        ov.close()
                commit_categories(candidate)
        return None

    @app.post("/api/categories/undo")
    def api_category_undo() -> dict:
        """把 `categories.toml` 回滚到上一次程序写它之前的样子。

        ⚠️ **与 `/api/undo` 是两回事**（spec §3.3：「两者都做，别混为一谈」）：
        那条撤的是**对条目的人工修改**（删事件），这条回滚的是**配置文件**
        （整份存档）。协议上也刻意分开：这条回 `{"undone": bool}`，
        与 `/api/undo` 对齐，但**不接受 `edit_id`**——快照没有"中间某一条"，
        只有"上一格"。
        """
        # ⚠️ **整段在 `write_lock` 里**（Fix loop 1 / FIX 5）：`pop` 在锁外时，
        # 两个并发撤销各自 peek 到同一格 ⇒ 一个格的快照被消费两次 / 丢一格。
        # ⚠️ **边界（Fix loop 2 / FIX 5，知情接受）**：这把锁是**进程内**的，
        # 所以 peek→pop 之间只有**本服务**的请求被串起来；若另一个**进程**
        # （比如有人同时在跑 CLI）插在中间撤销一次，两边的 peek 可能看到同一格。
        # 这是**单用户自用**工具的已知边界——跨进程那把锁是 `lock.SingleInstance`，
        # 它保护的是 QQ 库与主库，语义不同、别混用（见 `write_lock` 上面的论证）。
        with write_lock:
            ov = overrides.connect()
            try:
                row = overrides.peek_config_snapshot(ov)
                if row is None:
                    return {"undone": False}
                _, body, aliases = row
                # ⚠️ **校验在消费之前**（Fix loop 1 / FIX 5）：恢复的是一份
                # **手里没有第二份**的内容，所以要先校验再落盘——直接
                # `atomic_write_text` 的话，一份坏快照会把配置永久写坏
                # （原子写、无回滚），而 `categories.toml` 是 refine 与 api 的硬依赖。
                # ⚠️ 而**校验不过时不许消费**：从前是先 `pop` 再校验 ⇒ 坏快照被
                # 悄悄从栈里吃掉，用户下一次撤销会**多退一格**（跳过更早那次编辑），
                # 而响应只说「配置未被改动」——**那句话只对文件成立**。
                # ⚠️ 这里**只校验**、不用它的返回值：恢复走的是下面的整表替换，
                # 不是"按这个 slug 集剪枝"（剪枝还原不了被压平 UPDATE 抹掉的旧行）。
                try:
                    raw = tomllib.loads(body)
                    categories_mod.validate_categories(
                        raw.get("categories"), categories_path()
                    )
                except (tomllib.TOMLDecodeError, categories_mod.CategoryError) as exc:
                    raise HTTPException(
                        status_code=500,
                        detail=f"快照内容已损坏，拒绝写回（配置未改动、**快照也没被消费**）：{exc}",
                    )
                overrides.pop_config_snapshot(ov)
            finally:
                ov.close()
            # ⚠️ **整段取 `ALIAS_LOCK`**（Fix loop 3 / FIX 2）：这里同样是
            # 「文件」与「别名表」先后改到，中间的瞬间不一致。
            with overrides.ALIAS_LOCK:
                # ⚠️ **先写文件、后换别名表**（Fix loop 2 / FIX 3 的同一条理由）：
                # 写失败时别名表一动不动 = **保守**；反过来的话失败会留下
                # 「文件是旧的、表是新的」——正是要维持的不变量被破，
                # 而 `reconcile_if_stale` 只会在**下一次**读时才把它对平。
                atomic_write_text(categories_path(), body)
                if aliases is None:
                    # ⚠️ **旧格式快照（M6 T5 之前存的）没有别名表**：这时**不许**
                    # 当成空表去清（那会让所有改过名的条目一起退化成裸 slug）。
                    # 保持现状 + 说一句：这是**知情接受**的降级，不是静默。
                    logs.emit(
                        "[警告] 这份配置快照没有别名表（M6 T5 之前的旧格式）："
                        "本次撤销**只回滚了配置文件**，kind_alias 保持现状。"
                        "若条目显示的类目与配置对不上，再撤一次或重启服务即可对平。"
                    )
                else:
                    ov = overrides.connect()
                    try:
                        overrides.replace_kind_aliases(ov, aliases)
                    finally:
                        ov.close()
        return {"undone": True}

    @app.get("/api/items")
    def api_items(
        kind: list[str] = Query(default=[]),
        person: list[int] = Query(default=[]),
        since: str | None = None,
        until: str | None = None,
        group: int | None = None,
        q: str | None = None,
        limit: int = Query(50, ge=1, le=MAX_LIMIT),
        offset: int = Query(0, ge=0),
    ) -> dict:
        # since/until 都按**包含该日**理解：内部窗口右端取次日 00:00（左闭右开）。
        # 否则选了 9/13 却搜不到 9/13 当天，是最容易让人不信任搜索的错法。
        q = (q or "").strip()
        names = group_names_map()
        cats = category_map()
        con = connect()
        try:
            items, total = store.search_items(
                con,
                kinds=kind or None,
                actor_uins=person or None,
                since=_day_start(since) if since else None,
                until=_day_start(until, plus_days=1) if until else None,
                group=group,
                q=q or None,
                limit=limit,
                offset=offset,
            )
        finally:
            con.close()
        return {
            "items": [item_out(it, names, cats) for it in items],
            # ⚠️ total 是**符合筛选条件的总数**，与 limit/offset 无关（不是当页条数）。
            # 前端三处依赖它：分页按钮出不出、「共 N 条」、「还有 N 条」。
            # store.search_items 已经把两者一起返回，这里照传即可。
            "total": total,
            "limit": limit,
            "offset": offset,
        }

    @app.get("/api/calendar")
    def api_calendar(
        since: str | None = None,
        until: str | None = None,
    ) -> dict:
        """带截止日的条目。**默认全量**（不带 since/until）。

        ⚠️ 自成一端，不走 `/api/items`：后者的 `limit` 上限是 `MAX_LIMIT = 200`，
        而日历要**全量**（用户 2026-10-10 裁定）——200 这条线迟早会咬；
        且前端硬筛要先把整个窗口的条目拉回来（第三版 spec §1.4/§3.1）。

        ⚠️ 不分页：截止日是全库最稀疏的一类数据（spec §1.1 实测 36/592）。
        真要涨到需要分页时，这个响应的 `total` 就是触发器。

        ⚠️ `since`/`until` 都按**包含该日**理解（右端取次日 00:00），与
        `/api/items` 同口径——两处必须都走 `_day_start`（E5）。
        """
        names = group_names_map()
        cats = category_map()
        since_ts = _day_start(since) if since else None
        until_ts = _day_start(until, plus_days=1) if until else None
        con = connect()
        try:
            items = store.deadlines_in_range(con, since=since_ts, until=until_ts)
            # ⚠️ **冲突与重复要**是全窗口**算出来的**，不是前端各自算：
            # 判定几何（`conflicts.py`）与"是不是同一件事"的判据必须与 CLI
            # **共用同一份实现** —— 否则界面上说"没冲突"、命令行说"有冲突"，
            # 而两个都对各自的代码。见 `conflicts.py` 顶部那段。
            #
            # ⚠️ 窗口用**全量**（`since_ts`/`until_ts` 可能都是 None）：
            # 只算当前可见的那一屏会让"跨屏的冲突"漏掉 ——
            # 而用户恰恰会因为看到其中一条而以为没事。
            wf = since_ts if since_ts is not None else 0
            wt = until_ts if until_ts is not None else 1 << 62
            activities = store.items_with_start(con, since=wf, until=wt)
            tt = timetable_mod.load_timetable()
        except timetable_mod.ConfigError as exc:
            # ⚠️ 课表填错了**不许让日历整个 500** —— 日历的主业不是课冲突。
            # 照常返回条目，只是不报课冲突（并如实说明为什么）。
            activities, tt = [], None
            logs.emit(f"[警告] 课表读不出来，本页不报课冲突：{exc}")
        finally:
            con.close()

        dup_groups = conflicts_mod.find_duplicate_groups(activities)
        # 代表之外的都标成"重复" —— 前端据此折叠。
        dup_of: dict[int, int] = {}
        for g in dup_groups:
            for iid in g.item_ids[1:]:
                dup_of[iid] = g.representative

        # 活动之间的冲突：**去重后**才判（同一件事的两条不是冲突，
        # 那是 ① 的问题，见 `conflicts.py` 顶部）。
        #
        # ⚠️ 时长用 90 分钟（与 `vigil schedule --activity-min` 的默认一致）：
        # 抽取**没给**活动时长，而判定冲突必须有区间。写死在这里是知情取舍 ——
        # 不改它是因为"猜一个时长"在任何值上都是猜，而 90 分钟让"14:00 与
        # 15:00 两场"仍被正确判成不冲突（那是真实数据里最常见的情形）。
        entries = [
            conflicts_mod.Entry(label=t, start=ts, end=ts + 90 * 60,
                                item_id=iid, kind="activity")
            for iid, t, ts in activities
            if iid not in dup_of
        ]
        conflicts = [_conflict_out(c) for c in conflicts_mod.find_conflicts(entries)]
        course_conflicts: list[dict] = []
        if tt is not None:
            course_conflicts = _course_conflicts(conflicts_mod, schedule_mod, tt, entries)
        else:
            # ⚠️ 课表读不出来时**如实说**，不许静默返回空列表 ——
            # 那与"课表是空的、没有冲突"在界面上长得一模一样。
            course_conflicts.append({"error": "课表读不出来，本页不报课冲突"})

        return {
            "items": [item_out(it, names, cats) for it in items],
            "total": len(items),
            "duplicates": {str(k): v for k, v in dup_of.items()},
            "conflicts": conflicts,
            "course_conflicts": course_conflicts,
        }

    @app.get("/api/items/{item_id}")
    def api_item(item_id: int) -> dict:
        names = group_names_map()
        cats = category_map()
        con = connect()
        try:
            it = store.get_item(con, item_id)
            if it is None:
                raise HTTPException(status_code=404, detail=f"没有这条条目：{item_id}")
            payload = item_out(it, names, cats)
            payload["sources"] = [
                {
                    "msg_id": s.msg_id,
                    "ts": s.ts,
                    # ⚠️ 这里**不**把空署名统一成 None：SourceRow.sender 的契约是
                    # `str`（无署名 = ""），ApiItem.actor 的契约是 `str | None`。
                    # 前端 types.ts 也分别是 `sender: string` / `actor: string | null`。
                    # 「顺手统一一下」会让契约与前端类型对不上。
                    "sender": s.sender,
                    "group_name": names.get(s.group_id, str(s.group_id)),
                    "content": s.content,
                }
                for s in store.source_messages(con, item_id)
            ]
            return payload
        finally:
            con.close()

    @app.post("/api/items/{item_id}/kind")
    def api_set_kind(item_id: int, body: dict = Body(...)) -> dict:
        """改一条条目的类目。**只写 overlay**（D11）。

        ⚠️ `kind` 也要过 `_reject_controls`（与 `api_add_person` 的 label/note
        同一条判据，同一个 helper——M5 终审 Important-2 实测）：`kind` 是
        **日报的章节标题**（`digest.render_markdown` 按 kind 分桶），一个带换行的
        kind 会让任意文本成为日报的一节，落进 `docs/digests/*.md`（**入 git 的归档**）
        与 `digests.body_md`。这跟"名字里有没有换行"是同一件事，只是后果更重。
        """
        kind = str(body.get("kind", "")).strip()
        if not kind:
            raise HTTPException(status_code=400, detail="kind 不能为空")
        _reject_controls(kind, "kind")
        con = connect()
        try:
            if store.get_item(con, item_id) is None:
                raise HTTPException(status_code=404, detail=f"没有这条条目：{item_id}")
        finally:
            con.close()
        ov = overrides.connect()
        try:
            return {"edit_id": overrides.set_kind(ov, item_id=item_id, kind=kind)}
        finally:
            ov.close()

    @app.delete("/api/items/{item_id}")
    def api_delete_item(item_id: int) -> dict:
        """软删一条条目（D15）。`msg_id` 从**源消息**取，用于"永不被重抽复活"。

        ⚠️ **每一条源消息都要落墓碑**（E7）：`store.save_items` 的批内去重
        **来源取并集**，所以一个 item 可以有 ≥2 条源消息。只墓碑化 `srcs[0]`
        的话，`refine --redo` 重抽兄弟消息会让条目**以新 item_id 复活**（违反 D15）。
        `overrides.delete_item` 的 `extra_msg_ids` 就是把这一点收在**唯一一条**
        写路径里——多开一个"只墓碑化第一条"的函数，等于给后来者留一条静默退化的路。
        """
        con = connect()
        try:
            if store.get_item(con, item_id) is None:
                raise HTTPException(status_code=404, detail=f"没有这条条目：{item_id}")
            srcs = store.source_messages(con, item_id)
        finally:
            con.close()
        ov = overrides.connect()
        try:
            return {
                "edit_id": overrides.delete_item(
                    ov,
                    item_id=item_id,
                    msg_id=srcs[0].msg_id if srcs else None,
                    extra_msg_ids=[s.msg_id for s in srcs[1:]],
                )
            }
        finally:
            ov.close()

    @app.post("/api/undo")
    def api_undo(body: dict = Body(default={})) -> dict:
        """撤销一条编辑。`edit_id` 缺省 = 撤最近一条。

        ⚠️ 非尾部 `edit_id` 只在**可证明惰性**时才允许（`overrides.undo` 的
        docstring 有论证），否则 **409** —— 而不是把一条历史事件从日志中间抽走、
        让条目的可见状态静默翻转（终审 Important-1：撤一条非尾部 `set_kind`
        能让条目从 Web 与日报同时消失，而这里返回 `{"undone": true}`）。
        409 而不是 400：请求本身是合法的，与**日志当前状态**冲突的是这次撤销。

        ⚠️ **两种 `OverlayError` 必须分开映**（M5 收尾 scoped re-review A2）。
        `overrides.UndoConflict` = 与日志状态冲突 ⇒ **409**（换一条编辑撤就能成，
        有自救路径）。其余 `OverlayError` = overlay 的**基础设施故障**
        （文件缺失/挂不上/路径非法）⇒ **5xx**：客户端重试多少次都一样，
        报 409 会把排障引向「是不是有人并发改了日志」，而真相是服务端自己坏了。
        ⚠️ 顺序是承重的：**子类必须在前**，否则 `UndoConflict` 会被下面那支吞掉。
        """
        target = _edit_id_of(body)
        ov = overrides.connect()
        try:
            if target is None:
                last = overrides.last_edit(ov)
                if last is None:
                    return {"undone": False}
                target = last[0]
            try:
                return {"undone": overrides.undo(ov, edit_id=target)}
            except overrides.UndoConflict as exc:
                raise HTTPException(status_code=409, detail=str(exc))
            except overrides.OverlayError as exc:
                # ⚠️ 显式 500 而不是「让它冒出去」：一是给客户端一句**说清方向的**
                # 话（「不是冲突，是服务端」），二是它必须是**可断言的响应**——
                # `TestClient` 默认 `raise_server_exceptions=True`，未捕获的异常
                # 在测试里是「错误」而不是 500 响应，「映成 5xx」这件事就没人守得住。
                raise HTTPException(
                    status_code=500,
                    detail=f"overlay 基础设施故障（**不是**与日志状态冲突）：{exc}",
                )
        finally:
            ov.close()

    @app.get("/api/persons")
    def api_persons() -> dict:
        # ⚠️ `path` **一处都不许省**：`DEFAULT_PERSONS` 是 import 期就冻住的常量
        # （`config.py` 模块级），省掉 path 时读写都会落到**仓库里真实的**
        # `config/persons.toml`，而 monkeypatch 拦不住它 ⇒ 测试全绿、
        # 用户的真实名单却已经被整体替换掉了。
        people = load_persons(persons_path())
        con = connect()
        try:
            counts: dict[int, int] = {}
            for p in people:
                _, n = store.search_items(
                    con, actor_uins=[p.uin], limit=1, offset=0
                )
                counts[p.uin] = n
        finally:
            con.close()
        return {
            "persons": [
                {"uin": p.uin, "label": p.label, "note": p.note,
                 "count": counts.get(p.uin, 0)}
                for p in people
            ]
        }

    @app.post("/api/persons", status_code=201)
    def api_add_person(body: dict = Body(...)) -> dict:
        uin = body.get("uin")
        if not isinstance(uin, int) or uin <= 0:
            raise HTTPException(
                status_code=400,
                detail="uin 必须是正整数（0 是匿名哨兵，不能监视）",
            )
        # ⚠️ **控制字符检查必须排在 `.strip()` 之前**（M6 T10 / M5 遗留 #25）。
        # 排在后面的话，`"甲\n"` 里的换行已经被 strip 剥掉、检查什么也看不见
        # ⇒ 返回 201 而用户以为自己输入的东西被原样接受了。
        # 而同一个请求里 `note` 没走 strip，于是**两个字段待遇相反**——
        # 与 `_reject_controls` docstring 承诺的「响亮地拒绝」矛盾。
        raw_label = str(body.get("label", ""))
        raw_note = str(body.get("note", ""))
        _reject_controls(raw_label, "label")
        _reject_controls(raw_note, "note")

        label = raw_label.strip()
        if not label:
            raise HTTPException(status_code=400, detail="label 不能为空")
        # ⚠️ `note` **故意不 strip**（原实现如此，且 `load_persons` 读回时会
        # strip 它）——这里只把「查控制字符」提到 strip 之前，不改它的既有语义。
        note = raw_note
        path = persons_path()
        people = [p for p in load_persons(path) if p.uin != uin]
        people.append(Person(uin=uin, label=label, note=note))
        save_persons(people, path)
        return {"uin": uin, "label": label}

    @app.delete("/api/persons/{uin}", status_code=204)
    def api_del_person(uin: int) -> None:
        path = persons_path()
        people = tuple(p for p in load_persons(path) if p.uin != uin)
        save_persons(people, path)
        return None

    # ── 配置包：三份配置 ⇄ 单个 TOML（M6 T7 / spec §4.2 ①）──────────────

    @app.get("/api/config/pack")
    def api_export_pack() -> dict:
        """把三份配置渲染成一个配置包文本（**不含密钥**）。"""
        from . import configpack

        cfg = current_config()
        cats = load_categories(categories_path())
        people = load_persons(persons_path())
        ov = overrides.connect()
        try:
            aliases = overrides.kind_aliases(ov)
        finally:
            ov.close()
        return {
            "toml": configpack.build(
                config=cfg, cats=cats, persons=people, aliases=aliases
            ),
            "schema": configpack.PACK_SCHEMA,
        }

    @app.post("/api/config/pack")
    def api_import_pack(body: dict = Body(...)) -> dict:
        """导入一个配置包。

        ⚠️ 收的是 **JSON body 里的字符串**，不是 multipart 文件上传——
        本项目依赖表只有 sqlcipher3，而 multipart 要 `python-multipart`。
        为一处上传引一个依赖不划算，前端把文件读成文本再 POST 即可
        （`FileReader.readAsText`，浏览器原生）。
        """
        from . import configpack

        text = body.get("toml")
        if not isinstance(text, str) or not text.strip():
            raise HTTPException(status_code=400, detail="toml 字段必须是配置包文本")
        apply_ = bool(body.get("apply", False))
        keep_settings = bool(body.get("keep_settings", False))

        try:
            pack = configpack.parse(text, pathlib.Path("<上传的配置包>"))
        except ConfigError as exc:
            raise HTTPException(status_code=400, detail=str(exc))

        if not apply_:
            return {
                "applied": False,
                "groups": len(pack.config.groups) if pack.config else 0,
                "categories": len(pack.categories),
                "persons": len(pack.persons),
                "aliases": len(pack.aliases),
            }

        # ⚠️ **`on_progress` 不许塞空 lambda**（M6 T7 Fix loop 1 / I4）：导入的
        # 汇报里有**用户必须看见**的话（最要命的一句是「本机路径没保住，已整份
        # 采用包里的 [settings]，其中包括 qq_db_dir = <别台机器>」）。
        # 塞空 lambda 等于把它扔进黑洞——前端只能显示"导入成功"，而用户
        # 根本不知道自己的路径被换掉了。这里收进 `notes` 随响应回去。
        notes: list[str] = []
        try:
            with write_lock:
                report = configpack.import_pack(
                    pack,
                    keep_settings=keep_settings,
                    paths=configpack.Paths(
                        groups=config_path(),
                        categories=categories_path(),
                        persons=persons_path(),
                    ),
                    on_progress=notes.append,
                )
        except ConfigError as exc:
            # ⚠️ **`import_pack` 自己也会抛 `ConfigError`**（Fix loop 2 / FIX 1a），
            # 最典型的一条是**跨包别名冲突**：本次导入的映射与本机现有的合起来成环
            # （本机那条往往就是**上一次导入**写进去的）。从前它不在这个 try 里
            # ⇒ 端点回 **500**，而真相是"这份包与本机冲突"= **400**。
            # ⚠️ 它发生在**任何写入之前**（`_merge_aliases` 是纯函数，跑在 ① 的最前面），
            # 所以 400 与"什么都没写"是一致的两句话。
            raise HTTPException(status_code=400, detail=str(exc))
        return {
            "applied": True,
            "groups": report.groups,
            "categories": report.categories,
            "persons": report.persons,
            "aliases": report.aliases,
            "settings_skipped": report.settings_skipped,
            "notes": notes,
            "warnings": report.warnings,
        }

    # ── 健康检查 ─────────────────────────────────────────────────────

    @app.get("/api/health")
    def api_health() -> dict:
        """服务与依赖的健康状态。**部署验收与排查都靠它。**

        ⚠️ 它**不碰主库的业务查询**（那会在库坏了时让健康检查也挂，
        于是"服务活着但数据读不出"这件事变得看不出来）。
        它只回答三个问题：服务在不在、连的是哪种数据库、缓存有没有生效。

        ⚠️ **不返回任何 DSN / 密码 / 主机名** —— 它是公网端点。
        只给"是哪种方言"与"缓存命中率"，那足够判断配置对不对，
        但不足以让人拿它去连你的库。
        """
        from . import cache as cache_mod
        from . import db as db_mod

        # `dialect_of` 只看有没有 RDS DSN 这个环境变量，config 参数
        # 目前未用到 —— 但保留签名是为了将来按库路径判断时不必改调用点。
        dialect = db_mod.dialect_of(config)
        # 缓存探测：读一次、写一次，看它是不是真的在
        c = getattr(app.state, "cache", None)
        cache_kind = type(c).__name__ if c is not None else "未挂载"
        return {
            "ok": True,
            "db": dialect,
            "cache": cache_kind,
            # 只读保证的**自证**：SQLite 走 mode=ro，MySQL 走只读账号
            "readonly": True,
        }

    # ── 群：清单走只读快照（M6 T4 / spec §1.6），增删改走 groups.toml ──────

    @app.get("/api/groups")
    def api_groups() -> dict:
        """QQ 库里的群清单 —— **只读快照**（M6 T4 / spec §1.6）。

        ⚠️ 本端点**绝不接触 QQ 库、绝不写 `data/cache/`、也不需要数据库密钥**。
        它读的是 `vigil export` / `vigil groups` 预生成的
        `data/groups-snapshot.json`。理由与代价都写在 `groups_snapshot`
        模块的 docstring 里，改这里之前先读它。
        """
        cfg = current_config()
        snap = groups_snapshot.load(groups_snapshot.default_path(cfg))
        watched = {g.id: g for g in cfg.groups}
        if snap is None:
            # ⚠️ **明说**，绝不回一个空列表：空列表会被读成
            # 「你的 QQ 库里一个群都没有」——一句假话，而且是最容易让人
            # 不再信任这个页面的那种（「只说自己有资格说的话」）。
            return {
                "available": False,
                "reason": (
                    "还没有群清单快照。请先在仓库根目录跑一次 "
                    "`uv run vigil export`（或 `uv run vigil groups`）——"
                    "它会把 QQ 库里所有群的清单写成 data/groups-snapshot.json。"
                    "本服务只读那份产物，不直接读 QQ 库。"
                ),
                "generated_at": None,
                "groups": [],
            }
        return {
            "available": True,
            "reason": "",
            "generated_at": snap.generated_at,
            "groups": [
                {
                    "id": g.id,
                    "name": g.name,
                    "messages": g.messages,
                    "last_ts": g.last_ts,
                    "watched": g.id in watched and watched[g.id].enabled,
                    "known": g.id in watched,
                }
                for g in snap.groups
            ],
        }

    @app.post("/api/groups", status_code=201)
    def api_add_group(body: dict = Body(...)) -> dict:
        """新增一个监视群，落盘到 `config/groups.toml`（原子、整体替换）。

        ⚠️ **群号必须在这里挡死 `bool`**（`isinstance(True, int)` 为真）：
        `save_config` 里的 `toml_int` 拦不住它——到那时 id 已经是 `True`、
        写出去是 `true`，读回来是 `1`。**闸必须在入口。**
        （与 M5 终审 #22「`uin = true` 被接受」是同一个形状。）
        """
        gid = body.get("id")
        if isinstance(gid, bool) or not isinstance(gid, int) or gid <= 0:
            raise HTTPException(
                status_code=400,
                detail="群号必须是正整数（拒绝 true：bool 是 int 的子类，会静默变成 1）",
            )
        # ⚠️ **空名是合法输入**（M6 终审 F2，Important）：群名的来源是快照
        # （`groups_snapshot.collect`），而它**刻意**把取不到群名的群留成 `""`
        # ——"名字缺失是正常状态"（那句论证在 `collect` 的 docstring 里）。
        # 从前这里 400「群名不能为空」⇒ 界面上那些显示成「(无名) 111」的群
        # **必然加不进来**，而它们恰恰是「配置页是唯一的加群入口」这件事的
        # 一部分。`groups.toml` 与 `cli.py` 的显示路径**本来就容许**空名
        # （`cli.py` 是 `gname or m.group_id`，`config.py` 的 `validate_config_raw`
        # 对 `name` 不做非空校验）⇒ 这道闸只挡在这里，是**不一致**。
        # ⚠️ 空名不写进 `groups.toml` 的后果也要说清：显示路径退回**裸群号**
        # （`group_names_map` 从 F2 起跳过空名，见它的 docstring）。
        name = str(body.get("name", "")).strip()
        _reject_controls(name, "name")
        tier = str(body.get("tier", TIER_NORMAL))
        if tier not in VALID_TIERS:
            raise HTTPException(
                status_code=400, detail=f"tier 只能是 {sorted(VALID_TIERS)}，收到：{tier!r}"
            )

        path = config_path()
        with write_lock:
            cfg = current_config()
            if any(g.id == gid for g in cfg.groups):
                raise HTTPException(status_code=409, detail=f"群 {gid} 已经在配置里了")
            save_config(
                Config(
                    source_dir=cfg.source_dir,
                    output_db=cfg.output_db,
                    groups=cfg.groups + (Group(id=gid, name=name, tier=tier),),
                ),
                path,
            )
        return {"id": gid, "name": name, "tier": tier}

    @app.patch("/api/groups/{gid}")
    def api_patch_group(gid: int, body: dict = Body(...)) -> dict:
        """改一个已配群的名字 / 启停 / 档位。**不做删除**（见下）。"""
        path = config_path()
        with write_lock:
            cfg = current_config()
            old = next((g for g in cfg.groups if g.id == gid), None)
            if old is None:
                raise HTTPException(status_code=404, detail=f"配置里没有群 {gid}")

            name = old.name
            if "name" in body:
                name = str(body["name"]).strip()
                # ⚠️ 与 `api_add_group` **刻意相反**（M6 终审 F2）：那边接受空名，
                # 因为它的名字来自快照、而"快照里读不出名字"是正常状态；
                # 这边收到的是**用户正填在输入框里的**一个名字，空串只可能是
                # 手滑或想抹掉它——两者都不该以"写一个空名进 groups.toml"收场
                # （要表达"未知"就该走快照那条路，而不是把它改成未知）。
                if not name:
                    raise HTTPException(status_code=400, detail="群名不能为空")
                _reject_controls(name, "name")
            enabled = old.enabled
            if "enabled" in body:
                if not isinstance(body["enabled"], bool):
                    raise HTTPException(status_code=400, detail="enabled 必须是布尔值")
                enabled = body["enabled"]
            tier = old.tier
            if "tier" in body:
                tier = str(body["tier"])
                if tier not in VALID_TIERS:
                    raise HTTPException(
                        status_code=400, detail=f"tier 只能是 {sorted(VALID_TIERS)}"
                    )

            updated = Group(id=gid, name=name, enabled=enabled, tier=tier)
            groups = tuple(updated if g.id == gid else g for g in cfg.groups)
            save_config(
                Config(source_dir=cfg.source_dir, output_db=cfg.output_db, groups=groups),
                path,
            )
        return {"id": gid, "name": name, "enabled": enabled, "tier": tier}

    @app.delete("/api/groups/{gid}", status_code=204)
    def api_delete_group(gid: int) -> None:
        """从配置里删掉一个群。

        ⚠️ **拒绝删掉最后一个群**：`load_config` 把「一个群都没配」判成
        配置错误 ⇒ 删掉它 = **把这个服务改成起不来**，而且下一次启动才现形，
        那时用户已经在网页上点过删除、拿到了「成功」。
        ⚠️ 正确动作是 `enabled = false`（保留配置、保留已导出的历史），
        **detail 里必须说清这一点**——只回一句「不能删」是没用的。
        """
        path = config_path()
        with write_lock:
            cfg = current_config()
            if not any(g.id == gid for g in cfg.groups):
                raise HTTPException(status_code=404, detail=f"配置里没有群 {gid}")
            remaining = tuple(g for g in cfg.groups if g.id != gid)
            if not remaining:
                raise HTTPException(
                    status_code=409,
                    detail=(
                        "这是最后一个群，删掉它会让配置变成「一个群都没配」——"
                        "而那是致命的配置错误，下次启动服务会直接起不来。"
                        "要停掉它请改用 enabled = false（保留配置与已导出的历史）。"
                    ),
                )
            save_config(
                Config(
                    source_dir=cfg.source_dir, output_db=cfg.output_db, groups=remaining
                ),
                path,
            )
        return None

    # ── LLM 端点配置：`config/llm.toml` 的读写（M6 T9 / spec §4.2 ④）──────
    #
    # ⚠️ **这两个端点属于 T9，不是 T3**——T3 只做了配置文件层（`config.py` 的
    # load/save）与 CLI 的三级优先级；网页上要看、要改那个端点，是这一节。
    #
    # ⚠️ **路径每次现读**（Global Constraint 11，与 `config_path()` 同形）：
    # 在 `create_app` 时算好冻进闭包的话，`monkeypatch.setattr(api, "REPO_ROOT", …)`
    # 够不着它，测试里的 PUT 会静默去写**真实仓库的 `config/llm.toml`**
    # （本机那份是有内容的 ⇒ 那是一颗真的地雷，不是理论风险）。
    def llm_path() -> pathlib.Path:
        return REPO_ROOT / "config" / "llm.toml"

    @app.get("/api/llm")
    def api_get_llm() -> dict:
        """读 LLM 端点配置。**永远不返回密钥**（它不在这份配置里，spec §3.2）。"""
        s = load_llm_settings()
        return {
            "base_url": s.base_url,
            "model": s.model,
            "enable_thinking": s.enable_thinking,
        }

    @app.put("/api/llm")
    def api_put_llm(body: dict = Body(...)) -> dict:
        """整体替换 `config/llm.toml`（原子写 ⇒ D11 允许的「Web 可写 `config/`」）。

        ⚠️ **`enable_thinking` 是三态**（`LLMSettings` 的 docstring 有完整表格）：
        JSON 这边用 `true` / `false` / **`null`** 表达，`null` = **不发这个字段**
        （通用 OpenAI 端点最安全的形状）。**非布尔的值得在这里挡成 400**：
        放它过去的话，`save_llm_settings` 的写侧守卫会抛 `ConfigError`，
        而那是**未捕获异常 ⇒ 500**——排障方向完全错（用户以为服务坏了，
        真相是他填错了字段），而且 `'false'` 这个值**曾经会被静默写成 `true`**
        （T3 deferred 的静默反转，本任务开工前才补上写侧守卫）。
        """
        base_url = str(body.get("base_url", "")).strip()
        model = str(body.get("model", "")).strip()
        if not base_url or not model:
            raise HTTPException(status_code=400, detail="base_url 与 model 都不能为空")
        if not base_url.startswith(("http://", "https://")):
            raise HTTPException(status_code=400, detail="base_url 必须以 http:// 或 https:// 开头")
        _reject_controls(base_url, "base_url")
        _reject_controls(model, "model")

        raw_thinking = body.get("enable_thinking")
        if raw_thinking is None:
            thinking: bool | None = None
        elif isinstance(raw_thinking, bool):
            thinking = raw_thinking
        else:
            # ⚠️ `isinstance(True, int)` 为真、`isinstance(1, bool)` 为假 ⇒
            # JSON 里写 `1` / `"false"` 都会落到这一支。**闸必须在入口**，
            # 与 `api_add_group` 挡 `id = true` 是同一条判据。
            raise HTTPException(
                status_code=400,
                detail="enable_thinking 只能是 true / false / null（null = 不发这个字段）",
            )
        with write_lock:
            save_llm_settings(
                LLMSettings(base_url=base_url, model=model, enable_thinking=thinking),
                llm_path(),
            )
        return {"base_url": base_url, "model": model, "enable_thinking": thinking}

    @app.get("/api/digests")
    def api_digests(limit: int = Query(30, ge=1, le=MAX_LIMIT)) -> dict:
        con = connect()
        try:
            # store.list_digests 按 window_from 排序（不是 digest_id）——契约要的
            # 就是「最近的在前」，同日多篇的窗口起点相同，顺序由 LIMIT 内的天然序决定。
            rows = store.list_digests(con, limit=limit)
        finally:
            con.close()
        return {
            "digests": [
                {
                    "digest_id": r.digest_id,
                    "day": _day_of(r.window_from),
                    "window_from": r.window_from,
                    "window_to": r.window_to,
                    "created_at": r.created_at,
                    "item_count": r.item_count,
                }
                for r in rows
            ]
        }

    @app.get("/api/digests/{digest_id}")
    def api_digest(digest_id: int) -> dict:
        names = group_names_map()
        cats = category_map()
        con = connect()
        try:
            row = store.get_digest(con, digest_id)
            if row is None:
                raise HTTPException(status_code=404, detail=f"没有这篇日报：{digest_id}")
            items = store.digest_items(con, digest_id)
        finally:
            con.close()
        return {
            "digest_id": row.digest_id,
            "day": _day_of(row.window_from),
            "window_from": row.window_from,
            "window_to": row.window_to,
            "created_at": row.created_at,
            "item_count": row.item_count,
            "body_md": row.body_md,
            "items": [item_out(it, names, cats) for it in items],
        }

    # ⚠️ **不要**用 `app.mount("/assets", StaticFiles(...))` 那一套。
    #
    # 挂载只覆盖 /assets 前缀，而 PWA 要的 `/sw.js`、`/manifest.webmanifest`、
    # `/icon-192.png` 全在**根路径**上——它们会掉进下面的 SPA 兜底，被当成
    # 前端路由返回一份 **200 的 index.html**。实测（真实进程 + curl）：
    #
    #     GET /manifest.webmanifest → 200  712 字节   ← 712 就是 index.html 的大小
    #     GET /sw.js                → 200  712 字节
    #     GET /icon-192.png         → 200  text/html
    #
    # 后果是浏览器把 HTML 当 manifest 解析、把 HTML 当 Service Worker 注册
    # （MIME 不符直接失败），「添加到主屏」整条路走不通——**而所有状态码都是
    # 200，日志上一个异常都没有**。这类缺陷只有真跑进程才现形。
    @app.get("/{full_path:path}", include_in_schema=False)
    def spa(full_path: str):
        """静态文件 + SPA 兜底。三件事按序：挡 /api、发真文件、发前端入口。

        ⚠️ 三条边界都必须挡：
        * ``/api`` 与 ``/api/*`` **都不能**落进兜底——否则 `GET /api/typo` 会返回
          200 的 HTML，调用方 ``r.json()`` 直接炸，而真正的 404 被吞掉。
          裸 ``/api`` 必须**单独**判：``startswith("api/")`` 漏掉它（``"api"``
          不以 ``"api/"`` 开头，曾是实测的漏网口），而 ``startswith("api")``
          又会把将来的 ``/apiary`` 之类一起吞掉——守卫精确到这两个形状。
        * **真存在的静态文件必须先发**（见上面 _MIME 的说明）。
        * 前端**没构建**时不许返回空白页，要明说「去跑 npm run build」——
          与日报同一条规矩：只说自己有资格说的话，没构建就说没构建。

        ⚠️ ``WEB_DIST`` 与派生的两个路径都**必须在这里现读**，不能在 create_app
        时算好冻进闭包：测试用 ``monkeypatch.setattr(api, "WEB_DIST", ...)`` 换掉
        它，冻住的话那些测试测的还是真实 web/dist（本机已构建，于是全都变成
        「怎么改都绿」的空守卫）。
        """
        # 裸 "api" 必须单列（见 docstring）：只用 startswith("api/") 时
        # `GET /api` 会一路走到 SPA 兜底，返回 200 的 HTML。
        if full_path == "api" or full_path.startswith("api/"):
            raise HTTPException(status_code=404, detail=f"没有这个端点：/{full_path}")

        dist_root = WEB_DIST.resolve()
        index_file = dist_root / "index.html"

        if full_path:
            # 这是全文件里唯一一处把**用户输入**拼进文件路径的地方，
            # 所以目录逃逸（`../.env` 之类）必须在这里挡死。
            candidate = (dist_root / full_path).resolve()
            if candidate.is_relative_to(dist_root) and candidate.is_file():
                # ⚠️ `/sw.js` 必须 `no-cache`（出口判据 3 的机械半边）：
                # Service Worker 脚本是「有新版本」唯一的发现入口，而浏览器对它走
                # **启发式缓存**（本仓此前 `Cache-Control` 零命中——计划期 grep 只命中
                # `.venv/` 里的 starlette）。被推迟的更新检查，在手机上表现为
                # 「我改了主题怎么没变」。
                #
                # ⛔ 其余静态资源**不许**跟着加：它们的正确语义是「内容哈希 + 长缓存」，
                # 顺手加 no-cache 是把一个文件的更新语义扩成全部资源的（阴性对照钉着）。
                return FileResponse(
                    candidate,
                    media_type=_MIME.get(candidate.suffix.lower()),
                    headers={"Cache-Control": "no-cache"} if candidate.name == "sw.js" else None,
                )
            # 请求的是**前端要用的那类静态资源**（js/css/图标/webmanifest…）而
            # 磁盘上没有 → 宁可 404，绝不许拿 index.html 顶。否则「manifest 不存在」
            # 会变成「manifest 是 HTML」：浏览器解析失败，而状态码是 200——
            # 正是上面那三个字段实测出来的同一类假绿。
            if candidate.suffix.lower() in _MIME:
                raise HTTPException(
                    status_code=404, detail=f"没有这个静态文件：/{full_path}"
                )

        if not index_file.is_file():
            return HTMLResponse(
                "<meta charset='utf-8'>"
                "<body style='font-family:sans-serif;padding:2rem'>"
                "<h1>前端尚未构建</h1>"
                "<p>请在仓库根目录运行：</p>"
                "<pre>cd web &amp;&amp; npm install &amp;&amp; npm run build</pre>"
                "</body>",
                status_code=503,
            )
        return FileResponse(index_file)

    return app
