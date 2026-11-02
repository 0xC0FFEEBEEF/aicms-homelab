from __future__ import annotations

from sqlalchemy import inspect, text
from sqlalchemy.engine import Engine


def migrate_database(engine: Engine) -> None:
    inspector = inspect(engine)

    with engine.begin() as connection:
        existing_tables = set(inspector.get_table_names())

        if "hosts" in existing_tables:
            _ensure_column(connection, inspector, "hosts", "parent_host_id", "INTEGER")
            _ensure_column(connection, inspector, "hosts", "parent_platform", "VARCHAR(255)")
            _ensure_column(connection, inspector, "hosts", "parent_name", "VARCHAR(255)")
            _ensure_column(connection, inspector, "hosts", "guest_type", "VARCHAR(32)")
            _ensure_column(connection, inspector, "hosts", "guest_id", "VARCHAR(64)")
            _ensure_column(connection, inspector, "hosts", "relationship_source", "VARCHAR(64)")
            _ensure_column(connection, inspector, "hosts", "relationship_confidence", "FLOAT")
            _ensure_column(connection, inspector, "hosts", "last_relationship_verified_at", "DATETIME")

        if "guest_inventory" in existing_tables:
            _ensure_column(connection, inspector, "guest_inventory", "linked_host_id", "INTEGER")
            _ensure_column(connection, inspector, "guest_inventory", "management_state", "VARCHAR(32) DEFAULT 'discovered' NOT NULL")
            _ensure_column(connection, inspector, "guest_inventory", "relationship_source", "VARCHAR(64)")
            _ensure_column(connection, inspector, "guest_inventory", "relationship_confidence", "FLOAT")
            _ensure_column(connection, inspector, "guest_inventory", "last_relationship_verified_at", "DATETIME")

        _ensure_app_table(connection, "machine_identity_results")
        _ensure_app_table(connection, "signature_matches")
        _ensure_app_table(connection, "project_mapping_previews")
        _ensure_app_table(connection, "host_profile_overrides")
        _ensure_app_table(connection, "project_question_threads")
        _ensure_app_table(connection, "project_question_messages")
        _ensure_app_table(connection, "project_clarification_guidance")
        _ensure_app_table(connection, "research_runs")
        _ensure_app_table(connection, "research_sources")
        _ensure_app_table(connection, "research_findings")
        _ensure_app_table(connection, "investigation_sessions")
        _ensure_app_table(connection, "investigation_messages")
        _ensure_app_table(connection, "investigation_findings")
        _ensure_app_table(connection, "investigation_drafts")
        _ensure_app_table(connection, "investigation_proposals")

        if "project_mapping_previews" in existing_tables:
            _ensure_column(connection, inspector, "project_mapping_previews", "card_title_candidate", "VARCHAR(255)")
            _ensure_column(connection, inspector, "project_mapping_previews", "card_summary_candidate", "TEXT")
            _ensure_column(connection, inspector, "project_mapping_previews", "card_tech_tags_candidate_json", "JSON")
            _ensure_column(connection, inspector, "project_mapping_previews", "publish_title_candidate", "VARCHAR(255)")
            _ensure_column(connection, inspector, "project_mapping_previews", "publish_summary_candidate", "TEXT")
            _ensure_column(connection, inspector, "project_mapping_previews", "publish_long_description_candidate", "TEXT")
            _ensure_column(connection, inspector, "project_mapping_previews", "publish_sections_json", "JSON")


def _ensure_column(connection, inspector, table_name: str, column_name: str, ddl: str) -> None:
    columns = {column["name"] for column in inspector.get_columns(table_name)}
    if column_name in columns:
        return
    connection.execute(text(f"ALTER TABLE {table_name} ADD COLUMN {column_name} {ddl}"))


def _ensure_app_table(connection, table_name: str) -> None:
    from app.models import Base

    table = Base.metadata.tables.get(table_name)
    if table is None:
        return
    table.create(bind=connection, checkfirst=True)
