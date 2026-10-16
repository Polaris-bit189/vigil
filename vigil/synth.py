"""合成语料：可复现的演示数据源。

**为什么需要它**：VIGIL 的原始数据源是本机 QQ 加密库 —— 评委没有那个库，
复现就无从谈起。而赛题第⑦条把「可复现」写成了红线。合成语料把这一环
从「不可复现」变成「一条命令」。

**它不是"假数据"的同义词**。它是**覆盖下游全部代码路径的测试语料**：
四类 kind、三种日期写法、四条遮蔽规则、角色昵称、匿名消息、图片引用
（含与不含本地文件两种）、以及 prefilter 的全部丢弃路径。

**确定性是设计约束而非巧合**：同一个 seed 必须产出**逐字节相同**的库。
否则「复现」这件事本身就说不通 —— 评委跑出来的和材料里的不是一个东西。
所以全程用 `random.Random(seed)` 显式实例，**不用模块级 `random`**
（那会跟别的调用方共享状态，产物随调用顺序漂移）。

两条产出路径（同一个 spec，两种用途）：

    SyntheticSource ──→ ingest() ──→ output_db    走完整编排，验证 seam
    write_corpus()  ──→ demo .db                   直接落库，作可提交的种子

前者是**测试用**的（证明 seam 接得上），后者是**演示用**的（评委 clone
下来就有数据，不必跑生成器）。
"""

from __future__ import annotations

import datetime as dt
import hashlib
import pathlib
import random
import sqlite3
import tomllib
from dataclasses import dataclass, field

from .config import Config, ConfigError, REPO_ROOT
from .ingest import (
    MEDIA_TABLE,
    MESSAGE_TABLE,
    SENDER_TABLE,
    _MEDIA_TABLE_DDL,
    _MESSAGE_TABLE_DDL,
    _SENDER_TABLE_DDL,
)
from .source import GroupRead, Sender, SourceMessage

# 演示语料的默认落点（可提交，见 .gitignore 的注释）。
DEMO_DB_NAME = "vigil-demo.db"
DEMO_SPEC_NAME = "corpus.toml"

# 相对基准日。**刻意写死而不是用今天** —— 用 `dt.date.today()` 的话，
# 同一个 seed 在不同日子跑出不同的库，确定性就没了。
_BASE_DATE = dt.date(2026, 9, 1)

# 号码段：139-0000-xxxx 是运营商未分配段；群号 9000000xx。
# ⚠️ 别改成真实号段 —— 这份语料会进公开仓库。
_PHONE_PREFIX = "1390000"


@dataclass(frozen=True)
class SynthGroup:
    id: int
    name: str
    tier: str
    members: int


@dataclass(frozen=True)
class SenderSpec:
    role: str
    n: int
    name_pool: tuple[str, ...] = ()


@dataclass(frozen=True)
class TemplateSpec:
    kind: str
    deadline: str  # 'literal' | 'implied' | 'none'
    text: str


@dataclass(frozen=True)
class SynthSpec:
    seed: int
    days: int
    noise_ratio: float
    groups: tuple[SynthGroup, ...]
    senders: tuple[SenderSpec, ...]
    templates: tuple[TemplateSpec, ...]
    # 噪声模板：reason → 候选文本。reason 只是备注，生成器不按它分支。
    noise: dict[str, tuple[str, ...]] = field(default_factory=dict)


def default_spec_path(config: Config | None = None) -> pathlib.Path:
    """语料规格的默认位置。

    ⚠️ 走 `REPO_ROOT` 而不是 import 期的常量绑定：测试会
    `monkeypatch.setattr(config, "REPO_ROOT", tmp)`，冻住的值会让它失效。
    """
    _ = config
    return REPO_ROOT / "demo" / DEMO_SPEC_NAME


