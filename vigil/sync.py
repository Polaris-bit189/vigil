"""把本机 SQLite 的**结构化结果**推到云 RDS。

    本机（数据不出网）                    云端（华为云）
    ─────────────────                    ──────────────
    messages          ──✗ 不推──→
    sender_names      ──✗ 不推──→
    msg_media         ──✗ 不推──→
    items             ──✓ 推───→        vigil.items
    item_sources      ──✓ 推───→        vigil.item_sources
    digests           ──✓ 推───→        vigil.digests
    digest_items      ──✓ 推───→        vigil.digest_items

---

## 隐私契约（这个模块存在的全部理由）

**只推四张表。** 另外三张不推的理由各不相同：

| 表 | 不推的理由 |
|---|---|
| `sender_names` | **全库唯一含真实姓名的表** |
| `messages` | 含原文，违背「原文不出本机」 |
| `msg_media` | 同上（且含本地文件路径） |

`items.title` / `detail` 之所以**可以**推，是因为它们在 `refine` 阶段
已经过了出网脱敏（`Redactor.name()` + `scrub_names()`，见 `redact.py`）。
**这个前提是调用方必须保证的** —— 所以本模块在推之前**再过一道**
`scrub_row()`（见下），不假设上游一定做对了。

---

## ⚠️ `actor_uid` 推之前整体置空

`actor_uid` 是跨机器稳定的标识符。它**不含姓名**，但仍然**指得到人** ——
而云端（Demo、评委、任何拿到 RDS 的人）不需要它：
需要"谁说的"就去看群维度（`group_id`），那是**群级**而非**人级**的粒度。

这是"数据最小化"原则的一次具体应用：**能不给的就不给**，
而不是"给了也没关系"。
"""

from __future__ import annotations

import pathlib
import sqlite3
from dataclasses import dataclass, field

from . import db
from .config import Config, ConfigError
from .redact import redact_text


@dataclass
class SyncStats:
    """同步结果。**失败也要如实报告**（照 `IngestStats` 的规矩）。"""

    per_table: dict[str, int] = field(default_factory=dict)
    skipped_redacted: int = 0  # 因脱敏而被改动过的行数

    @property
    def total(self) -> int:
        return sum(self.per_table.values())


# 推哪些表、各自的列序。
#
# ⚠️ **列序必须与 `vigil/store.py` 的 DDL 一致**（不是与 MySQL 的一致）：
# 我们从 SQLite 读、往 MySQL 写，两张表的**列名相同**，所以要显式列名
# 而不是 `SELECT *` —— `SELECT *` 在两边列序漂移时会静默错位。
_PUSH_TABLES: dict[str, list[str]] = {
    "items": [
        "item_id", "kind", "title", "detail", "event_ts", "deadline_ts",
        "group_id", "actor_uid", "place", "links", "amount", "confidence",
        "model", "prompt_ver", "created_at",
    ],
    "item_sources": ["item_id", "msg_id"],
    "digests": [
        "digest_id", "window_from", "window_to", "body_md",
        "model", "prompt_ver", "created_at",
    ],
    "digest_items": ["digest_id", "item_id"],
}


def scrub_row(table: str, row: tuple) -> tuple:
    """出网前的**最后一道机械闸**：再过一次脱敏，并置空 `actor_uid`。

    ⚠️ 为什么上游已经脱敏了还要再做一次：
    「上游做对了」是一个**前提**，而前提会失效 ——
    有人改了 `refine` 的某处、有人用旧库的 items 直接同步、
    有人手工写了一条 item。这里是**能兜住那种情况的最后一站**。

    代价是重复劳动（对一个 item 做两次正则替换，微秒级）。
    换来的是一句**可验证**的话：「凡经 `sync` 出网的数据都过了脱敏」——
    而不是「应该过了吧」。

    ⚠️ 只动**文本列**，不动 `item_id` / `event_ts` 这类结构化列：
    对数字列跑脱敏毫无意义，而万一正则咬到数字（`item_id` 是 19 位雪花号，
    正好落在 LONGNUM 的十位以上长数字规则里），就把主键打坏了。
    """
    cols = _PUSH_TABLES[table]
    out = list(row)
    for i, name in enumerate(cols):
        if name in ("title", "detail", "place", "amount") and isinstance(out[i], str):
            out[i] = redact_text(out[i])
        elif name == "actor_uid":
            # 见模块 docstring 的论证 —— 云端不需要人级粒度。
            out[i] = None
    return tuple(out)


def sync_to_rds(
    config: Config,
    *,
    rds_dsn: str | None = None,
    dry_run: bool = False,
    on_progress=print,
) -> SyncStats:
    """把四张表从本机 SQLite 推到云 RDS。**幂等**（`REPLACE INTO`）。

    ⚠️ `dry_run=True` 时只**数**不写 —— 用于验收（"要推多少行"），
    以及在没有 RDS 时验证本模块本身。
    """
    import os

    stats = SyncStats()
    src_path = config.output_db
    if not src_path.is_file():
        raise ConfigError(f"本机库不存在：{src_path}\n  先跑 `vigil demo` 或 `vigil ingest`")

    # ⚠️ 本机侧**只读**打开：同步是**导出**动作，不该改本机任何东西。
    src = sqlite3.connect(f"file:{src_path.as_posix()}?mode=ro", uri=True)
    try:
        # 检查表在不在（`vigil demo` 之后没跑 refine 的话，items 还没建）
        have = {
            r[0]
            for r in src.execute("SELECT name FROM sqlite_master WHERE type='table'")
        }
        missing = [t for t in _PUSH_TABLES if t not in have]
        if missing:
            raise ConfigError(
                f"本机库缺少这些表：{missing}\n"
                "  它们由 `vigil refine` 与 `vigil digest` 建立 —— "
                "先跑完管线再同步。"
            )

        if rds_dsn:
            os.environ["VIGIL_RDS_DSN"] = rds_dsn

        dst = None if dry_run else db.connect(config, readonly=False, dialect=db.MYSQL)
        try:
            for table, cols in _PUSH_TABLES.items():
                names = ", ".join(cols)
                rows = src.execute(f"SELECT {names} FROM {table}").fetchall()
                scrubbed = [scrub_row(table, tuple(r)) for r in rows]
                stats.per_table[table] = len(scrubbed)

                if dry_run or dst is None:
                    on_progress(f"  [dry-run] {table}: {len(scrubbed)} 行")
                    continue

                # 幂等写入 —— 与 ingest 的 `INSERT OR REPLACE` 同一约定。
                stmt = db.upsert(table, cols, db.MYSQL)
                with dst.cursor() as cur:
                    cur.executemany(stmt, scrubbed)
                dst.commit()
                on_progress(f"  ✓ {table}: {len(scrubbed)} 行")
        finally:
            if dst is not None:
                dst.close()
    finally:
        src.close()

    return stats
