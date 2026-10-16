"""配置包：把三份配置合成一份可携带的 TOML（M6 T7）。

**形态**：单个文件，`[settings]` + `[[groups]]` + `[[categories]]` + `[[persons]]`
+ `[[kind_aliases]]`。

**四条边界见计划 §T7 的表格。** 其中最容易漏的是第三条——
`kind_aliases` **必须**进包：它是「改过 slug 名」这件事的后果，
属于配置的语义；不含它的话，另一个机器导入后**条目会全部悬空**。

⚠️ **本模块不做任何写入之外的副作用**，也不碰 `data/vigil.db`。
"""

from __future__ import annotations

import pathlib
import tomllib
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field

from . import overrides
from .categories import Category, CategoryError, save_categories, validate_categories
from .config import (
    Config,
    ConfigError,
    Person,
    atomic_write_text,
    save_config,
    save_persons,
    toml_int,
    toml_path,
    toml_str,
    validate_config_raw,
)

PACK_SCHEMA = 1

# 配置包只认这六个顶层键（Fix loop 1 / I2）。
#
# ⚠️ **不校验它的后果是静默的**：`raw.get("persons", [])` 对拼错的键名返回空元组
# ⇒ `import_pack` 把本机的 `persons.toml` **整体覆盖成 0 人**（`save_persons` 是
# 整体替换写），而**全程零报错**。这与 M5 那条 `_check_persons_shape`
# （`config.py`，`[[person]]` 少个 s）是**同一个形态**，
# 而那份文件里早就立过这条规矩并写了完整论证——照它的写法与理由做。
_PACK_KEYS = frozenset(
    {"schema", "settings", "groups", "categories", "persons", "kind_aliases"}
)

_HEADER = """\
# VIGIL 配置包
#
# 这一个文件里装着三份配置：监听群 / 类目 / 监视人物，外加 slug 改名映射。
# 用 `vigil config import <这个文件>` 或网页「配置」页导入。
#
# ⛔ **不含任何密钥**：`.env` 里的 SILICONFLOW_API_KEY / VIGIL_DB_KEY 永远不在这里。
# ⛔ **不含人工干预数据**（改过的分类、软删的条目）——那是**本机**的数据，
#    不是配置（spec §7）。
"""


@dataclass(frozen=True)
class Pack:
    config: Config | None
    categories: tuple[Category, ...]
    persons: tuple[Person, ...]
    aliases: tuple[tuple[str, str], ...] = ()


@dataclass(frozen=True)
class Paths:
    """导入的落点。**显式传入**，不在模块里读 `REPO_ROOT`——

    ⚠️ 与 `api.persons_path()` / `api.config_path()` 同一条判据：
    路径若在模块级冻住，测试里 `monkeypatch.setattr` 就够不着它，
    于是一次导入会**写进真仓库**，而所有测试照样绿（那个夹具会变成空守卫）。
    """

    groups: pathlib.Path
    categories: pathlib.Path
    persons: pathlib.Path


@dataclass
class ImportReport:
    groups: int = 0
    categories: int = 0
    persons: int = 0
    aliases: int = 0
    # ⚠️ **「真的跳过了本机 [settings]」才为 True**（Fix loop 1 / I4）：从前的写法是
    # `ImportReport(settings_skipped=keep_settings)` —— 只要用户勾了那个框就是 True，
    # 哪怕回落分支**把包里的（别台机器的）settings 真写了进去**。那是一句
    # **假话**：调用方（前端/CLI）拿它当"本机路径保住了"，而事实相反。
    settings_skipped: bool = False
    # ⚠️ **要说的话单独留一份**（I4）：CLI 靠 `on_progress` 打印，而 API 一侧
    # 没有 stdout（`on_progress` 从前只能塞空 lambda）⇒ 那句话必须有**结构化**的
    # 出口，才能回到响应体里让前端显示。
    warnings: list[str] = field(default_factory=list)


