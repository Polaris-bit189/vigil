"""VIGIL 命令行入口。

    vigil demo      离线一键：造合成语料 → 摄取 → 抽取 → 日报（不联网）
    vigil groups    列出数据源里所有群（含是否已纳入监听）
    vigil ingest    把纳入的群从数据源摄取进明文库
"""

from __future__ import annotations

import argparse
import datetime as dt
import pathlib
import sqlite3
import sys

from . import categories as categories_mod
from . import config as config_mod
from . import groups_snapshot
from . import ingest as ingest_mod
from . import lock, logs
from . import overrides
from . import reader
from . import refine as refine_mod
from . import repass as repass_mod
from . import source as source_mod
from . import store
from . import synth as synth_mod
from .categories import load_categories
from .config import (
    REPO_ROOT,
    ConfigError,
    load_config,
    load_key,
    load_llm_key,
    load_llm_settings,
    load_persons,
    requires_llm_key,
)


def _open_source(config):
    """按配置取数据源。取不到就快速失败——静默回退会让拼错的配置跑出假结果。"""
    try:
        return source_mod.open_source(config.source, config)
    except ConfigError as exc:
        sys.exit(f"[配置错误] {exc}")


def _load_config_only():
    """只读配置（不需要密钥）——查询层用。"""
    try:
        return load_config()
    except ConfigError as exc:
        sys.exit(f"[配置错误] {exc}")


def _require_export_db(config):
    if not config.output_db.is_file():
        sys.exit(
            f"[错误] 明文库不存在: {config.output_db}\n  请先运行 vigil ingest"
        )
    return config.output_db


def _load_or_die():
    """加载配置与密钥。出错就快速失败——配置问题不该被静默绕过。"""
    try:
        config = load_config()
    except ConfigError as exc:
        sys.exit(f"[配置错误] {exc}")
    key = load_key()
    if not key:
        sys.exit(
            "[配置错误] 没有数据库密钥。\n"
            "  请在仓库根目录建 .env 并写入：VIGIL_DB_KEY=<16位密钥>\n"
            "  密钥获取方式见 docs/SETUP-ROUTE-C.md"
        )
    return config, key


def _fmt_ts(ts: int) -> str:
    if not ts or ts <= 0:
        return "-"
    return dt.datetime.fromtimestamp(ts).strftime("%Y-%m-%d")


def cmd_groups(args) -> int:
    """列出 QQ 本地库里的群。

    ⚠️ **仍然上锁**（原 M6 T1 的规矩）：本命令会写群清单快照，而快照与
    `vigil ingest` 共用同一个文件。撞车时报「已有实例在跑」并给退出码 2，
    **响亮地失败**，而不是静默地互相踩。

    （早年这一条的理由是"会写 QQ 库的缓存文件"；开源版没有那个缓存了，
      但"快照不能并发写"这半条依然成立。）
    """
    try:
        with lock.SingleInstance():
            return _cmd_groups_locked(args)
    except lock.AlreadyRunning as exc:
        # 与 `cmd_daily` / `cmd_ingest` 同一语义：2 不是 1——「上一次还没跑完」
        # 等着就行，「这次跑失败了」才要人来看。
        logs.emit(f"[跳过] {exc}")
        return 2


def _cmd_groups_locked(args) -> int:
    config = _load_config_only()
    src = _open_source(config)

    counts = src.list_groups()
    try:
        names = src.group_names()
    except Exception as exc:  # noqa: BLE001 — 群名缺失不该让 listing 失败
        print(f"[警告] 群名读取失败（不影响群号）：{str(exc)[:70]}\n", file=sys.stderr)
        names = {}

    # ⚠️ 顺手写快照（M6 T4）：Web 的「配置」页只读这个文件，永远不碰 QQ 库。
    # ⚠️ 失败**只警告不中断**：快照是增强项，而 `vigil groups` 的主业是
    # 把那张家底表打到屏幕上（人工排障时最需要的东西）。
    # ⚠️ `except` 必须是 `Exception`、**不是** `OSError`（M6 T4 复审 Minor-2）：
    # 磁盘/权限之外的失败同样不该让整条命令中断——`collect()` 撞上形状不对的
    # `counts` 会抛 `TypeError`，而它对用户是同一条处置：表格照打、退出码照 0。
    # 只捕 `OSError` 的话，「失败只警告不中断」就只覆盖了一半（实测：
    # `TypeError` 会径直冒出 `_cmd_groups_locked`）。
    # ⚠️ 措辞也不能想当然：表格是在这段**之后**才打印的（M6 T4 复审 Minor-1），
    # 写「不影响上面的列表」是错的。
    try:
        snap_path = groups_snapshot.write(
            groups_snapshot.collect(counts, names),
            groups_snapshot.default_path(config),
        )
    except Exception as exc:  # noqa: BLE001 — 快照是增强项，任何失败都只警告
        print(f"[警告] 群清单快照写不出去（不影响群列表）：{exc}", file=sys.stderr)
    else:
        # ⚠️ **报进度必须留在 `try` 外面**（M6 终审 #14；规矩的完整论证在
        # `export.py` 的 `on_progress(snap_note)` 那一段）：写在 `try` 里的话，
        # "报进度失败"（stderr 被关掉 / 管道断开 / 日志写不进去）会被上面那句
        # `except Exception` 吞成「群清单快照写不出去」——**一个指向错误方向的
        # 诊断**：真相是输出流的问题，而那句话让用户去查快照（而它**已经写好了**）。
        # 能吞的只有"打扫类"失败；**报进度是本次运行的记录，它的失败必须冒出去**。
        # ⚠️ `else` 而不是"挪到 except 之后"：后者会在**失败时**也打印这一行
        # ——那就成了一句假话（写着"已写"，其实没写）。
        print(f"[快照] 已写 {snap_path}", file=sys.stderr)

    watched = {g.id for g in config.enabled_groups}
    known = {g.id for g in config.groups}

    print(f"{'':<3}{'消息数':>9}  {'最新':<11} {'群号':<11} 群名")
    print("-" * 76)
    for gid, n, tmax in counts:
        if args.only_watched and gid not in watched:
            continue
        mark = "★" if gid in watched else ("·" if gid in known else " ")
        print(f"{mark:<3}{n:>9,}  {_fmt_ts(tmax):<11} {gid:<11} {names.get(str(gid), '?')}")
    print("-" * 76)
    print("★ = 已纳入监听   · = 在配置里但已停用   空格 = 未纳入")
    print(f"共 {len(counts)} 个群有消息记录，已纳入 {len(watched)} 个")

    # 提示：已纳入但库里没消息的群（可能是刚加、还没同步）
    counted = {gid for gid, _, _ in counts}
    for g in config.enabled_groups:
        if g.id not in counted:
            print(f"  [注意] 已纳入的 {g.id} {g.name} 在库里没有任何消息")
    return 0


def cmd_demo(args) -> int:
    """离线一键：造合成语料 → 摄取。

    **这是复现红线的最小路径**：评委 `git clone` 之后一条命令就能拿到
    一个可浏览的库，不需要 QQ、不需要密钥、不需要联网。

    ⚠️ 它**只做摄取**，不跑 LLM 抽取——抽取需要模型（本地 Ollama 或云端
    MaaS），而"没有模型也能验证数据链路"这件事本身是有价值的：
    它把「环境问题」与「模型问题」分开，排障时少猜一半。

    要跑完整管线（含抽取与日报）用 `vigil demo` 之后接着 `vigil refine`，
    或直接用 `vigil daily`（那需要模型配置正确）。
    """
    config = _load_config_only()
    spec_path = synth_mod.default_spec_path(config)

    try:
        spec = synth_mod.load_spec(spec_path)
    except ConfigError as exc:
        sys.exit(f"[配置错误] {exc}")

    if args.seed is not None:
        # dataclasses.replace 而不是原地改：SynthSpec 是 frozen 的，
        # 而"改 seed"必须是显式的（它会换掉整份语料）。
        import dataclasses

        spec = dataclasses.replace(spec, seed=args.seed)

    logs.emit(f"合成语料规格：{spec_path}")
    logs.emit(f"  seed={spec.seed} days={spec.days} groups={len(spec.groups)}")

    with lock.SingleInstance():
        logs.emit("-" * 60)
        src = synth_mod.SyntheticSource(spec)
        stats = ingest_mod.ingest(config, src, on_progress=logs.emit)
        logs.emit("-" * 60)
        logs.emit(f"完成：{len(stats.per_group)} 个群，共 {stats.total_rows:,} 条消息")
        if stats.media_total:
            pct = stats.media_local / stats.media_total * 100
            logs.emit(
                f"图片引用：{stats.media_total:,} 张，本地可取 "
                f"{stats.media_local:,} 张（{pct:.0f}%）"
            )
        logs.emit(f"\n消息库已就绪：{config.output_db}")
        # ⚠️ 这一步只落**消息**（messages / sender_names / msg_media 三张表）。
        # `items`（结构化条目）与 `digests`（日报）要跑 LLM 抽取才有 ——
        # 不把这件事说清楚，用户 `vigil serve` 之后打开界面会看到
        # 「no such table: items」，而不知道是漏了哪一步。
        logs.emit("")
        logs.emit("  下一步：")
        logs.emit("    vigil refine   抽取条目（需要 LLM 端点，见 config/llm.toml）")
        logs.emit("    vigil serve    起网页（要在 refine 之后）")
        if stats.failed_groups:
            return 1
    return 0


