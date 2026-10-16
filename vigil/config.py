"""配置与密钥读取。

失败策略：配置缺字段、群号格式不对、密钥为空 → ConfigError 快速失败。
不猜、不兜底——配置写错是我们自己的问题，越早暴露越好。
"""

from __future__ import annotations

import os
import pathlib
import tomllib
from collections.abc import Callable, Sequence
from dataclasses import dataclass

from . import llm

REPO_ROOT = pathlib.Path(__file__).resolve().parent.parent
DEFAULT_CONFIG = REPO_ROOT / "config" / "groups.toml"
DEFAULT_ENV = REPO_ROOT / ".env"
DEFAULT_PERSONS = REPO_ROOT / "config" / "persons.toml"
DEFAULT_LLM_TOML = REPO_ROOT / "config" / "llm.toml"


class ConfigError(RuntimeError):
    """配置层面的错误——快速失败。"""


# 群分级：high = 不受关键词过滤（班级群/课程群/部门群里的
# 「闲聊」往往是真通知）；normal = 需要关键词或角色昵称命中。
TIER_HIGH = "high"
TIER_NORMAL = "normal"
VALID_TIERS = frozenset({TIER_HIGH, TIER_NORMAL})


@dataclass(frozen=True)
class Group:
    id: int
    name: str
    enabled: bool = True
    tier: str = TIER_NORMAL


@dataclass(frozen=True)
class Config:
    # 数据源目录。早年叫 `qq_db_dir`（当时只有 QQ 一个源）；开源版默认源是
    # 合成语料、公开仓库里没有 QQ，那个名字会让复现的人困惑。
    # 读侧仍接受 `qq_db_dir` 作别名并出声警告（见 validate_config_raw）。
    source_dir: pathlib.Path
    output_db: pathlib.Path
    groups: tuple[Group, ...]
    # 用哪个数据源实现。'synthetic' 是演示默认（离线、可复现）；
    # 'qq' 需要私有模块 qq_source.py（不随开源版本分发）。
    source: str = "synthetic"

    @property
    def enabled_groups(self) -> tuple[Group, ...]:
        return tuple(g for g in self.groups if g.enabled)

    def group_name(self, gid: int) -> str:
        for g in self.groups:
            if g.id == gid:
                return g.name
        return ""

    def tier_of(self, gid: int) -> str:
        """未知群的默认档位是 normal——保守，宁可多筛不可漏喂。"""
        for g in self.groups:
            if g.id == gid:
                return g.tier
        return TIER_NORMAL


def load_config(path: pathlib.Path | None = None) -> Config:
    path = path or DEFAULT_CONFIG
    if not path.is_file():
        raise ConfigError(f"找不到配置文件: {path}")

    try:
        with open(path, "rb") as f:
            raw = tomllib.load(f)
    except tomllib.TOMLDecodeError as exc:
        # ⚠️ 必须包成 ConfigError（M6 T2 修）。`TOMLDecodeError` 继承自
        # **ValueError** 而不是 RuntimeError，不包的话会绕过
        # `cli._load_config_only` 的 `except ConfigError`，用户看到的是
        # **裸 traceback** 而不是一行可读的配置错误。
        # ⚠️ `load_categories` 从 M5 起就是这么做的（那里有完整论证），
        # 而本函数一直没跟上——今天 `groups.toml` 只由人手编辑、撞上的概率低，
        # **M6 之后 Web 会写它** ⇒ 一次序列化缺陷就会以 traceback 收场。
        raise ConfigError(f"{path}: TOML 语法错误——{exc}") from exc

    return validate_config_raw(raw, path)


