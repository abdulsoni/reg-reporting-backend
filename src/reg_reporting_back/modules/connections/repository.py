"""Read and write access to the registered system connections.

Only the resolved target is persisted: db type, database name and the
non-secret connection parameters. Passwords are never stored, so a non-SQLite
system has to present its credentials again on each request that opens it.
"""

import json
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import text

from ...core.database import session

# Stored alongside the target so a stored connection can be reopened. `password`
# is deliberately absent: credentials are never written to disk.
RESERVED_PARAMS = ("host", "port", "username", "table_schema")


@dataclass(frozen=True, slots=True)
class SystemConnection:
    """A system the user has agreed to expose to the lineage explorer."""

    system_name: str
    db_type: str
    database_name: str
    connected: bool
    connected_at: str | None = None
    params: dict[str, Any] = field(default_factory=dict)


def _now() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


def _to_connection(row: Any) -> SystemConnection:
    raw_params = row["params_json"]
    try:
        params = json.loads(raw_params) if raw_params else {}
    except json.JSONDecodeError:  # pragma: no cover - defensive
        params = {}

    return SystemConnection(
        system_name=row["system_name"],
        db_type=row["db_type"],
        database_name=row["database_name"],
        connected=bool(row["connected"]),
        connected_at=row["connected_at"],
        params=params,
    )


class ConnectionRepository:

    @staticmethod
    def list_all() -> list[SystemConnection]:
        with session() as conn:
            rows = conn.execute(
                text(
                    """
                    SELECT system_name, db_type, database_name,
                           connected, connected_at, params_json
                    FROM system_connections
                    ORDER BY system_name
                    """
                )
            ).mappings().all()

        return [_to_connection(row) for row in rows]

    @staticmethod
    def list_connected() -> list[SystemConnection]:
        return [item for item in ConnectionRepository.list_all() if item.connected]

    @staticmethod
    def get(system_name: str) -> SystemConnection | None:
        for item in ConnectionRepository.list_all():
            if item.system_name == system_name:
                return item
        return None

    @staticmethod
    def is_connected(system_name: str) -> bool:
        item = ConnectionRepository.get(system_name)
        return bool(item and item.connected)

    @staticmethod
    def register(
        system_name: str,
        db_type: str,
        database_name: str,
        params: dict[str, Any] | None = None,
    ) -> SystemConnection:
        """Insert or update a system connection, leaving it connected."""

        safe_params = {key: value for key, value in (params or {}).items() if value is not None}

        with session() as conn:
            conn.execute(
                text(
                    """
                    INSERT INTO system_connections (
                        system_name, db_type, database_name, connected, connected_at, params_json
                    ) VALUES (:system_name, :db_type, :database_name, 1, :connected_at, :params_json)
                    ON CONFLICT(system_name) DO UPDATE SET
                        db_type = excluded.db_type,
                        database_name = excluded.database_name,
                        connected = 1,
                        connected_at = excluded.connected_at,
                        params_json = excluded.params_json
                    """
                ),
                {
                    "system_name": system_name,
                    "db_type": db_type,
                    "database_name": database_name,
                    "connected_at": _now(),
                    "params_json": json.dumps(safe_params),
                },
            )

        registered = ConnectionRepository.get(system_name)
        assert registered is not None  # noqa: S101 - just written
        return registered

    @staticmethod
    def disconnect(system_name: str) -> bool:
        """Mark a system as not connected. Returns True when it existed."""

        with session() as conn:
            result = conn.execute(
                text(
                    """
                    UPDATE system_connections
                    SET connected = 0, connected_at = NULL
                    WHERE system_name = :system_name
                    """
                ),
                {"system_name": system_name},
            )
            return bool(result.rowcount)

    @staticmethod
    def reset() -> None:
        """Forget every registered connection."""

        with session() as conn:
            conn.execute(text("DELETE FROM system_connections"))