def cmd_sync(args) -> int:
    """把本机库的结构化结果推到云 RDS。

    ⚠️ 它**只推四张表**（items / item_sources / digests / digest_items）。
    `messages` 与 `sender_names` 永不离开本机 —— 见 vigil/sync.py 的隐私契约。

    ⚠️ `--dry-run` 不需要 RDS：它只数行数。用来验证"要推什么"。
    """
    from . import sync as sync_mod

    config = _load_config_only()
    try:
        stats = sync_mod.sync_to_rds(
            config,
            rds_dsn=args.dsn,
            dry_run=args.dry_run,
            on_progress=logs.emit,
        )
    except ConfigError as exc:
        sys.exit(f"[配置错误] {exc}")

    logs.emit("-" * 60)
    if args.dry_run:
        logs.emit(f"[dry-run] 共 {stats.total:,} 行会推到 RDS（没有真的写）")
    else:
        logs.emit(f"完成：共 {stats.total:,} 行已推到 RDS")
    return 0


def cmd_schedule(args) -> int:
    """排期：把待办条目排进课表之外的空闲块。

    ⚠️ **只读**：它不写库、不改任何东西，所以**不上锁**（与 `read` / `who`
    同族）。排期结果每次都是现算的 —— 存下来的结果第二天就过期了。

    ⚠️ 退出码：**0 = 全部排下**，**1 = 有排不下的**。
    1 不是"出错"——它是「你需要知道这件事」，而无人值守下退出码是唯一
    还活着的信号（与 refine/digest 那条纪律一致）。
    """
    from . import conflicts as conflicts_mod
    from . import schedule as schedule_mod
    from . import timetable as timetable_mod

    config = _load_config_only()
    db = _require_export_db(config)

    tt = timetable_mod.load_timetable(args.timetable)
    if tt.is_empty:
        # ⚠️ **空课表必须明说**，不许排出一份"全是空闲"的结果 ——
        # 那看起来像"排成功了"，而实际上我们根本不知道用户什么时候有空。
        sys.exit(
            f"[配置错误] 还没填课表：{args.timetable or timetable_mod.DEFAULT_TIMETABLE}\n"
            "  排期要知道「你什么时候有空」——这个信息只有你能给。\n"
            "  往那份文件里加几段 [[courses]]（格式见文件头的注释）。"
        )

    window_days = args.days
    now = int(dt.datetime.now().timestamp())
    day0 = dt.datetime.now().replace(hour=0, minute=0, second=0, microsecond=0)
    window_from = int(day0.timestamp()) if args.from_today else now
    window_to = int((day0 + dt.timedelta(days=window_days)).timestamp())

    conn = sqlite3.connect(str(db), uri=True)
    try:
        todo = store.schedulable_items(conn, since=window_from, until=window_to)
        activities = store.items_with_start(conn, since=window_from, until=window_to)
    finally:
        conn.close()

    # ⚠️ **先去重再排期**（①）：同一件事被抽成多条时，`start_ts` 完全相同 ——
    # 不去重的话，排期器会把它们当成"两件都占时间的事"，而界面上用户看到的是
    # 一个活动被数了两遍。
    dup_groups = conflicts_mod.find_duplicate_groups(activities)
    dropped = {iid for g in dup_groups for iid in g.item_ids[1:]}
    if dup_groups:
        print(
            f"[去重] {len(dup_groups)} 组重复（共 {len(dropped)} 条多余），"
            f"按下标最早的那条计："
        )
        for g in dup_groups:
            when = dt.datetime.fromtimestamp(g.start_ts)
            print(f"  · {when:%m-%d %H:%M}  item {g.representative}（另有 {g.extra_count} 条重复）")
        print()

    # ⚠️ 有 `start_ts` 的活动是**障碍物**（它已经有确定时刻了），
    # 不是待排的任务 —— 给它再排一个时间是荒谬的。
    busy = [
        schedule_mod.Busy(start=ts - 30 * 60, end=ts + args.activity_min * 60, label=title)
        for iid, title, ts in activities
        if iid not in dropped
    ]
    items = [
        schedule_mod.SchedulableItem(item_id=iid, title=title, deadline_ts=dl)
        for iid, title, dl in todo
    ]

    plan = schedule_mod.plan_schedule(
        items, tt, busy=busy, window_from=window_from, window_to=window_to,
        duplicate_item_ids=dropped,
    )
    print(schedule_mod.describe(plan))
    print()
    print(
        f"（窗口 {window_days} 天；待排 {len(items)} 条；"
        f"已占用的活动 {len(activities) - len(dropped)} 条）"
    )
    # ⚠️ **冲突也是"要人来看"的事**：退出码 1 不只是"排不下"。
    # 无人值守下退出码是唯一还活着的信号 —— 有两个会撞在一起而不报，
    # 用户会以为一切正常。
    return 1 if (plan.unscheduled or plan.conflicts or plan.course_conflicts) else 0


def cmd_ingest(args) -> int:
    # ⚠️ **人工跑也要上锁**（M4 独立审查 F4）：`docs/SETUP-自动化.md` §七 给退出码 2
    # 写的理由就是「用户手动又跑一次」这个场景，而此前只有 `cmd_daily` 上锁 ⇒
    # 人工命令与任务计划撞车时**完全不受保护**——spec §4.8 的「幂等可重入」
    # 只堵了一半（两个 refine 并发会各读同一批待处理消息 ⇒ 抽两遍、烧两份 token）。
    # ⚠️ 不会与 `cmd_daily` 重入：`daily.run()` 调的是库函数（`ingest_mod.ingest`
    # 等）而不是 `cmd_*`，而锁**刻意不做重入**（见 `lock.py` 的 docstring）。
    try:
        with lock.SingleInstance():
            config = _load_config_only()
            logs.emit(f"摄取 {len(config.enabled_groups)} 个群 → {config.output_db}")

            stats = ingest_mod.ingest(config, _open_source(config), on_progress=logs.emit)
            logs.emit("-" * 60)
            logs.emit(f"完成：{len(stats.per_group)} 个群，共 {stats.total_rows:,} 条消息")

            if stats.skipped_by_group:
                logs.emit(f"\n[注意] 有 {stats.total_skipped:,} 条消息被降级跳过：")
                for gid, gname, n in stats.skipped_by_group:
                    logs.emit(f"  {gid} {gname}：{n:,} 条")
                logs.emit("  数据源侧已上报的降级（例如坏页），同群其余消息不受影响。")

            if stats.failed_groups:
                logs.emit(f"\n[失败] {len(stats.failed_groups)} 个群整群读不到：")
                for gid, err in stats.failed_groups:
                    logs.emit(f"  {gid}: {err}")

            if stats.media_total:
                pct = stats.media_local / stats.media_total * 100
                logs.emit(
                    f"\n图片引用：{stats.media_total:,} 张，本地可取 "
                    f"{stats.media_local:,} 张（{pct:.0f}%）"
                )
                logs.emit(
                    "  取不到的并非「对不上号」，而是数据源侧没有缓存该图（见 vigil/source.py）。"
                )
            if stats.failed_groups:
                # ⚠️ 非零退出码是 M4 自动化的报警信号——别吞掉。旧版这里无条件
                # return 0，于是「N 个群整群读不到」在任务计划看来是**成功**。
                # 坏页（skipped_by_group）**不算失败**：那是 QQ 库的物理损坏，
                # 已知、非致命、且同群其余消息不受影响，上面已经照实报了。
                return 1
            return 0
    except lock.AlreadyRunning as exc:
        # 与 `cmd_daily` 同一语义：2 不是 1——「上一次还没跑完」等着就行，
        # 「这次跑失败了」才要人来看（计划 §三 3.4）。
        logs.emit(f"[跳过] {exc}")
        return 2


def cmd_read(args) -> int:
    """读消息。不设任何过滤 = 全量保底检索。"""
    config = _load_config_only()
    db = _require_export_db(config)
    try:
        messages = reader.read_messages(
            db,
            group=args.group,
            sender=args.sender,
            keyword=args.keyword,
            since=args.since,
            until=args.until,
            limit=args.limit,
        )
    except ValueError as exc:
        sys.exit(f"[参数错误] {exc}")

    if not messages:
        print("（没有匹配的消息）")
        return 0

    names = {g.id: g.name for g in config.groups}
    for m in messages:
        gname = names.get(m.group_id, "")
        print(f"{m.time_str}  [{gname or m.group_id}]  {m.sender}: {m.content}")
    print(f"--- 共 {len(messages)} 条 ---")
    return 0


def cmd_who(args) -> int:
    """谁在说：按发言条数排行，用于「锁定哪个人」。"""
    config = _load_config_only()
    db = _require_export_db(config)
    try:
        stats = reader.sender_stats(
            db, group=args.group, since=args.since, limit=args.limit
        )
    except ValueError as exc:
        sys.exit(f"[参数错误] {exc}")

    if not stats:
        print("（没有匹配的消息）")
        return 0

    names = {g.id: g.name for g in config.groups}
    scope = names.get(args.group, str(args.group)) if args.group else "全部已纳群"
    print(f"发言排行 —— {scope}")
    print(f"{'条数':>7}  {'QQ号':<12} 姓名")
    print("-" * 52)
    for name, count, uin in stats:
        print(f"{count:>7,}  {uin or '-':<12} {name}")
    return 0


