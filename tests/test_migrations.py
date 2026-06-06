from __future__ import annotations

from pathlib import Path


def test_core_migration_uses_brigade_table_prefixes():
    sql = Path("migrations/0001_core_state.sql").read_text(encoding="utf-8")

    assert "create table if not exists brigade_missions" in sql
    assert "create table if not exists brigade_assignments" in sql
    assert "create table if not exists brigade_orchestrator_reasoning" in sql
    assert "create table if not exists brigade_users" in sql
    assert "create table if not exists brigade_chat_messages" in sql
    assert "create table if not exists brigade_knowledge_documents" in sql
    assert "create table if not exists missions" not in sql


def test_assignment_execution_claims_migration_exists():
    sql = Path("migrations/0003_assignment_execution_claims.sql").read_text(encoding="utf-8")

    assert "create table if not exists brigade_assignment_execution_claims" in sql
    assert "assignment_id text primary key" in sql
    assert "run_owner text not null" in sql


def test_removed_ui_layouts_migration_is_tombstoned():
    sql = Path("migrations/0005_ui_layouts.sql").read_text(encoding="utf-8")

    assert "select 1;" in sql
    assert "brigade_ui_layouts" not in sql


def test_external_connections_migration_exists():
    sql = Path("migrations/0006_external_connections.sql").read_text(encoding="utf-8")

    assert "create table if not exists brigade_connector_audit_events" in sql
    assert "create table if not exists brigade_external_identities" in sql
    assert "external_user_id text not null" in sql
    assert "redacted_metadata jsonb not null" in sql


def test_assignment_idempotency_migration_exists():
    sql = Path("migrations/0007_assignment_idempotency.sql").read_text(encoding="utf-8")

    assert "brigade_assignments_idempotency_key_unique_idx" in sql
    assert "where idempotency_key is not null" in sql
