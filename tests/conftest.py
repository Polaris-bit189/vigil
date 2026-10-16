"""共享测试夹具。

原则：测试不碰真实的 QQ 加密库、不碰 data/vigil.db、不联网。
全部用内存 SQLite 与构造出来的样本数据。
"""

from __future__ import annotations

import hashlib
import pathlib
import sqlite3
import sys

import pytest

REPO_ROOT = pathlib.Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

# ⚠️ 这个 import 是**安全**的：`vigil.config` 在模块级只算路径常量
# （`REPO_ROOT` / `LOCK_PATH`），不读文件也不建目录——真正读 `.env`、读
# `groups.toml` 的都在函数里。所以 import 它不会碰到真实的 `data/`。
from vigil import lock  # noqa: E402

# 同理安全：`vigil.overrides` 模块级只算 `OVERRIDES_DB` 路径常量与几张 DDL 字符串，
# **不建目录、不连库**（真正建库的 `ensure_schema()` 在函数里）。所以 import 它不会
# 碰真实的 `data/`。
from vigil import overrides  # noqa: E402

# 同理安全：`vigil.config` 模块级只算路径常量（`REPO_ROOT` / `DEFAULT_*`），
# 读文件全在函数里（`load_llm_settings` / `load_config` 都是被调用时才 open）。
from vigil import config  # noqa: E402


# 隔离根里那份最小类目表（M6 T5）。形状与内容都**自带**，不抄仓库里真实的
# `config/categories.toml`：抄一份等于把「测试红绿取决于用户的类目配置」留着，
# 而 M6 的整个卖点就是把那份文件交给用户在网页上编辑。
#
# ⚠️ **图标也照抄出厂文件**：`digest.render_markdown` 的章节标题是
# `{icon} {label}`，`tests/test_digest.py` 直接断言 `📚 学业` 那一节存在——
# 少了 icon 就会红（实测：740 passed / 1 failed）。
#
# ⚠️ 七个 slug 与**顺序**（`notice` 在第一个）是 `tests/test_api.py` 里
# `test_categories_carry_counts` 断言的东西；`academic` 的中文名是
# `test_items_returns_the_frozen_field_set` 断言 `kind_label == "学业"` 要的。
# 改这里之前先看那两条——它们会红，而且是**响亮**地红。
_ISOLATED_CATEGORIES_TOML = """\
[[categories]]
slug = "notice"
icon = "📋"
label = "通知公告"
desc = "隔离用类目：通知公告"

[[categories]]
slug = "academic"
icon = "📚"
label = "学业"
desc = "隔离用类目：学业"

[[categories]]
slug = "activity"
icon = "🎤"
label = "活动"
desc = "隔离用类目：活动"

[[categories]]
slug = "life"
icon = "🏠"
label = "生活"
desc = "隔离用类目：生活"

[[categories]]
slug = "secondhand"
icon = "🛒"
label = "二手"
desc = "隔离用类目：二手"

[[categories]]
slug = "lostfound"
icon = "🔍"
label = "失物招领"
desc = "隔离用类目：失物招领"

[[categories]]
slug = "job"
icon = "💼"
label = "兼职招聘"
desc = "隔离用类目：兼职招聘"
"""


# ── 默认路径 / REPO_ROOT 的隔离（autouse）─────────────────────────────
#
# ⚠️ **约定（M6 T4 复审 ⚠️-2，对 T5–T9 都成立）：测试里凡是要落到临时根的
#    路径，一律写绝对路径（`pathlib.Path(...).as_posix()`）**——不许在临时配置
#    里写相对路径、指望被测代码去拼 `REPO_ROOT`。三条理由：
#
#      · `config.REPO_ROOT` 与 `api.REPO_ROOT` 是**两个模块全局**
#        （`config.py` / `api.py` 各一个），下面的夹具只挪得动其中一个；
#      · `validate_config_raw` 对**相对** `output_db` 的解析基准是
#        `config.REPO_ROOT`。⚠️ **它是模块全局，`monkeypatch` 够得着**
#        （M6 T4 复审实测：`setattr(config, "REPO_ROOT", tmp)` 之后，相对
#        `output_db` 立刻挂到 tmp 下）——**够不着的不是在 import 期算好的
#        `REPO_ROOT` 本身，而是由它派生出来的那几个常量**
#        （`DEFAULT_PERSONS` / `DEFAULT_CATEGORIES` / `DEFAULT_LLM_TOML` /
#        `LOCK_PATH`）：它们的值里**已经烙进了**那个根，要挪就得逐个
#        `monkeypatch`（这就是下面四道 `_isolate_*` 夹具存在的原因）。
#        ⚠️ 这里纠正的是一句**错的论证**（M6 终审 #13）：结论（路径写绝对）
#        仍成立，但按错的理由去理解它，下一个人的判断会在别处错。
#      · 于是「只挪了 `api.REPO_ROOT`，而临时 `groups.toml` 里写的是相对
#        `output_db`」会把产物**指回真实仓库的 `data/`**：快照路径
#        （`groups_snapshot.default_path` = `output_db.parent / …`）正是从它推出来的。
#        而外部断言全都在看 tmp 根 ⇒ **空守卫**（评审已复现这条链）。
#
#   同理一律 `.as_posix()`：反斜杠在 TOML 基本字符串里是转义字符。