def cmd_media(args) -> int:
    """列出本地已有缓存的图片——这些是能直接打开的。"""
    config = _load_config_only()
    db = _require_export_db(config)
    try:
        items = reader.local_images(
            db, group=args.group, since=args.since, limit=args.limit
        )
    except ValueError as exc:
        sys.exit(f"[参数错误] {exc}")

    if not items:
        print("（没有本地可取的图片）")
        return 0

    names = {g.id: g.name for g in config.groups}
    for it in items:
        gname = names.get(it.group_id, "")
        print(f"{it.time_str}  [{gname or it.group_id}]  {it.sender}")
        print(f"    {it.path}")
    print(f"--- 共 {len(items)} 张 ---")
    return 0


def cmd_refine(args) -> int:
    """抽取：把消息变成结构化条目。"""
    # ⚠️ **人工跑也要上锁**（M4 独立审查 F4）：`docs/SETUP-自动化.md` §七 给退出码 2
    # 写的理由就是「用户手动又跑一次」这个场景，而此前只有 `cmd_daily` 上锁 ⇒
    # 人工命令与任务计划撞车时**完全不受保护**——spec §4.8 的「幂等可重入」
    # 只堵了一半（两个 refine 并发会各读同一批待处理消息 ⇒ 抽两遍、烧两份 token）。
    # ⚠️ 不会与 `cmd_daily` 重入：`daily.run()` 调的是库函数（`ingest_mod.ingest`
    # 等）而不是 `cmd_*`，而锁**刻意不做重入**（见 `lock.py` 的 docstring）。
    try:
        with lock.SingleInstance():
            from . import refine as refine_mod

            config = _load_config_only()
            db = _require_export_db(config)

            # ⚠️ 设置的**解析顺序**固定是：命令行 > config/llm.toml > 内置默认。
            # 命令行优先是为了让排障能"先试一下别的端点"而不用改文件。
            #
            # ⚠️ **必须接住 `ConfigError`**（M6 T3 re-review Important-3）：`main()`
            # 是裸的 `return args.func(args)`，**没有顶层兜底** ⇒ 用户在
            # `config/llm.toml` 里写错一个键（`base_url = 123`、
            # `enable_thinking = "false"`），拿到的是一段**裸 traceback**，
            # 而不是与 `_load_config_only()` 同形的 `[配置错误] …`。
            # 同批的 T2 刚为 `load_config` 的 `TOMLDecodeError` 修了同一件事
            # （Global Constraint 5：配置写错要快速失败成**一句人话**）。
            try:
                llm_settings = load_llm_settings(on_warn=logs.emit)
            except ConfigError as exc:
                sys.exit(f"[配置错误] {exc}")

            # ⚠️ 密钥闸**必须在上面那个 try 之后**：写错 `config/llm.toml` 时
            # 冒出来的 `ConfigError` 才有人接。放在它前面的话，用户拿到的
            # 是一段裸 traceback 而不是一句 `[配置错误]` —— 而那正是
            # 这个 try 存在的理由（见它上面那段注释）。
            #
            # ⚠️ `auth = "none"`（本地 Ollama）时**不要密钥**：判据走
            # `requires_llm_key()`，免得四处各写一遍而漂移。
            api_key = load_llm_key(llm_settings)
            if requires_llm_key(llm_settings) and not api_key and not args.dry_run:
                sys.exit(
                    "[配置错误] 没有 LLM 密钥。\n"
                    f"  请在仓库根目录的 .env 里写入：{llm_settings.api_key_env}=...\n"
                    '  （--dry-run 不需要密钥；本地 Ollama 请在 config/llm.toml '
                    '写 auth = "none"）'
                )

            try:
                since_ts = reader._to_epoch(args.since)
                until_ts = reader._to_epoch(args.until)
            except ValueError as exc:
                sys.exit(f"[参数错误] {exc}")

            # refine() 会对 --redo/--limit 的非法组合抛 ValueError。不接的话用户看到的是
            # 裸 traceback，而不是一行可读的参数错误——与 other 子命令的既有风格不一致。
            try:
                stats = refine_mod.refine(
                    config,
                    api_key=api_key,
                    db_path=db,
                    since=since_ts,
                    until=until_ts,
                    limit=args.limit,
                    redo=args.redo,
                    batch_size=args.batch,
                    context=args.context,
                    budget_tokens=args.budget,
                    # --model/--base-url 不给时是 None，而 None 会绕过 refine() 的默认值，
                    # 所以这里按「命令行 > 文件 > 内置默认」兜底
                    model=args.model or llm_settings.model,
                    base_url=args.base_url or llm_settings.base_url,
                    enable_thinking=(
                        True if args.think else llm_settings.enable_thinking
                    ),
                    prompt_ver=args.prompt_ver,
                    dry_run=args.dry_run,
                    on_progress=logs.emit,
                )
            except ValueError as exc:
                sys.exit(f"[参数错误] {exc}")

            logs.emit("-" * 60)
            # ⚠️ 三个数必须**自洽**：本地筛掉 + 送模型 == 扫描。
            # ⚠️ **不许用「其中」**连接「规则硬丢弃」与「本地筛掉」——两者**不是**
            # 包含关系：`prefilter.expand_context` 取 ±2 邻居时不看那条消息有没有
            # 被硬规则判死，所以被判死的消息照样可能作为上下文出网
            # （`digest.py:713-720` 的 docstring 记着这条，并且正是因此把"硬丢弃"
            # 计数从日报文案里删掉了）。旧版写「（其中硬丢弃 N 条）」——实测在真库上
            # 是假话。这里改用「；」并列，两个数各自独立。
            #
            # dry-run 时要报**计划**批数：实际批数必然是 0，显示「0 批」会让人
            # 以为什么都没准备好（Task 7 fix 实测指出）。
            shown_batches = stats.batches_planned if args.dry_run else stats.batches
            logs.emit(
                f"完成：扫描 {stats.scanned:,} 条 → 本地筛掉 "
                f"{stats.scanned - stats.sent_messages:,} 条"
                f" → 送模型 {stats.sent_messages:,} 条（{shown_batches:,} 批）"
                f"（候选 {stats.candidates:,} 条；规则硬丢弃 {stats.discarded_local:,} 条）"
            )
            logs.emit(f"产出条目：{stats.items_saved:,} 条")
            if not args.dry_run:
                logs.emit(f"token 用量：输入 {stats.input_tokens:,} / 输出 {stats.output_tokens:,}")
            if stats.budget_hit:
                # 报「实际跑了多少」而不是计划数——预算 break 后两者会差很多
                logs.emit(
                    f"[注意] 达到预算上限提前停止：实际跑了 {stats.batches} 批，"
                    f"计划 {stats.batches_planned} 批。剩余消息留待下次（或调大 --budget）"
                )
            if stats.deadlines_dropped:
                # 降级必须看得见——静默丢掉一个日期和静默编造一个日期同样有害
                logs.emit(
                    f"[注意] {stats.deadlines_dropped} 条截止日在源消息里找不到字面依据，"
                    f"未落库（条目本身仍照常出现）"
                )
            if stats.deadlines_backdated:
                # ⚠️ **与上一条分开报**（M6 T10 / M5 遗留 #4）：上一条说明模型在编，
                # 这一条说明**提示词或数据源有问题**（为什么会有三个月前的死线
                # 被当成新的？）。合成一句会让两种病都说不清——而在此之前它
                # **根本没有计数器**，一批 12 条过期死线被全清是零告警的。
                logs.emit(
                    f"[注意] {stats.deadlines_backdated} 条截止日**早于源消息时间**，"
                    "已降级为无死线（死线比消息还早 = 一条已过期的「⏰ 别忘」）"
                )
            if stats.errors:
                logs.emit(f"\n[失败] {len(stats.errors)} 批出错：")
                for e in stats.errors[:5]:
                    logs.emit(f"  {e}")
            if args.dry_run:
                logs.emit("（--dry-run：未调用模型、未写库）")
                return 0
            # ⚠️ 非零退出码是 M4 自动化的报警信号——别吞掉。旧版无条件 return 0，
            # 于是「3 批出错」在任务计划看来是成功。
            return 1 if stats.errors else 0
    except lock.AlreadyRunning as exc:
        # 与 `cmd_daily` 同一语义：2 不是 1——「上一次还没跑完」等着就行，
        # 「这次跑失败了」才要人来看（计划 §三 3.4）。
        logs.emit(f"[跳过] {exc}")
        return 2