def validate_config_raw(
    raw: dict, path: pathlib.Path, *, require_paths_exist: bool = True
) -> Config:
    """把**已解析**的 TOML 校验成 `Config`。

    ⚠️ **读路径与写路径共用这一个函数**（M6 T2）。写路径若另写一套校验，
    两份会漂移——而漂移的后果在这里尤其重：`save_config` 能写出
    `load_config` 拒绝的文件 ⇒ 一份配置永久落盘（原子写、无回滚）
    ⇒ **整个程序起不来**（`config.py` 是所有入口的硬依赖）。

    ⚠️ `require_paths_exist`（M6 T2 复审 FIX 3，controller 裁决）：
    **读路径 True、写路径 False**。`qq_db_dir` 的**外部存在性**是**环境事实**
    而不是配置属性——可移动盘没挂载、换机器、目录临时改名，都不该让
    「网页上保存一个群」整页失败；而 `load_config` 马上要用那个目录，
    不存在就该当场报错。⚠️ False 时**仍然解析路径**（该 `Path(...)` 的照做、
    `output_db` 该拼 `REPO_ROOT` 的照拼），**只跳过 `.is_dir()` 那一句**。

    ⚠️ 它**不是**段序的兜底（⚠️ 实测修正 M6 T2，见计划勘误 E4）：tomllib 下
    `[settings]` 写在 `[[groups]]` 之后仍落在顶层，本函数看不出顺序。
    它抓得住的是**结果**——「顶层根本没有 `settings`」（`raw.get("settings")`
    得到 `None` ⇒ 报「缺少 [settings] 段」）。写路径因此在落盘前仍能挡住
    「上游把 settings 整个弄没了」那类缺陷。
    """
    # ↓↓↓ 以下为原 `load_config` 从 `settings = raw.get("settings")`
    #     到 `return Config(...)` 的逐字搬移（FIX 2 / FIX 3 两处除外，均标明）↓↓↓
    settings = raw.get("settings")
    if not isinstance(settings, dict):
        raise ConfigError(f"{path}: 缺少 [settings] 段")

    # ⚠️ `qq_db_dir` 是**旧名兼容别名**（开源版改叫 `source_dir`）。
    # 读侧接受它并出声警告，是为了让老配置文件还能跑；写侧一律写新名，
    # 所以这个别名会在下一次网页保存时自然消失。
    # 用别名时的警告不能省：`qq_db_dir = "/path/to/QQ库"` 在新语义下
    # 指的是"数据源目录"，用户若以为它还是 QQ 专有键，换源时会困惑。
    if not settings.get("source_dir") and settings.get("qq_db_dir"):
        print(
            f"[警告] {path}: `qq_db_dir` 是旧键名，已按 `source_dir` 处理。\n"
            f"  建议改成 `source_dir = ...`（网页保存配置时会自动改）。"
        )

    for field in ("source_dir", "output_db"):
        if not settings.get(field) and not (field == "source_dir" and settings.get("qq_db_dir")):
            raise ConfigError(f"{path}: [settings] 缺少 {field}")

    source_dir = pathlib.Path(settings.get("source_dir") or settings["qq_db_dir"])
    # ⚠️ 相对路径要拼 `REPO_ROOT`（与 `output_db` 同一条规矩）。早年
    # `qq_db_dir` 是裸 Path，于是 `qq_db_dir = "qqdb"` 在 cwd=那个目录时
    # 读得通、cwd=仓库根时抛，行为随调用位置漂移。
    if not source_dir.is_absolute():
        source_dir = REPO_ROOT / source_dir

    output_db = pathlib.Path(settings["output_db"])
    if not output_db.is_absolute():
        output_db = REPO_ROOT / output_db

    # ⚠️ **存在性检查只对 `qq` 源做**。`source_dir` 的语义随 source 变：
    # 合成源根本不用它（语料由代码生成），要求那个目录存在是错的 ——
    # 一个演示用户不该为了跑 `vigil demo` 先去建一个空目录。
    # 而 qq 源确实需要它（要去那里找 nt_msg.db），不存在就是真问题。
    source_name = str(settings.get("source") or "synthetic")
    if require_paths_exist and source_name == "qq" and not source_dir.is_dir():
        raise ConfigError(f"数据源目录不存在: {source_dir}")

    # ⚠️ 三个分支不能合并（M6 T2 复审 FIX 2 / Important-2）。`[groups]`（**单层**
    # 方括号）与 `[[groups]]` 在 TOML 里只差一层括号，是极常见的笔误，
    # 但解析结果一个是 dict（**一张表**）、一个是 list（数组表）。
    # 不挡的话 `for entry in entries` 会去迭代 dict 的**键**（字符串），
    # 下一行 `entry.get("id")` 抛 `AttributeError`——**绕过本模块只捕
    # `ConfigError` 的契约**（`cli._load_config_only`），用户看到的是原始
    # traceback。实测（M6 T2 复审）：`AttributeError: 'str' object has no attribute 'get'`。
    # ⚠️ 顺序也不能反：先判 None 再判类型。反过来的话「根本没写」会被归到
    # 「类型不对」，把「段为空」的专用报错变成不可达代码。
    entries = raw.get("groups")
    if entries is None:
        raise ConfigError(f"{path}: [[groups]] 段为空（根本没写）")
    if not isinstance(entries, list):
        raise ConfigError(
            f"{path}: 需要 [[groups]] 数组段（注意是双层方括号），"
            f"实际是 {type(entries).__name__}"
        )

    groups: list[Group] = []
    for entry in entries:
        # ⚠️ **元素**也必须是表（M6 T2 的 C6，T7 之前收）。`groups = [1, 2]` 里
        # `entry` 是 `int` ⇒ 下一行 `entry.get("id")` 抛 `AttributeError`——**绕过
        # 本模块只捕 `ConfigError` 的契约**（`cli._load_config_only` /
        # `api.current_config`），用户看到的是**裸 traceback**。
        # 实测（T2 复审探针 2026-09-17；T7 修前 2026-09-18 复现同形）：
        # `AttributeError: 'int' object has no attribute 'get'`。
        #
        # ⚠️ 上面那两道守卫**挡不住它**：它们查的是「**段**是不是数组」，而这里段的
        # 形状是对的（list）——坏的是**元素**。而 TOML 允许数组里装标量，所以
        # `groups = [1, 2]` 是一份**语法完全合法**的文件，TOML 层面没有任何报错。
        #
        # ⚠️ **它对 T7 的配置包尤其要紧**：`configpack.parse` 消费本函数，而它接的是
        # **外部文件**（用户拷来拷去的包）。形状错的外部输入撞上 `AttributeError`
        # ⇒ 端点回 **500 而不是 400**，排障方向**完全错**：报错指向服务端，
        # 而真相是包写错了。
        #
        # ⚠️ `categories.py` 的 `[[categories]]` 有**同形**的守卫（`isinstance(entry, dict)`），
        # 但两处的理由各自写在自己的文件里——一条「见某某处」的注释是**把理由搬走**：
        # 删掉那边时这一处不会有任何东西变红，而注释还在说"有理由"
        # （本项目最贵的一课：缺的论证与错的论证都让后来者以为自己理解了）。
        if not isinstance(entry, dict):
            raise ConfigError(
                f"{path}: 每个 [[groups]] 段都必须是键值表，"
                f"实际是 {type(entry).__name__}——最常见的成因是把群号直接写成了"
                "数组元素（`groups = [111, 222]`），而本段要的形状是 `[[groups]]` "
                "后跟一行 `id = 111`。"
            )
        gid = entry.get("id")
        if not isinstance(gid, int) or gid <= 0:
            raise ConfigError(f"{path}: 群号必须为正整数，实际是 {gid!r}")
        tier = str(entry.get("tier", TIER_NORMAL))
        if tier not in VALID_TIERS:
            raise ConfigError(
                f"{path}: 群 {gid} 的 tier 只能是 {sorted(VALID_TIERS)}，实际是 {tier!r}"
            )
        groups.append(
            Group(
                id=gid,
                name=entry.get("name", ""),
                enabled=entry.get("enabled", True),
                tier=tier,
            )
        )

    if not groups:
        raise ConfigError(f"{path}: 一个群都没配（[[groups]] 段为空）")

    return Config(
        source_dir=source_dir,
        output_db=output_db,
        groups=tuple(groups),
        source=str(settings.get("source") or "synthetic"),
    )


