"""课表的读写与校验。

⚠️ 本文件用 `tmp_path` 而不是仓库里的 `config/timetable.toml`：
`save_timetable` 是**整体覆盖写**，用例打到真实路径上会把用户的课表抹掉。
（与 `test_config.py` 里 persons 那一组同一条理由。）
"""

from __future__ import annotations

import pathlib

import pytest

from vigil import timetable
from vigil.config import ConfigError


def _p(tmp_path) -> pathlib.Path:
    return tmp_path / "timetable.toml"


# ── HH:MM 解析 ────────────────────────────────────────────────


def test_parse_hhmm_accepts_common_forms():
    assert timetable.parse_hhmm("08:00", field="x") == 480
    assert timetable.parse_hhmm("8:00", field="x") == 480
    assert timetable.parse_hhmm(" 14:30 ", field="x") == 870


def test_parse_hhmm_accepts_fullwidth_colon():
    """⚠️ 中文输入法下打出来的是全角冒号 —— 用户不该为此再输一遍。"""
    assert timetable.parse_hhmm("14：30", field="x") == 870


@pytest.mark.parametrize("bad", ["8", "8:", "abc", "8:xx", "", "25:00", "12:60"])
def test_parse_hhmm_rejects_bad_values(bad):
    """⭐ 用户填错 ⇒ **响亮报错**，不静默容忍。

    ⚠️ 与 `refine._parse_start` 的策略**刻意相反**：那边的输入是模型输出
    （不可信、量大、错一个不该掀掉整轮），这边是用户手填（可信、量小、
    错了要立刻告诉他）。同一份数据两种来源，失败策略本就该不同 ——
    对用户填错的值静默容忍，他会以为填进去了。
    """
    with pytest.raises(ConfigError):
        timetable.parse_hhmm(bad, field="x")


def test_format_hhmm_round_trips():
    assert timetable.format_hhmm(480) == "08:00"
    assert timetable.format_hhmm(870) == "14:30"
    assert timetable.format_hhmm(0) == "00:00"


# ── 读写往返 ───────────────────────────────────────────────────


def test_missing_file_is_an_empty_timetable(tmp_path):
    """⚠️ 文件不存在 ⇒ 空元组，**不是报错**。

    "还没填课表"是完全合法的状态（刚装上、或用户就不想排期）——
    与 `load_config`（必需配置）的快速失败刻意不同。
    """
    assert timetable.load_timetable(_p(tmp_path)).courses == ()


def test_round_trip_preserves_every_field(tmp_path):
    """⭐ 写出去再读回来必须逐字段相等。

    ⚠️ 这条挡的是 weekday 的 **1-7 ⇄ 0-6** 换算写反 —— 那是本模块最容易
    出错的一处（两个方向都合法、都不报错，只是所有课都错一天）。
    """
    p = _p(tmp_path)
    courses = (
        timetable.Course(weekday=0, start=480, end=580, label="高等数学"),
        timetable.Course(weekday=6, start=840, end=940, label="体育", weeks="odd"),
        timetable.Course(weekday=3, start=600, end=700),  # 无 label、默认 weeks
    )
    # ⚠️ `weeks="odd"` 必须配 `term_start`（否则判断不了单双周，加载会响亮拒绝）
    tt = timetable.Timetable(courses=courses, term_start="2026-09-07")
    timetable.save_timetable(tt, p)
    assert timetable.load_timetable(p) == tt


def test_weekday_is_written_as_one_to_seven_on_disk(tmp_path):
    """⚠️ 落盘的是 **1-7**（人话），不是内部的 0-6。

    用户会手工编辑这份文件 —— 而「周日是 0 还是 7」是个真实存在的手误来源。
    钉住盘上的形态，防止有人"顺手"把它改成 0 基。
    """
    p = _p(tmp_path)
    timetable.save_timetable(
        timetable.Timetable(courses=(timetable.Course(weekday=0, start=480, end=580),)), p
    )
    text = p.read_text(encoding="utf-8")
    assert "weekday = 1" in text, "周一必须落成 1"
    assert "weekday = 0" not in text


def test_load_rejects_weekday_zero(tmp_path):
    """⭐ 填 0 ⇒ 报错（不是静默当成周日）。

    静默容忍的话，一个填 0 的用户会得到一节**周一**的课（如果实现把它当 0 基）
    或一节周日的课（如果当 1 基）—— 两种都不是他要的，而他不会知道。
    """
    p = _p(tmp_path)
    p.write_text('[[courses]]\nweekday = 0\nstart = "08:00"\nend = "09:00"\n', encoding="utf-8")
    with pytest.raises(ConfigError, match="1-7"):
        timetable.load_timetable(p)


def test_load_rejects_end_before_start(tmp_path):
    """⭐ end 早于 start ⇒ 报错（最可能是把上下午写反了）。"""
    p = _p(tmp_path)
    p.write_text('[[courses]]\nweekday = 1\nstart = "14:00"\nend = "10:00"\n', encoding="utf-8")
    with pytest.raises(ConfigError, match="晚于"):
        timetable.load_timetable(p)


def test_load_rejects_unknown_weeks_value(tmp_path):
    p = _p(tmp_path)
    p.write_text(
        '[[courses]]\nweekday = 1\nstart = "08:00"\nend = "09:00"\nweeks = "monthly"\n',
        encoding="utf-8",
    )
    with pytest.raises(ConfigError, match="weeks"):
        timetable.load_timetable(p)


def test_load_rejects_scalar_in_courses_array(tmp_path):
    """⚠️ `courses = ["周一"]` 是**语法合法**的 TOML（数组能装标量）。

    不挡的话 `entry.get` 抛 `AttributeError`，绕过本模块只捕 `ConfigError`
    的契约 ⇒ 用户看到裸 traceback。与 `load_persons` 里记的是同一处坑。
    """
    p = _p(tmp_path)
    p.write_text('courses = ["周一"]\n', encoding="utf-8")
    with pytest.raises(ConfigError, match="键值表"):
        timetable.load_timetable(p)


def test_load_rejects_broken_toml_as_config_error(tmp_path):
    """⚠️ TOML 语法错要包成 `ConfigError`（它继承 ValueError，不包会穿透）。"""
    p = _p(tmp_path)
    p.write_text("[[courses]\nweekday = 1\n", encoding="utf-8")
    with pytest.raises(ConfigError):
        timetable.load_timetable(p)


def test_save_then_load_with_cyrillic_label(tmp_path):
    """⚠️ label 走 `toml_str` 转义 —— 含引号/反斜杠的值不许把文件写坏。"""
    p = _p(tmp_path)
    weird = timetable.Course(weekday=1, start=480, end=580, label='他说"你好"\\再见')
    timetable.save_timetable(timetable.Timetable(courses=(weird,)), p)
    assert timetable.load_timetable(p).courses == (weird,)


def test_course_str_is_human_readable():
    """ⓘ 排期结果要给人看，`__str__` 是它的主要出口。"""
    c = timetable.Course(weekday=2, start=8 * 60, end=9 * 60 + 40)
    assert str(c) == "周三 08:00-09:40"