def cmd_repass(args) -> int:
    """存量重判：让**已有**条目跟上当前提示词与两道要素闸门（M5 D10）。

    ⚠️ 默认**只出清单、不写库**（`--apply` 才写）。存量重判不可逆：删掉的
    item 没有回收站，而被 `digest_items` 引用的那些一旦删掉，日报正文里
    那句话还印着、链接却指向一个不存在的条目。

    ⚠️ 写库必须持锁（与 refine / digest / `deadline-audit --apply` 同一套契约，
    rc=2 = 已有实例在跑）；**只读清单不上锁**——理由与 `deadline-audit` 的
    dry-run 一字不差：给只读检查上锁会凭空造出一个**假拒绝**。

    ⚠️ 退出码：**0 / 1 / 2**。有批次失败就是 1：失败的那批消息这一轮**没有
    取得判定**（勘误 E5），于是清单是**不完整**的——这件事需要人来看，
    不许报成功（M4 的教训：无人值守下退出码是唯一还活着的信号）。
    """

    config = _load_config_only()
    db = _require_export_db(config)

    # ⚠️ 设置的**解析顺序**固定是：命令行 > config/llm.toml > 内置默认。
    # 命令行优先是为了让排障能"先试一下别的端点"而不用改文件。
    # ⚠️ 接住 `ConfigError`（M6 T3 re-review Important-3）：`main()` 没有顶层
    # 兜底，不接就是一段裸 traceback——见 `cmd_refine` 里那段的完整论证。
    try:
        llm_settings = load_llm_settings(on_warn=logs.emit)
    except ConfigError as exc:
        sys.exit(f"[配置错误] {exc}")

    # ⚠️ 密钥闸**必须在这个 try 之后**：写错 `config/llm.toml` 时冒出来的
    # `ConfigError` 才有人接（与 `cmd_refine` / `cmd_digest` 同一处置）。
    #
    # ⚠️ 这里是本命令**唯一**一处密钥闸。旧版在 try 之前还抄了一份
    # （`load_llm_settings` 紧接着又调一次），两份的分工只差一个干跑字段，
    # 而**前那份让 `ConfigError` 从 `main()` 裸奔出去**——配置写错时用户
    # 拿到的是一段 traceback，恰恰是这个 try 存在的理由。
    #
    # ⚠️ **没有干跑豁免**：本命令的默认路径（不给 `--apply`）是"只出清单、
    # 不写库"，可它**照样要调模型**——清单是模型判出来的，不是 SELECT 出来的。
    # 所以 `--dry-run` 这个概念在这里不存在（解析器里没有这个 flag，
    # 写成 `args.dry_run` 会 AttributeError）。要免掉模型调用请用 `--subset`。
    #
    # ⚠️ `auth = "none"`（本地 Ollama）时**不要密钥**：判据走
    # `requires_llm_key()`，免得四处各写一遍而漂移。
    api_key = load_llm_key(llm_settings)
    if requires_llm_key(llm_settings) and not api_key:
        sys.exit(
            "[配置错误] 没有 LLM 密钥。\n"
            f"  请在仓库根目录的 .env 里写入：{llm_settings.api_key_env}=...\n"
            '  （--dry-run 不需要密钥；本地 Ollama 请在 config/llm.toml '
            '写 auth = "none"）'
        )

    # ⚠️ **每次跑都落一份 audit**（诊断结论）：上一轮干跑没留 payload，174 条删除
    # 事后一条都复核不了。不可逆操作的事前可见（清单）与事后可复核（payload）
    # 是同一个需求的两半——不给 `--audit` 也照样写，只是路径带时间戳。
    audit = args.audit
    if not audit:
        stamp = dt.datetime.now().strftime("%Y%m%d-%H%M%S")
        audit = str(REPO_ROOT / "data" / f"repass-audit-{stamp}.jsonl")

    # --model/--base-url 不给时是 None，而 None 会绕过 repass() 的默认值，
    # 所以这里按「命令行 > 文件 > 内置默认」兜底
    common = dict(
        api_key=api_key,
        db_path=db,
        batch_size=args.batch,
        context=args.context,
        budget_tokens=args.budget,
        subset=args.subset,
        model=args.model or llm_settings.model,
        base_url=args.base_url or llm_settings.base_url,
        enable_thinking=(True if args.think else llm_settings.enable_thinking),
        audit_path=audit,
        on_progress=logs.emit,
    )
    print(f"[repass] 审计文件：{audit}")

    if not args.apply:
        plan, stats = repass_mod.repass(config, apply=False, **common)
        _print_repass_plan(plan, stats)
        return 1 if stats.errors else 0

    try:
        with lock.SingleInstance():
            plan, stats = repass_mod.repass(config, apply=True, **common)
    except lock.AlreadyRunning as exc:
        logs.emit(f"[跳过] {exc}")
        return 2
    _print_repass_plan(plan, stats)
    return 1 if stats.errors else 0


def _print_repass_plan(plan, stats) -> None:
    """把清单打出来——**这是本命令存在的意义之一**：不可逆操作事前可见。

    ⚠️ 三只「不删」的桶都要单独报出来，一个都不许并进「保留」里：
    * `skipped_referenced`——被日报引用，删了会让归档悬空
    * `skipped_soft_deleted`——用户自己软删过（D15：行仍在）
    * `unjudged`——这一轮**没判成**的（勘误 E5），清单因此不完整
    把它们混成一句「保留 N 条」，读的人就分不清「判过了、该留」与
    「根本没判」——而后者意味着**这次重判的结果不能全信**。
    """
    print(
        f"[repass] 重判源消息 {stats.scanned_msgs} 条 → 复现 refine 上下文"
        f" {stats.sent_batches} 批 / 送 {stats.sent_messages} 条消息；"
        f"成功 {stats.batches} 批（失败 {stats.failed_batches} 批），"
        f"token {stats.total_tokens:,}"
    )
    # ⚠️ 三种「没产出」必须分开报（诊断结论：含义完全不同）：
    #   a0 = 整批返回空（模型认为这批没什么可抽）
    #   a2 = 模型明明产出了东西，但没为这条产出
    #   丢弃 = 模型产出了、摘录也命中了，却被 `_to_item` 闸门丢（**我们的**闸门）
    # 上一轮把三者混成一句「没产出」，于是「0 批失败」读起来像「一切正常」。
    print(
        f"  判据质量：模型输出 {stats.items_raw} 条 → 落地 {stats.items_landed} 条；"
        f"摘录没匹配上丢 {stats.dropped_unmatched} 条；"
        f"被闸门丢 {stats.dropped_by_gate} 条"
    )
    print(
        f"  没产出：整批返回空(a0) {stats.empty_payloads} 批；"
        f"没为这条产出(a2) {stats.msgs_no_output} 条消息"
    )
    print(f"  将删除 {len(plan.to_delete)} 条：")
    for iid in plan.to_delete:
        print(f"    - item {iid}")
    print(f"  保留 {len(plan.to_keep)} 条")
    if plan.deaths:
        from collections import Counter as _C

        dist = _C(plan.deaths.values())
        print(
            f"  将删的死法拆分：a2 {dist.get('a2', 0)} 条（有批内对照）/ "
            f"a0 {dist.get('a0', 0)} 条（整批返回空）/ quota {dist.get('quota', 0)} 条"
        )
    if plan.deferred:
        print(
            f"  ⚠️ **本轮不动** {len(plan.deferred)} 条（被 --subset 挡下，"
            f"它们仍在清单里）："
        )
        for iid in plan.deferred:
            print(f"    - item {iid}")
    if plan.downgrades:
        # ⚠️ 降级本轮**不执行**：overlay 不支持 set_field，而改主库会打破
        # 「vigil.db 一字未改」。必须显式报出来，否则读清单的人以为它做了。
        print(
            f"  ⚠️ 字段降级 {len(plan.downgrades)} 条**本轮不执行**"
            f"（overlay 暂无 set_field；改主库会破坏「vigil.db 一字未改」）"
        )
    if stats.tombstones:
        print(f"  已落墓碑 {stats.tombstones} 行（= 被软删条目的每条源消息）")
    if plan.skipped_referenced:
        print(
            f"  ⚠️ 跳过 {len(plan.skipped_referenced)} 条"
            f"（被日报引用，删了会让归档悬空）："
        )
        for iid in plan.skipped_referenced:
            print(f"    - item {iid}")
    if plan.skipped_soft_deleted:
        print(
            f"  ⚠️ 跳过 {len(plan.skipped_soft_deleted)} 条"
            f"（用户软删过，行要留着才撤得回）："
        )
        for iid in plan.skipped_soft_deleted:
            print(f"    - item {iid}")
    if plan.unjudged:
        print(
            f"  ⚠️ {len(plan.unjudged)} 条**没判成**（本轮清单不完整，"
            f"它们的条目一条都没动）："
        )
        for iid in plan.unjudged:
            print(f"    - item {iid}")
    if stats.errors:
        for e in stats.errors:
            print(f"  [错误] {e}", file=sys.stderr)


