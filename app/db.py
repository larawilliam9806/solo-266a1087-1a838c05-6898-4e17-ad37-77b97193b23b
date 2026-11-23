"""SQLite 持久层。

所有写操作都在 ``BEGIN IMMEDIATE`` 事务里完成：解析清单、校验帧、重算、
写库任何一步失败都会回滚，数据库保留提交前的数据。
"""

from __future__ import annotations

import json
import os
import sqlite3
from datetime import datetime, timezone
from typing import Any, Iterable

from . import coverage


SCHEMA = """
CREATE TABLE IF NOT EXISTS issues (
    issue_id     TEXT PRIMARY KEY,
    plates_json  TEXT NOT NULL,
    created_at   TEXT NOT NULL,
    updated_at   TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS volumes (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    issue_id     TEXT NOT NULL REFERENCES issues(issue_id) ON DELETE CASCADE,
    volume_code  TEXT NOT NULL,
    frames_json  TEXT NOT NULL,
    result_json  TEXT NOT NULL,
    created_at   TEXT NOT NULL,
    updated_at   TEXT NOT NULL,
    UNIQUE(issue_id, volume_code)
);

CREATE INDEX IF NOT EXISTS idx_volumes_issue ON volumes(issue_id);
"""


class StorageError(RuntimeError):
    """数据库读写失败。"""

    def __init__(self, message: str) -> None:
        super().__init__(message)
        self.message = message


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="microseconds")


def _json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"))


def _frames_to_json(frames: Iterable[coverage.Frame]) -> str:
    return _json(
        [{"frame_no": f.frame_no, "candidates": list(f.candidates)} for f in frames]
    )


def _stored_frames(raw: str) -> list[coverage.Frame]:
    return [
        coverage.Frame(frame_no=int(item["frame_no"]), candidates=tuple(item["candidates"]))
        for item in json.loads(raw)
    ]


class MissingRosterError(ValueError):
    def __init__(self) -> None:
        super().__init__("该期尚未登记版号清单，首次提交必须携带 plates")
        self.message = str(self)