def load_spec(path: pathlib.Path) -> SynthSpec:
    """读语料规格。**任何形状问题都快速失败**（ConfigError）。

    与 `load_config` 同一条规矩：静态配置写错了，越早响亮越好。
    静默回退到默认值会让一份拼错的语料跑出"看起来正常"的库。
    """
    try:
        raw = tomllib.loads(path.read_text(encoding="utf-8"))
    except OSError as exc:
        raise ConfigError(f"读不到语料规格 {path}: {exc}") from exc
    except tomllib.TOMLDecodeError as exc:
        raise ConfigError(f"{path}: TOML 解析失败 —— {exc}") from exc

    meta = raw.get("meta")
    if not isinstance(meta, dict):
        raise ConfigError(f"{path}: 缺少 [meta] 段")

    groups_raw = raw.get("groups")
    if not isinstance(groups_raw, list) or not groups_raw:
        raise ConfigError(f"{path}: [[groups]] 段为空 —— 语料至少要有一个群")

    groups = tuple(
        SynthGroup(
            id=int(g["id"]),
            name=str(g["name"]),
            tier=str(g.get("tier", "normal")),
            members=int(g.get("members", 20)),
        )
        for g in groups_raw
    )

    senders = tuple(
        SenderSpec(
            role=str(s["role"]),
            n=int(s["n"]),
            name_pool=tuple(str(x) for x in s.get("name_pool", ())),
        )
        for s in raw.get("senders", [])
    )
    if not senders:
        raise ConfigError(f"{path}: [[senders]] 段为空 —— 没人就没法造消息")

    templates = tuple(
        TemplateSpec(
            kind=str(t["kind"]),
            deadline=str(t.get("deadline", "none")),
            text=str(t["text"]),
        )
        for t in raw.get("templates", [])
    )
    if not templates:
        raise ConfigError(f"{path}: [[templates]] 段为空 —— 没有模板造不出消息")

    noise: dict[str, tuple[str, ...]] = {}
    for n in raw.get("noise", []):
        noise[str(n["reason"])] = tuple(str(x) for x in n.get("texts", ()))

    return SynthSpec(
        seed=int(meta.get("seed", 20261014)),
        days=int(meta.get("days", 120)),
        noise_ratio=float(meta.get("noise_ratio", 0.35)),
        groups=groups,
        senders=senders,
        templates=templates,
        noise=noise,
    )


# ── 渲染 ──────────────────────────────────────────────────────
# 占位符 → 具体值。**日期写法的多样性是刻意的**：
# deadline.py 的 `_date_patterns` 认三类写法（汉字 / 点号 / 斜杠 / 完整年月日），
# 只测一种等于没测另外几种。

def _date_forms(d: dt.date, today: dt.date) -> dict[str, str]:
    """给一个日期，产出四种写法。

    `{n}` 用的不是日期而是「距今天几天」，供"还差{n}人"这类模板用。
    """
    _ = today
    return {
        "date_cn": f"{d.month}月{d.day}日",
        "date_dot": f"{d.month}.{d.day}",
        "date_slash": f"{d.month}/{d.day}",
        "date_dash": d.isoformat(),
    }


# 时刻的选择池。**刻意覆盖 `deadline._time_patterns` 认的几种写法**：
# 24 小时制、中文数字、带时段词、带「半」—— 只测一种等于没测另外几种。
_TIME_POOL: tuple[tuple[int, int], ...] = (
    (8, 0), (9, 30), (10, 0), (14, 0), (14, 30), (15, 0),
    (16, 0), (19, 0), (19, 30), (20, 0),
)
_CN_NUM = ("", "一", "二", "三", "四", "五", "六", "七", "八", "九", "十",
           "十一", "十二")


def _time_forms(h: int, m: int) -> dict[str, str]:
    """给一个时刻，产出几种写法 —— 每一种都**能唯一确定**该时刻。

    ⚠️ 存在的理由（`docs/design/时间安排.md` §二「连带改动」）：语料里原本
    **一个时刻都没有**（实测 grep 零命中）⇒ `start_supported` 与 `_parse_start`
    改完也**测不了** —— 阳性用例没有可引用的依据。
    """
    h12 = h % 12 or 12
    if h < 6:
        period = "凌晨"
    elif h < 12:
        period = "上午"
    elif h < 13:
        period = "中午"
    elif h < 18:
        period = "下午"
    else:
        period = "晚上"
    cn = _CN_NUM[h12]
    half = "半" if m == 30 else ""
    cn_min = "" if m == 0 else f"{m}分"
    return {
        "time_hm": f"{h}:{m:02d}",              # 14:00
        "time_hm_cn": f"{h}：{m:02d}",          # 14：00（全角冒号）
        "time_cn": f"{period}{cn}点{half or cn_min}",   # 下午2点 / 晚上7点半
        "time_plain": f"{cn}点{half or cn_min}",        # 2点 / 7点半
        "time_dian": f"{h}点{half or cn_min}",          # 14点 / 14点30分
    }