@pytest.fixture(autouse=True)
def _isolate_categories_toml_path(tmp_path_factory, monkeypatch):
    """把 `categories.DEFAULT_CATEGORIES` 挪到一个**自带内容**的 tmp 文件。

    ⚠️ **M6 T5 Fix loop 2 / FIX 2 起它必须被隔离**：`store._ensure_overlay`
    （**每一次查询入口**都走它）会调 `overrides.reconcile_if_stale()`，
    而那个函数按 mtime 读这个常量所指的文件。不隔离的后果是两条：

      ① **环境相关的绿**：对账读的是**仓库里真实的** `config/categories.toml`，
         于是测试的行为取决于用户的类目配置（正是 E6 那条判据点名要禁的）；
      ② 更要命：它**会剪** overlay 里的别名——测试自己造的 `life→daily` 之类
         会被当成"目标不在现存 slug 里"删掉，一批与配置毫无关系的用例会莫名变红。

    ⚠️ 内容**自带**（`_ISOLATED_CATEGORIES_TOML`，与 `_isolate_api_repo_root` 用的是
    同一份），**不是**指向不存在的路径：`digest` / `refine` / `repass` 都会
    在内部 `load_categories()`，那是**硬依赖**，指到不存在的路径会让它们抛
    `CategoryError`。（这与 `_isolate_llm_toml_path` 刻意相反——那份的读侧有默认值。）

    ⚠️ 还要把 `overrides._LAST_RECONCILE` **预置成这个文件当前的状态**（闸门关着）：
    否则每个用例的第一次查询都会对账一次，把测试自己造的别名剪掉。
    预置的语义就是「这份文件我看过了、它没变」——与生产里"进程启动后的第一次
    查询对账一次"**等价地安全**（那一次也是对着**当时**的表剪：表是空的就是零剪）。
    ⚠️ 想验对账本身的用例**改这个文件**即可：mtime/size 一变闸门就开
    （`tests/test_store.py` 的 FIX 2 用例就是这么做的）。
    """
    from vigil import categories as _cats

    path = tmp_path_factory.mktemp("cats") / "categories.toml"
    path.write_text(_ISOLATED_CATEGORIES_TOML, encoding="utf-8")
    monkeypatch.setattr(_cats, "DEFAULT_CATEGORIES", path)
    st = path.stat()
    monkeypatch.setattr(overrides, "_LAST_RECONCILE", (st.st_mtime_ns, st.st_size))
    # ⚠️ **它的兄弟变量必须一起隔离**（M6 终审 #27）：`_RECONCILE_FAILURES` 与
    # `_LAST_RECONCILE` 是同一族的**模块级状态**，而上面那一行刚为它的兄弟做过
    # 一模一样的事——漏掉兄弟就是**同形状的第二个入口**，而失败形态是
    # 「与环境相关的假红」：某个用例让对账**连续**失败到阈值（3）之后计数留在
    # 模块上，下一个用例只该看到一条 warning，却拿到 `_log.error`——而那条升级
    # 分支有自己的用例（`test_repeated_reconcile_failures_escalate_to_error`），
    # 它会因此变成**取决于执行顺序**的红/绿。
    # ⚠️ 那个用例自己也会 monkeypatch 这个变量（它的第一段要断言"前两次只许
    # warning"）——两处**不冲突**（各设各的），这里加的是**兜底**：
    # 兜住的是"别的用例把计数留在模块上"那条路。
    monkeypatch.setattr(overrides, "_RECONCILE_FAILURES", 0)


@pytest.fixture
def shipped_categories_toml() -> pathlib.Path:
    """**出厂那份** `config/categories.toml` 的真实路径。

    ⚠️ 只有**明确要断言仓库资产**的用例才用它（`test_categories` 的
    「出厂文件必须能加载，且正是用户裁定的七类」）。其余用例走默认路径
    （= 上面那条夹具挪走的自带文件）——它们要的是"有一份能读通的类目表"，
    不是"用户的类目表长什么样"。
    """
    return REPO_ROOT / "config" / "categories.toml"