@dataclass(frozen=True)
class Person:
    """一个被监视的人物。**键是 uin（QQ 号）**——uid 在查询时反查（见模块 docstring）。"""

    uin: int
    label: str
    note: str = ""


def atomic_write_text(path: pathlib.Path, text: str) -> None:
    """原子写文本：同目录临时文件 → fsync → ``os.replace``。

    ⚠️ **不许**写成 ``path.write_text(...)``：那是"截断 + 写"，
    中途失败（磁盘满、进程被杀）会留下**半个文件**——配置直接废掉。
    ``os.replace`` 在同一文件系统上是原子替换，Windows 上也成立。

    ⚠️ 临时文件必须与目标**同目录**（``os.replace`` 不跨卷）。
    ⚠️ 失败路径要清掉临时文件：留下一堆 ``.tmp`` 会污染 `config/`，
    而 `config/` 是要进 git 的。
    """
    tmp = path.with_name(path.name + ".tmp")
    try:
        with open(tmp, "w", encoding="utf-8", newline="\n") as f:
            f.write(text)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, path)
    except BaseException:
        tmp.unlink(missing_ok=True)
        raise


# ── 写侧序列化（M6 T2）──────────────────────────────────────────────
#
# ⚠️ Python 标准库**只有 `tomllib`（读）**，没有写入器。本仓库的依赖表只有
# sqlcipher3，不为写 TOML 引第三方库。所以写侧是手写的，而手写序列化
# **唯一的正确性判据是「全函数」**：
#
#     任何合法的值进去，产出的文本都要能被 `tomllib` 读回**同一个值**。
#
# ⚠️ 光有「全函数」还不够：它只覆盖**值**，不覆盖**段序**（`[settings]` 掉到
# `[[groups]]` 后面）与**键名**。**键名**那半由各自的 `save_*` 用「先解析、再
# 喂给读路径的同一个校验函数」挡住（见 `save_config` / `save_categories`）；
# **段序**那半**挡不住**（⚠️ 实测修正 M6 T2：tomllib 下两种顺序解析出的顶层表
# 一模一样，见 `save_config` 的 docstring），只能靠代码里的书写顺序
# 与 `tests/test_config.py` 里那条行锚定断言钉着。


def toml_str(value: str) -> str:
    """TOML 基本字符串——**全函数**：任何字符串进去，`save` 的产物都 `load` 得回来。

    ⚠️ 只转义反斜杠与双引号是**不够**的：一个带换行的 label 会写出**解析不了**的 toml，
    而原子写会让它**永久落盘**——`save` 成功返回、`load` 从此抛 `TOMLDecodeError`，
    整份文件连读都读不出来（M5 T7 实测，见 task-7-report.md）。

    ⚠️ 这里只是**转义**那一半；「名字里不许有换行」的**拒绝**在 API 层——
    `config/*.toml` 是人手输的，把一个换行静默存进"名字"里、再在界面上错乱显示，
    比直接报错更不诚实。

    ⚠️ 原名为 `_toml_str`，M6 T2 去掉下划线：`categories.py` 的写侧要用它，
    而跨模块 import 一个下划线名字是在制造「这是私有的、但谁都在用」的假象。
    """
    out: list[str] = []
    for ch in value:
        esc = TOML_ESCAPES.get(ch)
        if esc is not None:
            out.append(esc)
        elif ch < " " or ch == "\x7f":
            out.append(f"\\u{ord(ch):04X}")
        else:
            out.append(ch)
    return '"' + "".join(out) + '"'


def toml_int(value: object, field: str) -> int:
    """把值当整数写出——**拒绝 bool，也拒绝「能转成整数的字符串」**。

    ⚠️ `bool` 是 `int` 的子类（`True == 1`），所以裸的 `isinstance(x, int)`
    会放行 `True` ⇒ 写出 `uin = true`，读回来是 `uin = 1`（M5 遗留 #22，已复现）。

    ⚠️ 也不接受 `"10001"` 这类字符串：给「静默转一下」留口子，就等于把
    `api._edit_id_of` 那个 `int("abc")` → 500、`1.9` → 静默截断成 1 的缺陷
    换个形状请回来（那里已有完整论证）。**契约是「整数」而不是「能转成整数的东西」。**

    ⚠️ 它同时是 M5 遗留 #19 的落点：旧代码是裸 f-string `f"uin = {p.uin}"`，
    一个非 int 的值可以**注入 TOML**（已复现：写出第二个 `[[persons]]` 段头，
    并让整份文件 `TOMLDecodeError` 报废）。
    """
    if isinstance(value, bool) or not isinstance(value, int):
        raise ConfigError(
            f"{field} 必须是整数（**拒绝 bool**：True 是 int 的子类，会静默变成 1；"
            f"也拒绝字符串：静默转换会把「1.9 截断」那类缺陷请回来），"
            f"实际是 {value!r}（{type(value).__name__}）"
        )
    return value


