"""每日管线：export → refine → digest，一条命令跑完，失败看得见。

存在的理由（spec §4.8 / §5 M4）：任务计划每天调一次，产出当天该有的日报。
拆成三条独立命令由调度器串起来是**不行**的——任务计划不保存输出，
三个任务各自的成败、各自的耗时、各自的错误会散成三份互相不知情的东西，
而"那天到底跑成了没有"恰恰是无人值守唯一要回答的问题。

⚠️ **本模块只管编排**：不配置日志、不加锁、不定退出码。那三件事归
`cli.cmd_daily`——这样 `run()` 可以用假的三个阶段纯本地测试，而那三件事
（要碰文件系统、要拿进程级锁、要决定退出码）各自单独验。

⚠️ **`emit` 坏掉不算"阶段失败"**（Task 1 把它钉成「写不进日志就抛 OSError」）：
`run()` 自己的 emit 点**不设兜底**，抛就一路抛到 `cmd_daily` 的顶层兜底
→ 非零退出码。理由见 `run()` 的 docstring 末尾，那里也写着为什么这与
"任何阶段失败都不抛"并不矛盾。

失败语义见 `run()` 的 docstring，那是本模块唯一需要记住的东西。
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass

from . import digest as digest_mod
from . import ingest as ingest_mod
from . import source as source_mod
from . import logs
from . import refine as refine_mod
from .config import Config

STAGE_EXPORT = "export"
STAGE_REFINE = "refine"
STAGE_DIGEST = "digest"

# 无人值守路径的 token 预算上限。**必须有这个数**：`vigil refine` 的 `--budget`
# 只存在于 CLI，而 `daily` 是没人看着跑的那条路——没有上限就意味着"首次挂机
# 或断更积压时，08:00 那次会在无人看管下把全部积压跑完"，时长与花费都无上界
# （`StartWhenAvailable` 还会把错过的补跑）。撞预算**不是失败**（护栏按设计
# 生效）：`refine` 照常返回、账目照记，日报自己的覆盖率检查会说「仅抽取了 X/Y」。
#
# 数怎么来的（锚点全部是实测，不是手感值）：
#   · **单条扫描消息 ≈ 15 token**：M1 的 1,050 条窗口实跑 13,520 输入 token
#     （≈12.9/条）；输出按 M1 模型对照那轮的实测比例折算（41 批实跑输出
#     6,448 token、单批峰值 516，按该轮成本 ¥0.038 反推输入 ≈43,500
#     ⇒ 输出/输入 ≈15%）≈2/条。**用「扫描条数」而不是「批数」换算**，因为
#     送模型的比例（预筛 + 扩上下文）会随群的性质变，实测约 11–22%。
#   · 一天正常增量：已验收的三篇日报是 **151 / 514 / 536 条**（9–10 个群）
#     ⇒ ≈ 2,300 / 7,700 / 8,000 token。
#   · 单群爆量日（2026-08-08：1 个群 1,059 条）⇒ ≈ 16,000 token。
#   · 一次全史积压（47,719 条）⇒ ≈ 72 万 token，与 M1 独立估算的
#     「全量约 60 万**输入** token」一致；断更一个月（约 1.5 万条）≈ 23 万。
# 取 **300,000**：≈37 倍正常日、≈19 倍爆量日——**宁可宽松也不要卡死日常**；
# 同时把"首次挂机就把全史跑完"从 72 万截到 ≤42%，一次跑不完但**有界**，
# 而 refine 是增量的（跑过的消息记进 `refine_runs`），剩下的第二天接着跑。
DAILY_BUDGET_TOKENS = 300_000


@dataclass(frozen=True)
class StageResult:
    """一个阶段的结局。`skipped_reason` 非空表示"因上游失败而跳过"。"""

    name: str
    ok: bool
    detail: str
    skipped_reason: str = ""


@dataclass(frozen=True)
class RunReport:
    """一次 daily 的完整账目。全部进日志，失败时进 LAST-ERROR.txt。"""

    stages: tuple[StageResult, ...] = ()
    last_error: str = ""
    deadline: str = ""

    # ⚠️ 这里曾有一个 `counters: dict[str, str]`（阶段名 → 该阶段话术）。T7 审查 F4
    # 判它是**死字段**：`grep -rn counters vigil/ tests/ web/src` 除本文件外**零命中**，
    # `cmd_daily` 不读它、测试也不钉它的值（变异成 `{}` 全绿）。而它承载的信息
    # `report.stages[i].detail` 一字不少，所以删掉——不给「看起来有、其实没人看」
    # 的字段留位（那正是本轮反复抓到的「空守卫」形状）。要加回来，请先给出消费方。

    @property
    def ok(self) -> bool:
        # ⚠️ 因上游失败而**跳过**的阶段 `ok=False`（见 `run()` 的 digest 分支）。
        # 它不会把一份本来成功的账目拖成失败：置 `export_failed` 的那两条路径
        # 本身就已经把 export 记成 `ok=False` 了（见 `test_export_failure_...`）。
        # 反过来若记 `ok=True`，LAST-ERROR.txt 就只剩上游那条，
        # 「今天为什么没有日报」反而看不出来。
        return all(s.ok for s in self.stages)


def _summarize(results: list[StageResult]) -> str:
    """把失败的阶段拼成一句人话。

    ⚠️ 判据是 `ok` 而不是 `skipped_reason`：跳过的那条**照样进** last_error，
    且它把上游的理由复述了一遍（"digest: 已跳过：export 失败 ⇒ …"）。
    那不是重复计入——`LAST-ERROR.txt` 的读者（第二天早上的人）问的第一个
    问题是"今天为什么没有日报"，答案必须在这一句里。

    （曾同时返回一张 `counters` 表，T7 审查 F4 判它死字段后删掉了——
    见 `RunReport` 上那段。）
    """
    failed = [r for r in results if not r.ok]
    return "；".join(f"{r.name}: {r.detail}" for r in failed)


def run(
    *,
    config: Config,
    llm_key: str,
    day: str | None = None,
    on_progress: Callable[[str], None] | None = None,
) -> RunReport:
    """跑一轮完整管线。**任何阶段失败都不抛异常**——全部记进返回值。

    ⚠️ 为什么不抛：`daily` 的调用方是任务计划，它唯一能看的只有退出码。
    一个抛到顶的异常只会留下一个 traceback，而这轮"跑到哪一步、产出多少"
    全部丢失。所以这里把每个阶段都圈起来，**失败也继续**，最后一次性汇报。

    ⚠️ 捕宽 `Exception` 的前提是**可见**：每一条失败都进 `StageResult`
    → 进 `RunReport.last_error` → 由 CLI 写进 `LAST-ERROR.txt` 并返回非零。
    静默吞异常在本项目是不可接受的（M1/M3 反复记录）。

    失败语义（冻结，见计划 §三 3.3）：

    ========  ==============================  ==========================
    阶段      失败判据                        对后续的影响
    ========  ==============================  ==========================
    export    ``failed_groups`` 非空，或抛异常  **跳过 digest**（见下）
    refine    ``errors`` 非空，或抛异常         digest 照跑
    digest    ``errors`` 非空，或抛异常         ——
    ========  ==============================  ==========================

    **export 失败为什么跳过 digest**：export 失败 ⇒ `messages` 不完整 ⇒
    日报那句「当天 N 个群 M 条消息」可能读起来像"这天很安静"，而其实是
    "根本没读进来"。这个项目已经抓过四次同一句假话的不同变体（见 M2/M3
    的「只说自己有资格说的话」），不能让它有第五个入口。

    ⚠️ **"不抛"的唯一例外是 `emit` 自己坏掉**（Task 1 把 `logs.emit` 钉成
    「写不进日志就抛 `OSError`」）。这时候三种情形分别是：

    * `emit` 在**阶段内部**炸（阶段拿到的 `on_progress` 就是 `emit`）——
      `OSError` 冒到该阶段的 `except Exception`，于是那条**记成阶段失败**
      （`ok=False`，理由写进 `detail`/`last_error`），本函数照常往下跑。
      绝不允许"吞掉后照常继续"：那会得到 `ok=True` 但日志有洞的账目。
    * `emit` 在**本函数自己的 emit 点**炸（开场横幅、阶段话术、小结）——
      这里**不套 try**，异常直接抛出 `run()`，由 `cmd_daily` 的顶层兜底
      → **非零退出码**。理由：这些 emit 点之所以存在，就是为了让"跑到哪一步"
      留下来；记录写不进去时，唯一的真话是"这次没跑成"，而退出码是无人值守
      下唯一还活着的信号。
    * 两种情形都**不会**产出"看起来成功"的返回值——`test_broken_emit_...`
      与 `test_emit_failure_inside_a_stage_...` 各钉住一条。
    """
    emit = on_progress or logs.emit
    # ⚠️ 无人值守这条路**必须自己读** `config/llm.toml`（没有命令行可接）。
    # ⚠️ `on_warn=emit`：文件缺失时那句「会慢 40 倍」必须进日志——
    # 任务计划不保存 stdout，而 `LAST-ERROR.txt` 只在**阶段失败**时才写。
    from .config import load_llm_settings

    llm_settings = load_llm_settings(on_warn=emit)
    emit(
        f"[daily] LLM 端点：{llm_settings.base_url} · 模型 {llm_settings.model} · "
        f"思考={'不发该字段' if llm_settings.enable_thinking is None else llm_settings.enable_thinking}"
    )
    day = day or digest_mod.yesterday()
    since, until = digest_mod.day_window(day)

    results: list[StageResult] = []
    emit(f"═══ vigil daily 开始 · 日报窗口 {day} ═══")

    # ── 阶段 1：export ────────────────────────────────────────────────
    emit(f"[daily] 1/3 ingest：从数据源摄取到 {config.output_db}")
    export_failed = False
    try:
        src = source_mod.open_source(config.source, config)
        est = ingest_mod.ingest(config, src, on_progress=emit)
        if est.failed_groups:
            export_failed = True
            who = "、".join(str(gid) for gid, _ in est.failed_groups[:5])
            detail = (
                f"{len(est.failed_groups)} 个群整群读不到（{who}…），"
                f"其余 {len(est.per_group)} 个群共 {est.total_rows:,} 条消息"
            )
        else:
            detail = f"{len(est.per_group)} 个群，共 {est.total_rows:,} 条消息"
            if est.total_skipped:
                # 坏页读不到是**已知且非致命**的（QQ 库物理损坏），照实说
                detail += f"（另有 {est.total_skipped:,} 条被降级跳过）"
        results.append(StageResult(STAGE_EXPORT, not export_failed, detail))
    except Exception as exc:  # noqa: BLE001 — 见 docstring：记下并继续
        export_failed = True
        results.append(StageResult(STAGE_EXPORT, False, f"抛出异常：{exc}"))
        emit(f"[daily] ingest 失败：{exc}")

    # ── 阶段 2：refine ────────────────────────────────────────────────
    emit("[daily] 2/3 refine：把消息提炼成结构化条目")
    try:
        rst = refine_mod.refine(
            config,
            api_key=llm_key,
            db_path=config.output_db,
            prompt_ver=refine_mod.PROMPT_VERSION,
            model=llm_settings.model,
            base_url=llm_settings.base_url,
            enable_thinking=llm_settings.enable_thinking,
            # ⚠️ **必须传**：不传就是 None，而 `refine()` 的预算护栏只在
            # `budget_tokens is not None` 时生效 ⇒ 无人值守这条路**一点上限都没有**
            # （M4 终审 I-3）。取值依据见 `DAILY_BUDGET_TOKENS` 上面那段。
            budget_tokens=DAILY_BUDGET_TOKENS,
            on_progress=emit,
        )
        if rst.errors:
            # ⚠️ **必须带上第一条错误的原文**：LAST-ERROR.txt 是第二天早上的人
            # 唯一能看的东西，只说"1 批出错"等于没说（哪一批？429 还是超时？）。
            # brief 自带的 `test_refine_failure_does_not_skip_digest` 就是按
            # 这个判据写的（`assert "429" in report.last_error`），而 brief 的
            # 实现只写了条数——**那条测试对不上那段实现**，见报告 §偏差 D3。
            # 全部错误可能有几十条（每批一条），只摘第一条 + 总数，并且截断——
            # 但截断必须**看得见**（写"…"），不许静默吃掉。
            head = rst.errors[0]
            if len(head) > 300:
                head = head[:300] + "…（截断，全文见日志）"
            more = f"；其余 {len(rst.errors) - 1} 条见日志" if len(rst.errors) > 1 else ""
            detail = (
                f"扫描 {rst.scanned:,} 条 → 送模型 {rst.sent_messages:,} 条"
                f"（{rst.batches} 批）→ 产出 {rst.items_saved} 条；"
                f"{len(rst.errors)} 批出错：{head}{more}"
            )
            results.append(StageResult(STAGE_REFINE, False, detail))
        else:
            detail = (
                f"扫描 {rst.scanned:,} 条 → 送模型 {rst.sent_messages:,} 条"
                f"（{rst.batches} 批）→ 产出 {rst.items_saved} 条"
            )
            results.append(StageResult(STAGE_REFINE, True, detail))
    except Exception as exc:  # noqa: BLE001
        results.append(StageResult(STAGE_REFINE, False, f"抛出异常：{exc}"))
        emit(f"[daily] refine 失败：{exc}")

    # ── 阶段 3：digest ────────────────────────────────────────────────
    if export_failed:
        why = "export 失败 ⇒ messages 不完整，日报的「这天安静」判据不可信"
        emit(f"[daily] 3/3 digest：跳过（{why}）")
        results.append(
            StageResult(STAGE_DIGEST, False, f"已跳过：{why}", skipped_reason=why)
        )
    else:
        emit(f"[daily] 3/3 digest：合成 {day} 的日报")
        try:
            dst = digest_mod.digest(
                config,
                api_key=llm_key,
                db_path=config.output_db,
                since=since,
                until=until,
                day_label=day,
                model=llm_settings.model,
                base_url=llm_settings.base_url,
                enable_thinking=llm_settings.enable_thinking,
                on_progress=emit,
            )
            if dst.errors:
                detail = "；".join(dst.errors)
                results.append(StageResult(STAGE_DIGEST, False, detail))
            else:
                detail = (
                    f"{dst.groups} 个群 {dst.messages:,} 条消息 → "
                    f"{dst.items} 条 item → 日报 {dst.lines} 行"
                )
                if dst.output_path:
                    detail += f"，已落 {dst.output_path}"
                results.append(StageResult(STAGE_DIGEST, True, detail))
        except Exception as exc:  # noqa: BLE001
            results.append(StageResult(STAGE_DIGEST, False, f"抛出异常：{exc}"))
            emit(f"[daily] digest 失败：{exc}")

    report = RunReport(
        stages=tuple(results), last_error=_summarize(results), deadline=day,
    )

    emit("─── 本轮小结 ───")
    for r in report.stages:
        mark = "✓" if r.ok else "✗"
        emit(f"  {mark} {r.name}: {r.detail}")
    emit("═══ vigil daily 结束：" + ("全部成功" if report.ok else "有失败") + " ═══")
    return report