@pytest.fixture(autouse=True)
def _isolate_overrides_path(tmp_path_factory, monkeypatch):
    """把人工干预层的默认路径挪出仓库——**否则写路径会建真实的 `data/overrides.db`**。

    ⚠️ `store.ensure_schema()` 现在会调 `overrides.ensure_schema()`（M5 Task 4 Step 5），
    而它**每次 refine / digest 都跑**（测试里有 5 个文件、几十处在调它）。
    没有这个夹具，跑一次全量就在真实的 `data/` 里留下一个干预层库；更糟的是：

      ① **污染**：将来有用例往里面写了事件（改分类 / 软删 / 撤销），那些就是
         **用户真实的人工修改数据**被测试改写——而 `data/` 不入 git，
         **没有任何东西能把它还原**；
      ② **假红**：与 `data/vigil.lock` 同族的形态——只在"真库恰好有状态"时出现，
         看着像产品缺陷、其实是测试污染。

    ⚠️ **不能用 `tmp_path`**（本夹具初版就踩了）：有些用例把 `tmp_path` 当**产品
    目录**用，并按**文件集合**断言。`tests/test_digest.py` 的两条持久化用例就是
    `sorted(p.name for p in tmp_path.iterdir()) == ["2026-09-13.md"]`——
    overlay 库一旦落在 `tmp_path` 里，它们会**变成红的，而红的原因与它们要守的
    不变量毫无关系**（实测：2 failed / 459 passed）。
    所以这里用 `tmp_path_factory.mktemp()` 另开一个**每次调用唯一**的目录：
    它不在被测代码会去列目录的那些路径下。

    ⚠️ 用 autouse：漏掉一个测试文件就退化为上面两条。
    ⚠️ 只换路径、**不建文件**：建库仍然是 `ensure_schema()` 的职责
    （读路径不建库是刻意的设计，夹具不该替它把文件先造出来）。
    """
    monkeypatch.setattr(
        overrides, "OVERRIDES_DB", tmp_path_factory.mktemp("overrides") / "overrides.db"
    )


@pytest.fixture(autouse=True)
def _isolate_lock_path(tmp_path, monkeypatch):
    """把单实例锁的默认路径挪到 tmp——**否则跑测试会碰真实的 `data/vigil.lock`**。

    ⚠️ 两个后果，第二个比第一个严重得多：

      ① **污染**：跑一次全量就在 `data/` 里造一个 `vigil.lock`（虽然 `data/`
         不入库）；
      ② **假红**：F4 之后 `cmd_export`/`cmd_refine`/`cmd_digest` 都加锁，
         而 `cli_env` 那批用例**不换锁路径**。于是**若真有实例在跑**
         （比如 M4 冒烟的短触发器正好在跑 `vigil daily`），那些测试会拿到
         退出码 **2** 而**莫名变红**——**看着像产品缺陷，其实是测试污染**。

    ⚠️ 用 autouse：漏掉一个测试文件就退化为上面第 ② 条，而它是**环境相关的**
    ——只在"恰好有实例在跑"时出现，最难查。
    """
    monkeypatch.setattr(lock, "LOCK_PATH", tmp_path / "vigil.lock")


@pytest.fixture(autouse=True)
def _isolate_llm_toml_path(tmp_path_factory, monkeypatch):
    """把 LLM 端点配置的默认路径挪出仓库（M6 T3，勘误 E6）。

    ⚠️ **不隔离的后果是"环境相关的绿"，不是"写脏仓库"**——与上两条夹具的
    形态不同，值得单独说清楚：`load_llm_settings` 是**读**，不会写 `config/`。
    真正的问题是**测试的行为取决于仓库里那份文件的当前内容**：

      `cmd_refine` / `cmd_digest` / `cmd_repass` / `daily.run()` 都读它
      （T3 之后），于是 `test_daily.py::test_stage_arguments_are_wired_from_
      the_day_window` 与 `test_refine.py::test_cli_refine_model_falls_back_to_
      default` 这两条**与 LLM 配置无关**的用例，会因为「有人在网页上换了
      模型」而变红——**而那正是 M6 邀请用户做的事**。

    ⚠️ **实测过**（2026-09-17，T3）：把 `config/llm.toml` 的 `model` 改成一个
    别的值 ⇒ 上面两条**同时变红**（`2 failed, 636 passed`）；改回来 ⇒ 全绿。
    它们今天绿**只是因为出厂文件恰好等于内置默认**——这叫假绿。

    ⚠️ **指向一个不存在的路径**，不是 `tmp_path` 下的空文件：
    `load_llm_settings` 对「文件不存在」返回内置默认（+ `on_warn`），
    那正是这些用例期望的基线；而**空文件**会抛「缺少 [llm] 段」⇒ 全部变红。

    ⚠️ 用 `mktemp` 而不是 `tmp_path`（理由与 `_isolate_overrides_path` 一字不差）：
    有些用例把 `tmp_path` 当**产品目录**用并按**文件集合**断言
    （`tests/test_digest.py` 就是 `sorted(p.name for p in tmp_path.iterdir())`）。

    ⚠️ 用 autouse：漏掉一个测试文件，那个文件里的用例就退回"看仓库文件脸色"。
    """
    monkeypatch.setattr(
        config, "DEFAULT_LLM_TOML", tmp_path_factory.mktemp("llm") / "llm.toml"
    )