def build(
    *,
    config: Config,
    cats: Sequence[Category],
    persons: Sequence[Person],
    aliases: Sequence[tuple[str, str]] | dict[str, str] = (),
) -> str:
    """渲染成配置包的文本。**不落盘**（`write_pack` 才落）。"""
    lines = [_HEADER, "", f"schema = {PACK_SCHEMA}", ""]

    # ⚠️ `[settings]` 写在所有 `[[…]]` 之前，与现有 `groups.toml` 的段序一致、
    # 便于人读与 diff（约定，不是不变量）。
    # ⚠️ **不许**在这里写「不改顺序会嵌套成 `groups[-1].settings`」那类理由
    # ——那是**实测不成立**的（计划勘误 E4 / M6 T2）：TOML 的 `[header]` 是
    # 从根算的**绝对路径**，`[settings]` 写在哪都在顶层；会嵌套的只有
    # `[groups.settings]` 那种显式带前缀的写法。本项目最贵的一课是**错的论证
    # 比没有论证更危险**：一个编出来的技术后果会让后来者以为自己理解了，
    # 于是不去验证。
    lines += [
        "[settings]",
        f"source_dir = {toml_path(config.source_dir)}",
        f"output_db = {toml_path(config.output_db)}",
        "",
    ]
    for g in config.groups:
        lines += [
            "[[groups]]",
            f"id = {toml_int(g.id, '群号')}",
            f"name = {toml_str(g.name)}",
            f"enabled = {'true' if g.enabled else 'false'}",
            f"tier = {toml_str(g.tier)}",
            "",
        ]
    for c in cats:
        lines += [
            "[[categories]]",
            f"slug = {toml_str(c.slug)}",
            f"label = {toml_str(c.label)}",
        ]
        if c.icon:
            lines.append(f"icon = {toml_str(c.icon)}")
        lines += [f"desc = {toml_str(c.desc)}", ""]
    for p in persons:
        lines += [
            "[[persons]]",
            f"uin = {toml_int(p.uin, 'uin')}",
            f"label = {toml_str(p.label)}",
        ]
        if p.note:
            lines.append(f"note = {toml_str(p.note)}")
        lines.append("")
    items = aliases.items() if isinstance(aliases, dict) else aliases
    for old, new in items:
        lines += [
            "[[kind_aliases]]",
            f"old_kind = {toml_str(old)}",
            f"new_kind = {toml_str(new)}",
            "",
        ]
    return "\n".join(lines)


def write_pack(
    path: pathlib.Path,
    *,
    config: Config,
    cats: Sequence[Category],
    persons: Sequence[Person],
    aliases: Sequence[tuple[str, str]] | dict[str, str] = (),
) -> pathlib.Path:
    text = build(config=config, cats=cats, persons=persons, aliases=aliases)
    atomic_write_text(path, text)
    return path