def toml_path(value: pathlib.Path) -> str:
    """把路径写成 TOML 字符串：**能在仓库内相对表达就相对**，否则写绝对。

    ⚠️ **这个名字从 M6 T2 复审（FIX 4）起不带下划线前缀**：计划的 T7
    `configpack.build` 要**跨模块**调它，而跨模块 import 一个下划线名字
    是在制造「这是私有的、但谁都在用」的假象——与 `toml_str` 完全同一条判据。

    ⚠️ 这里**刻意不把旧名字逐字写出来**（M6 终审 #8）：写出来会让
    `grep` 那个带前缀的名字**永远不为零**（全仓只剩这一处散文在命中），
    而"零命中"在本项目里是有人会拿来当判据的东西——一条永不归零的 grep
    会让那条判据**静默失效**（本文件旁边那条"行号注释没有守卫"的教训同族）。

    ⚠️ 不统一成绝对、也不统一成相对——两个方向各有一个具体的坏法：

      · **全写绝对** ⇒ 配置文件搬到别的目录时仍指回原仓库。T8 的「配置包
        在另一个目录导入并生效」会因此**静默指向旧库**（判据 4 直接失败）。
      · **全写相对** ⇒ `REPO_ROOT` 之外的路径（QQ 库在 `C:/Users/...`）
        根本没法相对表达；而**相对路径的解析基准是 cwd、不是 `REPO_ROOT`**
        （⚠️ 实测修正 M6 T2：`validate_config_raw` 只对 `output_db` 做
        `REPO_ROOT / ...`，`qq_db_dir` 是裸 `Path(...)`。实测：
        `qq_db_dir = "qqdb"` 在 cwd=那个目录时读得通，cwd=仓库根时抛
        「QQ 数据库目录不存在: qqdb」）。⇒ 全写相对等于把「从哪个目录启动」
        冻进配置，换个 cwd 就报一句**指向根本没错的路径**的错误。
    """
    try:
        return toml_str(value.relative_to(REPO_ROOT).as_posix())
    except ValueError:
        return toml_str(value.as_posix())


def _check_persons_shape(path: pathlib.Path, raw: dict) -> None:
    """名单文件的**形状**不对就响亮报错（M5 终审 Minor-4 实测）。

    ⚠️ 原实现是 `raw.get("persons", [])`：键名拼错时**静默返回空名单**。
    而 `save_persons` 是**整体覆盖**写（「整体替换不是追加」是它刻意的语义，
    否则删不掉人）⇒ 一次手误的后果是：

        写 `[[person]]`（少个 s） → load 得到 () → 界面上「加一个人」时
        load → append → save ⇒ **原有名单被整体覆盖掉，全程零报错**。

    而 `config/persons.toml` 的文件头**自己邀请手工编辑**（「或手工往下面追加
    一段 `[[persons]]`」），也就是说这条路径一定会被走到。

    ⚠️ 与本模块的立场一致：「配置写错是我们自己的问题，**越早暴露越好**。
    不猜、不兜底」。所以这里不"宽容地多认几个键名"，直接抛 `ConfigError`——
    并且把最常见的那个形状（`[[person]]`）在消息里点名。
    """
    unknown = sorted(k for k in raw if k != "persons")
    if unknown:
        raise ConfigError(
            f"{path}: 只认 `[[persons]]` 段，却发现了这些顶层键：{unknown}。"
            "最常见的形状是 `[[person]]`（少写一个 s）——写错的段落会被**整体忽略**："
            "读出来是一份空名单，而下一次保存是**整体覆盖**写，"
            "手工加的人会无声消失（M5 终审实测）。请改成 `[[persons]]`。"
        )
    section = raw.get("persons", [])
    if not isinstance(section, list):
        raise ConfigError(
            f"{path}: `persons` 必须是**数组表**（`[[persons]]`，两层方括号），"
            f"实际是 {type(section).__name__}——写成 `[persons]` 时读出来的是"
            "一张表而不是一串人，后面的逐条校验会以一个看不懂的异常收场。"
        )


def load_persons(path: pathlib.Path | None = None) -> tuple[Person, ...]:
    """读监视人物名单。

    ⚠️ **文件不存在返回空元组**（与 `load_config` 的快速失败不同）：
    "一个人都没监视"是完全合法的状态，而 `load_config` 面对的是一份**必需**的配置。

    ⚠️ **但文件存在、形状却不对时必须响亮**（`_check_persons_shape`）：键名拼错
    （`[[person]]`）在旧实现里静默读成空名单，而 `save_persons` 是整体覆盖写
    ⇒ 一次手误会让手工加的人无声消失。

    ⚠️ `uin = 0` 是匿名哨兵（实测真库 98 行），**必须拒绝**——
    匿名者没有身份，把它加进监视名单是个不会生效的操作，越早暴露越好。
    """
    target = path or DEFAULT_PERSONS
    if not target.is_file():
        return ()

    with open(target, "rb") as f:
        raw = tomllib.load(f)

    _check_persons_shape(target, raw)

    out: list[Person] = []
    seen: set[int] = set()
    for entry in raw.get("persons", []):
        # ⚠️ **元素**也必须是表——与 `validate_config_raw` 里 `groups` 那处
        # **完全同形**（M6 T7 一并收，见那里的长注释）：`persons = [1, 2]` 是
        # **语法完全合法**的 TOML（数组里可以装标量），而 `_check_persons_shape`
        # 只看段名与段的类型 ⇒ `entry.get("uin")` 抛 `AttributeError`、**绕过
        # 本模块只捕 `ConfigError` 的契约**，用户看到的是裸 traceback。
        # 实测（2026-09-18，修前）：`AttributeError: 'int' object has no attribute 'get'`。
        # ⚠️ 这条路径会被走到：`config/persons.toml` 的文件头**自己邀请手工编辑**，
        # 而 `/api/persons` 就在它上面。
        if not isinstance(entry, dict):
            raise ConfigError(
                f"{target}: 每个 [[persons]] 段都必须是键值表，"
                f"实际是 {type(entry).__name__}——最常见的成因是把 uin 直接写成了"
                "数组元素（`persons = [10001]`），而本文件要的形状是 "
                "`[[persons]]` 后跟一行 `uin = 10001`。"
            )
        uin = entry.get("uin")
        if not isinstance(uin, int) or uin <= 0:
            raise ConfigError(
                f"{target}: 人物 uin 必须为正整数；0 是匿名哨兵，不能监视。实际是 {uin!r}"
            )
        if uin in seen:
            raise ConfigError(f"{target}: uin {uin} 重复出现")
        label = str(entry.get("label", "")).strip()
        if not label:
            raise ConfigError(f"{target}: uin {uin} 缺 label（label 是给人看的名字）")
        seen.add(uin)
        out.append(Person(uin=uin, label=label, note=str(entry.get("note", "")).strip()))
    return tuple(out)