def cmd_digest(args) -> int:
    """日报：把 items 写成一天一页 Markdown。"""
    # ⚠️ **人工跑也要上锁**（M4 独立审查 F4）：`docs/SETUP-自动化.md` §七 给退出码 2
    # 写的理由就是「用户手动又跑一次」这个场景，而此前只有 `cmd_daily` 上锁 ⇒
    # 人工命令与任务计划撞车时**完全不受保护**——spec §4.8 的「幂等可重入」
    # 只堵了一半（两个 refine 并发会各读同一批待处理消息 ⇒ 抽两遍、烧两份 token）。
    # ⚠️ 不会与 `cmd_daily` 重入：`daily.run()` 调的是库函数（`ingest_mod.ingest`
    # 等）而不是 `cmd_*`，而锁**刻意不做重入**（见 `lock.py` 的 docstring）。
    try:
        with lock.SingleInstance():
            from . import digest as digest_mod

            config = _load_config_only()
            db = _require_export_db(config)

            # ⚠️ 设置的**解析顺序**固定是：命令行 > config/llm.toml > 内置默认。
            # 命令行优先是为了让排障能"先试一下别的端点"而不用改文件。
            # ⚠️ 接住 `ConfigError`（M6 T3 re-review Important-3）：`main()` 没有顶层
            # 兜底，不接就是一段裸 traceback——见 `cmd_refine` 里那段的完整论证。
            try:
                llm_settings = load_llm_settings(on_warn=logs.emit)
            except ConfigError as exc:
                sys.exit(f"[配置错误] {exc}")

            # ⚠️ 密钥闸**必须在上面那个 try 之后**：写错 `config/llm.toml` 时
            # 冒出来的 `ConfigError` 才有人接。放在它前面的话，用户拿到的
            # 是一段裸 traceback 而不是一句 `[配置错误]` —— 而那正是
            # 这个 try 存在的理由（见它上面那段注释）。
            #
            # ⚠️ `auth = "none"`（本地 Ollama）时**不要密钥**：判据走
            # `requires_llm_key()`，免得四处各写一遍而漂移。
            api_key = load_llm_key(llm_settings)
            if requires_llm_key(llm_settings) and not api_key and not args.dry_run:
                sys.exit(
                    "[配置错误] 没有 LLM 密钥。\n"
                    f"  请在仓库根目录的 .env 里写入：{llm_settings.api_key_env}=...\n"
                    '  （--dry-run 不需要密钥；本地 Ollama 请在 config/llm.toml '
                    '写 auth = "none"）'
                )

            day = args.date or digest_mod.yesterday()
            try:
                since, until = digest_mod.day_window(day)
            except ValueError as exc:
                sys.exit(f"[参数错误] {exc}")

            stats = digest_mod.digest(
                config,
                api_key=api_key,
                db_path=db,
                since=since,
                until=until,
                day_label=day,
                # --model/--base-url 不给时是 None，而 None 会绕过 digest() 的默认值，
                # 所以这里按「命令行 > 文件 > 内置默认」兜底
                model=args.model or llm_settings.model,
                base_url=args.base_url or llm_settings.base_url,
                enable_thinking=(True if args.think else llm_settings.enable_thinking),
                write_file=not args.no_write,
                dry_run=args.dry_run,
                on_progress=logs.emit,
            )

            logs.emit("-" * 60)
            # ⚠️ 失败**不再提前 return**：提前返回会让日志里只有一句「[失败]」，
            # 而没有本次的计数——事后根本判断不出这轮跑到哪、抓到几条。所以先报错，
            # 汇总照打，最后由这一处统一给退出码。
            for err in stats.errors:
                logs.emit(f"[失败] {err}")

            logs.emit(
                f"完成：{stats.day} 窗口内 {stats.groups} 个群 {stats.messages:,} 条消息"
                f" → {stats.items} 条 item → 日报 {stats.lines} 行"
            )
            if stats.mechanical:
                # 不为 0 就说明模型漏写了条目，是提示词该改的信号——必须显眼
                logs.emit(
                    f"[注意] 其中 {stats.mechanical} 行是程序补的（模型没写到），"
                    f"读起来会生硬——这通常意味着提示词该调了"
                )
            if stats.deadlines_dropped:
                # 静默丢弃截止日是另一种失败——降级必须看得见
                logs.emit(
                    f"[注意] {stats.deadlines_dropped} 条截止日在源消息里找不到字面依据，"
                    f"未进「别忘」（条目本身仍照常出现）——已保留 {stats.deadlines_kept} 条"
                )
            if stats.unmatched_quotes:
                logs.emit(f"[注意] 有 {stats.unmatched_quotes} 处摘录没匹配上条目，已丢弃")
            if args.dry_run:
                logs.emit("（--dry-run：未调用模型、未写库、未落文件）")
                return 0

            logs.emit(f"token 用量：输入 {stats.input_tokens:,} / 输出 {stats.output_tokens:,}")
            if stats.output_path:
                logs.emit(f"日报文件：{stats.output_path}")
            else:
                logs.emit("（--no-write：只入库，未落文件）")
            # ⚠️ 非零退出码是 M4 自动化的报警信号——别吞掉
            return 1 if stats.errors else 0
    except lock.AlreadyRunning as exc:
        # 与 `cmd_daily` 同一语义：2 不是 1——「上一次还没跑完」等着就行，
        # 「这次跑失败了」才要人来看（计划 §三 3.4）。
        logs.emit(f"[跳过] {exc}")
        return 2


def cmd_daily(args) -> int:
    """每日管线。任务计划的入口——见 docs/SETUP-自动化.md。

    ⚠️ **加锁是在这里、不是在 daily.run() 里**：锁是**进程级**关注点，
    而 run() 只做编排。分开也让 run() 能在测试里不碰文件系统。

    ⚠️ 退出码契约（计划 §三 3.4，冻结）：0 成功 / 1 失败或不完整 /
    **2 = 已有实例在跑**。2 不并进 1：无人值守下「上一次还没跑完」（等就行）
    与「这次跑失败了」（要人来看）需要两种不同处置。

    ⚠️ 这里的 `except Exception` 是**顶层兜底**，必须留痕并返回非零。
    不许写成 `except Exception: logs.emit(...); return 0`——那正好是本
    milestone 要消灭的形状（`exit 0 但日志有洞`，见计划 §8.5）。
    外面也不许套"吞掉并继续"的 try/except：真有异常冒上来（如跨零点换文件
    失败，T1 把它钉成必抛），它**一路往外走**才对。

    ⚠️ 但别以为这一层能兜住"日志写失败"：普通写失败（磁盘满）被
    `logging.StreamHandler.emit` 自带的 `try/except` 吞掉、只调 `handleError`
    往 stderr 打一份，而任务计划**丢弃 stderr** ⇒ 它**到不了这里**。
    那条路径归 `logs.py` 的 `handleError`（T1 的文件）。
    """
    from . import daily as daily_mod

    logs.setup()
    # 开跑就清 LAST-ERROR：于是「文件存在」⟺「本次跑失败过」（见 logs 的 docstring）
    logs.clear_last_error()
    logs.prune_old_logs()

    try:
        config = load_config()
    except ConfigError as exc:
        logs.emit(f"[配置错误] {exc}")
        logs.write_last_error(f"配置错误：{exc}")
        return 1
    llm_key = load_llm_key()
    if not llm_key:
        msg = "没有 LLM 密钥（.env 里的 SILICONFLOW_API_KEY）"
        logs.emit(f"[配置错误] {msg}")
        logs.write_last_error(msg)
        return 1

    try:
        with lock.SingleInstance():
            report = daily_mod.run(
                config=config, llm_key=llm_key, day=args.date,
            )
    except lock.AlreadyRunning as exc:
        # ⚠️ 退出码 2 不是 1：无人值守下「上一次还没跑完」只要等着就行，
        # 「这次跑失败了」才要人来看。详见计划 §三 3.4。
        logs.emit(f"[跳过] {exc}")
        return 2


    except Exception as exc:  # noqa: BLE001 — 顶层兜底，必须留痕
        logs.emit(f"[失败] daily 未预期地抛出：{exc!r}")
        logs.write_last_error(f"daily 未预期异常：{exc!r}")
        return 1

    if not report.ok:
        logs.write_last_error(report.last_error)
        return 1
    return 0


