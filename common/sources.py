"""Sources live in the database (TECH.md: Config model). Everything that writes validates first.

Validation is the same code the pipeline uses to load a profile: the row is merged with its profile YAML
and checked by `config_schema`, so a bad link, a missing cam box or a broken override never reaches the
table. There is deliberately no delete: removing a source would lose its claim history.
"""

from __future__ import annotations

import json
import uuid
from pathlib import Path
from typing import Any

import sqlalchemy as sa

import config_schema as cs

EDITABLE = {"name", "enabled", "clip_policy", "overrides"}
INSERT = (
    "INSERT INTO sources (profile, source_key, url, name, enabled, clip_policy, overrides, updated_by) "
    "VALUES (:profile, :key, :url, :name, :enabled, :clip_policy, CAST(:overrides AS jsonb), :by) RETURNING *"
)

Row = dict[str, Any]


def _profile_path(config_dir: Path, profile: str) -> Path:
    path = Path(config_dir) / "profiles" / f"{profile}.yaml"
    if profile.startswith("_") or "/" in profile or not path.is_file():
        raise ValueError(f"unknown profile {profile!r}")
    return path


def _as_source(row: Row) -> dict[str, Any]:
    """A database row in the shape `config_schema.load_profile` takes for a source."""
    source = {
        "url": row["url"],
        "id": row["source_key"],
        "enabled": row["enabled"],
        "clip_policy": row["clip_policy"],
        "overrides": row["overrides"],
    }
    if row.get("name"):
        source["name"] = row["name"]
    return source


def _validate(config_dir: Path, profile: str, source: dict[str, Any]) -> cs.Profile:
    """The source merged with its profile, exactly as the pipeline will load it. Raises ValueError."""
    (merged,) = cs.load_profile(_profile_path(config_dir, profile), sources=[source])
    return merged


def _get(conn: sa.Connection, source_id: uuid.UUID) -> Row:
    row = conn.execute(sa.text("SELECT * FROM sources WHERE id = :id"), {"id": source_id}).mappings().first()
    if row is None:
        raise ValueError(f"no such source: {source_id}")
    return dict(row)


def _insert(conn: sa.Connection, *, profile: str, source: cs.Source, updated_by: str) -> Row:
    return dict(
        conn.execute(
            sa.text(INSERT),
            {
                "profile": profile,
                "key": source.id,
                "url": source.url,
                "name": source.name,
                "enabled": source.enabled,
                "clip_policy": source.clip_policy,
                "overrides": json.dumps(source.overrides),
                "by": updated_by,
            },
        )
        .mappings()
        .one()
    )


def seed_from_yaml(conn: sa.Connection, config_dir: Path) -> int:
    """Load the `sources:` lists of the profile YAML into an EMPTY table. Returns how many were added.

    A database that already has sources is never touched: from then on the database owns them.
    """
    if conn.execute(sa.text("SELECT count(*) FROM sources")).scalar_one():
        return 0
    added = 0
    for path in sorted((Path(config_dir) / "profiles").glob("[!_]*.yaml")):
        for profile in cs.load_profile(path):
            _insert(conn, profile=path.stem, source=profile.source, updated_by="seed")
            added += 1
    return added


def list_sources(conn: sa.Connection, profile: str | None = None) -> list[Row]:
    rows = conn.execute(
        sa.text(
            "SELECT * FROM sources WHERE (CAST(:profile AS text) IS NULL OR profile = :profile) "
            "ORDER BY profile, source_key"
        ),
        {"profile": profile},
    ).mappings()
    return [dict(r) for r in rows]


def add_source(
    conn: sa.Connection,
    config_dir: Path,
    *,
    profile: str,
    url: str,
    updated_by: str,
    name: str | None = None,
    enabled: bool = True,
    clip_policy: str = "open",
    overrides: dict[str, Any] | None = None,
) -> Row:
    """Validate a new source against its profile and store it. Raises ValueError, storing nothing."""
    candidate: dict[str, Any] = {
        "url": url,
        "enabled": enabled,
        "clip_policy": clip_policy,
        "overrides": overrides or {},
    }
    if name:
        candidate["name"] = name
    merged = _validate(config_dir, profile, candidate)
    exists = conn.execute(
        sa.text("SELECT 1 FROM sources WHERE profile = :profile AND source_key = :key"),
        {"profile": profile, "key": merged.source.id},
    ).first()
    if exists:
        raise ValueError(f"source {merged.source.id!r} already exists in profile {profile!r}")
    return _insert(conn, profile=profile, source=merged.source, updated_by=updated_by)