_GROUPS_HEADER = """\
# VIGIL 监听群配置
#
# ── 怎么加群 ───────────────────────────────────────────────
#   1. 跑 `vigil groups`，它会列出 QQ 本地库里所有群
#      （含群号、群名、消息数，以及「是否已纳入」）
#   2. 把新群的 id 按下面格式加一段 [[groups]]
#   3. 重跑 `vigil export`。**每次都是全量重读所有已纳群**（不做增量记账，
#      实测 4.7 万条几秒），写入用 INSERT OR REPLACE，所以重跑不会产生重复行。
#      新群加进来后第一次 export 就会拉到它的全部历史。
#
# ── 怎么退群 ───────────────────────────────────────────────
#   把 enabled 改成 false（保留配置便于回看），或整段删掉。
#   已导出的历史数据留在库里不动——那是你的记录。
#
# ⚠️ **本文件由程序整体重写**（网页「配置」页保存时会覆盖它）：
#    手工改也行，但**手工加的注释会在下一次网页保存时消失**。
#
# 注意：刚加入的新群，QQ 需要先把它同步到本地库
#      （group_info.db 出现群名、nt_msg.db 开始落消息），
#      之后 `vigil groups` 才看得见。刚加完立刻查可能是空的。
"""


def save_config(config: Config, path: pathlib.Path | None = None) -> None:
    """整体替换写出群配置（原子）。

    ⚠️ 是**整体替换**不是追加：追加语义下「删掉一个群」永远做不到
    （与 `save_persons` 同一条理由）。

    ⚠️ `[settings]` 写在 `[[groups]]` **之前**——这是**约定，不是不变量**
    （⚠️ 实测修正 M6 T2，已入计划勘误 E4）。理由只有一条，就是**与现有手写
    文件的形状一致、便于人读与 diff**。原稿说「`[table]` 掉到
    `[[array_of_tables]]` 后面会被解析成最后一个数组元素下的子表 ⇒
    `raw.get("settings")` 变成 `None` ⇒ 程序起不来」——**实测不成立**：
    tomllib 里 `[settings]` 是**根相对**路径，写在 `[[groups]]` 之后仍落在顶层；
    只有 `[groups.settings]` 那种**穿过数组**的写法才嵌套，而
    `validate_config_raw` 也**分辨不出**顺序（它查的是「顶层有没有 settings」）。
    这条约定靠本函数的书写顺序 +
    `tests/test_config.py::test_save_config_writes_settings_before_groups`
    （那条是**约定锁**，不是不变量——它没有变异反证，见它的 docstring）钉着。

    ⚠️ 路径走 `toml_path`（仓库内相对、仓库外绝对），不要图省事写绝对。
    """
    target = path or DEFAULT_CONFIG

    # ⚠️ 拒绝零个群**不是洁癖**：`validate_config_raw` 把「一个群都没配」
    # 当成配置错误，所以写出一份零群的配置 = **把程序改成起不来**——
    # 而且下一次启动才现形，那时用户已经在网页上点过保存、拿到了「成功」。
    # ⚠️ 这同时**否掉了一个看起来更自然的设计**：网页上「删掉最后一个群」。
    # 正确动作是 `enabled = false`（保留配置、保留历史）。
    if not config.groups:
        raise ConfigError(
            "拒绝写出零个群的配置：读回来会被判成「一个群都没配」的配置错误，"
            "整个程序起不来。要退掉最后一个群，请改成 `enabled = false`"
            "（保留配置、保留已导出的历史）。"
        )

    lines = [
        _GROUPS_HEADER,
        "",
        "[settings]",
        f"source_dir = {toml_path(config.source_dir)}",
        f"output_db = {toml_path(config.output_db)}",
        f"source = {toml_str(config.source)}",
        "",
    ]
    for g in config.groups:
        lines.append("[[groups]]")
        lines.append(f"id = {toml_int(g.id, '群号')}")
        lines.append(f"name = {toml_str(g.name)}")
        lines.append(f"enabled = {'true' if g.enabled else 'false'}")
        lines.append(f"tier = {toml_str(g.tier)}")
        lines.append("")

    text = "\n".join(lines)

    # ⚠️ **顺序是承重的：先自己解析+校验，再落盘**。反过来的话，一份不合法的
    # 配置已经落盘了才发现不对——而 `atomic_write_text` 是原子写，
    # **没有任何自动回滚**，那份坏配置会一直躺在那里直到有人手动修。
    # ⚠️ 校验的是**最终文本**（而不是内存里的 `Config`）：只有这样才能抓到
    # 序列化本身的缺陷（漏转义的引号），而不只是数据的问题。
    # ⚠️ 它**抓不到段序**——tomllib 认为两种顺序都合法（见上方约定那一段）。
    try:
        raw = tomllib.loads(text)
    except tomllib.TOMLDecodeError as exc:
        raise ConfigError(f"内部错误：为 {target} 生成的 TOML 解析不了——{exc}") from exc
    # ⚠️ `require_paths_exist=False`（M6 T2 复审 FIX 3）：写路径**不查** `qq_db_dir`
    # 的外部存在性——那是环境事实（可移动盘没挂载也要能保存），不是配置属性。
    validate_config_raw(raw, target, require_paths_exist=False)

    atomic_write_text(target, text)