def _render(
    rng: random.Random,
    text: str,
    *,
    event_date: dt.date,
    today: dt.date,
) -> str:
    """把模板渲染成一条消息正文。

    号码类占位符刻意用**合成段**，且每次从 rng 取 —— 让遮蔽层有东西可遮。
    """
    subs = _date_forms(event_date, today)
    # ⚠️ 时刻从 `_TIME_POOL` 里取（不是随机 `rng.randrange(24)`）：
    # 池子里的每个时刻都在**每个时段词**的覆盖范围内，而 0-4 点这样的
    # 边角时刻会让"上午/下午"的判定没有语料压到。
    subs.update(_time_forms(*rng.choice(_TIME_POOL)))
    subs.update(
        {
            "place": rng.choice(
                ["第一教学楼", "图书馆", "体育馆", "食堂二楼", "实验楼 305"]
            ),
            "amount": f"{rng.choice([50, 80, 120, 200, 500])}元",
            "phone": _PHONE_PREFIX + f"{rng.randrange(1000):04d}",
            "qq": str(80000000 + rng.randrange(1000000)),
            "group_no": str(900000000 + rng.randrange(100000)),
            "term": rng.choice(["期末", "期中", "四六级", "选课", "体测", "军训"]),
            "n": str(rng.randrange(2, 9)),
        }
    )
    out = text
    for key, val in subs.items():
        out = out.replace("{" + key + "}", val)
    return out


def _media_for(rng: random.Random, content: str) -> tuple[tuple[str, str], ...]:
    """这条消息引用的图片。

    ⚠️ 只有**特定模板**才带图（含「二维码」「扫」这类字），且**一半一半**地
    让一半 md5 落到缓存索引里、一半落不到 —— 这样 `msg_media.path`
    的两种状态（有值 / NULL）都能在演示库里出现。
    """
    if not any(k in content for k in ("二维码", "扫", "图")):
        return ()
    n = rng.choice([1, 1, 2])
    out = []
    for _ in range(n):
        # md5 是 32 位 hex —— 形状与 QQ 源一致（那边就是从 BLOB 里正则扫出来的）
        digest = hashlib.md5(
            f"{rng.randrange(1 << 32)}-{content[:8]}".encode()
        ).hexdigest()
        out.append((digest, rng.choice(["jpg", "png"])))
    return tuple(out)


def _peers(rng: random.Random, spec: SynthSpec) -> list[tuple[str, str]]:
    """造一个人名单：[(uid, 群昵称)]。

    群昵称的**形态**是刻意的 —— 「班助」类带角色词（压 ROLE_PATTERN）、
    「同学」类只有名字（压"无角色时不误留"）。
    """
    fallback = ["张三", "李四", "王五", "赵六", "孙七", "周八", "吴九", "郑十"]
    out: list[tuple[str, str]] = []
    used = 0
    for s in spec.senders:
        pool = s.name_pool or tuple(fallback)
        for i in range(s.n):
            name = pool[i % len(pool)] if s.name_pool else fallback[used % len(fallback)]
            uid = f"u_{s.role}_{i}"
            if s.role == "同学":
                nick = name
            else:
                # 「22-1班班助张三」——角色词 + 前缀 + 人名，与真实语料同形
                nick = f"22-1班{s.role}{name}"
            out.append((uid, nick))
            used += 1
    return out


