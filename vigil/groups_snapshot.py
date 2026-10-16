"""群清单快照 —— Web 侧的「可选群」数据源（M6 T4，spec §1.6 的裁定）。

**为什么要有这个模块**：Web 端点需要一份「QQ 库里有哪些群」的清单，
而现成的实现（`cli.cmd_groups`）靠 `strip_fake_header` **写**
`data/cache/nt_msg_clear.db`。直接复用会有两个**互相独立**的问题：

1. 从后门打破「Web 不写 `data/`」（`data/cache/` 也是 `data/`）；
2. 它需要 **`VIGIL_DB_KEY`** ⇒ 把整个 QQ 库的解密能力交给 Web 进程，
   而 `create_app(config)` 今天**只拿 `Config`、不拿密钥**。

⇒ 群清单由 **CLI 预生成**成 JSON，Web 只读产物。Web 侧零写入 `data/`、零密钥。

**失败策略与 `config.py` 相反，且这是刻意的**：
本模块是**派生物**（`groups.toml` 才是真相源，这里是它的一个视图）。
快照读不出来只意味着「该重跑一次 export」，把整个服务拖死是过度反应——
所以 `load()` 一律返回 `None`，由调用方回一句人话。
"""

from __future__ import annotations

import json
import pathlib
import time
from collections.abc import Sequence
from dataclasses import asdict, dataclass
from typing import Any

from .config import Config, atomic_write_text

# ⚠️ 加字段就要**加版本号**。`load()` 会拒绝版本不符的文件——没有这道闸的话，
# 加字段那天旧代码会静默错读新文件，而快照恰恰是「用户看着它决定加不加群」
# 的东西，错读的代价是**加错群**。宁可让界面说「快照过期，请重跑 export」。
SCHEMA_VERSION = 1


@dataclass(frozen=True)
class GroupInfo:
    """QQ 本地库里的一个群。`messages` / `last_ts` 是该群的**全量**历史统计。"""

    id: int
    name: str
    messages: int
    last_ts: int


@dataclass(frozen=True)
class Snapshot:
    generated_at: int
    groups: tuple[GroupInfo, ...]


def default_path(config: Config) -> pathlib.Path:
    """快照与主库同目录（`data/`）——它们是同一份「导出产物」的两个视角。"""
    return config.output_db.parent / "groups-snapshot.json"


def collect(
    counts: Sequence[tuple[int, int, int]], names: dict[str, str]
) -> tuple[GroupInfo, ...]:
    """把 `qqdb.list_group_message_counts` 的输出与 `qqdb.group_names` 的结果拼起来。

    ⚠️ 群名取不到时**留空串而不是 `"?"`**：`"?"` 是一个看起来像名字的占位符，
    而界面上「(无名)」与「?」给用户的判断完全不同。名字缺失是**正常状态**
    （`group_info.db` 可能落后于 `nt_msg.db`）。
    """
    return tuple(
        GroupInfo(
            id=int(gid),
            name=names.get(str(gid), ""),
            messages=int(n),
            last_ts=int(t or 0),
        )
        for gid, n, t in counts
    )


def write(
    groups: Sequence[GroupInfo],
    path: pathlib.Path,
    *,
    now: int | None = None,
) -> pathlib.Path:
    """原子写出快照。

    ⚠️ **必须原子**（`atomic_write_text`）：Web 每个请求都可能读它，
    而它由 CLI 在**另一个进程**里写 —— 正是「读者可能看到半个文件」的
    经典场景。写一半的 JSON 会让界面变成「快照坏了」而不是「快照旧了」。
    """
    payload: dict[str, Any] = {
        "schema": SCHEMA_VERSION,
        "generated_at": int(time.time()) if now is None else int(now),
        "groups": [asdict(g) for g in groups],
    }
    atomic_write_text(path, json.dumps(payload, ensure_ascii=False, indent=1))
    return path


def load(path: pathlib.Path) -> Snapshot | None:
    """读快照。**任何问题都返回 `None`，绝不抛异常**（见模块 docstring）。"""
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if not isinstance(raw, dict):
        return None
    if raw.get("schema") != SCHEMA_VERSION:
        return None

    generated_at = raw.get("generated_at")
    entries = raw.get("groups")
    if not isinstance(generated_at, int) or not isinstance(entries, list):
        return None

    out: list[GroupInfo] = []
    for e in entries:
        if not isinstance(e, dict):
            return None
        try:
            out.append(
                GroupInfo(
                    id=int(e["id"]),
                    name=str(e["name"]),
                    messages=int(e["messages"]),
                    last_ts=int(e["last_ts"]),
                )
            )
        except (KeyError, TypeError, ValueError):
            return None
    return Snapshot(generated_at=int(generated_at), groups=tuple(out))