def save_persons(persons: Sequence[Person], path: pathlib.Path | None = None) -> None:
    """整体替换写出人物名单（原子）。

    ⚠️ 是**整体替换**不是追加：追加语义下"删掉一个人"永远做不到。
    """
    target = path or DEFAULT_PERSONS
    lines = [
        "# VIGIL 监视人物名单",
        "#",
        "# ── 怎么加人 ─────────────────────────────────────────────",
        "#   1. 在前端「按人物筛选」里添加，或手工往下面追加一段 [[persons]]",
        "#   2. uin 是 QQ 号。不要填 0（那是匿名哨兵）。",
        "#   3. 本文件由程序原子写出；手工改也行，下次保存会整体覆盖。",
        "#",
        "# label 是你自己起的名字（不是群昵称快照）——群昵称会变，label 不会。",
        "",
    ]
    for p in persons:
        lines.append("[[persons]]")
        # ⚠️ 走 `toml_int` 而不是裸 f-string（M5 遗留 #19/#22）：
        # 裸 f-string 让一个非 int 的值**可注入 TOML**（已复现：能写出第二个
        # `[[persons]]` 段头，并让整份文件 `TOMLDecodeError` 报废），
        # 而 `uin = true` 会被接受（bool ⊂ int）⇒ 建出一个 uin=1 的人。
        # `Person` 是 frozen dataclass 但**不强制类型**，所以产品路径今天
        # 够不到它——T8 的批量导入会让它够到，故必须在此之前收掉。
        lines.append(f"uin = {toml_int(p.uin, 'uin')}")
        lines.append(f"label = {toml_str(p.label)}")
        if p.note:
            lines.append(f"note = {toml_str(p.note)}")
        lines.append("")
    atomic_write_text(target, "\n".join(lines))


@dataclass(frozen=True)
class LLMSettings:
    """LLM 端点配置（`config/llm.toml`）。**这里没有密钥，也不许加**（spec §3.2）。

    ⚠️ `enable_thinking` 是**三态**，不是布尔：

    | 值 | 报文里 | 什么时候用它 |
    |---|---|---|
    | `None` | **不发这个键** | 通用 OpenAI 端点（SiliconFlow/Qwen 的扩展字段它们不认，会 400） |
    | `False` | 发 `false` | Qwen3/Qwen3.5 这类混合思考模型（不发会慢 **40 倍**，实测） |
    | `True` | 发 `true` | 显式要思考 |

    ⚠️ **默认是 `None` 而不是 `False`**——这与 `llm.LLMConfig` 的默认一致
    （那里有完整论证：「留 None 而不是 False 做默认：老模型不一定认这个字段，
    不发才是对它们最安全的形状」）。而"出厂路线上会不会变慢"由
    **`config/llm.toml` 里显式写着 `enable_thinking = false`** 保证，
    不由这个 dataclass 的默认值保证。
    """

    base_url: str = llm.DEFAULT_BASE
    model: str = llm.DEFAULT_MODEL
    enable_thinking: bool | None = None
    # 要不要密钥。'none' = **不发 Authorization 头** —— 本地 Ollama 必须用这个，
    # 它对空的 `Bearer ` 会直接拒绝。'bearer' = 照常发。
    #
    # ⚠️ 为什么是**显式字段**而不是"嗅探 base_url 里有没有 localhost"：
    # 嗅探是隐式契约，`http://192.168.1.10:11434` 这类自建端点会漏判 ——
    # 而漏判的后果是要么本地跑不了、要么把空 key 发去真端点。
    # 显式字段可测、可读、可审计。
    auth: str = "bearer"
    # 密钥从哪个环境变量取（只在 auth == "bearer" 时有意义）。
    api_key_env: str = "SILICONFLOW_API_KEY"


def _llm_str(section: dict, key: str, default: str, path: pathlib.Path) -> str:
    """`[llm]` 里的字符串字段：**缺省回落、写错报错、空串报错**。

    ⚠️ 三条判定各有实测理由（M6 T3 re-review，Important-2 / Minor）：

      · **键不存在** ⇒ 用 `default`。这是**逐项回落**（不是整段回落）：
        用户只写了他关心的那个字段，剩下的照内置默认。
      · **不是 str** ⇒ `ConfigError`。原实现是 `str(section.get(...)).strip()`，
        实测 `base_url = 123` 被**静默转成 `"123"` 而且真会拿去发请求**、
        `model = 1.5` 变 `"1.5"`——**同一个文件里 `enable_thinking` 是响亮报错的**，
        两者待遇相反；而且这种失败要等到请求真的发出去才现形。
        契约是「字符串」而不是「能转成字符串的东西」（与 `toml_int` 同一条判据）。
      · **空串（含全空白）** ⇒ `ConfigError`。原实现把它静默吞成"没写"，
        于是 `save_llm_settings(LLMSettings(base_url=""))` 写出的 `base_url = ""`
        **读回来变成内置默认** ⇒ round-trip 不保值（reviewer 实测）。
        这里选**拒**而不是"保值往返"：`base_url = ""` 是一个**发不出去的 URL**，
        与 `enable_thinking = "false"` 同类——不猜、不兜底，当场说清楚。

    ⚠️ 返回值一律 `.strip()` 过：于是**写出去的是什么，读回来就是什么**
    （`save_llm_settings` 也 strip，见那里的 docstring）。
    """
    if key not in section:
        return default
    raw = section[key]
    if not isinstance(raw, str):
        raise ConfigError(
            f"{path}: {key} 必须是**字符串**（带引号），实际是 {raw!r}"
            f"（{type(raw).__name__}）——不加引号会被解析成数字/布尔，"
            "而它会被拿去发请求，静默转一下的代价是排障时看不出问题在哪。"
        )
    value = raw.strip()
    if not value:
        raise ConfigError(
            f"{path}: {key} 不能是空串——空串会**静默回落成内置默认**，"
            "于是写出去再读回来就不是同一个值了（round-trip 不保值）。"
            f"要回到内置默认，请把整行删掉（`{key}` 的缺省语义就是回落到内置默认）。"
        )
    return value


