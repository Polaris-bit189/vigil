"""课表：用户自己的**周期性**时间约束。

存在的理由（`docs/design/消息整合与排期.md` §三）：排期缺的**不是算法，是约束源**。

    要排的：一组带死线的条目     ← ✅ 已有（`items.deadline_ts`）
    排到哪：我的可用时间         ← ❌ 没有它就排不出来

没有课表，任何"排期算法"都只能凭空发明一个时间轴 —— 那不是排期，是猜。

⚠️ **为什么是用户自己填，而不是从群消息里抽**（方案 C，文档明确排除）：
从群里抽的是**别人的**课表，与「只处理自己关心的信息」这个产品定位冲突。
用户主动提供自己的时间约束，数据是他自己的，合规上干净得多。

⚠️ **为什么用 TOML 文件而不是数据库表**：
与 `persons.toml` / `categories.toml` 同族 —— 它是**配置**（用户自己写的、
要能手工编辑、要能进配置包），不是运行时数据。放进 `config/` 让它自动获得
`configpack` 的导入导出、`atomic_write_text` 的原子写、以及"能被 diff"的形态。

⚠️ **周期怎么表达**：`weekday` + `start`/`end`（分钟级）。刻意**不做 RRULE**：
真实课表就是"每周三这个点"，而 RRULE 的表达力（每两周、每月第几个周三）
会带来一整套解析与测试，而我们一条都用不到。
⚠️ **但单双周是真实存在的**（很多学校隔周上课）⇒ 用 `weeks` 字段表达
「哪些周有课」（`"all"` 或 `"odd"` / `"even"`），这是最小可用的表达力。
"""

from __future__ import annotations

import pathlib
import tomllib
from dataclasses import dataclass

from .config import ConfigError, atomic_write_text, toml_int, toml_str

DEFAULT_TIMETABLE = pathlib.Path(__file__).resolve().parent.parent / "config" / "timetable.toml"

# 周一到周日。**与 `format.ts` / `_WEEKDAY_CN` 同一口径**（0 = 周一）。
WEEKDAY_CN = ("一", "二", "三", "四", "五", "六", "日")

# 单双周。⚠️ 用字符串而不是布尔：将来加「每 3 周」这类不必改 schema。
WEEKS_ALL = "all"
WEEKS_ODD = "odd"
WEEKS_EVEN = "even"
VALID_WEEKS = frozenset({WEEKS_ALL, WEEKS_ODD, WEEKS_EVEN})


# ── 默认作息 ──────────────────────────────────────────────────
#
# ⚠️ 这两个数**不该硬编码在算法里**（`docs/design/时间安排.md` 待定 3 的
# 同一条理由：写死一个数字等于替用户做了个他不一定认同的决定）。
# 放这里当**默认值**，用户可以在 `config/timetable.toml` 的 `[settings]` 改
# —— 改完是显式的，而不是埋在代码里没人看得见。
DEFAULT_DAY_START = 8 * 60    # 08:00
DEFAULT_DAY_END = 22 * 60     # 22:00


@dataclass(frozen=True)
class Timetable:
    """课表 = 课程 + 作息设置。

    ⚠️ **作息（day_start/day_end）与课程同等重要**：只减掉课程的话，
    排期器会把任务排到凌晨三点 —— 那是"有空"但"不可用"。
    而"我几点到几点是醒着的"是**用户的**信息，只能他说了算。
    """

    courses: tuple[Course, ...] = ()
    day_start: int = DEFAULT_DAY_START
    day_end: int = DEFAULT_DAY_END
    # 学期第一周的周一（ISO 日期串，如 "2026-09-01"）。
    # ⚠️ **只有用到单双周时才需要**：`weeks = "odd"/"even"` 要一个基准周，
    # 而"这周是单周还是双周"因校而异 —— 没法猜，只能用户给。
    term_start: str = ""

    @property
    def has_week_parity_courses(self) -> bool:
        """有没有声明单双周的课。有的话必须有 `term_start` 才算得出来。"""
        return any(c.weeks != WEEKS_ALL for c in self.courses)

    @property
    def is_empty(self) -> bool:
        """⚠️ **排期器必须先问这个**：空课表要明说「你还没填课表」，
        而不是排出一份"全是空闲"的结果 —— 那看起来像"排成功了"。

        ⚠️ 刻意**不实现 `__bool__`**：`if tt:` 与 `if tt.courses:`
        看起来是同一件事，实际语义完全不同（前者问"填了没有"、
        后者问"有几节课"）。两个都在的话，读代码的人得去查 `__bool__`
        的实现才知道哪个是哪个 —— 显式属性省掉这次跳转。
        """
        return not self.courses


