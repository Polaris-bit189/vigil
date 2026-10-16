"""测试设施自检。这个文件本身没有业务价值，
但它让「pytest 能不能跑」这件事有个明确的判据。"""

from __future__ import annotations


def test_pytest_runs():
    assert True


def test_memdb_fixture_works(memdb):
    memdb.execute("CREATE TABLE t (x INTEGER)")
    memdb.execute("INSERT INTO t VALUES (1)")
    assert memdb.execute("SELECT x FROM t").fetchone() == (1,)


def test_vigil_package_is_importable():
    import vigil

    assert vigil.__version__ == "0.1.0"