def load_llm_settings(
    path: pathlib.Path | None = None,
    *,
    on_warn: Callable[[str], None] | None = None,
) -> LLMSettings:
    """读 LLM 端点配置。

    ⚠️ **文件不存在返回内置默认**（与 `load_persons` 同形，与 `load_config`
    的快速失败不同）：没写这份文件是**合法的**——内置默认就是硅基流动那一套。

    ⚠️ **但这一支必须出声**（`on_warn`）。默认里 `enable_thinking=None`
    ⇒ **不发该字段**，而实测那会让 Qwen3.5-35B-A3B 单批从 2.8s 涨到
    **111.5s**。「删掉一个配置文件换来 40 倍慢、且没有任何提示」正是本项目
    反复禁止的静默退化。传 `on_warn=logs.emit` 就把这句话说出来；
    测试里不传就是安静的。

    ⚠️ `enable_thinking` 的缺省判定**必须是 `not in` 而不是 falsy 检查**：
    `section.get("enable_thinking")` 对「键不存在」（`None`）与「值是
    `false`」（`False`）返回**不同的东西**，而 `if value:` 会把后者静默吞成
    "没写" ⇒ 切到自建端点后行为悄悄变了。
    """
    target = path or DEFAULT_LLM_TOML
    if not target.is_file():
        if on_warn is not None:
            on_warn(
                f"[注意] 没有 {target}，用内置默认端点（{llm.DEFAULT_BASE}）。"
                "⚠️ 该默认**不发 enable_thinking 字段**，而混合思考模型"
                "（Qwen3/Qwen3.5）不收到它时会**慢 40 倍**（实测 111.5s vs 2.8s/批）。"
                "要恢复原速，请建这个文件并写 `enable_thinking = false`。"
            )
        return LLMSettings()

    try:
        with open(target, "rb") as f:
            raw = tomllib.load(f)
    except tomllib.TOMLDecodeError as exc:
        raise ConfigError(f"{target}: TOML 语法错误——{exc}") from exc

    section = raw.get("llm")
    if section is None:
        raise ConfigError(
            f"{target}: 缺少 [llm] 段（注意是**单层**方括号；`[[llm]]` 会读成一个数组）"
        )
    if not isinstance(section, dict):
        raise ConfigError(
            f"{target}: `llm` 必须是**一张表**（`[llm]`），"
            f"实际是 {type(section).__name__}——写成 `[[llm]]`（双层方括号）时"
            "读出来的是一串表。"
        )

    base_url = _llm_str(section, "base_url", llm.DEFAULT_BASE, target)
    model = _llm_str(section, "model", llm.DEFAULT_MODEL, target)

    if "enable_thinking" not in section:
        thinking: bool | None = None
    else:
        raw_thinking = section["enable_thinking"]
        if not isinstance(raw_thinking, bool):
            raise ConfigError(
                f"{target}: enable_thinking 只能是 true / false，或**整行删掉**"
                f"（删掉 = 不发这个字段）。实际是 {raw_thinking!r}"
                f"（{type(raw_thinking).__name__}）——"
                '写成 `"false"`（带引号）是字符串，不是布尔。'
            )
        thinking = raw_thinking

    auth = _llm_str(section, "auth", "bearer", target)
    if auth not in ("bearer", "none"):
        raise ConfigError(
            f"{target}: auth 只能是 'bearer' 或 'none'，实际是 {auth!r}。\n"
            "  · 'none'   = 不发 Authorization 头（本地 Ollama 必须用这个）\n"
            "  · 'bearer' = 发 `Authorization: Bearer <key>`（云端端点）"
        )
    api_key_env = _llm_str(section, "api_key_env", "SILICONFLOW_API_KEY", target)

    return LLMSettings(
        base_url=base_url,
        model=model,
        enable_thinking=thinking,
        auth=auth,
        api_key_env=api_key_env,
    )


_LLM_HEADER = """\
# VIGIL 的 LLM 端点配置
#
# ⚠️ **本文件里永远不放密钥。** 密钥走仓库根的 `.env`（`SILICONFLOW_API_KEY`），
#    因为 `config/*.toml` 是**要进 git** 的（spec §3.2 / D11）。
#
# ⚠️ **本文件由程序整体重写**（网页「配置」页保存时会覆盖它）：
#    手工改也行，但**手工加的注释会在下一次网页保存时消失**。
#
# 改完不需要重启服务：`vigil refine` / `vigil digest` 每次运行都会重新读它。
"""