def parse(text: str, path: pathlib.Path) -> Pack:
    """解析并**完整校验**一个配置包。任何问题都抛 `ConfigError`。

    ⚠️ 校验的是**整包**，而且是**在任何写入之前**——`import_pack` 靠这一点
    才能做到「全有或全无」。半新半旧的配置是最坏的结果：用户看到「导入失败」，
    以为什么都没发生，实际已经写进去了一半。
    """
    try:
        raw = tomllib.loads(text)
    except tomllib.TOMLDecodeError as exc:
        raise ConfigError(f"{path}: TOML 语法错误——{exc}") from exc

    # ⚠️ **顶层键集合**（Fix loop 1 / I2，理由见 `_PACK_KEYS` 那段）。放在版本闸
    # **之前**：`schema` 自己拼错（`schem = 1`）时，先说"这个键我不认得"比
    # 报"版本不符（读到 None）"有用得多——后者会把人引去查版本，而真相是笔误。
    unknown = sorted(k for k in raw if k not in _PACK_KEYS)
    if unknown:
        raise ConfigError(
            f"{path}: 配置包里有不认识的顶层键：{unknown}（只认 "
            f"{sorted(_PACK_KEYS)}）。最常见的形状是 `[[person]]`（少写一个 s）"
            "——写错的段落会被**整体忽略**：读出来是一份空名单，而导入是"
            "**整体覆盖**写，本机原有的人物会无声消失。"
            "（若这份包确实来自更新版的 VIGIL，请先升级本程序。）"
        )

    if raw.get("schema") != PACK_SCHEMA:
        raise ConfigError(
            f"{path}: 配置包版本不符（读到 {raw.get('schema')!r}，"
            f"本程序认 {PACK_SCHEMA}）。这份包可能是更新版的 VIGIL 导出的——"
            "形状变了就必须拒绝，静默错读会让导入的配置**看起来对了、其实错了**。"
        )

    # ⚠️ 群清单可以为空（"这份包只带类目"是合法的），但**真导入时**会撞
    # `validate_config_raw` 的「一个群都没配」——那是 `save_config` 的判据，
    # 与"包里有没有群"是两回事。这里用一份**宽容的**校验拿到分组数据。
    groups_raw = raw.get("groups", [])
    cfg: Config | None = None
    if raw.get("settings") is not None:
        try:
            cfg = validate_config_raw(raw, path)
        except ConfigError as exc:
            # 包里的 source_dir 多半指向**导出那台机器**的路径。这是**预期内**的，
            # 所以把话说明白：要么在本机建好那个目录，要么用 `--keep-settings`。
            raise ConfigError(
                f"{path}: [settings] 段校验不过——{exc}\n"
                "  ⚠️ 若这份包来自另一台机器，它的 source_dir 是**那边**的路径。"
                "用 `--keep-settings`（网页上勾「保留本机路径」）只导入群/类目/人物。"
            ) from exc
    elif groups_raw:
        raise ConfigError(
            f"{path}: 有 [[groups]] 却没有 [settings]——群配置没法独立存在"
            "（它需要 source_dir 才知道去哪儿读消息）"
        )

    try:
        cats = validate_categories(raw.get("categories"), path)
    except CategoryError as exc:
        # ⚠️ 转成 `ConfigError`（Fix loop 1 / I1）：本函数的契约是「**任何问题都抛
        # `ConfigError`**」，而两个调用方（`api_import_pack` → 400、
        # `cmd_config_import` → 一行 `[配置错误]`）只捕它。不转的话
        # `CategoryError` 会以 **API 500 / CLI 裸 traceback** 收场
        # ——那正是 C6 的同形缺陷（groups 已经 400 了、categories 没跟上）。
        raise ConfigError(f"{path}: 类目段校验不过——{exc}") from exc

    persons: list[Person] = []
    seen: set[int] = set()
    for entry in _array_section(raw, "persons", path):
        if not isinstance(entry, dict):
            raise ConfigError(f"{path}: 每个 [[persons]] 段都必须是键值表")
        uin = entry.get("uin")
        if isinstance(uin, bool) or not isinstance(uin, int) or uin <= 0:
            raise ConfigError(f"{path}: 人物 uin 必须为正整数，实际是 {uin!r}")
        if uin in seen:
            raise ConfigError(f"{path}: uin {uin} 重复出现")
        label = str(entry.get("label", "")).strip()
        if not label:
            raise ConfigError(f"{path}: uin {uin} 缺 label")
        seen.add(uin)
        persons.append(
            Person(uin=uin, label=label, note=str(entry.get("note", "")).strip())
        )

    aliases: list[tuple[str, str]] = []
    seen_old: set[str] = set()
    for entry in _array_section(raw, "kind_aliases", path):
        if not isinstance(entry, dict):
            raise ConfigError(f"{path}: 每个 [[kind_aliases]] 段都必须是键值表")
        old = str(entry.get("old_kind", "")).strip()
        new = str(entry.get("new_kind", "")).strip()
        if not old or not new:
            raise ConfigError(f"{path}: kind_aliases 的两端都不能为空")
        if old == new:
            raise ConfigError(f"{path}: kind_aliases 的两端相同（{old}）")
        if old in seen_old:
            raise ConfigError(f"{path}: 旧类目 {old} 出现了两次")
        seen_old.add(old)
        aliases.append((old, new))

    # ⚠️ **环是可静态检测的**（Fix loop 1 / I1）：不必真去写一次表才知道
    # （写一次还更糟——那会先把半张表写进去）。不检的后果是 `import_pack` 走到
    # `set_kind_alias` 时抛 `OverlayError` ⇒ API 500 / CLI 裸 traceback，
    # **与本文件反复在修的 C6 是同一个形状**：外部文件的形状问题却以服务端异常收场。
    chain = dict(aliases)
    for start in chain:
        seen_kinds = {start}
        cur = chain[start]
        while cur in chain:
            if cur in seen_kinds:
                raise ConfigError(
                    f"{path}: kind_aliases 成环（{start} → … → {cur}）——"
                    "环上的旧名永远落不到一个**现存**类目上（spec §4.2 ④ 要禁的状态），"
                    "而写入侧会以 `OverlayError` 拒绝它。请在包里删掉环上的一条。"
                )
            seen_kinds.add(cur)
            cur = chain[cur]

    return Pack(
        config=cfg,
        categories=cats,
        persons=tuple(persons),
        aliases=tuple(aliases),
    )