def cmd_deadline_audit(args) -> int:
    """截止日核验：列出源文里**找不到字面依据**的 deadline_ts。--apply 才写库。"""
    from . import deadline as deadline_mod

    config = _load_config_only()
    db = _require_export_db(config)

    # ⚠️ `uri=True` 是 Task 5 加的（裁决 R4），本处是**一致性**改动而非阻断修复：
    # 本命令只调 `items_with_deadline` / `item_sources_text`，两者都**没有**
    # `_ensure_overlay`，所以今天它还走不到 `ATTACH`（真被阻断的是 `digest.py` /
    # `refine.py` 那两条连接——它们的查询入口会 attach）。补这里是为了不让
    # "第 4 个没带标志的连接"成为后来者照抄的样板：查询层入口要
    # `ATTACH 'file:...?mode=ro'`，而 URI 形式只在连接带 `SQLITE_OPEN_URI` 时才被解析。
    # 路径不以 `file:` 开头时这个标志**不影响行为**（配置里的 db 路径不许以 `file:` 开头）。
    conn = sqlite3.connect(str(db), uri=True)
    try:
        rows = store.items_with_deadline(conn)
        if not rows:
            print("（库里没有带截止日的条目）")
            return 0
        sources = store.item_sources_text(conn, [r[0] for r in rows])
        bad = deadline_mod.unverified_item_ids(rows, sources)
        kept = [r for r in rows if r[0] not in bad]

        print(f"带截止日的条目：{len(rows)} 条")
        print(f"  源文里找得到依据：{len(kept)} 条")
        print(f"  找不到依据（应清掉）：{len(bad)} 条")
        for item_id, ts in rows:
            if item_id in bad:
                title = conn.execute(
                    # ⚠️ 这里**刻意不接 overlay**：`deadline-audit` 是对**底层数据**的
                    # 核验（"这条 item 的截止日在源文里有没有依据"），不是视图。
                    # 它就该看真实的 items——包括已软删的那些。
                    # 这是 spec §3.3 那张"5 处读路径"表里唯一一处「不需要 apply」的。
                    "SELECT title FROM items WHERE item_id = ?", (item_id,)
                ).fetchone()[0]
                print(
                    f"    item {item_id}: 标着 "
                    f"{dt.datetime.fromtimestamp(ts):%Y-%m-%d} · {title[:24]}"
                )
        if not bad:
            print("无需改动。")
            return 0
        if not args.apply:
            print("\n（--dry-run：未写库。确认无误后加 --apply）")
            return 0

        # ⚠️ **这是第四个写库命令，也要上锁**（M4 终审 U-3/M-1）：F4 的裁定是
        # 「人工命令也要上锁」（R71），但实现的是**按名字枚举**的三个
        # （export/refine/digest）⇒ 终审实测：锁被持有时 `export` 退 2（被保护），
        # 而 `deadline-audit --apply` 退 **0** 且 `data/vigil.db` **仍被改**。
        # 拿不到锁 ⇒ 退 2 + 一句「[跳过]」，与那三个同一语义：等着就行，不是失败。
        #
        # ⚠️ **只有 `--apply` 上锁，dry-run 不上锁**（判断与理由）：
        # `lock.SingleInstance` 的语义是「同一时刻只许一个**写库**的 vigil 进程」
        # （见 `lock.py` 的模块 docstring），而默认那条路只有 SELECT + print——
        # 它与 `read`/`who`/`media`/`groups` 是同一族的**人工即时查询**
        # （`tests/test_cli.py::test_query_commands_do_not_write_to_the_log`
        # 正是这么归类的）。给只读的检查上锁会凭空造出一个**假拒绝**（rc=2），
        # 而 2 的约定含义是「已经有实例在写」。
        #
        # ⚠️ 代价（知情取舍）：审计的那些 SELECT 已经跑完，结果与被写的这一刻之间
        # 隔了一小段。这是安全的——`clear_deadlines` 只把 `deadline_ts` 置 NULL
        # （只影响传入的行、可重复执行），而并发的 refine 只**新增** items，
        # 不改任何已有行的这一列。
        try:
            with lock.SingleInstance():
                changed = store.clear_deadlines(conn, sorted(bad))
        except lock.AlreadyRunning as exc:
            # 与其它三个写库命令同一行话术（`logs.emit` 先 print 后写日志）
            logs.emit(f"[跳过] {exc}")
            return 2
        print(f"\n已把 {changed} 条的 deadline_ts 置 NULL（其余字段未动）。")
        return 0
    finally:
        conn.close()


def cmd_downgrade(args) -> int:
    """字段降级：把**不再过当前闸门**的 `place` / `deadline_ts` 置 NULL。

    ⚠️ 存在的理由（M5 终审 Important-3）：`store.downgrade_item_fields` 全仓**零
    调用方**，于是 M5 出口判据 3（「不再存在 `deadline_ts < event_ts` 的条目」）
    没有任何所有者——真库 item 83 一直是「event 2026-08-05 / deadline 2026-05-06」，
    `ItemCard` 在 8 月的条目上渲染「⏰ 05-06 截止」。这条命令就是它的调用方。

    ⚠️ **为什么是独立子命令、不是 `repass --downgrade-only`**：`repass` 是**LLM 重判**
    （要 api key、烧 token，覆盖范围是「重判后仍保留下来的条目」`plan.to_keep`），
    而字段降级**不需要模型**——判据是纯机械的（`place_supported` / `deadline_sane`），
    范围是全部条目。并成一个命令会让两份「降级清单」看起来是同一份，
    而它们不是（那份只覆盖 `to_keep`）——那正是本项目最忌的「看起来像同一个数」。

    ⚠️ 默认**只出清单、不写库**（`--apply` 才写），与 `repass` / `deadline-audit`
    一字不差的同一条惯例：不可逆操作事前可见。

    ⚠️ 写库必须持锁（rc=2 = 已有实例在跑，等着就行）；只读清单不上锁——
    给只读检查上锁会凭空造出一个**假拒绝**（与 `deadline-audit` 同）。

    ⚠️ **`--fields` 的默认值是 `deadline`，不是 `both`**（M5 收尾 scoped
    re-review A1 实测的**破坏性默认值**）。修复前默认 `both`：
    将来有人只是照着 `--apply` 这个惯例敲一遍、**不带 flag**，
    就会**连带把 7 条 place 也置 NULL**——而用户 2026-09-17 只批准了 deadline
    那一次。真库操作不可逆，这种「顺手多清一列」没有任何一步会报错。
    ⇒ 判据是：**不带 `--fields` 跑 `--apply` 不可能意外清掉 place。** 两条守卫钉着
    （`tests/test_cli.py`）：① 解析层默认值逐字 == `deadline`；
    ② 行为层——造一条「两条闸门都过不了」的条目，不带 flag 跑 `--apply`，
    place **一个字不动**，再用 `--fields both` 做阳性对照证明那条目确实可降级。
    ⚠️ 这一改**只动默认值**，`choices` 与 `both` 的语义一字未动：
    要清 place 就得显式写出来——不可逆的范围必须有人**说出来**，不能由默认值代办。
    """
    config = _load_config_only()
    db = _require_export_db(config)

    # `--fields both|place|deadline` ⇒ 字段名集合。写成映射而不是把字符串
    # 直接传下去：`'/'.join("place")` 会拼成 `p/l/a/c/e`，而把字符串当
    # 集合用时 `"place" in wanted` 恰好也能对上——两种写法里只有一种是对的，
    # 所以这里显式列出。
    fields = {
        "both": repass_mod.DOWNGRADE_FIELDS,
        "place": ("place",),
        "deadline": ("deadline",),
    }[args.fields]

    # ⚠️ `uri=True`：本命令要问 overlay（跳过软删条目），而查询层入口会
    # `ATTACH 'file:...?mode=ro'`——URI 形式只在连接带 SQLITE_OPEN_URI 时被解析（裁决 R4）。
    conn = sqlite3.connect(str(db), uri=True)
    try:
        all_ids = [
            int(r[0]) for r in conn.execute("SELECT item_id FROM items ORDER BY item_id")
        ]
        # ⚠️ 软删的条目**不碰**（`_plan_downgrades` 的同一条裁定）：那是用户自己
        # 隐藏的行，替他改字段同样是"替他改了他已经决定过的东西"。
        soft = store.soft_deleted_item_ids(conn, all_ids)
        scope = [i for i in all_ids if i not in soft]
        updates = repass_mod.plan_field_downgrades(conn, scope, fields=fields)
        # ⚠️ 被跳过的那批也要单独算一遍并报出来：出口判据的 SQL
        # （`SELECT count(*) FROM items WHERE deadline_ts < event_ts`）数的是
        # **全部** items，含软删——不报的话，「复核：剩 N 条」会与判据对不上，
        # 而读的人只会看到两个互相矛盾的数字（R13 同族）。
        skipped = repass_mod.plan_field_downgrades(conn, sorted(soft), fields=fields)

        print(
            f"[downgrade] 扫描 {len(all_ids)} 条（其中被软删、跳过 {len(soft)} 条）；"
            f"降级字段：{'/'.join(fields)}"
        )
        if skipped:
            print(
                f"  ⚠️ 另有 {len(skipped)} 条**被软删**的条目也过不了闸门，"
                "本轮不动（用户自己隐藏的行，不替他改）："
            )
            for item_id, _, _ in skipped:
                print(f"    - item {item_id}")
            print(
                "  ⚠️ 上面这句与判据 SQL 的口径不同：判据数的是全部 items（含软删）。"
                "要让那个数归零，得先撤销软删，或显式裁决「软删条目要不要一起降级」。"
            )
        if not updates:
            print("无需改动。")
            return 0

        rows = {
            int(r[0]): r
            for r in conn.execute(
                "SELECT item_id, place, deadline_ts, event_ts, title FROM items WHERE item_id IN"
                f" ({','.join('?' * len(updates))})",
                [u[0] for u in updates],
            )
        }
        print(f"将降级 {len(updates)} 条：")
        for item_id, new_place, new_dl in updates:
            _, place, deadline_ts, event_ts, title = rows[item_id]
            head = f"    - item {item_id} 「{title[:20]}」"
            if new_dl != deadline_ts:
                reason = (
                    "倒挂：早于 event 那天"
                    if deadline_ts is not None and event_ts is not None
                    else "无依据"
                )
                when = (
                    dt.datetime.fromtimestamp(deadline_ts).strftime("%Y-%m-%d")
                    if deadline_ts
                    else "NULL"
                )
                print(f"{head} deadline_ts {when} → NULL（{reason}）")
            if new_place != place:
                print(f"{head} place {place!r} → NULL（源文里找不到依据）")
        if not args.apply:
            print("\n（--dry-run：未写库。确认无误后加 --apply）")
            return 0

        try:
            with lock.SingleInstance():
                with store.transaction(conn):
                    n = store.downgrade_item_fields(conn, updates)
        except lock.AlreadyRunning as exc:
            logs.emit(f"[跳过] {exc}")
            return 2
        print(f"\n已降级 {n} 条（只改 place/deadline_ts 两列，其余字段未动）。")
        left = conn.execute(
            "SELECT COUNT(*) FROM items"
            " WHERE deadline_ts IS NOT NULL AND deadline_ts < event_ts"
        ).fetchone()[0]
        print(f"复核：deadline_ts < event_ts 的条目剩 {left} 条。")
        return 0
    finally:
        conn.close()