def save_llm_settings(
    settings: LLMSettings, path: pathlib.Path | None = None
) -> None:
    """整体替换写出 LLM 配置（原子）。

    ⚠️ `enable_thinking is None` 时**整行不写**——那是"不发这个字段"的
    唯一表达方式。写成 `enable_thinking = null` 的话，读回来是 Python 的
    `None` 但通过的是 **TOML 里不存在的 null 语法**（TOML 根本没有 null），
    文件直接解析不了。

    ⚠️ **写出去的 `base_url` / `model` 一定是非空且已 `strip` 的**，两条理由
    （M6 T3 re-review，Minor-3 实测）：

      · **空串会把 round-trip 弄坏**：写出 `base_url = ""`，`load_llm_settings`
        读回来是**内置默认**——存进去的和读出来的不是一回事，而界面上看不出区别。
      · **带首尾空白也会**：`load` 会 `strip`，所以 `"  https://x  "` 写出去、
        读回来是 `"https://x"`。所以写侧先 `strip` 再落盘：**写出去的文本里
        那个值，就是读回来的那个值**（这条是 `load`/`save` 之间的契约）。

    拒绝用 `ConfigError`（与 `enable_thinking` 非布尔同一条判据：**不猜、不兜底**）。

    ⚠️ **`enable_thinking` 也必须在写侧就挡非布尔**（M6 T3 deferred，T9 之前补）。
    修复前的**实测**（本机跑过，原始输出在 T9 报告里）：

        save_llm_settings(LLMSettings(base_url="https://x/v1", model="m",
                                      enable_thinking='false'))
        ⇒ 产物里是 `enable_thinking = true`，读回来 `True`

    成因就是下面那行 `'true' if settings.enable_thinking else 'false'`：
    **非空字符串是 truthy**，"关"于是被写成"开"——**静默反转**。
    而读侧是严格的（`load_llm_settings` 里那段 `isinstance(raw_thinking, bool)`），
    同一个文件里两侧待遇相反，且坏值要等下一次读才现形、现形的还是一句
    "文件里的 enable_thinking 写错了"——方向完全不对。
    ⚠️ 它不是理论问题：Web 配置界面走 JSON，`body.get("enable_thinking")`
    拿到字符串是完全可能的。
    """
    target = path or DEFAULT_LLM_TOML
    for field, value in (("base_url", settings.base_url), ("model", settings.model)):
        if not isinstance(value, str) or not value.strip():
            raise ConfigError(
                f"{field} 必须是非空字符串（空串/全空白会写出一份读回来"
                f"变成内置默认的配置——round-trip 不保值），实际是 {value!r}"
            )
    # ⚠️ `is not None` 与非 `bool` 是两个判据，顺序不能反：`None` 是**合法的**
    # 第三态（= 不发这个字段），而它同样不是 `bool`。
    # ⚠️ 挡 `0` / `1` 与挡 `'false'` 是同一件事的两半：前者是 **falsy 的非布尔**，
    # 不挡的话会被静默写成 `false`——照样不是用户的意思。
    if settings.enable_thinking is not None and not isinstance(
        settings.enable_thinking, bool
    ):
        raise ConfigError(
            "enable_thinking 只能是 true / false / 不传（不传 = 不发这个字段），"
            f"实际是 {settings.enable_thinking!r}"
            f"（{type(settings.enable_thinking).__name__}）——"
            '字符串 "false" 是 truthy，会被写成 `enable_thinking = true`（静默反转），'
            "所以这里不猜、不兜底。"
        )

    lines = [_LLM_HEADER, "", "[llm]", ""]
    lines.append(f"base_url = {toml_str(settings.base_url.strip())}")
    lines.append(f"model = {toml_str(settings.model.strip())}")
    if settings.enable_thinking is not None:
        lines.append(
            f"enable_thinking = {'true' if settings.enable_thinking else 'false'}"
        )
    lines.append("")

    text = "\n".join(lines)
    try:
        raw = tomllib.loads(text)
    except tomllib.TOMLDecodeError as exc:
        raise ConfigError(f"内部错误：为 {target} 生成的 TOML 解析不了——{exc}") from exc
    if not isinstance(raw.get("llm"), dict):
        raise ConfigError(f"内部错误：为 {target} 生成的 TOML 里没有 [llm] 表")

    atomic_write_text(target, text)


# TOML 基本字符串里**必须转义**的字符。实测坏字符集是
# **U+0000–U+0008、U+000A–U+001F（`\t` 除外）、U+007F**——原样落进引号里会让 tomllib 解析失败；
# 而 `]` / `#` / `=` / `"""` / U+2028 / C1 / NBSP / BOM 都实测 round-trip 一致，**不必动**
# （别凭直觉扩大转义集：多转义不会更安全，只会改变正文）。
TOML_ESCAPES = {
    "\\": "\\\\",
    '"': '\\"',
    "\n": "\\n",
    "\r": "\\r",
    "\t": "\\t",
}


def _read_env(name: str, env_path: pathlib.Path | None = None) -> str:
    """先读同名环境变量，再读仓库根的 .env。找不到返回空串。"""
    value = os.environ.get(name, "").strip()
    if value:
        return value

    path = env_path or DEFAULT_ENV
    if not path.is_file():
        return ""
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, raw = line.partition("=")
        if key.strip() == name:
            return raw.strip().strip("'\"")
    return ""


def load_key(env_path: pathlib.Path | None = None) -> str:
    """读取数据库密钥：优先环境变量 VIGIL_DB_KEY，其次仓库根的 .env。"""
    return _read_env("VIGIL_DB_KEY", env_path)


def load_llm_key(
    settings: LLMSettings | None = None, *, env_path: pathlib.Path | None = None
) -> str:
    """读取 LLM 密钥：优先环境变量，其次 `.env`。

    变量名从 `settings.api_key_env` 取（默认 `SILICONFLOW_API_KEY`）——
    做成配置项而不是硬编码，是因为"密钥叫什么"随端点而变：
    硅基流动、华为云 MaaS、自建网关各叫各的。

    与数据库密钥分开取名，是因为它们是完全不同的凭证——
    混用会让「换模型」变成一件危险的事。

    ⚠️ **`auth = "none"` 时调用方不该调它**（本地 Ollama 不需要密钥）。
    本函数照常返回空串 —— 判断"要不要密钥"是调用方的事（见
    `requires_llm_key`），因为只有调用方知道自己在做什么。
    """
    name = settings.api_key_env if settings is not None else "SILICONFLOW_API_KEY"
    return _read_env(name, env_path)


def requires_llm_key(settings: LLMSettings) -> bool:
    """这个端点要不要密钥。`auth = "none"`（本地 Ollama）时不要。

    抽成函数是为了**四处硬闸共用同一个判据** —— 各写一遍 `settings.auth != "none"`
    迟早会漂移，而漂移的症状是"某个命令莫名其妙要密钥"。
    """
    return settings.auth != "none"