@dataclass(frozen=True)
class Course:
    """一节课：每周 `weekday` 的 `start`–`end` 分钟。

    ⚠️ `start` / `end` 是**距零点的分钟数**，不是 "HH:MM" 字符串：
    排期全程是区间算术（比较、求交、找空隙），字符串每次都要解析一遍，
    而解析出来的东西还要再验证一次格式 —— 存分钟数就只有一个真值。
    入口处（`api` / TOML）解析一次，之后永远是对的。
    """

    weekday: int  # 0 = 周一 … 6 = 周日
    start: int    # 距零点分钟数，如 8:00 → 480
    end: int
    label: str = ""
    weeks: str = WEEKS_ALL

    @property
    def weekday_cn(self) -> str:
        return f"周{WEEKDAY_CN[self.weekday]}"

    def hhmm(self, minutes: int) -> str:
        return f"{minutes // 60:02d}:{minutes % 60:02d}"

    def __str__(self) -> str:
        return f"{self.weekday_cn} {self.hhmm(self.start)}-{self.hhmm(self.end)}"


def parse_hhmm(value: object, *, field: str) -> int:
    """``"08:00"`` → 480（分钟）。解析不了就抛 —— **不猜**。

    ⚠️ 抛而不是返回 None（与 `refine._parse_start` 的选择相反，这是刻意的）：
    那边的输入是**模型输出**（不可信、量大、错一个不该掀掉整轮），这边的输入是
    **用户手填**（可信、量小、错了要立刻告诉他）。同一份数据两种来源，
    失败策略本就该不同 —— 对用户填错的值静默容忍，他会以为填进去了。
    """
    if not isinstance(value, str):
        raise ConfigError(f"{field} 必须是 \"HH:MM\" 形式的字符串，实际是 {type(value).__name__}")
    text = value.strip()
    parts = text.replace("：", ":").split(":")
    if len(parts) != 2:
        raise ConfigError(f"{field} 必须是 \"HH:MM\"（例：08:00），实际是 {value!r}")
    try:
        h, m = int(parts[0]), int(parts[1])
    except ValueError:
        raise ConfigError(f"{field} 的时/分必须是数字，实际是 {value!r}") from None
    if not (0 <= h <= 23 and 0 <= m <= 59):
        raise ConfigError(f"{field} 超出范围（时 0-23、分 0-59），实际是 {value!r}")
    return h * 60 + m


def format_hhmm(minutes: int) -> str:
    """480 → ``"08:00"``。与 `parse_hhmm` 互逆。"""
    return f"{minutes // 60:02d}:{minutes % 60:02d}"


def _check_course(raw: dict, *, target: pathlib.Path, index: int) -> Course:
    """校验一段 ``[[courses]]``。**每一项都要点名**，否则用户不知道改哪个。"""
    where = f"{target}: 第 {index + 1} 段 [[courses]]"

    raw_wd = raw.get("weekday")
    if not isinstance(raw_wd, int) or isinstance(raw_wd, bool) or not (1 <= raw_wd <= 7):
        raise ConfigError(
            f"{where}: weekday 必须是 1-7 的整数（1=周一 … 7=周日），实际是 {raw_wd!r}。"
            "⚠️ 这里用 **1-7**（人话），而内部存 **0-6** —— "
            "直接填 0 会被拒，因为「周日是 0 还是 7」是个真实存在的手误来源。"
        )
    if "start" not in raw or "end" not in raw:
        raise ConfigError(f"{where}: 必须同时有 start 与 end（例：start = \"08:00\"）")

    start = parse_hhmm(raw["start"], field=f"{where} 的 start")
    end = parse_hhmm(raw["end"], field=f"{where} 的 end")
    if end <= start:
        raise ConfigError(
            f"{where}: end（{raw['end']}）必须**晚于** start（{raw['start']}）—— "
            "跨零点的课不在此表达（它更可能是把上下午写反了）。"
        )

    weeks = raw.get("weeks", WEEKS_ALL)
    if weeks not in VALID_WEEKS:
        raise ConfigError(
            f"{where}: weeks 只能是 {'/'.join(sorted(VALID_WEEKS))}，实际是 {weeks!r}"
        )
    label = raw.get("label", "")
    if not isinstance(label, str):
        raise ConfigError(f"{where}: label 必须是字符串，实际是 {type(label).__name__}")

    return Course(
        weekday=raw_wd - 1,  # 1-7 → 0-6
        start=start,
        end=end,
        label=label.strip(),
        weeks=weeks,
    )