def cmd_serve(args) -> int:
    """起本地只读 Web 服务。手机访问见 spec §4.7（tailscale serve 代理本端口）。"""
    import uvicorn  # 延迟导入：不跑服务的人不该为它付启动成本

    from .api import WEB_DIST, create_app

    config = _load_config_only()
    _require_export_db(config)

    if not (WEB_DIST / "index.html").is_file():
        # 不许静默起一个空白页：明说前端没构建，并给出构建命令。
        # ⚠️ flush=True：stdout 重定向到文件时 print 是块缓冲的（实测日志 0 字节），
        # 而这句正是「不许静默」的载体——被缓冲就等于没说过。
        print(
            f"[注意] 前端还没构建（找不到 {WEB_DIST / 'index.html'}）。\n"
            f"  服务仍会启动，但页面只会显示一行提示。构建：\n"
            f"      cd web && npm install && npm run build",
            flush=True,
        )

    app = create_app(config)
    print(f"VIGIL 服务：http://{args.host}:{args.port}", flush=True)
    if args.host == "127.0.0.1":
        print(
            "  （只监听本机——手机访问见 spec §4.7：tailscale serve 代理本端口）",
            flush=True,
        )
    uvicorn.run(app, host=args.host, port=args.port, log_level="warning")
    return 0


def _config_pack_paths() -> "configpack.Paths":
    """配置包的三个落点。**每次现读模块属性**，不在 import 期冻住。

    ⚠️ 三个名字各有一个具体的坏法（都与 `api.persons_path()` 那条判据同族）：
      · `config.DEFAULT_PERSONS` —— 若在这里写成 `from .config import
        DEFAULT_PERSONS`，它在 import 期就绑死了，测试里
        `monkeypatch.setattr(config, "DEFAULT_PERSONS", ...)` 够不着
        ⇒ `vigil config import` 会**整体覆盖真实的 `config/persons.toml`**
        （用户的真实名单，不在 `data/` 里、没有备份），而测试全绿。
      · `categories.DEFAULT_CATEGORIES` —— 同族，`tests/conftest.py` 的
        autouse 夹具正是靠 `setattr` 挪它的。
      · `REPO_ROOT` —— 它是**本模块**的一个全局名字（import 期绑定），
        测试挪它要 `monkeypatch.setattr(cli, "REPO_ROOT", ...)`；这里照旧读它。
    """
    from . import configpack

    return configpack.Paths(
        groups=REPO_ROOT / "config" / "groups.toml",
        categories=categories_mod.DEFAULT_CATEGORIES,
        persons=config_mod.DEFAULT_PERSONS,
    )


def _read_kind_aliases() -> dict[str, str]:
    """读 slug 改名表。**库文件不在 ⇒ 空表**；表不在/读不出来 ⇒ 补表或响亮。

    ⚠️ 三种形状都**实测过**（M6 T7 冒烟，真机上踩到的是第二种）：
      · **库文件不在** ⇒ 返回空表。与 `overrides.attach_readonly` 同一条判据：
        库不存在时「没有任何干预」是一个**确定的语义**，不是猜测。
      · **文件在、表不在** —— ⚠️ 真机上就是这个形状：`data/overrides.db` 是 M5
        建的，里面只有 `item_edits` / `item_state`，而 `kind_alias` 是 T5 新增的、
        要等某次 `ensure_schema()` 跑过才在。直接 `SELECT … FROM kind_alias`
        会抛 `no such table`，**而 `vigil config export` 的本职与 overlay 毫无关系**
        ⇒ 一条只读导出命令以裸 traceback 收场（冒烟实测）。
        ⇒ 补一句幂等的 `ensure_schema()`（`attach_readonly` 建库那一支做的正是
        同一件事：overlay 本来就在可写范围内，D11 管的是 `data/vigil.db`）。
      · **其他 sqlite 故障一律不吞**（库坏了 / 盘满了）：`ensure_schema` 或 SELECT
        照实抛。**绝不降级成空表**——那会导出一个**缺了改名映射**的包，
        而导入方的条目会全部悬空（spec §4.2 ④），是一句"导出成功了"的假话。
        （与 `attach_readonly` 的立场一致：文件**存在**时挂不上就报错，不降级。）
    """
    target = overrides.OVERRIDES_DB
    if not target.is_file():
        return {}
    overrides.ensure_schema(target)
    conn = overrides.connect(target)
    try:
        return overrides.kind_aliases(conn)
    finally:
        conn.close()


def cmd_config_export(args) -> int:
    """导出一个自包含的配置包（**不含密钥**，spec §4.2 ①）。"""
    from . import configpack

    config = _load_config_only()
    cats = load_categories()
    persons = load_persons()
    aliases = _read_kind_aliases()

    out = pathlib.Path(args.out) if args.out else (
        config.output_db.parent / f"vigil-config-{dt.date.today().isoformat()}.toml"
    )
    configpack.write_pack(out, config=config, cats=cats, persons=persons, aliases=aliases)
    logs.emit(f"已导出配置包：{out}")
    logs.emit(
        f"  群 {len(config.groups)} / 类目 {len(cats)}"
        f" / 人物 {len(persons)} / 改名 {len(aliases)}"
    )
    logs.emit("  ⛔ 不含密钥，不含人工干预数据")
    return 0


def cmd_config_import(args) -> int:
    """导入配置包。⚠️ 默认**真的写**（`--dry-run` 只校验）。"""
    from . import configpack

    path = pathlib.Path(args.file)
    if not path.is_file():
        sys.exit(f"[参数错误] 找不到配置包：{path}")

    # ⚠️ 解码失败也要给一行可读的报错：选错文件（一份 .docx、一张图）是个
    # 极常见的操作，而 `read_text` 抛的 `UnicodeDecodeError` 继承自 **ValueError**，
    # 它既不进下面的 `except ConfigError`，用户看到的就是裸 traceback
    # ——与 `load_config` 把 `TOMLDecodeError` 包成 `ConfigError` 是同一条理由。
    try:
        text = path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError) as exc:
        sys.exit(f"[参数错误] 读不了 {path}：{exc}")

    try:
        pack = configpack.parse(text, path)
    except ConfigError as exc:
        sys.exit(f"[配置错误] {exc}")

    if args.dry_run:
        logs.emit(
            f"[dry-run] 包校验通过：群 {len(pack.config.groups) if pack.config else 0} / "
            f"类目 {len(pack.categories)} / 人物 {len(pack.persons)} / 改名 {len(pack.aliases)}"
        )
        return 0

    # ⚠️ **这是一条写命令**（一次写三份配置），所以要上锁——与 `export` /
    # `refine` / `digest` / `daily` / `repass --apply` 同一条规矩（M4 审 F4）。
    # ⚠️ 退出码 2 的语义与其它写命令一字不差：**「上一次还没跑完，等着就行」**
    # 与「这次跑失败了」是两回事——无人值守时后者才要人来看（计划 §三 3.4）。
    # ⚠️ 撞车时**一个字节都不许写**：先写再报 2 会留下半新半旧的配置。
    try:
        with lock.SingleInstance():
            report = configpack.import_pack(
                pack,
                keep_settings=args.keep_settings,
                paths=_config_pack_paths(),
                on_progress=logs.emit,
            )
    except ConfigError as exc:
        # ⚠️ **`import_pack` 自己也会抛 `ConfigError`**（Fix loop 2 / FIX 1a）：
        # 最典型的一条是**跨包别名冲突**（本次导入的映射与本机现有的合起来成环
        # ——而本机那条往往就是**上一次导入**写进去的）。
        # 从前这圈 `except` 只罩着 `parse`，于是它直接冒出 `cli.main`：
        # 用户看到一坨 traceback，任务计划只看见一个数字。
        # ⚠️ 它发生在**任何写入之前**（纯函数合并校验跑在最前面）。
        sys.exit(f"[配置错误] {exc}")
    except lock.AlreadyRunning as exc:
        logs.emit(f"[跳过] {exc}")
        return 2
    return 0 if (report.groups or report.categories) else 1