class SyntheticSource:
    """实现 `source.Source` 协议。同一个 spec 永远产出同一份语料。"""

    def __init__(self, spec: SynthSpec) -> None:
        self.spec = spec
        self._rng = random.Random(spec.seed)  # 显式实例，不碰模块级 random
        # 语料一次生成、多次读取 —— Source 的五个方法都从这份缓存取。
        self._groups: dict[int, list[SourceMessage]] = {}
        self._senders: dict[tuple[int, str], Sender] = {}
        self._media_index: dict[str, pathlib.Path] = {}
        self._build()

    # ── 生成 ──────────────────────────────────────────────

    def _build(self) -> None:
        spec = self.spec
        peers_all = _peers(self._rng, spec)
        for group in spec.groups:
            # 每个群取自己那部分人；群主取前几个当"管事的"
            peers = peers_all[: max(2, min(len(peers_all), group.members // 2))]
            msgs: list[SourceMessage] = []
            mid = group.id * 100_000
            for day in range(spec.days):
                date = _BASE_DATE + dt.timedelta(days=day)
                for _ in range(self._rng.randrange(1, 5)):
                    if self._rng.random() < spec.noise_ratio:
                        content = self._noise_text()
                        kind_hint = None
                    else:
                        tpl = self._rng.choice(spec.templates)
                        content = _render(
                            self._rng, tpl.text, event_date=date, today=date
                        )
                        kind_hint = tpl
                    _ = kind_hint
                    uid, nick = self._rng.choice(peers)
                    # ⚠️ 约 3% 的消息是**匿名**（uid 为空）—— 压 refine 的
                    # 匿名分支。不给它，"匿名者必须逐条区分"那条逻辑在
                    # 演示数据上永远走不到。
                    if self._rng.random() < 0.03:
                        uid = ""
                    mid += 1
                    ts = int(
                        dt.datetime.combine(
                            date,
                            dt.time(
                                self._rng.randrange(7, 23), self._rng.randrange(60)
                            ),
                        )
                        .replace(tzinfo=dt.timezone(dt.timedelta(hours=8)))
                        .timestamp()
                    )
                    media = _media_for(self._rng, content)
                    for md5, ext in media:
                        # 一半进索引（本地"有"这张图），一半不进（path=NULL）
                        if self._rng.random() < 0.5:
                            self._media_index[md5] = pathlib.Path(
                                f"demo/media/{md5}.{ext}"
                            )
                    msgs.append(
                        SourceMessage(
                            msg_id=mid,
                            group_id=group.id,
                            ts=ts,
                            sender_uid=uid,
                            content=content,
                            media=media,
                        )
                    )
            self._groups[group.id] = msgs
            for uid, nick in peers:
                self._senders[(group.id, uid)] = Sender(
                    group_nick=nick,
                    qq_nick=nick,
                    # ⚠️ **不能用内建 `hash()`** —— 它对字符串加了**每进程随机**的盐
                    # （PYTHONHASHSEED），于是"同一个 seed 两次跑出不同的 uin"，
                    # 确定性当场失效。这是实测抓出来的：连跑两次 `vigil demo`，
                    # `sender_names.uin` 整列都不同，而 `messages` 完全一致
                    # （那几列不经过 hash，所以掩盖了问题）。
                    # 改用 md5 取前 8 位 —— 跨进程、跨机器都稳定。
                    uin=80000000 + int(hashlib.md5(uid.encode()).hexdigest()[:8], 16) % 10_000_000,
                )

    def _noise_text(self) -> str:
        pools = list(self.spec.noise.values())
        if not pools:
            return "收到"
        return self._rng.choice(self._rng.choice(pools))

    # ── Source 协议 ───────────────────────────────────────

    def list_groups(self) -> list[tuple[int, int, int]]:
        out = [
            (
                g.id,
                len(self._groups.get(g.id, ())),
                max((m.ts for m in self._groups.get(g.id, ())), default=0),
            )
            for g in self.spec.groups
        ]
        return sorted(out, key=lambda r: -r[1])

    def group_names(self) -> dict[str, str]:
        return {str(g.id): g.name for g in self.spec.groups}

    def read_group(self, gid: int) -> GroupRead:
        if gid not in self._groups:
            # ⚠️ 未配置的群**抛异常**而不是返回空 —— 空返回值与"这个群没消息"
            # 同形，会被记成 0 条成功（见 source.Source.read_group 的契约）。
            raise KeyError(f"语料里没有群 {gid}")
        return GroupRead(list(self._groups[gid]), [])

    def sender_names(self) -> dict[tuple[int, str], Sender]:
        return dict(self._senders)

    def media_index(self) -> dict[str, pathlib.Path]:
        return dict(self._media_index)


def write_corpus(spec: SynthSpec, out_path: pathlib.Path) -> pathlib.Path:
    """把语料直接落成三张表的明文库 —— **这就是可提交的 demo 种子**。

    形状与 `ingest()` 的产物完全一致，所以它可以直接当 `output_db` 用：
    评委 clone 下来、`vigil serve` 就能看到界面，不必先跑生成器。
    """
    src = SyntheticSource(spec)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    # ⚠️ 先删旧的：语料规格可能改了（群变少），残留的行会让产物
    # 依赖上一次的库 —— 那确定性就没了。
    out_path.unlink(missing_ok=True)
    conn = sqlite3.connect(str(out_path))
    try:
        conn.executescript(_MESSAGE_TABLE_DDL)
        conn.executescript(_SENDER_TABLE_DDL)
        conn.executescript(_MEDIA_TABLE_DDL)
        cache = src.media_index()
        for g in spec.groups:
            read = src.read_group(g.id)
            conn.executemany(
                f"INSERT OR REPLACE INTO {MESSAGE_TABLE} VALUES (?,?,?,?,?)",
                [(m.msg_id, g.id, m.ts, m.sender_uid, m.content) for m in read.messages],
            )
            media_rows = [
                (
                    m.msg_id,
                    g.id,
                    m.ts,
                    md5,
                    ext,
                    str(cache[md5]) if md5 in cache else None,
                )
                for m in read.messages
                for md5, ext in m.media
            ]
            if media_rows:
                conn.executemany(
                    f"INSERT OR REPLACE INTO {MEDIA_TABLE} VALUES (?,?,?,?,?,?)",
                    media_rows,
                )
        conn.executemany(
            f"INSERT OR REPLACE INTO {SENDER_TABLE} VALUES (?,?,?,?,?,?)",
            [
                (gid, uid, s.group_nick, s.qq_nick, s.uin, 1 if s.in_group else 0)
                for (gid, uid), s in src.sender_names().items()
            ],
        )
        conn.commit()
    finally:
        conn.close()
    return out_path
