"""模型选型的对照实验工具 —— 同一批输入喂给多个模型，比机械指标。

不是产品代码，是选型证据的生成器。设计上刻意做三件事：

1. **条件严格对齐**：所有模型看到**逐字节相同**的批次与提示词，
   只有 `model` 这一个字段不同。批次由生产管线的 prefilter/refine 真实生成，
   不是另写一套——否则量出来的不是产品会遇到的输入。
2. **不写生产库**：全程只读 data/vigil.db，产出落在内存和报告文件里。
   评估不该污染 items / refine_runs。
3. **判定归生产代码**：一条 item 收不收，由 `refine._to_item` 说了算
   （摘录匹配、类目合法性都是它的职责）。本工具的 `_drop_reason` 只做**归因**，
   不参与判定——两套判定逻辑必然漂移。

⚠️ 「匹配率」在本工具里的确切含义：模型返回的条目里，**逐字摘录能在原批次里
找到来源**的比例。它衡量的是「编造」而非「质量」——一个什么都不产出的模型
匹配率无意义（分母为 0），所以报告里对空产出显式标 N/A，绝不当成 100%。

跑法（仓库根）：
    PYTHONIOENCODING=utf-8 .venv/Scripts/python.exe vigil_eval.py --since 2026-09-12
    PYTHONIOENCODING=utf-8 .venv/Scripts/python.exe vigil_eval.py --since 2026-09-07 --models Qwen/Qwen3.5-27B,Qwen/Qwen3-32B
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import re
import sqlite3
import statistics
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
from pathlib import Path

from vigil import prefilter, reader, refine
from vigil.categories import load_categories
from vigil.config import load_config, load_llm_key
from vigil.llm import LLMConfig, LLMError, chat_json
from vigil.redact import Redactor
from vigil.store import PendingMessage

# 候选模型。价格为 ¥/M token，取 [0,128k) 输入档——本项目的批次在 1–5k token，
# 永远够不到 128k 台阶。数据来源：siliconflow.cn/pricing 表格（2026-09-14 实测抓取），
# Qwen3.5 系列为阶梯计价，报告里用的是低档。
CANDIDATES: dict[str, tuple[float, float]] = {
    "Qwen/Qwen3.5-35B-A3B": (0.40, 3.20),
    "Qwen/Qwen3.5-27B": (0.60, 4.80),
    "Qwen/Qwen3-32B": (1.00, 4.00),
    "Qwen/Qwen3-14B": (0.50, 2.00),
    # 上一轮的基准，放进同一张表才可比——历史上它的数据是在旧提示词下测的
    "Qwen/Qwen2.5-32B-Instruct": (1.26, 1.26),
    "Qwen/Qwen2.5-7B-Instruct": (0.0, 0.0),
    "Qwen/Qwen3.5-4B": (0.0, 0.0),
}


@dataclass
class ModelResult:
    """一个模型的全部观测。缺的字段留 None，不编。"""

    model: str
    batches_done: int = 0
    batches_planned: int = 0
    errors: list[str] = field(default_factory=list)
    raw_items: int = 0  # 模型自己吐的条目数
    kept_items: int = 0  # 过得了 _to_item 的条目数
    drops: dict[str, int] = field(default_factory=dict)
    empty_batches: int = 0
    secs: list[float] = field(default_factory=list)
    in_tok: int = 0
    out_tok: int = 0
    out_tok_max: int = 0
    src_ids: set[int] = field(default_factory=set)
    samples: list[dict] = field(default_factory=list)
    raw_dump: list[dict] = field(default_factory=list)

    @property
    def total_secs(self) -> float:
        return sum(self.secs)

    @property
    def match_rate(self) -> float | None:
        """摘录命中率。没产出就是 None——**不是 1.0**，那是空守卫。"""
        if self.raw_items == 0:
            return None
        return self.kept_items / self.raw_items

    @property
    def cost(self) -> float:
        inp, out = CANDIDATES.get(self.model, (0.0, 0.0))
        return self.in_tok / 1e6 * inp + self.out_tok / 1e6 * out

    @property
    def inquiries(self) -> list[dict]:
        """模型自己标成「咨询/询问」的产出——用户点名要丢弃的那一类。"""
        return [s for s in self.samples if s.get("inquiry")]

    @property
    def suspects(self) -> list[dict]:
        """标题在来源里找不到依据的条目——**这批才是幻觉的候选**。

        摘录匹配率抓不到它们（引用可能是对的），只有标题重合度能。
        """
        return [s for s in self.samples if s.get("title_overlap", 1.0) < LOW_OVERLAP]


def build_batches(
    db: str, *, since: int | None, until: int | None, batch_size: int, context: int
) -> tuple[str, list[list[PendingMessage]], int]:
    """复现真实管线的批次构造。返回 (system, 批次列表, 窗口内消息数)。"""
    config = load_config()
    conn = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
    conn.text_factory = lambda b: b.decode("utf-8", "replace")
    rows = conn.execute(
        """
        SELECT msg_id, group_id, ts, COALESCE(sender_uid,''), content
        FROM messages
        WHERE ts > 0
          AND (? IS NULL OR ts >= ?)
          AND (? IS NULL OR ts <  ?)
        ORDER BY ts, msg_id
        """,
        (since, since, until, until),
    ).fetchall()
    conn.close()

    msgs = [
        PendingMessage(
            msg_id=r[0], group_id=r[1], ts=r[2], sender_uid=r[3], sender="", content=r[4]
        )
        for r in rows
    ]
    candidates, _ = prefilter.screen(msgs, tier_of=config.tier_of)
    in_scope = prefilter.expand_context(msgs, candidates, context=context)
    batches = prefilter.make_batches(in_scope, max_batch=batch_size)
    system = refine.build_system_prompt(load_categories())
    return system, batches, len(msgs)


_PUNCT = re.compile(r"[\s，。！？、：；「」『』【】（）()\[\]…~～\-—]+")


def title_overlap(title: str, source: str) -> float:
    """标题里有多大比例的字符能在来源原文里找到。

    ⚠️ **这个指标是补摘录匹配率的漏**，理由是本轮实测逼出来的：
    6 个模型的摘录匹配率全在 95–99%，**连已知会幻觉的那个 7B 也是 95%**
    ——因为 `_match_source` 校验的是 `quote`，而记忆里那条「纯幻觉」
    （标题与来源完全无关）的 quote 是匹配得上的。**引用对 ≠ 摘要对。**

    所以一条 item 要过两道：quote 能在原文里找到（不许编引用），
    title 也要能在原文里找到依据（不许编内容）。本函数管第二道。

    它只用来**标可疑**，不作判据——标题本就是模型对原文的概括，
    允许它用原文没有的词（「通知」→「公告」）。低于阈值才需要人看。
    """
    t = _PUNCT.sub("", title or "")
    if not t:
        return 0.0
    s = _PUNCT.sub("", source or "")
    return sum(1 for ch in t if ch in s) / len(t)


# 低于这个重合度就标可疑。0.45 沿用 vigil_acceptance.py 的既有阈值——
# 同一个概念在两处用不同的数会让人无法对照两份报告。
LOW_OVERLAP = 0.45

# 模型**自己**把产出标成询问/咨询的词。用它而不是「原文像不像疑问句」，
# 是因为「有没有人捡到一个充电宝」是疑问句但**含真信息**（失物招领），
# 拿句式判会误伤。而模型若在标题里写了「咨询」「询问」，那是它自己承认
# 这条不含信息——正是用户反馈里要丢弃的那一类。
_INQUIRY = re.compile(r"咨询|询问|求问|请问|打听")


def is_inquiry(title: str) -> bool:
    return bool(_INQUIRY.search(title or ""))


# 并排对照取多少条来源。人一次能认真判读的量就这么大——
# 再多就变成翻页而不是判读了。
COMPARE_SHEET = 20


def _drop_reason(raw: object, batch: list[PendingMessage], known: frozenset[str]) -> str:
    """只用于**归因**——判定权在 refine._to_item 手里。

    本函数回答「它为什么被丢了」，不回答「该不该丢」。
    """
    if not isinstance(raw, dict):
        return "非对象"
    quote = str(raw.get("quote") or "").strip()
    if refine._match_source(quote, batch) is None:
        return "摘录匹配不上"
    if str(raw.get("kind", "")).strip() not in known:
        return "类目非法"
    if not str(raw.get("title") or "").strip():
        return "缺标题"
    return "其他"


def run_model(
    model: str,
    system: str,
    batches: list[list[PendingMessage]],
    *,
    api_key: str,
    thinking: bool | None,
    timeout: int,
    today: str,
) -> ModelResult:
    """一个模型跑完全部批次。批次顺序执行——为了延迟可归因、也别撞限流。"""
    result = ModelResult(model=model, batches_planned=len(batches))
    cfg = LLMConfig(
        api_key=api_key, model=model, timeout=timeout, enable_thinking=thinking
    )

    for index, batch in enumerate(batches, start=1):
        user = refine.build_user_prompt(batch, Redactor(), today=today)
        started = time.monotonic()
        try:
            call = chat_json(cfg, system=system, user=user)
        except LLMError as exc:
            result.secs.append(time.monotonic() - started)
            result.errors.append(f"第 {index} 批: {exc}")
            continue
        result.secs.append(time.monotonic() - started)
        result.batches_done += 1
        result.in_tok += call.input_tokens
        result.out_tok += call.output_tokens
        result.out_tok_max = max(result.out_tok_max, call.output_tokens)

        raw_items = call.payload.get("items")
        if not isinstance(raw_items, list):
            raw_items = []
        if not raw_items:
            result.empty_batches += 1

        result.raw_dump.append({"batch": index, "items": raw_items})
        for raw in raw_items:
            result.raw_items += 1
            item = refine._to_item(raw, batch, known_kinds=KNOWN_KINDS)
            if item is None:
                reason = _drop_reason(raw, batch, KNOWN_KINDS)
                result.drops[reason] = result.drops.get(reason, 0) + 1
                continue
            result.kept_items += 1
            result.src_ids.update(item.src_msg_ids)
            source = next(
                (m for m in batch if m.msg_id in item.src_msg_ids), None
            )
            result.samples.append(
                {
                    "batch": index,
                    "msg_id": source.msg_id if source else 0,
                    "kind": item.kind,
                    "title": item.title,
                    "quote": str(raw.get("quote") or ""),
                    "detail": item.detail,
                    "place": item.place,
                    "amount": item.amount,
                    "deadline": raw.get("deadline"),
                    "confidence": item.confidence,
                    "source": source.content if source else "",
                    "ts": source.ts if source else 0,
                    "title_overlap": title_overlap(item.title, source.content if source else ""),
                    "inquiry": is_inquiry(item.title),
                }
            )
    return result


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--db", default="data/vigil.db")
    ap.add_argument("--since", default=None, help="YYYY-MM-DD")
    ap.add_argument("--until", default=None, help="YYYY-MM-DD")
    ap.add_argument("--batch", type=int, default=30)
    ap.add_argument("--context", type=int, default=2)
    ap.add_argument(
        "--models",
        default="Qwen/Qwen3.5-35B-A3B,Qwen/Qwen3.5-27B,Qwen/Qwen3-32B,Qwen/Qwen3-14B",
    )
    ap.add_argument(
        "--thinking",
        choices=["off", "on", "auto"],
        default="off",
        help="off=发 enable_thinking:false（默认，用户决定）；auto=不发该字段",
    )
    ap.add_argument("--timeout", type=int, default=180)
    # 默认值留 None：replay 时要能分辨「用户没给 --out」和「用户就要默认名」，
    # 否则 replay 会覆盖掉别的报告。解析在 main 里做。
    ap.add_argument("--out", default=None)
    ap.add_argument(
        "--replay",
        default=None,
        help="从已有的 .json 重出报告，不调模型、不花钱。"
        "改报告口径时用它，别为了改排版重烧一遍 token",
    )
    args = ap.parse_args()

    global KNOWN_KINDS
    KNOWN_KINDS = frozenset(c.slug for c in load_categories())

    if args.replay:
        return replay(args)
    args.out = args.out or "M1-模型评估.md"

    models = [m.strip() for m in args.models.split(",") if m.strip()]
    unknown = [m for m in models if m not in CANDIDATES]
    if unknown:
        print(f"[警告] 这些模型没有价格数据，成本列会显示 0：{unknown}")

    try:
        since = reader._to_epoch(args.since)
        until = reader._to_epoch(args.until)
    except ValueError as exc:
        sys.exit(f"[参数错误] {exc}")

    system, batches, window = build_batches(
        args.db,
        since=since,
        until=until,
        batch_size=args.batch,
        context=args.context,
    )
    thinking = {"off": False, "on": True, "auto": None}[args.thinking]
    today = dt.date.today().isoformat()

    print(f"窗口 {window:,} 条消息 → {len(batches)} 批（每批 ≤{args.batch}）")
    print(f"模型 {len(models)} 个，thinking={args.thinking}，并发跑\n")

    api_key = load_llm_key()
    results: dict[str, ModelResult] = {}
    with ThreadPoolExecutor(max_workers=len(models)) as pool:
        futures = {
            pool.submit(
                run_model,
                m,
                system,
                batches,
                api_key=api_key,
                thinking=thinking,
                timeout=args.timeout,
                today=today,
            ): m
            for m in models
        }
        for future in as_completed(futures):
            model = futures[future]
            try:
                r = future.result()
            except Exception as exc:  # noqa: BLE001 — 一个模型崩了不该带走整轮
                print(f"  ✗ {model} 整体失败: {type(exc).__name__}: {exc}")
                continue
            results[model] = r
            print(
                f"  ✓ {model:<28} 产出 {r.kept_items:>3}/{r.raw_items:<3} 条"
                f"  批 {r.batches_done}/{r.batches_planned}"
                f"  失败 {len(r.errors)}  共 {r.total_secs:,.0f}s"
            )

    ordered = [results[m] for m in models if m in results]
    meta = {
        "ran_at": f"{dt.datetime.now():%Y-%m-%d %H:%M}",
        "since": args.since,
        "until": args.until,
        "batch": args.batch,
        "thinking": args.thinking,
        "timeout": args.timeout,
        "window": window,
        "batches": len(batches),
        "prompt_ver": refine.PROMPT_VERSION,
    }
    write_report(meta, ordered, args.out)
    print(f"\n报告已写入 {args.out}")
    return 0


def replay(args) -> int:
    """从 dump 重出报告。只重算纯函数型的指标（如标题重合度），不碰网络。"""
    path = Path(args.replay)
    dump = json.loads(path.read_text(encoding="utf-8"))
    raw_meta = dump["meta"]
    # 早期 dump 的 meta 是 vars(args)，没有 ran_at/window/batches。
    # 容忍它，否则改个报告排版就被一份旧数据卡住。
    meta = {
        "ran_at": raw_meta.get("ran_at", "（旧格式 dump，无记录）"),
        "since": raw_meta.get("since"),
        "until": raw_meta.get("until"),
        "batch": raw_meta.get("batch", 30),
        "thinking": raw_meta.get("thinking", "off"),
        "timeout": raw_meta.get("timeout", 180),
        # 缺就是 None（渲染成「未记录」），不是 0——见 _count 的注释
        "window": raw_meta.get("window"),
        "batches": raw_meta.get("batches"),
        "prompt_ver": raw_meta.get("prompt_ver", refine.PROMPT_VERSION),
    }
    meta["ran_at"] = f"{meta['ran_at']}（重出于 {dt.datetime.now():%Y-%m-%d %H:%M}）"

    ordered: list[ModelResult] = []
    for model, raw in dump["results"].items():
        r = ModelResult(model=model)
        for key, value in raw.items():
            if key == "seconds":
                continue
            if hasattr(r, key):
                setattr(r, key, value)
        r.src_ids = set(raw.get("src_ids") or [])
        # 旧 dump 可能没有这两个字段——就地补算，别让报告里出现空列
        for s in r.samples:
            if "title_overlap" not in s:
                s["title_overlap"] = title_overlap(s["title"], s["source"])
            if "inquiry" not in s:
                s["inquiry"] = is_inquiry(s["title"])
        ordered.append(r)

    out = args.out or str(path.with_suffix(".md"))
    write_report(meta, ordered, out)
    print(f"已从 {path} 重出报告 → {out}（未调用模型）")
    return 0


def summarise(result: ModelResult) -> dict[str, str]:
    """表格用的一行。缺数据显式写 N/A 或 —，不填 0 冒充数字。"""
    rate = result.match_rate
    secs = result.secs
    median = f"{statistics.median(secs):.1f}" if secs else "—"
    return {
        "产出": f"{result.kept_items}/{result.raw_items}",
        "询问类": (
            f"{len(result.inquiries)}"
            f"({len(result.inquiries) / result.kept_items:.0%})"
            if result.kept_items
            else "N/A"
        ),
        "引用匹配率": "N/A" if rate is None else f"{rate:.0%}",
        "可疑标题": str(len(result.suspects)),
        "空批": f"{result.empty_batches}/{result.batches_done}",
        "失败": str(len(result.errors)),
        "中位延迟": median + "s",
        "总延迟": f"{result.total_secs:,.0f}s",
        "输出tok": f"{result.out_tok:,}",
        "单批输出峰值": f"{result.out_tok_max:,}",
        "成本¥": f"{result.cost:.3f}",
    }


def _count(value: int | None) -> str:
    """缺值写「未记录」，**不写 0**。

    踩过的坑：老格式 dump 的 meta 里没有 window/batches，replay 时回退成 0，
    报告头部就印出「库内 0 条 → 0 批」——**看着正常，数字是假的**，
    而且没人会去核对一个「0」。缺数据就该长得像缺数据。
    """
    return "未记录" if value is None else f"{value:,}"


def write_report(meta: dict, results: list[ModelResult], out: str) -> None:
    """出报告。`meta` 里是这次运行的参数——replay 时从 dump 里读回来，保持一致。"""
    cols = [
        "产出", "询问类", "引用匹配率", "可疑标题", "空批", "失败",
        "中位延迟", "总延迟", "输出tok", "单批输出峰值", "成本¥",
    ]
    lines = [
        "# M1 模型选型对照实验",
        "",
        f"- 时间：{meta['ran_at']}",
        f"- 窗口：{meta['since'] or '起点'} ~ {meta['until'] or '最新'}，"
        f"库内 {_count(meta['window'])} 条 → 预筛/扩上下文后 {_count(meta['batches'])} 批"
        f"（每批 ≤{meta['batch']} 条）",
        f"- thinking：`{meta['thinking']}`　temperature：0.1　timeout：{meta['timeout']}s",
        f"- 提示词版本：`{meta['prompt_ver']}`（换提示词必须换版本号，"
        f"否则新旧产出混在 refine_runs 里分不清）",
        "- 条件对齐：所有模型**逐字节相同**的系统/用户提示词，只有 `model` 字段不同",
        "",
        "两个机械指标，看的是**不同的**失败模式，缺一不可：",
        "",
        "| 指标 | 管什么 | 漏什么 |",
        "|---|---|---|",
        "| **引用匹配率** | `quote` 逐字摘录能否在原文找到 → **编引用** | 引用对但**内容编** |",
        "| **可疑标题** | `title` 有多少字能在原文找到依据 → **编内容** | 概括性改写（误报） |",
        "",
        f"⚠️ 第一项有盲区，是 M1 选型实测逼出来的：候选模型的引用匹配率普遍 95%+，"
        f"**连已知会幻觉的 `Qwen2.5-7B-Instruct` 也不例外**——因为那条「标题与来源完全无关」"
        f"的幻觉，它的 quote 恰恰是匹配得上的。**引用对 ≠ 摘要对**，所以必须有第二项。"
        f"可疑阈值 {LOW_OVERLAP:.0%}，与 `vigil_acceptance.py` 同值。",
        "",
        "## 总表",
        "",
        "| 模型 | " + " | ".join(cols) + " |",
        "|" + "---|" * (len(cols) + 1),
    ]
    for r in results:
        s = summarise(r)
        lines.append(f"| `{r.model}` | " + " | ".join(s[c] for c in cols) + " |")

    short = {r.model: r.model.split("/")[-1] for r in results}

    # 丢弃归因
    lines += ["", "## 丢弃归因（模型返回了但被本地校验拦下）", ""]
    for r in results:
        if not r.drops:
            lines.append(f"- `{r.model}`：无丢弃")
            continue
        detail = "、".join(f"{k} {v}" for k, v in sorted(r.drops.items(), key=lambda x: -x[1]))
        lines.append(f"- `{r.model}`：{detail}")

    # 跨模型对照：同一个来源消息，谁认出来了
    lines += ["", "## 来源消息覆盖对照", ""]
    lines.append("同一批输入下，各模型认为「有价值」的来源消息集合。")
    lines.append("差集是**召回分歧点**，值得人工看看到底谁漏了、谁多报了。")
    lines.append("")
    union = set().union(*(r.src_ids for r in results)) if results else set()
    lines.append(f"- 并集：{len(union)} 条不同来源消息")
    lines.append("")
    lines.append("| 模型 | 认出的来源数 | 占并集 | 只有它认出的 |")
    lines.append("|---|---|---|---|")
    for r in results:
        exclusive = r.src_ids - set().union(
            *(o.src_ids for o in results if o is not r)
        )
        pct = f"{len(r.src_ids) / len(union):.0%}" if union else "—"
        lines.append(f"| `{r.model}` | {len(r.src_ids)} | {pct} | {len(exclusive)} |")

    # ── 判读用：同一条来源消息，各模型分别抽成了什么 ──
    #
    # 这是给人工判定准备的核心材料。按「有几个模型认出了这条」排序：
    # 多数共识的多半是真信息，只有一个模型认出的要么是妙手要么是幻觉——
    # 光看标题分不出来，必须连同原文一起给人看。
    by_source: dict[int, list[tuple[str, dict]]] = {}
    for r in results:
        for s in r.samples:
            by_source.setdefault(s["msg_id"], []).append((r.model, s))

    lines += ["", "## 同一条来源的产出对照（人工判读用）", ""]
    lines.append("按「有几个**不同模型**认出这条来源」分组，每组先给原文。")
    lines.append("")
    lines.append("⚠️ 计的是**模型数**不是产出条数——同一个模型对一条长通知产出 2 条 item")
    lines.append("是合法的（如选课通知里的学分制/体育选项），把它算成「2 个模型」会虚高共识。")
    lines.append("")

    def n_models(entries: list[tuple[str, dict]]) -> int:
        return len({model for model, _ in entries})

    multi = sorted(
        (kv for kv in by_source.items() if n_models(kv[1]) > 1),
        key=lambda kv: (-n_models(kv[1]), -len(kv[1])),
    )
    single = [kv for kv in by_source.items() if n_models(kv[1]) == 1]
    for tag, group in (("多个模型都认出", multi), ("只有 1 个模型认出", single)):
        lines += [f"### {tag}（{len(group)} 条来源）", ""]
        for msg_id, entries in group:
            src = entries[0][1]
            when = (
                dt.datetime.fromtimestamp(src["ts"]).strftime("%m-%d %H:%M")
                if src["ts"]
                else "-"
            )
            n = n_models(entries)
            extra = f"，共 {len(entries)} 条产出" if len(entries) != n else ""
            lines.append(
                f"- **原文**（{when}，{n} 个模型认出{extra}）：{src['source'][:110]}"
            )
            for model, s in entries:
                flag = " ⚠️标题可疑" if s.get("title_overlap", 1.0) < LOW_OVERLAP else ""
                lines.append(
                    f"  - `{short[model]}` 【{s['kind']}】{s['title']}"
                    + (f"　｜地点={s['place']}" if s["place"] else "")
                    + (f"　｜金额={s['amount']}" if s["amount"] else "")
                    + (f"　｜截止={s['deadline']}" if s["deadline"] else "")
                    + flag
                )
        lines.append("")

    # ── 并排对照：同一条来源，所有模型各自抽成了什么 ──
    #
    # 这是**选型判定的主材料**。放在长清单之前，是因为逐条清单几百行，
    # 人判断不了；并排看同一批来源，类目对不对、有没有漏、有没有多报，
    # 一眼就分得出。取「被最多个模型认出的」来源，可比性最强。
    all_by_source: dict[int, list[tuple[str, dict]]] = {}
    for r in results:
        for s in r.samples:
            all_by_source.setdefault(s["msg_id"], []).append((r.model, s))

    def n_models_of(entries: list[tuple[str, dict]]) -> int:
        return len({m for m, _ in entries})

    top = sorted(
        all_by_source.items(), key=lambda kv: (-n_models_of(kv[1]), kv[0])
    )[:COMPARE_SHEET]
    lines += ["", f"## 并排对照（公认度最高的 {len(top)} 条来源）", ""]
    lines.append(
        f"挑的是**被最多个模型认出**的来源，所以每条都有得比。"
        f"同一行看下来：谁的类目更贴、谁漏了、谁多报了，一眼分得出。"
        f"（`—` = 该模型没产出这条）"
    )
    lines.append("")
    for msg_id, entries in top:
        src = entries[0][1]
        when = (
            dt.datetime.fromtimestamp(src["ts"]).strftime("%m-%d %H:%M")
            if src["ts"]
            else "-"
        )
        lines.append(
            f"**{when}**　{n_models_of(entries)}/{len(results)} 个模型认出"
            f"　｜ 原文：{src['source'][:100]}"
        )
        lines.append("")
        got = {m: s for m, s in entries}
        lines.append("| 模型 | 类目 | 标题 |")
        lines.append("|---|---|---|")
        for r in results:
            s = got.get(r.model)
            if s is None:
                lines.append(f"| `{short[r.model]}` | — | — |")
            else:
                lines.append(f"| `{short[r.model]}` | {s['kind']} | {s['title']} |")
        lines.append("")

    # 询问类单独成节——这是用户点名的噪声源，修提示词前后要拿它对比
    lines += ["", "## 询问类产出（模型自己在标题里写了「咨询/询问」）", ""]
    lines.append(
        "用户反馈：这类信息价值低、多为噪声，**应直接丢弃**。提示词已加规则"
        "（见 `refine.build_system_prompt`），这一节用来验证规则有没有生效。"
    )
    lines.append("")
    for r in results:
        if not r.inquiries:
            lines.append(f"- `{r.model}`：无")
            continue
        pct = len(r.inquiries) / r.kept_items if r.kept_items else 0
        lines.append(f"### `{r.model}`（{len(r.inquiries)} 条，占产出 {pct:.0%}）")
        for s in r.inquiries:
            lines.append(f"- 【{s['kind']}】{s['title']}")
            lines.append(f"  - 原文：{s['source'][:100]}")
        lines.append("")

    # 可疑标题单独成节——这是幻觉最可能的藏身处，别埋在几百行对照里
    lines += ["", "## 可疑标题（`title` 在原文里找不到依据）", ""]
    total_suspect = sum(len(r.suspects) for r in results)
    lines.append(
        f"共 {total_suspect} 条。**需要人工逐条看**：可能是真幻觉，"
        f"也可能是模型用了自己的话概括（误报）。"
    )
    lines.append("")
    for r in results:
        if not r.suspects:
            lines.append(f"- `{r.model}`：无")
            continue
        lines.append(f"### `{r.model}`（{len(r.suspects)} 条）")
        for s in r.suspects:
            lines.append(
                f"- 重合 {s['title_overlap']:.0%}　【{s['kind']}】{s['title']}"
            )
            lines.append(f"  - 原文：{s['source'][:120]}")
        lines.append("")

    # 失败详情
    failures = [(r.model, e) for r in results for e in r.errors]
    if failures:
        lines += ["", "## 失败批次", ""]
        for model, err in failures:
            lines.append(f"- `{model}`：{err}")

    Path(out).write_text("\n".join(lines) + "\n", encoding="utf-8")

    dump = {
        "meta": meta,
        "results": {
            r.model: {
                "raw_items": r.raw_items,
                "kept_items": r.kept_items,
                "drops": r.drops,
                "empty_batches": r.empty_batches,
                "batches_done": r.batches_done,
                "batches_planned": r.batches_planned,
                "errors": r.errors,
                "secs": r.secs,
                "in_tok": r.in_tok,
                "out_tok": r.out_tok,
                "out_tok_max": r.out_tok_max,
                "src_ids": sorted(r.src_ids),
                "samples": r.samples,
                "raw_dump": r.raw_dump,
            }
            for r in results
        },
    }
    Path(out).with_suffix(".json").write_text(
        json.dumps(dump, ensure_ascii=False, indent=1), encoding="utf-8"
    )


KNOWN_KINDS: frozenset[str] = frozenset()

if __name__ == "__main__":
    raise SystemExit(main())
