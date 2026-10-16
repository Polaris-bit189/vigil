"""查询层：读导出的明文库。

这一层**不需要密钥**——导出时已经把 protobuf 正文抽成文本、把 uid 解析成姓名，
所以查询是纯 SQL，又快又简单。

对应两种用法：
  - 定向：锁定某个群 → 锁定某个人 → 看他说了什么
  - 保底：不设过滤，全量关键词检索（同学的消息也可能带重要信息）
"""

from __future__ import annotations

import datetime as dt
import sqlite3
from dataclasses import dataclass
from pathlib import Path

# 姓名优先级：群昵称 > QQ 昵称 > QQ 号
_NAME_EXPR = """
    COALESCE(
        NULLIF(s.group_nick, ''),
        NULLIF(s.qq_nick, ''),
        CASE WHEN s.uin > 0 THEN CAST(s.uin AS TEXT) END,
        '未知'
    )
"""


@dataclass(frozen=True)
class Message:
    ts: int
    group_id: int
    sender: str
    uin: int
    content: str

    @property
    def time_str(self) -> str:
        if not self.ts:
            return "-"
        return dt.datetime.fromtimestamp(self.ts).strftime("%Y-%m-%d %H:%M:%S")


def _to_epoch(value: str | None) -> int | None:
    """把 YYYY-MM-DD（或 YYYY-MM-DD HH:MM）转成 unix 秒。"""
    if not value:
        return None
    for fmt in ("%Y-%m-%d %H:%M", "%Y-%m-%d"):
        try:
            return int(dt.datetime.strptime(value, fmt).timestamp())
        except ValueError:
            continue
    raise ValueError(f"时间格式应为 YYYY-MM-DD 或 'YYYY-MM-DD HH:MM'，实际是 {value!r}")


def read_messages(
    db_path: Path,
    *,
    group: int | None = None,
    sender: str | None = None,
    keyword: str | None = None,
    since: str | None = None,
    until: str | None = None,
    limit: int = 50,
) -> list[Message]:
    """按条件读消息，按时间倒序。所有条件都是可选的。"""
    where: list[str] = []
    params: list[object] = []

    if group is not None:
        where.append("m.group_id = ?")
        params.append(group)
    if sender:
        where.append(
            "(s.group_nick LIKE ? OR s.qq_nick LIKE ? OR CAST(s.uin AS TEXT) = ?)"
        )
        params += [f"%{sender}%", f"%{sender}%", sender]
    if keyword:
        where.append("m.content LIKE ?")
        params.append(f"%{keyword}%")

    since_ts, until_ts = _to_epoch(since), _to_epoch(until)
    if since_ts is not None:
        where.append("m.ts >= ?")
        params.append(since_ts)
    if until_ts is not None:
        where.append("m.ts < ?")
        params.append(until_ts)

    sql = f"""
        SELECT m.ts, m.group_id, m.content, {_NAME_EXPR} AS sender, COALESCE(s.uin, 0)
        FROM messages m
        LEFT JOIN sender_names s
               ON s.group_id = m.group_id AND s.uid = m.sender_uid
        {"WHERE " + " AND ".join(where) if where else ""}
        ORDER BY m.ts DESC
        LIMIT ?
    """
    params.append(limit)

    conn = sqlite3.connect(str(db_path))
    try:
        rows = conn.execute(sql, params).fetchall()
    finally:
        conn.close()

    return [Message(ts=r[0], group_id=r[1], content=r[2], sender=r[3], uin=r[4]) for r in rows]


def sender_stats(
    db_path: Path, *, group: int | None = None, since: str | None = None, limit: int = 20
) -> list[tuple[str, int, int]]:
    """谁最活跃：返回 [(姓名, 条数, QQ号)]，按条数降序。

    这是「锁定哪个人」的入口——先看谁在说，再去看他说了什么。
    """
    where, params = [], []
    if group is not None:
        where.append("m.group_id = ?")
        params.append(group)
    since_ts = _to_epoch(since)
    if since_ts is not None:
        where.append("m.ts >= ?")
        params.append(since_ts)

    sql = f"""
        SELECT {_NAME_EXPR} AS sender, COUNT(*) AS n, COALESCE(s.uin, 0)
        FROM messages m
        LEFT JOIN sender_names s
               ON s.group_id = m.group_id AND s.uid = m.sender_uid
        {"WHERE " + " AND ".join(where) if where else ""}
        GROUP BY sender, s.uin
        ORDER BY n DESC
        LIMIT ?
    """
    params.append(limit)

    conn = sqlite3.connect(str(db_path))
    try:
        return [(r[0], r[1], r[2]) for r in conn.execute(sql, params).fetchall()]
    finally:
        conn.close()


@dataclass(frozen=True)
class LocalImage:
    ts: int
    group_id: int
    sender: str
    md5: str
    ext: str
    path: str

    @property
    def time_str(self) -> str:
        if not self.ts:
            return "-"
        return dt.datetime.fromtimestamp(self.ts).strftime("%Y-%m-%d %H:%M")


def local_images(
    db_path: Path,
    *,
    group: int | None = None,
    since: str | None = None,
    limit: int = 20,
) -> list[LocalImage]:
    """本地**确实有文件**的图片，按时间倒序。

    只返回 path 非空的行——这些图可以直接打开（可能还要 OCR）。
    未缓存的（path 为 NULL，占多数）不在结果里：它们不是"丢了"，
    而是 QQ 压根没下载到本地（见 vigil/media.py）。
    """
    where = ["mm.path IS NOT NULL"]
    params: list[object] = []
    if group is not None:
        where.append("mm.group_id = ?")
        params.append(group)
    since_ts = _to_epoch(since)
    if since_ts is not None:
        where.append("mm.ts >= ?")
        params.append(since_ts)

    sql = f"""
        SELECT mm.ts, mm.group_id, {_NAME_EXPR} AS sender, mm.md5, mm.ext, mm.path
        FROM msg_media mm
        LEFT JOIN messages m ON m.msg_id = mm.msg_id
        LEFT JOIN sender_names s
               ON s.group_id = mm.group_id AND s.uid = m.sender_uid
        WHERE {" AND ".join(where)}
        ORDER BY mm.ts DESC
        LIMIT ?
    """
    params.append(limit)

    conn = sqlite3.connect(str(db_path))
    try:
        rows = conn.execute(sql, params).fetchall()
    finally:
        conn.close()
    return [LocalImage(*r) for r in rows]