def main(argv: list[str] | None = None) -> int:
    # ⚠️ **必须在任何输出之前**（含 argparse 自己的 usage/error）——
    # 理由见 `_harden_streams` 的 docstring：不修的话中文 Windows 上
    # `vigil demo` 会崩在成功路径中途。
    logs.harden_streams()

    parser = argparse.ArgumentParser(prog="vigil", description="VIGIL 守夜人")
    sub = parser.add_subparsers(dest="command", required=True)

    p_groups = sub.add_parser("groups", help="列出数据源里的群")
    p_groups.add_argument(
        "--only-watched", action="store_true", help="只显示已纳入监听的群"
    )
    p_groups.set_defaults(func=cmd_groups)

    p_ingest = sub.add_parser("ingest", help="把纳入的群从数据源摄取进明文库")
    p_ingest.set_defaults(func=cmd_ingest)

    # `export` 是 `ingest` 的**旧名别名**（开源版把"导出 QQ 库"改成了
    # "从数据源摄取"，因为默认源已不是 QQ）。留这个别名是因为它出现在
    # 老用户的脚本、任务计划与文档里 —— 改名而不留别名等于让那些东西
    # 在升级当天静默失效。
    sub.choices["export"] = p_ingest

    p_sync = sub.add_parser(
        "sync",
        help="把结构化结果推到云 RDS（只推 items/digests，原文与姓名不出本机）",
    )
    p_sync.add_argument(
        "--dsn", default=None,
        help="RDS 连接串；不给则读环境变量 VIGIL_RDS_DSN",
    )
    p_sync.add_argument(
        "--dry-run", action="store_true",
        help="只数行数、不写 RDS（不需要真的库）",
    )
    p_sync.set_defaults(func=cmd_sync)

    p_sched = sub.add_parser(
        "schedule",
        help="排期：把有死线的条目排进课表之外的空闲块（**只读**，不写库）",
    )
    p_sched.add_argument(
        "--days", type=int, default=14,
        help="往后看几天（默认 14）",
    )
    p_sched.add_argument(
        "--timetable", default=None,
        help="课表文件路径（默认 config/timetable.toml）",
    )
    p_sched.add_argument(
        "--from-today", action="store_true",
        help="窗口从今天 00:00 起算（默认从此刻起 —— 已经过去的时段排不了）",
    )
    p_sched.add_argument(
        "--activity-min", type=int, default=90,
        help="有确定时刻的活动按多长占用时间（默认 90 分钟；抽取没给时长）",
    )
    p_sched.set_defaults(func=cmd_schedule)

    p_demo = sub.add_parser(
        "demo",
        help="离线一键：造合成语料 → 摄取（不联网、不需密钥）",
    )
    p_demo.add_argument(
        "--seed", type=int, default=None,
        help="语料随机种子（默认取 demo/corpus.toml 的 meta.seed）",
    )
    p_demo.add_argument(
        "--skip-refine", action="store_true",
        help="只造语料与摄取，不跑 LLM 抽取（无模型时可用来验证链路）",
    )
    p_demo.set_defaults(func=cmd_demo)

    p_who = sub.add_parser("who", help="谁在说：发言排行")
    p_who.add_argument("--group", type=int, help="限定群号")
    p_who.add_argument("--since", help="起始日期 YYYY-MM-DD")
    p_who.add_argument("--limit", type=int, default=20, help="显示人数")
    p_who.set_defaults(func=cmd_who)

    p_read = sub.add_parser("read", help="读消息（可按群/人/时间/关键词过滤）")
    p_read.add_argument("--group", type=int, help="限定群号")
    p_read.add_argument("--sender", help="限定发信人（姓名片段或完整 QQ 号）")
    p_read.add_argument("--keyword", help="正文关键词")
    p_read.add_argument("--since", help="起始日期 YYYY-MM-DD")
    p_read.add_argument("--until", help="结束日期 YYYY-MM-DD")
    p_read.add_argument("--limit", type=int, default=50, help="显示条数")
    p_read.set_defaults(func=cmd_read)

    p_media = sub.add_parser("media", help="本地可取的图片（已在磁盘上）")
    p_media.add_argument("--group", type=int, help="限定群号")
    p_media.add_argument("--since", help="起始日期 YYYY-MM-DD")
    p_media.add_argument("--limit", type=int, default=20, help="显示条数")
    p_media.set_defaults(func=cmd_media)

    p_refine = sub.add_parser("refine", help="抽取：把消息提炼成结构化条目")
    p_refine.add_argument("--since", help="起始日期 YYYY-MM-DD")
    p_refine.add_argument("--until", help="结束日期 YYYY-MM-DD")
    p_refine.add_argument("--limit", type=int, help="最多处理多少条")
    p_refine.add_argument("--redo", action="store_true", help="重抽已处理过的消息")
    p_refine.add_argument("--batch", type=int, default=30, help="每批消息数")
    p_refine.add_argument("--context", type=int, default=2, help="候选各带几条上下文")
    p_refine.add_argument("--budget", type=int, help="token 预算上限")
    p_refine.add_argument("--model", default=None, help="覆盖默认模型")
    p_refine.add_argument(
        "--base-url", default=None, help="覆盖 LLM 端点（默认读 config/llm.toml）"
    )
    p_refine.add_argument(
        "--think",
        action="store_true",
        help="打开模型的思考模式（默认关闭：实测 Qwen3.5-35B-A3B 开思考时"
        "单批要 111.5s / 11,124 输出 token，关掉只要 2.8s / 220 token）",
    )
    p_refine.add_argument(
        "--prompt-ver", default=refine_mod.PROMPT_VERSION,
        help="提示词版本号；改了提示词就换个号，便于区分与重跑",
    )
    p_refine.add_argument("--dry-run", action="store_true", help="只报告不调用模型")
    p_refine.set_defaults(func=cmd_refine)

    p_repass = sub.add_parser(
        "repass",
        help="存量重判：让已有条目的来源消息按当前提示词重跑一遍（只删不换）",
    )
    p_repass.add_argument(
        "--apply", action="store_true",
        help="真的写库；**不给就只出清单**——存量重判不可逆，默认先看清单",
    )
    p_repass.add_argument("--batch", type=int, default=30, help="每批消息数")
    p_repass.add_argument(
        "--context", type=int, default=2,
        help="复现 refine 时的上下文邻居数（默认 2，与 refine 一致）；"
        "**改小它就等于换条件重判**",
    )
    p_repass.add_argument("--budget", type=int, help="token 预算上限")
    p_repass.add_argument(
        "--subset", choices=("a2", "a0"), default=None,
        help="只执行某一类死法（a2 = 同批有产出、唯独没为这条产出，有批内对照；"
        "a0 = 整批返回空）。其余照常进清单但不执行",
    )
    p_repass.add_argument(
        "--audit", default=None,
        help="每批的模型原话与判定落成的 JSONL 路径"
        "（默认 data/repass-audit-<时间戳>.jsonl）——不可逆操作要事后可复核",
    )
    p_repass.add_argument("--model", default=None, help="覆盖默认模型")
    p_repass.add_argument(
        "--base-url", default=None, help="覆盖 LLM 端点（默认读 config/llm.toml）"
    )
    p_repass.add_argument("--think", action="store_true", help="打开模型思考模式")
    p_repass.set_defaults(func=cmd_repass)

    p_dl = sub.add_parser(
        "deadline-audit", help="截止日核验：源文里找不到依据的一律清掉"
    )
    p_dl.add_argument("--apply", action="store_true", help="真的写库（默认只报告）")
    p_dl.set_defaults(func=cmd_deadline_audit)

    p_dg = sub.add_parser(
        "downgrade",
        help="字段降级：把不再过当前闸门的 place/deadline_ts 置 NULL（默认只出清单）",
    )
    p_dg.add_argument("--apply", action="store_true", help="真的写库（默认只出清单）")
    p_dg.add_argument(
        "--fields", choices=("both", "place", "deadline"), default="deadline",
        help="降级哪些字段（**默认 deadline**）。⚠️ 最激进的范围 `both` 必须"
        "**显式**指定：默认值这样定，是为了让「不带 --fields 跑 --apply」"
        "**不可能**意外清掉 place（见 cmd_downgrade 的 docstring）。"
        "判据是数据层的两道闸门：place 要在源文里找得到依据；deadline 不早于 event 那天",
    )
    p_dg.set_defaults(func=cmd_downgrade)

    p_serve = sub.add_parser("serve", help="起本地 Web 服务（只读库）")
    p_serve.add_argument("--host", default="127.0.0.1", help="监听地址")
    p_serve.add_argument("--port", type=int, default=8787, help="端口")
    p_serve.set_defaults(func=cmd_serve)

    p_digest = sub.add_parser("digest", help="日报：把 items 合成一天一页 Markdown")
    p_digest.add_argument("--date", help="日报日期 YYYY-MM-DD（默认昨天）")
    p_digest.add_argument("--model", default=None, help="覆盖默认模型")
    p_digest.add_argument(
        "--base-url", default=None, help="覆盖 LLM 端点（默认读 config/llm.toml）"
    )
    p_digest.add_argument(
        "--think",
        action="store_true",
        help="打开模型的思考模式（默认关闭，与 refine 一致）",
    )
    p_digest.add_argument(
        "--no-write", action="store_true", help="只入库，不写 docs/digests/ 文件"
    )
    p_digest.add_argument("--dry-run", action="store_true", help="只报告不调用模型")
    p_digest.set_defaults(func=cmd_digest)

    p_daily = sub.add_parser(
        "daily", help="每日管线：export → refine → digest（任务计划入口）"
    )
    p_daily.add_argument("--date", help="日报日期 YYYY-MM-DD（默认昨天）")
    p_daily.set_defaults(func=cmd_daily)

    p_cfg = sub.add_parser("config", help="配置包：导入 / 导出")
    cfg_sub = p_cfg.add_subparsers(dest="config_action", required=True)

    p_cfg_x = cfg_sub.add_parser("export", help="把三份配置导出成一个 TOML 包")
    p_cfg_x.add_argument(
        "--out", default=None, help="输出路径（默认 data/vigil-config-<日期>.toml）"
    )
    p_cfg_x.set_defaults(func=cmd_config_export)

    p_cfg_i = cfg_sub.add_parser("import", help="从 TOML 包导入配置")
    p_cfg_i.add_argument("file", help="配置包路径")
    p_cfg_i.add_argument(
        "--keep-settings", action="store_true",
        help="保留本机的 source_dir / output_db（导入别人的包时几乎总要加）",
    )
    p_cfg_i.add_argument("--dry-run", action="store_true", help="只校验，不写")
    p_cfg_i.set_defaults(func=cmd_config_import)

    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