def _fallback_warning(why: str, source_dir: pathlib.Path, local: pathlib.Path) -> str:
    """`--keep-settings` 保不住本机设置时那句话（Fix loop 1 / FIX 1 + I4）。

    ⚠️ 抽成函数只为一件事：**同一句话要走两个通道**（`on_progress` 给人看、
    `ImportReport.warnings` 给 API 用），两处各写一遍必然漂移。

    ⚠️ 两句话缺一不可：**指名**（`source_dir = <那个路径>`）与**出路**
    （先修好本机那份再导一次）。只说"用了包里的"等于让用户自己去猜是哪台机器的
    路径；只说路径不给出路，等于把问题丢回给他。
    """
    return (
        f"[导入] ⚠️ 本机那份 groups.toml 读不出来（{why}）——"
        "无法保留本机路径，已**整份采用包里**的 [settings]。"
        f"其中包括 source_dir = {source_dir}"
        "（**那是导出这份包的那台机器**的路径，本机多半没有它）。"
        f"如果你要用本机自己的路径：先手工把 {local} 修好"
        "（或用 `vigil groups` 建一份），再导入一次。"
    )


def _fmt_aliases(table: Mapping[str, str]) -> str:
    """把别名表印成一行给人看的东西（`a→b, c→d`）。空表印 `（空）`。"""
    if not table:
        return "（空）"
    return "，".join(f"{k}→{v}" for k, v in sorted(table.items()))


def _merge_aliases(
    local: Mapping[str, str],
    wanted: Sequence[tuple[str, str]],
) -> dict[str, str]:
    """把包里的别名逐条施加到**本机现有的表**上，返回**最终表**（纯函数，不碰数据库）。

    ⚠️ **语义必须与 `overrides.set_kind_alias` 逐步一致**（跟到底 + 链式传播）：
      · `final` = 从 `new_kind` 沿表一路跟到的终点；
      · 表里所有**指向 `old_kind`** 的行一起改指 `final`（链式传播）；
      · 再记下 `old_kind → final`。
    `test_merge_aliases_matches_the_write_path` 把两条路径并排跑，逐字节比——
    **两份实现一旦漂移，症状是"静态检查放行、写进去却炸"**，而那正是本轮
    要修的那类缺陷（只在罕见形状下现形）。

    ⚠️ **合起来会成环 ⇒ `ConfigError`**（Fix loop 2 / FIX 1a）：这是 reviewer 实测
    出来的场景，也是我上一轮**被推翻的那条论证**——本机那条别名往往**就是上一次
    导入写进去的**，所以「两份各自 parse 通过的正常包」先后导入就会撞车。
    ⇒ 消息里**两边的映射都要印出来**：只说"冲突了"等于把排查丢回给用户。

    ⚠️ 它必须在**任何写入之前**跑完（`import_pack` 就是这么用的）：那是
    「全有或全无」在别名这一段上的机械形式。
    """
    merged = dict(local)
    for old, new in wanted:
        final = new
        seen = {old}
        while final in merged:
            if merged[final] in seen:
                raise ConfigError(
                    "导入的别名映射与**本机现有的**冲突（合并起来会成环）："
                    f"本机现有 {_fmt_aliases(local)}；包里要写 "
                    f"{_fmt_aliases(dict(wanted))}；冲突点是 {old} → {new} 这条"
                    f"会绕回 {merged[final]}。"
                    "⚠️ 本机那条多半是**上一次导入**（或一次改名）写进去的——"
                    "两份各自合法的包先后导入就会撞上这一条。"
                    "要导入这份包：先在本机把冲突的那条别名处理掉"
                    "（网页「类目」页再改一次名，或手工修 `data/overrides.db` 的 "
                    "`kind_alias` 表），再导一次。"
                )
            seen.add(final)
            final = merged[final]
        merged = {k: (final if v == old else v) for k, v in merged.items()}
        merged[old] = final
    return merged