@pytest.fixture(autouse=True)
def _isolate_api_repo_root(tmp_path_factory, monkeypatch):
    """把 `api.config_path()`（Web 侧**现读**的 `groups.toml`）的解析根挪出仓库。

    ⚠️ 与 `_isolate_llm_toml_path` 是**同一族**（M6 T4）；形态更险一点：
    那边只是读文件，这边除了读，`load_config` 还要 `qq_db_dir.is_dir()`——
    那是个**外部**目录（QQ 的安装位置），换台机器、换个盘就没有。

    ⚠️ **不隔离的后果是「一批无关用例跟着用户改配置红绿」**：M6 T4 起
    `/api/items`、`/api/items/{id}`、`/api/digests/{id}` 经 `item_out` 现读
    `groups.toml` 取群名（`api.current_config()`，也正是 `test_api._config_for`
    的 docstring 早就点过的那个「假红来源」）。而那份文件**正是 M6 邀请用户
    在网页上改的东西**：用户在「配置」页改个群名或停用群，就会让一批与群配置
    毫无关系的用例变红。

    ⚠️ **只挪根、不写文件**也不行：`load_config` 读不到文件抛 `ConfigError`
    ⇒ 每个走 `/api/*` 的用例都变 500。所以这里在根下写一份**能读通的**最小配置。

    ⚠️ 数据根（`output_db`）同样落在这个 tmp 根下：`groups_snapshot.default_path`
    由它推出来（`output_db.parent / "groups-snapshot.json"`），
    于是「快照会被写到哪里」也一并隔离，不会碰到仓库里真实的 `data/`。

    ⚠️ 路径写**绝对 + posix**：相对路径的解析基准是 `config.REPO_ROOT`
    （**那个**全局没被挪走），而反斜杠是 TOML 基本字符串里的转义字符。

    ⚠️ 用 `mktemp` 而不是 `tmp_path`（理由与上面两道一字不差）：
    有些用例把 `tmp_path` 当**产品目录**用并按**文件集合**断言
    （`tests/test_digest.py` 就是 `sorted(p.name for p in tmp_path.iterdir())`）。

    ⚠️ **M6 T5 起还要写一份 `config/categories.toml`**：`api.categories_path()`
    与 `config_path()` 是**同一个根**，而 `api_categories` / `item_out` 从 T5 起
    都**现读**它。这与 E6（`DEFAULT_LLM_TOML`）是同一族：
    **新增的默认路径常量必须被夹具隔离**，否则测试要么写进真仓库、
    要么跟着用户的配置红绿。
    """
    from vigil import api as _api

    root = tmp_path_factory.mktemp("api_root")
    (root / "config").mkdir()
    (root / "data").mkdir()
    (root / "qqdb").mkdir()
    (root / "config" / "groups.toml").write_text(
        "[settings]\n"
        f'source_dir = "{(root / "qqdb").as_posix()}"\n'
        f'output_db = "{(root / "data" / "vigil.db").as_posix()}"\n'
        "\n"
        "[[groups]]\n"
        "id = 111\n"
        'name = "隔离用群"\n'
        "enabled = true\n",
        encoding="utf-8",
    )
    # ⚠️ M6 T5 起 `api.categories_path()` 也在这个根下现读
    # （`categories_path()` = `REPO_ROOT / "config" / "categories.toml"`），
    # 而它**正是 M6 邀请用户在网页上改的那份文件**（T5 的类目编辑 + T10 的设置页）。
    # 不写这一份的话：`load_categories` 抛"找不到类目配置"⇒ 每个走
    # `/api/categories` 或 `item_out`（它要 `kind_label`）的用例都变 500。
    #
    # ⚠️ **内容自带、不抄仓库里那份真文件**：抄一份等于把「测试红绿取决于用户
    # 的类目配置」这件事留着——用户删掉一个类目，一批无关用例跟着红
    # （与上面 `_isolate_llm_toml_path` 记的假绿是同一族）。这里要的只是
    # 「有一份能读通的类目表」，所以用一份最小的。
    (root / "config" / "categories.toml").write_text(
        _ISOLATED_CATEGORIES_TOML, encoding="utf-8"
    )
    monkeypatch.setattr(_api, "REPO_ROOT", root)