def load_timetable(path: pathlib.Path | None = None) -> Timetable:
    """读课表。

    ⚠️ **文件不存在返回空课表**（与 `load_persons` 同形、与 `load_config` 相反）：
    "还没填课表"是完全合法的状态（用户刚装上、或他就不想排期）。

    ⚠️ **但形状不对时必须响亮**：`save_timetable` 是**整体覆盖写**，
    一次键名手误如果被静默读成"空课表"，用户下次保存就会把整份课表抹掉
    —— 与 `load_persons` 那里记的是同一个坑。
    """
    target = path or DEFAULT_TIMETABLE
    if not target.is_file():
        return Timetable()

    try:
        with open(target, "rb") as f:
            raw = tomllib.load(f)
    except tomllib.TOMLDecodeError as exc:
        # ⚠️ 包成 ConfigError（`TOMLDecodeError` 继承 ValueError 而非 RuntimeError，
        # 不包会绕过调用方的 `except ConfigError`，用户看到裸 traceback）
        # —— 与 `load_config` 里那条是同一件事。
        raise ConfigError(f"{target}: TOML 语法错误——{exc}") from exc

    settings = raw.get("settings", {})
    if not isinstance(settings, dict):
        raise ConfigError(
            f"{target}: `settings` 必须是一张表（`[settings]`），"
            f"实际是 {type(settings).__name__}"
        )
    day_start = _opt_hhmm(settings, "day_start", DEFAULT_DAY_START, target)
    day_end = _opt_hhmm(settings, "day_end", DEFAULT_DAY_END, target)
    if day_end <= day_start:
        raise ConfigError(
            f"{target}: settings.day_end 必须晚于 day_start"
            f"（现在 {format_hhmm(day_start)}–{format_hhmm(day_end)}）"
        )
    term_start = settings.get("term_start", "")
    if not isinstance(term_start, str):
        raise ConfigError(f"{target}: settings.term_start 必须是字符串（如 \"2026-09-01\"）")
    if term_start:
        _check_term_start(term_start, target)

    courses = raw.get("courses", [])
    if not isinstance(courses, list):
        raise ConfigError(
            f"{target}: `courses` 必须是一串表（每个写成 `[[courses]]`），"
            f"实际是 {type(courses).__name__}"
        )
    out: list[Course] = []
    for i, entry in enumerate(courses):
        if not isinstance(entry, dict):
            raise ConfigError(
                f"{target}: 第 {i + 1} 个 courses 元素必须是键值表，"
                f"实际是 {type(entry).__name__}——最常见的成因是把它写成了数组元素"
                f"（`courses = [\"周一\"]`），而本文件要的形状是 `[[courses]]` "
                "后跟几行 weekday = 1 / start = \"08:00\"。"
            )
        out.append(_check_course(entry, target=target, index=i))

    # ⚠️ **单双周必须有基准周**：没有 `term_start` 时"这周是单还是双"无从判断，
    # 而猜一个会让**所有**隔周课错一周 —— 所以响亮拒绝，不静默按"全上"处理。
    tt = Timetable(
        courses=tuple(out), day_start=day_start, day_end=day_end, term_start=term_start
    )
    if tt.has_week_parity_courses and not term_start:
        raise ConfigError(
            f"{target}: 有课程声明了单双周（weeks = \"odd\"/\"even\"），"
            "但 [settings] 里没有 term_start —— 没有基准周就判断不了"
            "「这周是单周还是双周」，而猜一个会让所有隔周课错一周。\n"
            "  请补一行：term_start = \"2026-09-01\"（**第一周的周一**）"
        )
    return tt