def _array_section(raw: dict, key: str, path: pathlib.Path) -> list:
    """取一个 `[[…]]` 段：**段不在 ⇒ 空列表**；**是别的形状 ⇒ 响亮**（I2）。

    ⚠️ 与 `config._check_persons_shape` 同形、同理由：`[persons]`（单层方括号）
    与 `[[persons]]` 只差一层括号，是极常见的笔误，而解析结果一个是 dict、
    一个是 list。不挡的话 `for entry in …` 会去迭代 dict 的**键**（字符串），
    下一行以一个**看不懂的异常**收场——那正是"这个文件写错了"最不该有的表现。

    ⚠️ 但**段不在仍然是合法的**：`build` 在名单为空时**根本不写那一段**
    （"这份包不带人物"是正常产物），所以这一支返回空列表、不报错。
    对应地，「段为空」与「段不在」在今天**语义相同**（都是"没有"）——
    真正要分开的是**形状**，不是长度。

    ⚠️ **这道守卫买到的是「话说得对」，不是「400 vs 500」**（Fix loop 2 / FIX 3，
    reviewer 指出——**不许写成它没有的作用**）：去掉它，下面
    `for entry in _array_section(...)` 会去迭代 dict 的**键**（字符串），
    逐条那句 `isinstance(entry, dict)` 照样抛 `ConfigError` ⇒ 端点**仍然是 400**。
    差别只在**文案**：一句"`persons` 必须是数组表（`[[persons]]`）"，
    对上一句指向**错误方向**的"每个 `[[persons]]` 段都必须是键值表"
    （用户会去查那些条目，而真正写错的是段头那一层方括号）。
    ⇒ 它的判据也只能是 message（`test_parse_rejects_a_single_bracket_section`
    的红正落在正则上；端点那一半由 `test_api_import_pack_returns_400_for_a_
    single_bracket_section` 单独钉住）。
    """
    section = raw.get(key, [])
    if not isinstance(section, list):
        raise ConfigError(
            f"{path}: `{key}` 必须是**数组表**（`[[{key}]]`，两层方括号），"
            f"实际是 {type(section).__name__}——写成单层方括号时读出来的是"
            "一张表而不是一串条目，后面的逐条校验会以一个看不懂的异常收场。"
        )
    return section