class Store:
    def __init__(self, db_path: str) -> None:
        self.db_path = db_path

    # -- 连接 ---------------------------------------------------------------

    def connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(
            self.db_path,
            timeout=30,
            isolation_level=None,
            check_same_thread=False,
        )
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys = ON")
        conn.execute("PRAGMA busy_timeout = 30000")
        return conn

    def initialize(self) -> None:
        directory = os.path.dirname(os.path.abspath(self.db_path))
        os.makedirs(directory, exist_ok=True)
        conn = self.connect()
        try:
            conn.execute("PRAGMA journal_mode = WAL")
            conn.executescript(SCHEMA)
        finally:
            conn.close()

    def ping(self) -> bool:
        conn = self.connect()
        try:
            conn.execute("SELECT 1").fetchone()
            return True
        finally:
            conn.close()

    # -- 写入 ---------------------------------------------------------------

    def _load_issue(
        self, conn: sqlite3.Connection, issue_id: str
    ) -> tuple[bool, list[str] | None]:
        row = conn.execute(
            "SELECT plates_json FROM issues WHERE issue_id = ?", (issue_id,)
        ).fetchone()
        if row is None:
            return True, None
        return False, json.loads(row["plates_json"])

    def _recompute_volumes(
        self,
        conn: sqlite3.Connection,
        issue_id: str,
        roster: list[str],
        now: str,
        skip_volume: str | None = None,
    ) -> list[str]:
        """用新清单重算该期所有卷；任何一卷排不通都会抛异常由调用方回滚。"""

        rows = conn.execute(
            "SELECT volume_code, frames_json FROM volumes WHERE issue_id = ? "
            "ORDER BY volume_code",
            (issue_id,),
        ).fetchall()
        recomputed: list[str] = []
        for row in rows:
            code = row["volume_code"]
            if code == skip_volume:
                # 该卷随后用本次提交的新内容覆盖。
                continue
            other = coverage.compute_volume(roster, _stored_frames(row["frames_json"]))
            conn.execute(
                "UPDATE volumes SET result_json = ?, updated_at = ? "
                "WHERE issue_id = ? AND volume_code = ?",
                (_json(other), now, issue_id, code),
            )
            recomputed.append(code)
        return recomputed

    def set_roster(self, issue_id: str, plates: list[str]) -> dict[str, Any]:
        """单独登记或更换全期清单，并重判该期所有卷（全有或全无）。"""

        roster = coverage.normalize_roster(plates)
        now = utc_now()
        conn = self.connect()
        try:
            conn.execute("BEGIN IMMEDIATE")
            issue_created, existing_roster = self._load_issue(conn, issue_id)
            roster_changed = issue_created or existing_roster != roster

            if issue_created:
                conn.execute(
                    "INSERT INTO issues (issue_id, plates_json, created_at, updated_at) "
                    "VALUES (?, ?, ?, ?)",
                    (issue_id, _json(roster), now, now),
                )
                recomputed: list[str] = []
            elif roster_changed:
                conn.execute(
                    "UPDATE issues SET plates_json = ?, updated_at = ? WHERE issue_id = ?",
                    (_json(roster), now, issue_id),
                )
                recomputed = self._recompute_volumes(conn, issue_id, roster, now)
            else:
                recomputed = []

            conn.execute("COMMIT")
        except (coverage.ArrangementError, MissingRosterError, StorageError):
            _rollback(conn)
            raise
        except sqlite3.Error as exc:
            _rollback(conn)
            raise StorageError(f"写入数据库失败：{exc}") from exc
        except Exception:
            _rollback(conn)
            raise
        finally:
            conn.close()

        return {
            "issue_id": issue_id,
            "issue_created": issue_created,
            "roster_changed": roster_changed,
            "recomputed_volume_codes": recomputed,
        }

    def submit_volume(
        self,
        issue_id: str,
        volume_code: str,
        plates: list[str] | None,
        raw_frames: list[dict[str, Any]],
    ) -> dict[str, Any]:
        """原子提交“新卷 + 全期清单”。

        * 清单未变：只重判本卷；
        * 清单变化：用新清单重判该期所有卷，任何一卷排不通都整体回滚；
        * 任何写入失败同样整体回滚，旧数据原样保留。
        """

        conn = self.connect()
        try:
            conn.execute("BEGIN IMMEDIATE")
            issue_created, existing_roster = self._load_issue(conn, issue_id)

            if plates is None:
                if issue_created:
                    raise MissingRosterError()
                roster = existing_roster
            else:
                roster = coverage.normalize_roster(plates)

            roster_changed = issue_created or existing_roster != roster

            frames = coverage.normalize_frames(raw_frames, roster)
            result = coverage.compute_volume(roster, frames)

            now = utc_now()
            if issue_created:
                conn.execute(
                    "INSERT INTO issues (issue_id, plates_json, created_at, updated_at) "
                    "VALUES (?, ?, ?, ?)",
                    (issue_id, _json(roster), now, now),
                )
            elif roster_changed:
                conn.execute(
                    "UPDATE issues SET plates_json = ?, updated_at = ? WHERE issue_id = ?",
                    (_json(roster), now, issue_id),
                )

            recomputed: list[str] = []
            if roster_changed:
                recomputed = self._recompute_volumes(
                    conn, issue_id, roster, now, skip_volume=volume_code
                )

            existing = conn.execute(
                "SELECT id, created_at FROM volumes WHERE issue_id = ? AND volume_code = ?",
                (issue_id, volume_code),
            ).fetchone()

            if existing is None:
                created_at = now
                conn.execute(
                    "INSERT INTO volumes "
                    "(issue_id, volume_code, frames_json, result_json, created_at, updated_at) "
                    "VALUES (?, ?, ?, ?, ?, ?)",
                    (issue_id, volume_code, _frames_to_json(frames), _json(result), now, now),
                )
            else:
                created_at = existing["created_at"]
                conn.execute(
                    "UPDATE volumes SET frames_json = ?, result_json = ?, updated_at = ? "
                    "WHERE issue_id = ? AND volume_code = ?",
                    (_frames_to_json(frames), _json(result), now, issue_id, volume_code),
                )

            conn.execute("COMMIT")
        except (
            coverage.ArrangementError,
            MissingRosterError,
            StorageError,
        ):
            _rollback(conn)
            raise
        except sqlite3.Error as exc:
            _rollback(conn)
            raise StorageError(f"写入数据库失败：{exc}") from exc
        except Exception:
            _rollback(conn)
            raise
        finally:
            conn.close()

        return {
            "issue_id": issue_id,
            "volume_code": volume_code,
            "issue_created": issue_created,
            "roster_changed": roster_changed,
            "recomputed_volume_codes": recomputed,
            "volume": _volume_view(
                {
                    "volume_code": volume_code,
                    "frames": [
                        {"frame_no": f.frame_no, "candidates": list(f.candidates)}
                        for f in frames
                    ],
                    "result": result,
                    "created_at": created_at,
                    "updated_at": now,
                }
            ),
        }

    # -- 读取 ---------------------------------------------------------------

    def get_issue(self, issue_id: str) -> dict[str, Any] | None:
        conn = self.connect()
        try:
            row = conn.execute(
                "SELECT issue_id, plates_json, created_at, updated_at "
                "FROM issues WHERE issue_id = ?",
                (issue_id,),
            ).fetchone()
            if row is None:
                return None
            return {
                "issue_id": row["issue_id"],
                "plates": json.loads(row["plates_json"]),
                "created_at": row["created_at"],
                "updated_at": row["updated_at"],
            }
        finally:
            conn.close()

    def list_issues(self) -> list[dict[str, Any]]:
        conn = self.connect()
        try:
            rows = conn.execute(
                "SELECT i.issue_id, i.plates_json, i.created_at, i.updated_at, "
                "       COUNT(v.id) AS volume_count "
                "FROM issues i LEFT JOIN volumes v ON v.issue_id = i.issue_id "
                "GROUP BY i.issue_id ORDER BY i.issue_id"
            ).fetchall()
            return [
                {
                    "issue_id": row["issue_id"],
                    "plate_count": len(json.loads(row["plates_json"])),
                    "volume_count": row["volume_count"],
                    "created_at": row["created_at"],
                    "updated_at": row["updated_at"],
                }
                for row in rows
            ]
        finally:
            conn.close()

    def get_volumes(self, issue_id: str) -> list[dict[str, Any]]:
        conn = self.connect()
        try:
            rows = conn.execute(
                "SELECT volume_code, frames_json, result_json, created_at, updated_at "
                "FROM volumes WHERE issue_id = ? ORDER BY volume_code",
                (issue_id,),
            ).fetchall()
            return [
                {
                    "volume_code": row["volume_code"],
                    "frames": json.loads(row["frames_json"]),
                    "result": json.loads(row["result_json"]),
                    "created_at": row["created_at"],
                    "updated_at": row["updated_at"],
                }
                for row in rows
            ]
        finally:
            conn.close()

    def get_volume(self, issue_id: str, volume_code: str) -> dict[str, Any] | None:
        conn = self.connect()
        try:
            row = conn.execute(
                "SELECT volume_code, frames_json, result_json, created_at, updated_at "
                "FROM volumes WHERE issue_id = ? AND volume_code = ?",
                (issue_id, volume_code),
            ).fetchone()
            if row is None:
                return None
            return {
                "volume_code": row["volume_code"],
                "frames": json.loads(row["frames_json"]),
                "result": json.loads(row["result_json"]),
                "created_at": row["created_at"],
                "updated_at": row["updated_at"],
            }
        finally:
            conn.close()


def _volume_view(volume: dict[str, Any]) -> dict[str, Any]:
    return {
        "volume_code": volume["volume_code"],
        "frames": volume["frames"],
        "span": volume["result"]["span"],
        "pages": volume["result"]["pages"],
        "created_at": volume["created_at"],
        "updated_at": volume["updated_at"],
    }


def volume_view(volume: dict[str, Any]) -> dict[str, Any]:
    """对外的单卷视图（隐藏内部 result 包装）。"""
    return _volume_view(volume)


def _rollback(conn: sqlite3.Connection) -> None:
    try:
        conn.execute("ROLLBACK")
    except sqlite3.Error:
        pass