_ISOLATED_PERSONS_TOML = """\
[[persons]]
uin = 10001
label = "隔离用人物"
"""


@pytest.fixture(autouse=True)
def _isolate_persons_toml_path(tmp_path_factory, monkeypatch):
    """把 `config.DEFAULT_PERSONS` 挪到 tmp，并**自带内容**（M6 T7 Fix loop 1 / FIX 2）。

    ⚠️ **为什么它必须存在**（Global Constraint 9 / 勘误 E6 的字面要求）：
    M6 T7 给本项目添了**第一条会写 `DEFAULT_PERSONS` 的产品路径** ——
    `vigil config import`（`cli._config_pack_paths()` 里 `persons=config.DEFAULT_PERSONS`）。
    而 `save_persons` 是**整体替换**写（那是它刻意的语义：不整体替换就删不掉人）
    ⇒ 没有这道夹具时，任何一个"忘了 monkeypatch 就直接
    `cli.main(["config", "import", …])`"的用例，都会把**用户真实的
    `config/persons.toml`**（真数据，而且 `config/` 要进 git）整体覆盖掉。
    那与 `_isolate_api_repo_root` 记的那次「测试往真仓库写人」是**同一条链**。

    ⚠️ **内容自带、不指向不存在的路径**（与 `_isolate_llm_toml_path` **刻意相反**）：
    `load_persons` 对"文件不存在"返回**空元组**，而那是个**合法语义**
    （"一个人都没监视"）⇒「夹具没生效」与「夹具生效且名单为空」在断言层面
    **完全同形**。写一份非空的，`test_config.py::test_default_persons_path_
    is_isolated_by_conftest` 才能反过来把**夹具本身**钉住。
    内容**自带**、不抄仓库里那份真名单（理由同 `_isolate_categories_toml_path`：
    抄一份等于把"测试红绿取决于用户的名单"这件事留着）。

    ⚠️ 用 `mktemp` 而不是 `tmp_path`（理由与上面几道一字不差）：
    有些用例把 `tmp_path` 当**产品目录**用并按**文件集合**断言
    （`tests/test_digest.py` 就是 `sorted(p.name for p in tmp_path.iterdir())`）。

    ⚠️ 用 autouse：漏掉一个测试文件，那个文件里的用例就退回"写用户的真名单"。
    """
    path = tmp_path_factory.mktemp("persons") / "persons.toml"
    path.write_text(_ISOLATED_PERSONS_TOML, encoding="utf-8")
    monkeypatch.setattr(config, "DEFAULT_PERSONS", path)