def _snapshot_categories_before_import(path: pathlib.Path) -> bool:
    """把 `path` **此刻**的原文 + **此刻**的别名表存成一格撤销快照。返回是否拍了。

    ⚠️ **为什么必须由导入来拍**（M6 终审 F1，Important）：`import_pack` **整体
    重写** `paths.categories`，而 `/api/categories/undo` 的契约是「把
    `categories.toml` 回滚到**上一次程序写它之前**的样子」——**导入也是「程序写它」**。
    不拍的话，撤销栈里那一格还是更早的那次改动（比如一次改名），于是

        改名 → 导入一份只带类目的包 → 点「撤销上一次类目改动」

    会把类目退到**改名之前**，而包里的群 / 人物 / 别名**原样留着**：一次
    **混合状态**，而成功路径**一个字都不说**（界面上那句话与它实际做的事对不上）。

    ⚠️ **三条位置约束都是承重的**（调用点在 `import_pack` 里）：
      · **文件被重写之前** —— 快照要的是"覆盖前的原文"，拍晚了存的就是新内容；
      · **别名表被改之前** —— 快照存的是「文件原文 + 别名表」这一**对**，
        两者必须来自**同一时刻**（拍在别名写之后 ⇒ 撤销会把「旧文件 + 新别名表」
        装回去 ⇒ `old_kind` 又变成现存 slug ⇒ 不变量破；见
        `overrides.save_config_snapshot` 与 T5 fix loop 2 的 FIX 1/C-1）；
      · **`ALIAS_LOCK` 之内** —— 否则对账（`reconcile_if_stale`，挂在每一次查询上）
        能插进"拍了快照、还没写文件"的那一瞬里剪枝。

    ⚠️ **调用方必须先 `overrides.ensure_schema()`**：`overrides.connect()` **不建表**，
    而 `save_config_snapshot` 内部要读 `kind_alias` 去填 `aliases` 列。终审的变异 D
    实测过这条缝有多险：把快照插进 `import_pack` 却漏了 `ensure_schema`，
    一次「不带别名的包」的导入会在**三条无关用例**上炸出
    `sqlite3.OperationalError: no such table: kind_alias`——**红在异常上**，
    而没有任何一条用例是因为"快照真的拍错了"而红的。

    ⚠️ **文件不在 ⇒ 不拍**（返回 `False`）：没有文件就没有"它被覆盖之前的样子"
    可回滚。这一支是**真实**的（出口判据 4 就是往一个全新的根里导入，
    那个根下还没有 `categories.toml`），所以它**不许抛**。代价如实记：
    这一格的缺失意味着撤销栈里更早的那一格会露头（"多退一格"）。
    """
    if not path.is_file():
        return False
    ov = overrides.connect()
    try:
        overrides.save_config_snapshot(ov, body=path.read_text(encoding="utf-8"))
    finally:
        ov.close()
    return True


def _read_local_config(path: pathlib.Path) -> tuple[Config | None, str]:
    """读本机那份 `groups.toml`：返回 `(配置或 None, 读不出来的原因)`。

    ⚠️ 两件各不相同的事在这里被归成同一支，理由都是"此刻没法保留本机设置"，
    但**原因必须分别带出去**（下文 `import_pack` 要把那句话说出来）：
      · **文件不在** —— 第一次导入，没有任何"本机设置"可保留；
      · **文件坏了**（语法错 / 校验不过）—— 里面的 source_dir 取不出来。
        ⚠️ `TOMLDecodeError` 必须一并接住：它继承自 **ValueError** 而不是
        RuntimeError，只捕 `ConfigError` 的话一次手改笔误会让导入以
        **裸 traceback** 收场（这正是 `load_config` 从 M6 T2 起要包它的同一条理由）。
    """
    if not path.is_file():
        return None, f"文件不存在（{path}）"
    try:
        with open(path, "rb") as f:
            return validate_config_raw(tomllib.load(f), path), ""
    except (ConfigError, tomllib.TOMLDecodeError) as exc:
        return None, str(exc)


