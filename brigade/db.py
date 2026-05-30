from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

PROJECT_ROOT = Path(__file__).resolve().parent.parent
SCHEMA_LOCK_ID = 4_258_201


def _default_migrations_root() -> Path:
    candidates = (
        PROJECT_ROOT / "migrations",
        Path.cwd() / "migrations",
        Path("/app/migrations"),
    )
    for candidate in candidates:
        if candidate.exists():
            return candidate
    return candidates[0]


DEFAULT_MIGRATIONS = _default_migrations_root()


@dataclass(frozen=True)
class Migration:
    path: Path
    sql: str

    @property
    def migration_id(self) -> str:
        return self.path.stem


@dataclass(frozen=True)
class MigrationReport:
    applied: list[str]
    skipped: list[str]
    warnings: list[str]
    failed: list[dict[str, str]] | None = None

    def to_dict(self) -> dict[str, Any]:
        failed = self.failed or []
        return {
            "applied": self.applied,
            "skipped": self.skipped,
            "warnings": self.warnings,
            "failed": failed,
            "applied_count": len(self.applied),
            "skipped_count": len(self.skipped),
            "failed_count": len(failed),
            "ok": not self.warnings and not failed,
        }


class MigrationApplyError(RuntimeError):
    def __init__(self, report: MigrationReport) -> None:
        self.report = report
        super().__init__("migration failed")


def load_migrations(root: Path = DEFAULT_MIGRATIONS) -> list[Migration]:
    if not root.exists():
        return []
    return [
        Migration(path=path, sql=path.read_text(encoding="utf-8"))
        for path in sorted(root.glob("*.sql"))
    ]


def combined_schema_sql(root: Path = DEFAULT_MIGRATIONS) -> str:
    return "\n\n".join(migration.sql for migration in load_migrations(root))


def ensure_schema(dsn: str, root: Path = DEFAULT_MIGRATIONS) -> None:
    apply_migrations(dsn, root=root)


def migration_status(dsn: str, root: Path = DEFAULT_MIGRATIONS) -> dict[str, Any]:
    migrations = load_migrations(root)
    applied = set(applied_migrations(dsn))
    known_ids = {migration.migration_id for migration in migrations}
    failed = failed_migrations(dsn)
    pending = [
        migration.migration_id
        for migration in migrations
        if migration.migration_id not in applied
    ]
    unknown = sorted(applied - known_ids)
    return {
        "migrations": [
            {
                "id": migration.migration_id,
                "path": str(migration.path),
                "applied": migration.migration_id in applied,
                "failed": any(item["id"] == migration.migration_id for item in failed),
                "state": _migration_state(migration.migration_id, applied, failed),
            }
            for migration in migrations
        ],
        "applied": sorted(applied),
        "pending": pending,
        "failed": failed,
        "unknown": unknown,
        "ok": not pending and not failed and not unknown,
    }


def applied_migrations(dsn: str) -> list[str]:
    psycopg = _import_psycopg()
    with psycopg.connect(dsn, autocommit=True) as conn:
        with conn.cursor() as cursor:
            cursor.execute(
                """
                create table if not exists brigade_schema_migrations (
                  id text primary key,
                  applied_at timestamptz not null default now()
                )
                """
            )
            cursor.execute("select id from brigade_schema_migrations order by id")
            return [str(row[0]) for row in cursor.fetchall()]


def failed_migrations(dsn: str) -> list[dict[str, str]]:
    psycopg = _import_psycopg()
    with psycopg.connect(dsn, autocommit=True) as conn:
        with conn.cursor() as cursor:
            _ensure_migration_tables(cursor)
            cursor.execute(
                """
                select id, failed_at::text, error
                from brigade_schema_migration_failures
                order by failed_at desc, id
                """
            )
            return [
                {"id": str(row[0]), "failed_at": str(row[1]), "error": str(row[2])}
                for row in cursor.fetchall()
            ]


def apply_migrations(dsn: str, root: Path = DEFAULT_MIGRATIONS) -> MigrationReport:
    psycopg = _import_psycopg()
    migrations = load_migrations(root)
    applied: list[str] = []
    skipped: list[str] = []
    warnings: list[str] = []

    with psycopg.connect(dsn, autocommit=True) as conn:
        with conn.cursor() as cursor:
            cursor.execute("select pg_advisory_lock(%s)", (SCHEMA_LOCK_ID,))
            try:
                _ensure_migration_tables(cursor)
                cursor.execute("select id from brigade_schema_migrations")
                rows = cursor.fetchall() if hasattr(cursor, "fetchall") else []
                seen = {str(row[0]) for row in rows}
                for migration in migrations:
                    migration_id = migration.migration_id
                    if migration_id in seen:
                        skipped.append(migration_id)
                        continue
                    try:
                        cursor.execute(migration.sql)
                        cursor.execute(
                            "insert into brigade_schema_migrations (id) values (%s)",
                            (migration_id,),
                        )
                        cursor.execute(
                            "delete from brigade_schema_migration_failures where id = %s",
                            (migration_id,),
                        )
                        applied.append(migration_id)
                        seen.add(migration_id)
                    except Exception as exc:
                        error = str(exc)
                        warnings.append(f"{migration_id}: {error}")
                        cursor.execute(
                            """
                            insert into brigade_schema_migration_failures (id, failed_at, error)
                            values (%s, now(), %s)
                            on conflict (id)
                            do update set failed_at = excluded.failed_at, error = excluded.error
                            """,
                            (migration_id, error),
                        )
                        report = MigrationReport(
                            applied=applied,
                            skipped=skipped,
                            warnings=warnings,
                            failed=[{"id": migration_id, "error": error}],
                        )
                        raise MigrationApplyError(report) from exc
            finally:
                cursor.execute("select pg_advisory_unlock(%s)", (SCHEMA_LOCK_ID,))

    return MigrationReport(applied=applied, skipped=skipped, warnings=warnings)


def _ensure_migration_tables(cursor: Any) -> None:
    cursor.execute(
        """
        create table if not exists brigade_schema_migrations (
          id text primary key,
          applied_at timestamptz not null default now()
        )
        """
    )
    cursor.execute(
        """
        create table if not exists brigade_schema_migration_failures (
          id text primary key,
          failed_at timestamptz not null default now(),
          error text not null
        )
        """
    )


def _migration_state(
    migration_id: str,
    applied: set[str],
    failed: list[dict[str, str]],
) -> str:
    if migration_id in applied:
        return "applied"
    if any(item["id"] == migration_id for item in failed):
        return "failed"
    return "pending"


def _import_psycopg():
    try:
        import psycopg
    except ImportError as exc:
        raise RuntimeError(
            "psycopg is required to initialize the Postgres repository layer. "
            "Use './ops/brigade-live.sh ...' for the running prototype, or install "
            "host dependencies with 'python3 -m pip install -e .'."
        ) from exc
    return psycopg
