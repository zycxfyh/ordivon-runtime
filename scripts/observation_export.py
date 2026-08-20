#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import socket
import sqlite3
import stat
import subprocess
import sys
import time
from typing import Any
from urllib.parse import quote

MAPPING_VERSION = "runtime-observation-v1"
PROJECT_ID = "ordivon-runtime"
COMPONENT_ID = "runtime-registry"
SCHEMA_VERSION = 4
DEFAULT_REGISTRY_ROOT = Path("/var/lib/ordivon/registry")
DEFAULT_OBSERVATION_ROOT = Path("/var/lib/ordivon/observation/exporters/runtime-registry")


class RuntimeObservationExportError(RuntimeError):
    pass


def _core() -> Any:
    try:
        import ordivon_observation_core as core
    except ImportError as error:
        raise RuntimeObservationExportError(
            "install the exact ordivon-observation-core exporter contract"
        ) from error
    return core


def _revision(value: str, label: str) -> str:
    if len(value) != 40 or any(ch not in "0123456789abcdef" for ch in value):
        raise ValueError(f"{label} must be an exact 40-character Git revision")
    return value


def _default_instance_id() -> str:
    hostname = socket.gethostname().strip()
    return hostname or "local"


def _git_head(repo_root: Path) -> str:
    result = subprocess.run(
        ["git", "-C", str(repo_root), "rev-parse", "HEAD"],
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode != 0:
        raise RuntimeObservationExportError(
            "cannot auto-detect a Git revision; pass --owner-revision and "
            "--exporter-revision explicitly"
        )
    return _revision(result.stdout.strip(), "git revision")


def _render_timeline(
    native_events: tuple[tuple[dict[str, Any], ...], ...],
) -> str:
    if not native_events:
        return "(no Runtime Registry events to export)"
    lines: list[str] = []
    for stream in native_events:
        if not stream:
            continue
        lines.append(f"runtime-job:{stream[0]['jobId']}")
        for row in stream:
            when = time.strftime(
                "%Y-%m-%d %H:%M:%S", time.localtime(row["observedAtMs"] / 1000)
            )
            attempt = f" attempt={row['attemptId']}" if row["attemptId"] else ""
            reason = f" ({row['reasonCode']})" if row["reasonCode"] else ""
            lines.append(
                f"  #{row['eventSequence']:>3}  {when}  "
                f"{row['eventType']}  {row['previousState'] or '-'} -> "
                f"{row['newState'] or '-'}{reason}{attempt}"
            )
    return "\n".join(lines)


def _private_directory(path: Path, label: str, *, create: bool) -> Path:
    value = path.expanduser()
    if value.is_symlink():
        raise RuntimeObservationExportError(f"{label} cannot be a symlink")
    if not value.exists():
        if not create:
            raise RuntimeObservationExportError(f"{label} does not exist")
        value.mkdir(parents=True, mode=0o700)
        os.chmod(value, 0o700)
    resolved = value.resolve(strict=True)
    if not resolved.is_dir() or stat.S_IMODE(resolved.stat().st_mode) != 0o700:
        raise RuntimeObservationExportError(f"{label} must be a private 0700 directory")
    return resolved


def _outside_owner(path: Path, owner_root: Path, label: str) -> None:
    resolved = path.expanduser().resolve(strict=False)
    if resolved == owner_root or owner_root in resolved.parents:
        raise RuntimeObservationExportError(
            f"{label} must remain outside the Runtime registry root"
        )


def _database(root: Path) -> Path:
    database = root / "registry.sqlite3"
    if database.is_symlink() or not database.is_file():
        raise RuntimeObservationExportError(
            "Runtime registry must be a regular non-symlink file"
        )
    if stat.S_IMODE(database.stat().st_mode) != 0o600:
        raise RuntimeObservationExportError("Runtime registry must have mode 0600")
    return database


def _connection(database: Path) -> sqlite3.Connection:
    uri = f"file:{quote(str(database), safe='/')}?mode=ro"
    connection = sqlite3.connect(uri, uri=True)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA query_only = ON")
    connection.execute("PRAGMA foreign_keys = ON")
    row = connection.execute("SELECT MAX(version) AS version FROM schema_migrations").fetchone()
    if row is None or int(row["version"] or 0) != SCHEMA_VERSION:
        connection.close()
        raise RuntimeObservationExportError(
            f"Runtime schema must be exactly {SCHEMA_VERSION}"
        )
    return connection


def _relation_digest(value: str, label: str) -> str:
    candidate = value if value.startswith("sha256:") else value[value.rfind("sha256:") :]
    if (
        len(candidate) != 71
        or not candidate.startswith("sha256:")
        or any(ch not in "0123456789abcdef" for ch in candidate[7:])
    ):
        raise RuntimeObservationExportError(
            f"{label} does not contain a canonical SHA-256 digest"
        )
    return candidate


def _relations(core: Any, row: sqlite3.Row) -> tuple[Any, ...]:
    values = [
        core.ObservationRelation(
            "belongs_to", "ordivon.runtime.job", row["job_id"]
        ),
        core.ObservationRelation(
            "requested_by",
            "ordivon.runtime.client-request",
            row["client_request_id"],
        ),
        core.ObservationRelation(
            "linked_to", "ordivon.runtime.workspace", row["workspace_id"]
        ),
        core.ObservationRelation(
            "references",
            "ordivon.runtime.request",
            row["request_digest"],
            _relation_digest(row["request_digest"], "request_digest"),
        ),
        core.ObservationRelation(
            "references",
            "ordivon.runtime.operation",
            row["operation_digest"],
            _relation_digest(row["operation_digest"], "operation_digest"),
        ),
    ]
    if row["attempt_id"] is not None:
        values.append(
            core.ObservationRelation(
                "linked_to", "ordivon.runtime.attempt", row["attempt_id"]
            )
        )
    if row["previous_event_id"] is not None:
        values.append(
            core.ObservationRelation(
                "caused_by", "ordivon.runtime.event", row["previous_event_id"]
            )
        )
    return tuple(sorted(set(values)))


def _read_events(
    registry_root: Path,
    *,
    producer: Any,
    checkpoint: Any,
    job_limit: int,
    event_limit_per_job: int,
    job_ids: tuple[str, ...],
    preview: bool = False,
) -> tuple[
    tuple[Any, ...],
    dict[str, int],
    int,
    int,
    tuple[tuple[dict[str, Any], ...], ...],
]:
    core = _core()
    connection = _connection(_database(registry_root))
    try:
        connection.execute("BEGIN")
        total_jobs = int(connection.execute("SELECT COUNT(*) FROM jobs").fetchone()[0])
        job_columns = (
            "SELECT job_id,client_request_id,request_digest,operation_digest,"
            "workspace_id,execution_plan_digest,created_at_ms FROM jobs "
        )
        if job_ids:
            if len(job_ids) > job_limit:
                raise RuntimeObservationExportError(
                    f"selected Runtime Job count {len(job_ids)} exceeds bounded job_limit {job_limit}"
                )
            placeholders = ",".join("?" for _ in job_ids)
            jobs = connection.execute(
                job_columns
                + f"WHERE job_id IN ({placeholders}) ORDER BY created_at_ms,job_id",
                job_ids,
            ).fetchall()
            observed_ids = {str(row["job_id"]) for row in jobs}
            missing = [job_id for job_id in job_ids if job_id not in observed_ids]
            if missing:
                raise RuntimeObservationExportError(
                    "selected Runtime Jobs are absent: " + ", ".join(missing)
                )
        else:
            if not preview and total_jobs > job_limit:
                raise RuntimeObservationExportError(
                    f"Runtime Job count {total_jobs} exceeds bounded job_limit {job_limit}"
                )
            jobs = connection.execute(
                job_columns + "ORDER BY created_at_ms,job_id LIMIT ?",
                (job_limit,),
            ).fetchall()
        all_events: list[tuple[Any, ...]] = []
        all_native: list[tuple[dict[str, Any], ...]] = []
        updates: dict[str, int] = {}
        for job in jobs:
            stream_id = f"runtime-job:{job['job_id']}"
            rows = connection.execute(
                "SELECT e.event_id,e.job_id,e.attempt_id,e.event_sequence,e.event_type,"
                "e.origin,e.previous_state,e.new_state,e.reason_code,e.detail_digest,"
                "e.observed_at_ms,j.client_request_id,j.request_digest,j.operation_digest,"
                "j.workspace_id,j.execution_plan_digest,j.created_at_ms,"
                "(SELECT p.event_id FROM job_events p WHERE p.job_id=e.job_id "
                "AND p.event_sequence=e.event_sequence-1) AS previous_event_id "
                "FROM job_events e JOIN jobs j ON j.job_id=e.job_id "
                "WHERE e.job_id=? AND e.event_sequence>? "
                "ORDER BY e.event_sequence LIMIT ?",
                (
                    job["job_id"],
                    checkpoint.sequence(stream_id),
                    event_limit_per_job,
                ),
            ).fetchall()
            mapped: list[Any] = []
            native_rows: list[dict[str, Any]] = []
            for row in rows:
                native = {
                    "eventId": row["event_id"],
                    "jobId": row["job_id"],
                    "attemptId": row["attempt_id"],
                    "eventSequence": int(row["event_sequence"]),
                    "eventType": row["event_type"],
                    "origin": row["origin"],
                    "previousState": row["previous_state"],
                    "newState": row["new_state"],
                    "reasonCode": row["reason_code"],
                    "detailDigest": row["detail_digest"],
                    "observedAtMs": int(row["observed_at_ms"]),
                    "clientRequestId": row["client_request_id"],
                    "requestDigest": row["request_digest"],
                    "operationDigest": row["operation_digest"],
                    "workspaceId": row["workspace_id"],
                    "executionPlanDigest": row["execution_plan_digest"],
                    "jobCreatedAtMs": int(row["created_at_ms"]),
                    "previousEventId": row["previous_event_id"],
                }
                native_rows.append(native)
                source = core.ObservationSource(
                    project_id=PROJECT_ID,
                    component_id=COMPONENT_ID,
                    instance_id=producer.instance_id,
                    stream_id=stream_id,
                    sequence=int(row["event_sequence"]),
                    native_kind=(
                        "ordivon.runtime."
                        + str(row["event_type"]).lower().replace("_", "-")
                    ),
                    native_id=row["event_id"],
                    native_revision=None,
                    native_digest=core.canonical_digest(native),
                    mapping_version=MAPPING_VERSION,
                )
                mapped.append(
                    core.ObservationEnvelope.build(
                        occurred_at_ms=int(row["observed_at_ms"]),
                        source=source,
                        relations=_relations(core, row),
                        attributes={
                            "eventType": row["event_type"],
                            "origin": row["origin"],
                            "previousState": row["previous_state"],
                            "newState": row["new_state"],
                            "reasonCode": row["reason_code"],
                            "hasAttempt": row["attempt_id"] is not None,
                        },
                        privacy=core.ObservationPrivacy(
                            "private_content_ref", "runtime-observation-metadata-v1"
                        ),
                        payload_ref=core.ObservationPayloadRef(
                            owner=PROJECT_ID,
                            kind="ordivon.runtime.event-detail",
                            native_id=row["event_id"],
                            digest_value=row["detail_digest"],
                            locator_class="owner_store",
                        ),
                    )
                )
            if mapped:
                all_events.append(tuple(mapped))
                all_native.append(tuple(native_rows))
                updates[stream_id] = mapped[-1].source.sequence
        connection.rollback()
        return tuple(all_events), updates, len(jobs), total_jobs, tuple(all_native)
    finally:
        connection.close()


def export_runtime_observations(
    *,
    registry_root: str | Path,
    instance_id: str,
    checkpoint_path: str | Path,
    outbox_root: str | Path,
    owner_revision: str,
    exporter_revision: str,
    exported_at_ms: int,
    job_limit: int = 1_000,
    event_limit_per_job: int = 256,
    job_ids: tuple[str, ...] = (),
    fail_after_bundle: bool = False,
    dry_run: bool = False,
    human: bool = False,
) -> dict[str, Any]:
    core = _core()
    if not instance_id or instance_id != instance_id.strip():
        raise ValueError("instance_id must be non-empty and trimmed")
    if not 1 <= job_limit <= 10_000:
        raise ValueError("job_limit must be between 1 and 10000")
    if not 1 <= event_limit_per_job <= 10_000:
        raise ValueError("event_limit_per_job must be between 1 and 10000")
    selected_job_ids = tuple(job_ids)
    if any(
        not isinstance(job_id, str)
        or not job_id
        or job_id != job_id.strip()
        for job_id in selected_job_ids
    ):
        raise ValueError("job_ids must contain non-empty trimmed strings")
    if len(selected_job_ids) != len(set(selected_job_ids)):
        raise ValueError("job_ids must be unique")
    selected_job_ids = tuple(sorted(selected_job_ids))
    _revision(owner_revision, "owner_revision")
    _revision(exporter_revision, "exporter_revision")
    if exported_at_ms < 0:
        raise ValueError("exported_at_ms must be non-negative")
    owner_root = _private_directory(
        Path(registry_root), "Runtime registry root", create=False
    )
    checkpoint_path_value = Path(checkpoint_path)
    outbox = Path(outbox_root)
    _outside_owner(checkpoint_path_value, owner_root, "checkpoint")
    _outside_owner(outbox, owner_root, "outbox")
    producer = core.ObservationProducerIdentity(PROJECT_ID, COMPONENT_ID, instance_id)
    before = core.load_checkpoint(
        checkpoint_path_value,
        producer_identity=producer,
        mapping_version=MAPPING_VERSION,
    )
    stream_events, updates, job_count, registry_job_count, native_events = _read_events(
        owner_root,
        producer=producer,
        checkpoint=before,
        job_limit=job_limit,
        event_limit_per_job=event_limit_per_job,
        job_ids=selected_job_ids,
        preview=dry_run or human,
    )
    if dry_run or human:
        event_count = sum(len(events) for events in stream_events)
        preview: dict[str, Any] = {
            "schemaVersion": 1,
            "kind": "ordivon.runtime-observation-export-result",
            "status": "preview",
            "eventCount": event_count,
            "streamCount": len(stream_events),
            "jobCount": job_count,
            "registryJobCount": registry_job_count,
            "checkpointDigest": before.integrity_digest,
        }
        if human:
            preview["timeline"] = _render_timeline(native_events)
        return preview
    if not stream_events:
        return {
            "schemaVersion": 1,
            "kind": "ordivon.runtime-observation-export-result",
            "status": "no_events",
            "eventCount": 0,
            "streamCount": 0,
            "jobCount": job_count,
            "registryJobCount": registry_job_count,
            "checkpointDigest": before.integrity_digest,
            "bundlePath": None,
            "bundleDigest": None,
        }
    after = before.advance(updates, updated_at_ms=exported_at_ms)
    batches = tuple(
        core.ObservationBatch.build(
            request_id=(
                f"runtime-observation:{instance_id}:"
                f"{chunk[0].source.stream_id}:{chunk[0].source.sequence}-"
                f"{chunk[-1].source.sequence}"
            ),
            events=chunk,
        )
        for events in stream_events
        for offset in range(0, len(events), core.MAX_BATCH_EVENTS)
        if (chunk := events[offset : offset + core.MAX_BATCH_EVENTS])
    )
    bundle = core.ObservationExportBundle.build(
        producer_identity=producer,
        mapping_version=MAPPING_VERSION,
        owner_revision=owner_revision,
        exporter_revision=exporter_revision,
        exported_at_ms=exported_at_ms,
        checkpoint_before=before,
        checkpoint_after=after,
        batches=batches,
    )
    bundle_path = core.write_export_bundle(outbox, bundle)
    if fail_after_bundle:
        raise RuntimeObservationExportError("injected failure after durable bundle")
    core.write_checkpoint(
        checkpoint_path_value,
        after,
        expected_digest=(before.integrity_digest if checkpoint_path_value.exists() else None),
    )
    event_count = sum(len(events) for events in stream_events)
    return {
        "schemaVersion": 1,
        "kind": "ordivon.runtime-observation-export-result",
        "status": "exported",
        "ownerRevision": owner_revision,
        "exporterRevision": exporter_revision,
        "eventCount": event_count,
        "streamCount": len(stream_events),
        "jobCount": job_count,
        "registryJobCount": registry_job_count,
        "batchCount": len(batches),
        "checkpointBeforeDigest": before.integrity_digest,
        "checkpointAfterDigest": after.integrity_digest,
        "bundlePath": str(bundle_path),
        "bundleDigest": bundle.integrity_digest,
    }


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Export bounded Runtime Registry metadata observations"
    )
    parser.add_argument("--registry-root", type=Path)
    parser.add_argument("--instance-id")
    parser.add_argument("--checkpoint", type=Path)
    parser.add_argument("--outbox", type=Path)
    parser.add_argument("--owner-revision")
    parser.add_argument("--exporter-revision")
    parser.add_argument("--exported-at-ms", type=int)
    parser.add_argument("--job-limit", type=int, default=1_000)
    parser.add_argument(
        "--job-id",
        action="append",
        default=[],
        help="export only this exact Runtime Job; may be repeated",
    )
    parser.add_argument("--event-limit-per-job", type=int, default=256)
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="preview the export without writing a checkpoint or bundle",
    )
    parser.add_argument(
        "--human",
        action="store_true",
        help="render a read-only human event timeline instead of writing a bundle",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    repo_root = Path(__file__).resolve().parents[1]
    try:
        registry_root = args.registry_root or DEFAULT_REGISTRY_ROOT
        instance_id = (args.instance_id or "").strip() or _default_instance_id()
        checkpoint_path = args.checkpoint or (
            DEFAULT_OBSERVATION_ROOT / "checkpoint.json"
        )
        outbox_root = args.outbox or (DEFAULT_OBSERVATION_ROOT / "outbox")
        owner_revision = args.owner_revision or _git_head(repo_root)
        exporter_revision = args.exporter_revision or _git_head(repo_root)
        result = export_runtime_observations(
            registry_root=registry_root,
            instance_id=instance_id,
            checkpoint_path=checkpoint_path,
            outbox_root=outbox_root,
            owner_revision=owner_revision,
            exporter_revision=exporter_revision,
            exported_at_ms=(
                args.exported_at_ms
                if args.exported_at_ms is not None
                else time.time_ns() // 1_000_000
            ),
            job_limit=args.job_limit,
            event_limit_per_job=args.event_limit_per_job,
            job_ids=tuple(args.job_id),
            dry_run=args.dry_run or args.human,
            human=args.human,
        )
    except (RuntimeObservationExportError, OSError, sqlite3.Error, ValueError) as error:
        print(
            f"runtime observation export: {type(error).__name__}: {error}",
            file=sys.stderr,
        )
        return 1
    if args.human:
        print(result["timeline"])
    else:
        print(json.dumps(result, indent=2, ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