def update_source(
    conn: sa.Connection,
    config_dir: Path,
    source_id: uuid.UUID,
    *,
    changes: dict[str, Any],
    updated_by: str,
    allow_reopen: bool = False,
) -> Row:
    """Change name, enabled, clip_policy or overrides, validated as a whole. Reopening a blocked source
    (blocked -> open) needs `allow_reopen`, which only the operator passes, never Hermes."""
    unknown = set(changes) - EDITABLE
    if unknown:
        raise ValueError(f"{sorted(unknown)} cannot be changed here (allowed: {sorted(EDITABLE)})")
    current = _get(conn, source_id)
    if current["clip_policy"] == "blocked" and changes.get("clip_policy") == "open" and not allow_reopen:
        raise PermissionError("a blocked source can only be reopened by the operator")
    _validate(config_dir, current["profile"], _as_source(current | changes))
    merged = current | changes
    conn.execute(
        sa.text(
            "UPDATE sources SET name = :name, enabled = :enabled, clip_policy = :clip_policy, "
            "overrides = CAST(:overrides AS jsonb), updated_at = clock_timestamp(), updated_by = :by WHERE id = :id"
        ),
        {
            "id": source_id,
            "name": merged["name"],
            "enabled": merged["enabled"],
            "clip_policy": merged["clip_policy"],
            "overrides": json.dumps(merged["overrides"]),
            "by": updated_by,
        },
    )
    return _get(conn, source_id)


def pause(conn: sa.Connection, source_id: uuid.UUID, *, updated_by: str) -> None:
    """Pause a source. An existing pause, in particular a claim pause, keeps its reason."""
    conn.execute(
        sa.text(
            "UPDATE sources SET paused_at = COALESCE(paused_at, clock_timestamp()), "
            "pause_reason = COALESCE(pause_reason, 'manual'), updated_at = clock_timestamp(), updated_by = :by "
            "WHERE id = :id"
        ),
        {"id": source_id, "by": updated_by},
    )


def resume(conn: sa.Connection, source_id: uuid.UUID, *, updated_by: str, allow_claim: bool = False) -> None:
    """Resume a source. A pause caused by a claim (FR-10) needs `allow_claim`: only the operator passes it."""
    if _get(conn, source_id)["pause_reason"] == "claim" and not allow_claim:
        raise PermissionError("a source paused after a claim can only be resumed by the operator")
    conn.execute(
        sa.text(
            "UPDATE sources SET paused_at = NULL, pause_reason = NULL, updated_at = clock_timestamp(), "
            "updated_by = :by WHERE id = :id"
        ),
        {"id": source_id, "by": updated_by},
    )


def record_claim(conn: sa.Connection, source_id: uuid.UUID) -> None:
    """A copyright claim, strike or objection: count it and pause the source (FR-10)."""
    conn.execute(
        sa.text(
            "UPDATE sources SET claim_count = claim_count + 1, paused_at = COALESCE(paused_at, clock_timestamp()), "
            "pause_reason = 'claim', updated_at = clock_timestamp(), updated_by = 'system' WHERE id = :id"
        ),
        {"id": source_id},
    )


def load_profiles(conn: sa.Connection, config_dir: Path) -> tuple[list[cs.Profile], list[str]]:
    """One merged profile per stored source. A row that fails validation is skipped and reported in the
    second value (so the caller can alert), and never stops the others from loading."""
    profiles: list[cs.Profile] = []
    problems: list[str] = []
    for row in list_sources(conn):
        try:
            profiles.append(_validate(config_dir, row["profile"], _as_source(row)))
        except ValueError as exc:
            problems.append(f"{row['profile']}/{row['source_key']}: {' '.join(str(exc).split())[:240]}")
    return profiles, problems