@pytest.fixture(autouse=True)
def _isolate_cli_repo_root(tmp_path_factory, monkeypatch):
    """把 `cli.REPO_ROOT` 挪出仓库（M6 T7 Fix loop 1 / I3）。

    ⚠️ **它与 `api.REPO_ROOT` / `config.DEFAULT_PERSONS` 是同一个洞的第三个入口**：
    `cli.py` 里 `REPO_ROOT` 是**它自己**的一个全局名字（`from .config import REPO_ROOT`
    在 import 期绑定），所以 `monkeypatch.setattr(config, "REPO_ROOT", …)`
    **够不着它**（`_isolate_api_repo_root` 挪的是 `api` 的那一份，同样够不着）。

    ⚠️ 不隔离的后果（reviewer 实测，2026-09-18）：一条**不用 `pack_env`** 的用例
    调 `cli.main(["config", "import", …])`，就会写**仓库根的 `config/groups.toml`**
    ——实测把那份从 **15 群改成 1 群**、`persons.toml` 抹成 **0 人**，
    **而用例全绿**（它断言的是它自己造的包，看不见仓库被改了）。
    这正是 Global Constraint 9「测试不碰真实资产」的字面违反。

    ⚠️ 还有一处**今天没人看**的落点：`cmd_repass` 的 `--audit` 默认路径
    （`REPO_ROOT / "data" / f"repass-audit-{stamp}.jsonl"`，`cli.py:456`）。
    没隔离时，任何不显式传 `--audit` 的 repass 用例都会往**真仓库的 `data/`** 里
    落一份审计文件——那是**数据**，`data/` 不入 git、删了就没了。

    ⚠️ **根下写一份能读通的 `config/groups.toml`**（照 `_isolate_api_repo_root` 的
    同形理由）：`cmd_config_import --keep-settings` 会读它来拿"本机路径"；
    让它**存在且合法**，`--keep-settings` 走的才是**真实的那一支**（保留本机），
    而不是"读不出来 ⇒ 回落"的异常分支——否则一整类用例会**静默地测错分支**。

    ⚠️ 路径写**绝对 + posix**：相对路径的解析基准是 `config.REPO_ROOT`
    （**那个**全局没被挪走），而反斜杠是 TOML 基本字符串里的转义字符。

    ⚠️ 用 `mktemp` 而不是 `tmp_path`（理由与上面几道一字不差）：
    有些用例把 `tmp_path` 当**产品目录**用并按**文件集合**断言。
    """
    from vigil import cli as _cli

    root = tmp_path_factory.mktemp("cli_root")
    (root / "config").mkdir()
    (root / "data").mkdir()
    (root / "qqdb").mkdir()
    (root / "config" / "groups.toml").write_text(
        "[settings]\n"
        f'source_dir = "{(root / "qqdb").as_posix()}"\n'
        f'output_db = "{(root / "data" / "vigil.db").as_posix()}"\n'
        "\n"
        "[[groups]]\n"
        "id = 111\n"
        'name = "隔离用群"\n'
        "enabled = true\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(_cli, "REPO_ROOT", root)


@pytest.fixture(autouse=True)
def _isolate_jobs_registry(monkeypatch):
    """每个用例一份**全新的**后台 job 注册表（M6 T6；勘误 E6 点名要一并考虑的那件）。

    ⚠️ `jobs.registry` 是**模块级单例**（`api.py` 用它），而「同一时刻只许一个
    job」这条守卫（`JobBusy` → 409）看的是**那个进程**里有没有 running 的 job
    ⇒ job 的**残留**天然跨用例可见：`slow_llm` 那类夹具就是**故意**留一个
    running job 的（`test_api_reorder_refuses_a_second_job_while_one_runs`），
    而它的线程要等夹具 teardown 置了那个 Event 之后才把 state 改成 `failed`
    ——**那一步是异步的**，下一个用例若跑在它前面，POST 就会撞 409。

    ⚠️ **实测（T6，2026-09-17）：把这个夹具改成空转，连跑 5 次
    （`tests/test_api.py tests/test_jobs.py tests/test_reorder.py`），
    每次都是 `108 passed`。** 也就是说它今天**不是**一条被逼出来的守卫，
    而是一道**防御**——上面那个竞态窗口确实存在（异步），但 teardown 的
    `gate.set()` 几乎总是先落地。这里**刻意不写「不换就会红」**：那是编的，
    而本项目最贵的一课是**错的论证比没有论证更危险**（勘误 E4/E7）。
    留着它的理由只有一条：这类竞态的失败形态是**环境相关的假红**
    ——只在"恰好来不及"时出现，最难查；而它的代价只是一个 `JobRegistry()`。

    ⚠️ 用 autouse（与上面四道同形）：漏掉一个测试文件就退回"看残留脸色"。
    ⚠️ 造新实例、**不清空旧实例**：旧实例可能正被一个 daemon 线程改着，
    清空它等于让那个线程往一个没人看的对象上写（无害，而且是真实发生的事）。
    """
    from vigil import jobs as _jobs

    monkeypatch.setattr(_jobs, "registry", _jobs.JobRegistry())


def _overlay_fingerprint(db: pathlib.Path) -> tuple:
    """`(主文件 sha256, item_edits 行数, MAX(edit_id), item_state 行数)`。**只读**。

    ⚠️ 对**不存在**的路径早退（返回 `("<missing>", 0, 0, 0)`）并且**不建文件**：
    量它的动作本身一个字节都不许写（`ensure_schema` / `connect` 都会建文件
    ——那是写，不是量）。⚠️ 这条"不建文件"今天**由早退守着，不是由 `mode=ro` 守着**
    （连接那行在早退之后）——`_real_overlay_stays_untouched` 里有针对它的自检。
    ⚠️ **不量 `-shm`**：任何连接（**含只读的**）挂上去都可能动它，它一变就会产生
    「与环境相关」的假红，而假红的守卫最后都会被人注释掉。
    ⚠️ **不量 mtime**：判据是「**内容**没变」，而 mtime 不是内容的一部分
    （fix loop 3 实测过一次：真实文件 mtime 从 `02:29:53` 走到 `03:27:30` 而
    字节**完全没变**）。至于"到底谁碰了它"，**我没测出来**——所以这里不写原因。
    ⚠️ `-wal` 里的行**由行数反映**（只读连接看得见 WAL 里的已提交行）——
    所以"只比较主文件字节"是够的，没有"写进 WAL 看不见"的缺口。
    ⚠️ 库在但表不在（半成品/坏文件）⇒ 记一个哨兵值而不是抛：那种状态下
    **主文件 sha256 已经变了**，断言照样会红；在这里抛只会得到一句不像判据的 traceback。
    """
    if not db.is_file():
        return ("<missing>", 0, 0, 0)
    main = hashlib.sha256(db.read_bytes()).hexdigest()
    con = sqlite3.connect(f"file:{db.as_posix()}?mode=ro", uri=True)
    try:
        n, mx = con.execute(
            "SELECT COUNT(*), IFNULL(MAX(edit_id), 0) FROM item_edits"
        ).fetchone()
        st = con.execute("SELECT COUNT(*) FROM item_state").fetchone()[0]
        return (main, int(n), int(mx), int(st))
    except sqlite3.Error as exc:
        return (main, f"<读不出来：{type(exc).__name__}>", 0, 0)
    finally:
        con.close()


def _default_overlay_paths() -> list[pathlib.Path]:
    """会话开始时**「默认写入目标」**那条路径 + **仓库里**那份 `data/overrides.db`。

    ⚠️ 取 `overrides.OVERRIDES_DB` 的**会话初始值**，而不是写死 `REPO_ROOT / …`：
    那正是 `monkeypatch` 在 teardown 里把默认路径**还原成**的那一个——**伤害落的就是它**。
    fix loop 4 起它由 `_un_isolated_overlay` 指到会话级 tmp（于是伤害落进 tmp），
    而仓库里那份**必须始终原封不动** ⇒ 两条都量、分别断言。
    """
    from vigil import overrides as _ov

    paths = [pathlib.Path(_ov.OVERRIDES_DB)]
    repo_path = pathlib.Path(_ov.REPO_ROOT) / "data" / "overrides.db"
    if repo_path not in paths:
        paths.append(repo_path)
    return paths


@pytest.fixture(scope="session", autouse=True)
def _un_isolated_overlay(tmp_path_factory):
    """把 `overrides.OVERRIDES_DB` 的**模块默认值**重定向到一个**会话级 tmp**（fix loop 4 / FIX 1）。

    **从「探测器」改成「预防器」**：`_isolate_overrides_path` 是**逐用例**打补丁的，
    它在每个用例 teardown 里**先于** worker 把默认路径**还原成仓库里那条真路径**，
    下一个用例开始前才重新指向 tmp——复审量到那个窗口有 **15–18ms**，而 worker 是不是
    恰好落在窗口里是**硬币**（M2-1b 下那条 session 不变式的命中率只有 **19/37**）。

    重定向之后，那个窗口里的"默认路径"指向的是**会话级 tmp** ⇒ 伤害**根本落不到
    真实资产上**（复审实测 6/6：真库 sha/行数一动不动，伤害落进替身 `rows=2`）。

    ⚠️ 逐用例的隔离**照旧**（本夹具只改模块默认值，不动 `_isolate_overrides_path`）。
    ⚠️ 替身必须**建好表**：半成品文件会让 worker 的写以 `no such table` 收场，
    于是"伤害"被一次响亮失败挡掉——而下一个人会把那种绿读成"没有伤害"。
    """
    from vigil import overrides as _ov

    original = _ov.OVERRIDES_DB
    target = tmp_path_factory.mktemp("un-isolated") / "overrides.db"
    _ov.ensure_schema(target)
    _ov.OVERRIDES_DB = target
    try:
        yield target
    finally:
        _ov.OVERRIDES_DB = original


@pytest.fixture(scope="session", autouse=True)
def _real_overlay_stays_untouched(_un_isolated_overlay, tmp_path_factory):
    """⭐ 会话级不变式（**兜底**）：会话里没有任何东西写进"隔离之外"的那两个库。

    量的两条（见 `_default_overlay_paths`）：
      ① **仓库里真实的** `data/overrides.db` —— 本项目第一条规矩，**必须原封不动**；
      ② 会话级替身（`_un_isolated_overlay` 指的那个 tmp）—— **伤害会落在这里**。

    **为什么还要它**（M6 T6 fix loop 3，复审 Important）：那条「夹具 teardown 要等 worker
    结束」的判据，**等的就是 `_drain_jobs` 自己**；复审用两个改法证明了它守不住任何东西：

      * 把 `_drain_jobs` 的 while 改成 `return True` ⇒ 时序子集 **6 passed 全绿**；
      * 把两个夹具 teardown 换回裸 `gate.set()` ⇒ 只有故意失败的探针红，而**同一
        形状下 worker 真的把 2 条 `item_edits` 写进了隔离之外的库**。

    ⇒ 这正是 M5 那条教训的原形：**判据用它要守的那套机制当仪器，就永远证明不了
    那套机制**（"探针被写进被测物内部"）。

    **为什么这条独立于被测物**：它不读 `jobs.registry`、不调 `_drain_jobs`、
    不看任何夹具 teardown 的行为——它只在会话的**一头一尾**各量一次**那些文件
    本身**（sha256 + 行数），量具与 Python 侧的机制**没有任何共享状态**。

    **为什么必须是会话级**：伤害发生在**夹具 teardown 之后**（worker 是异步的），
    function 级的量具会在伤害发生**之前**就把结论定了——那正是 fix loop 2 那条空守卫
    的形态（"断言跑在伤害之前"）。

    ⚠️ 它是**兜底**：第一道防线是 `_un_isolated_overlay`（预防）。所以它红了**先看替身**
    ——替身红=有人绕过了逐用例隔离；**仓库那份红**才是真正不可接受的。

    ⚠️ 它红了也先排除一件事：**服务在本轮测试期间被人工操作过**（那会真的改写这些
    文件——那是环境动的，不是测试写的）。
    """
    # ⚠️ 量具自身的自检（fix loop 4 / FIX 5a）：对**不存在**的路径量一次，**不许**把
    # 文件造出来。（今天这条由 `is_file()` 早退守着；把早退删掉、连接不带 `mode=ro`
    # ⇒ 那一次量就把文件造出来了 —— 那时这条断言会红，变异反证实测过。）
    probe = tmp_path_factory.mktemp("fingerprint-probe") / "never-created.db"
    _overlay_fingerprint(probe)
    assert not probe.is_file(), (
        "`_overlay_fingerprint` 建了文件——它不是只读的量具（量它自己就会写）"
    )

    targets = _default_overlay_paths()
    before = {p: _overlay_fingerprint(p) for p in targets}
    yield
    after = {p: _overlay_fingerprint(p) for p in targets}
    changed = [p for p in before if before[p] != after[p]]
    assert not changed, (
        "测试写进了**隔离之外**的 overlay 库（本项目第一条规矩：测试不碰真实资产）：\n"
        + "\n".join(f"  {p}\n    会话开始：{before[p]}\n    会话结束：{after[p]}" for p in changed)
        + "\n⚠️ 先排除：服务在测试期间被人工操作过（那是环境动的）。"
        "确认没有的话，就是**夹具 teardown 之后仍有 worker 在写**——"
        "查 `tests/test_api.py::_release_and_wait` 与两道 teardown 是否真的等到了终态。"
    )


@pytest.fixture
def memdb():
    """内存库。用完即弃，不污染任何真实文件。

    ⚠️ `uri=True` 是 Task 5 加的，**必须**：查询层入口会
    `ATTACH 'file:...?mode=ro'`，而 URI 形式**只在连接带 `SQLITE_OPEN_URI`
    时才被解析**（Task 4 实测、T5 复核）。不带这个标志，每条走查询层的用例都会抛
    `OverlayError`——而那不是产品缺陷。对 `:memory:` 而言这个标志
    **不影响任何行为**（路径不以 `file:` 开头时它就是个空开关），
    与生产侧三处连接的补齐（`cli.py` / `digest.py` / `refine.py`，裁决 R4）是同一件事。
    """
    conn = sqlite3.connect(":memory:", uri=True)
    yield conn
    conn.close()


@pytest.fixture
def msg_factory():
    """快速造 PendingMessage。Task 5 的预筛测试要用。"""
    from vigil.store import PendingMessage

    def make(
        msg_id: int,
        content: str,
        *,
        group_id: int = 100,
        ts: int = 1_700_000_000,
        sender: str = "某同学",
        uid: str = "u_x",
    ) -> PendingMessage:
        return PendingMessage(
            msg_id=msg_id,
            group_id=group_id,
            ts=ts,
            sender_uid=uid,
            sender=sender,
            content=content,
        )

    return make