def import_pack(
    pack: Pack,
    *,
    keep_settings: bool,
    paths: Paths,
    on_progress=print,
) -> ImportReport:
    """把包写进 `paths`。**全有或全无**：校验在 `parse` 里已经跑完。

    ⚠️ **写入顺序有意义**：先写 `kind_aliases`，再写配置。
    反过来的话，中间崩溃会留下「配置里已经没有 `life` 了、但别名还没建」
    ⇒ 条目永久悬空。这与 `api.api_update_category` 的顺序是**同一条理由**。

    ⚠️ **再往前还有一步：拍快照**（M6 终审 F1）。`categories.toml` 会被这一步
    整体重写，而它是撤销栈（`/api/categories/undo`）唯一保护的**那一份文件**
    ——不拍的话，「撤销上一次类目改动」会**跳过这次导入**、多退一格，把类目退到
    更早那次改动之前，而群/人物/别名留在导入后的状态里。见
    `_snapshot_categories_before_import` 的完整论证。

    ⚠️ **整段取 `overrides.ALIAS_LOCK`**（T5 的不变量）：别名写完、类目文件
    还没写的那一瞬间，若 `reconcile_if_stale`（挂在**每一次查询**上）跑进来，
    它会拿**磁盘上那份还是旧的**类目表去剪枝 ⇒ 刚写进去的别名被判成
    "目标不存在"剪掉 ⇒ 条目永久悬空。写端点取这把锁覆盖
    `set_kind_alias → 写文件` 整段，与 `api_update_category` 完全同形。
    """
    # ⚠️ **不许写成 `ImportReport(settings_skipped=keep_settings)`**（I4）：
    # 那是"用户勾了框"而不是"本机设置真的留住了"——回落分支里两个说法相反。
    # 这个字段只在**真的合并保留**的那一支置位（见下面 ⑤）。
    report = ImportReport()

    # ⓪ 快照 → ① 别名 → ② 类目 → ③ 剪枝：四段在同一把锁里（见 docstring）。
    with overrides.ALIAS_LOCK:
        # ⚠️ `ensure_schema()` 不能省：`overrides.connect()` **不建表**，
        # 而 `vigil config import` 可能是这台机器上第一条碰 overlay 的命令
        # （没有它就是一个 `no such table: kind_alias` 的裸 traceback）。
        # ⚠️ **F1 起它必须排在最前面**：下面的快照要读 `kind_alias`，而包里可能
        # 一条 `[[kind_aliases]]` 都没有——从前的写法是"只在 `pack.aliases` 非空时
        # 建表"，把快照插在写文件之前就会红在 `no such table` 上（终审变异 D 的实测）。
        overrides.ensure_schema()

        # ⓪ 快照（M6 终审 F1）：导入**整体重写** `categories.toml` ⇒ 撤销栈必须
        # 先记下"覆盖前的那一份"，否则一次「改名 → 导入 → 撤销」会退到改名之前
        # 而包里的群/人物/别名留着（一次混合状态）。完整论证见函数 docstring。
        #
        # ⚠️ 与 `api_update_category` 的「空操作不拍快照」**刻意不同**：那边判等
        # 只需要比较两个 `Category`；这边是整份重写 + 别名合并 + 群/人物，
        # 判等要把四种改动都算进去才准。判错的代价**不对称**——漏拍就是 F1 本身
        # （用户被送到一个说不通的现场），多拍一格只是"撤销看着没反应"。
        # ⇒ 宁可多拍，不许漏拍。
        _snapshot_categories_before_import(paths.categories)

        if pack.aliases:
            ov = overrides.connect()
            try:
                # ⚠️ **先整体算出最终表、再整体落**（Fix loop 2 / FIX 1a+1b）。
                # 从前的写法是**逐条 `set_kind_alias`**，而那个函数**每写一行就
                # 自己 commit** ⇒ 中途抛错时前面写的行留在库里：
                # reviewer 实测「`{'b':'a','x':'y'}` 落库、而类目文件未动」——
                # **「全有或全无」在别名这一段是假的**。
                # ⇒ 现在：① `_merge_aliases` **纯函数**算出合并结果，冲突
                #    （跨包成环）在**写任何东西之前**以 `ConfigError` 响亮拒绝；
                # ② 落库走**已有的** `replace_kind_aliases`（T5 的整表写路径，
                #    DELETE + INSERT + **一次** commit）⇒ 要么全落、要么全不落。
                # ⚠️ 代价如实记：`created_at` 会被刷成写入时刻——那是**留痕**字段，
                # 没有任何读路径依赖它（`replace_kind_aliases` 的 docstring 记过）。
                merged = _merge_aliases(overrides.kind_aliases(ov), pack.aliases)
                overrides.replace_kind_aliases(ov, merged)
            finally:
                ov.close()
            report.aliases = len(pack.aliases)

        # ② 类目
        save_categories(pack.categories, paths.categories)
        report.categories = len(pack.categories)

        # ③ 剪枝：维持 T5 的不变量「表里不许有 `old_kind ∈ 现存 slug` 的行」。
        # ⚠️ **不能只在"包里有别名"时才剪**：导入会把**某个 slug 带回来**
        # （本机把 `life` 改名成 `life_v2` 之后，导入一份含 `life` 的包）——
        # 那一刻表里那条 `life→life_v2` 就变成"顶着现存 slug"的毒行，
        # `store._EFFECTIVE_KIND` 里无条件的 `ka2` 会把用户手工选的合法类目顶掉
        # （`prune_kind_aliases` 的 docstring 里有实测反例）。
        overrides.ensure_schema()
        ov = overrides.connect()
        try:
            overrides.prune_kind_aliases(
                ov, live_slugs=[c.slug for c in pack.categories]
            )
        finally:
            ov.close()

    # ④ 人物
    save_persons(pack.persons, paths.persons)
    report.persons = len(pack.persons)

    # ⑤ 群（+ settings）
    if pack.config is not None and not keep_settings:
        save_config(pack.config, paths.groups)
        report.groups = len(pack.config.groups)
    elif pack.config is not None:
        # ⚠️ `keep_settings` 只保留 **[settings] 段**，群清单仍然来自包。
        # 「保留本机路径」被理解成「整个 groups.toml 都不动」是一个很自然的误读，
        # 而那样导入一份包就什么都不发生了——用户会以为导入坏了。
        #
        # ⚠️ 本机那份可能还不存在（第一次导入）、也可能坏了，那时**没法保留**，
        # 只能整份用包里的——但那又需要包的 source_dir 在本机存在。
        # 所以这一支要么成功、要么由 `validate_config_raw` 响亮报错。
        local = paths.groups
        current, why = _read_local_config(local)
        if current is not None:
            merged = Config(
                source_dir=current.source_dir,
                output_db=current.output_db,
                groups=pack.config.groups,
            )
            save_config(merged, local)
            report.groups = len(merged.groups)
            # ⚠️ **只有走到这里才算"跳过了"**（I4）：上面那一句真的让本机的
            # source_dir / output_db 原样留着了。写成"用户勾了就为 True"
            # （`ImportReport(settings_skipped=keep_settings)`）在下面那一支
            # 是一句**假话**——那里恰恰把外机的 settings 写了进去。
            report.settings_skipped = True
        if report.groups == 0:
            save_config(pack.config, local)
            report.groups = len(pack.config.groups)
            # ⚠️ **这一支必须出声，而且要点名那个路径**（Fix loop 1 / FIX 1，
            # controller 裁定）。被否掉的写法是"静默采用包里的 [settings]"：
            # 用户勾了「保留本机路径」，拿到的却是**别台机器的 source_dir**，
            # 全程零提示——他会在下一次 `vigil export` 撞上「QQ 数据库目录不存在」，
            # 而**看不出为什么**（本项目的立场：宁可响亮，不许静默退化）。
            #
            # ⚠️ **但不能改成"拒绝导入"**（另一条更容易走错的路）：包里的
            # 群/类目/人物仍然是用户想要的。**要的是把话说出来，不是把事拦住**
            # ——所以上面那句 `save_config` 照写，只是把真相贴在它旁边。
            #
            # ⚠️ 两句话缺一不可：**指名**（`source_dir = <那个路径>`）与**出路**
            # （先修好本机那份再导一次）。只说"用了包里的"等于让用户自己去猜
            # 是哪台机器的路径；只说路径不给出路，等于把问题丢回给他。
            warning = _fallback_warning(why, pack.config.source_dir, local)
            on_progress(warning)
            # ⚠️ **两个通道各留一份**（I4）：CLI 打印它，API 没有 stdout，
            # 只能靠 `report.warnings` 把它带出 `create_app` 的闭包。
            report.warnings.append(warning)

    # ⚠️ 摘要行那句「保留本机 [settings]」的判据是 **`report.settings_skipped`**，
    # **不是**用户勾没勾框（`keep_settings`）——Fix loop 2 / FIX 2。
    # 回落分支（本机那份读不出来）恰恰**没**保留本机设置，而它同样带着
    # `--keep-settings` ⇒ 用 `keep_settings` 判就会在摘要里说一句**假话**，
    # 而那句话还会作为 `notes` 回到 API 响应里给前端显示。
    on_progress(
        f"[导入] 群 {report.groups} / 类目 {report.categories} / "
        f"人物 {report.persons} / 改名 {report.aliases}"
        + ("（保留本机 [settings]）" if report.settings_skipped else "")
    )
    return report