def _opt_hhmm(settings: dict, key: str, default: int, target: pathlib.Path) -> int:
    """`[settings]` 里的可选 HH:MM 项。缺省回落。"""
    if key not in settings:
        return default
    return parse_hhmm(settings[key], field=f"{target}: settings.{key}")


def _check_term_start(value: str, target: pathlib.Path) -> None:
    """学期起点必须是**周一**的 ISO 日期串。

    ⚠️ 强制周一：它要用来算"这是第几周"，而基准如果不是周一，
    同一周的周一到周日会算出不同的周次 —— 那会让单双周在周内跳变。
    """
    import datetime as dt

    try:
        d = dt.date.fromisoformat(value)
    except ValueError:
        raise ConfigError(
            f"{target}: settings.term_start 必须是 ISO 日期（如 \"2026-09-01\"），"
            f"实际是 {value!r}"
        ) from None
    if d.weekday() != 0:
        raise ConfigError(
            f"{target}: settings.term_start 必须是**周一**（现在 {value} 是"
            f"周{WEEKDAY_CN[d.weekday()]}）—— 它要用来算第几周，"
            "基准不是周一的话，同一周的周一到周日会算出不同周次。"
        )


def save_timetable(tt: Timetable, path: pathlib.Path | None = None) -> None:
    """整体替换写出课表（原子）。

    ⚠️ 整体替换，不是追加 —— 追加语义下"删掉一节课"永远做不到（与 `save_persons` 同）。
    """
    target = path or DEFAULT_TIMETABLE
    lines = [
        "# VIGIL 课表 —— 你的**周期性**时间约束",
        "#",
        "# 排期要回答「这条通知该放在什么时候做」，而它缺的从来不是算法，",
        "# 是「你什么时候有空」。这份文件就是那个约束源。",
        "#",
        "# ── 怎么填 ───────────────────────────────────────────────",
        "#   每节课一段 [[courses]]：",
        "#     weekday = 3          ← 1=周一 … 7=周日（必填）",
        "#     start   = \"14:00\"     （必填）",
        "#     end     = \"15:40\"     （必填，须晚于 start）",
        "#     label   = \"高等数学\"  ← 可省",
        "#     weeks   = \"all\"      ← 可省；单双周填 \"odd\" / \"even\"",
        "#",
        "#   [settings] 是作息（排期会在**这些时段之外**找空闲）：",
        "#     day_start = \"08:00\"   ← 可省，默认 08:00",
        "#     day_end   = \"22:00\"   ← 可省，默认 22:00",
        "#     term_start = \"2026-09-01\" ← 只在用了单双周时才需要（第一周的周一）",
        "#",
        "# ⚠️ weekday 用 **1-7**（1 是周一），不是 0-6 —— 0 会被拒。",
        "# ⚠️ end 必须晚于 start；跨零点的课不在这里表达。",
        "# ⚠️ 本文件由程序原子写出；手工改也行，下次保存会整体覆盖。",
        "",
    ]
    if (
        tt.day_start != DEFAULT_DAY_START
        or tt.day_end != DEFAULT_DAY_END
        or tt.term_start
    ):
        lines.append("[settings]")
        if tt.day_start != DEFAULT_DAY_START:
            lines.append(f"day_start = {toml_str(format_hhmm(tt.day_start))}")
        if tt.day_end != DEFAULT_DAY_END:
            lines.append(f"day_end = {toml_str(format_hhmm(tt.day_end))}")
        if tt.term_start:
            lines.append(f"term_start = {toml_str(tt.term_start)}")
        lines.append("")
    for c in tt.courses:
        lines.append("[[courses]]")
        lines.append(f"weekday = {toml_int(c.weekday + 1, 'weekday')}")  # 0-6 → 1-7
        lines.append(f"start = {toml_str(format_hhmm(c.start))}")
        lines.append(f"end = {toml_str(format_hhmm(c.end))}")
        if c.label:
            lines.append(f"label = {toml_str(c.label)}")
        if c.weeks != WEEKS_ALL:
            lines.append(f"weeks = {toml_str(c.weeks)}")
        lines.append("")
    atomic_write_text(target, "\n".join(lines))
