from __future__ import annotations

import importlib.util
import json
import os
from pathlib import Path
import sqlite3
import tempfile
import unittest

REPO = Path(__file__).resolve().parents[2]
SCRIPT = REPO / "scripts" / "observation_export.py"
SPEC = importlib.util.spec_from_file_location("runtime_observation_export", SCRIPT)
assert SPEC is not None and SPEC.loader is not None
runtime_export = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(runtime_export)

OBSERVATION_AVAILABLE = importlib.util.find_spec("ordivon_observation_core") is not None
if OBSERVATION_AVAILABLE:
    import ordivon_observation_core as observation  # noqa: E402

OWNER_REVISION = "1" * 40
EXPORTER_REVISION = "2" * 40
INSTANCE_ID = "runtime:observation-test"
DIGEST_A = "sha256:" + "a" * 64
DIGEST_B = "sha256:" + "b" * 64
DIGEST_C = "sha256:" + "c" * 64
DIGEST_D = "sha256:" + "d" * 64


@unittest.skipUnless(OBSERVATION_AVAILABLE, "exact Observation contract is optional")
class RuntimeObservationExporterTests(unittest.TestCase):
    def create_registry(self, root: Path) -> Path:
        root.mkdir(mode=0o700)
        database = root / "registry.sqlite3"
        connection = sqlite3.connect(database)
        try:
            connection.execute("PRAGMA foreign_keys=ON")
            migrations = REPO / "crates" / "ordivon-runtime-core" / "migrations" / "runtime"
            for path in sorted(migrations.glob("*.sql")):
                connection.executescript(path.read_text(encoding="utf-8"))
            connection.executemany(
                "INSERT INTO schema_migrations(version,name,checksum,applied_at_ms) "
                "VALUES(?,?,?,?)",
                [(version, f"migration-{version}", DIGEST_A, version) for version in range(1, 5)],
            )
            jobs = [
                (
                    "job-019fd000-0000-7000-8000-000000000001",
                    "principal:test",
                    "request:observation-a",
                    "runtime-request-v1:" + DIGEST_A,
                    DIGEST_B,
                    "workspace:observation-a",
                    '{"private":"workspace snapshot must not be exported"}',
                    '{"command":"private command must not be exported"}',
                    DIGEST_C,
                    1_000,
                    "run",
                    None,
                    None,
                    0,
                ),
                (
                    "job-019fd000-0000-7000-8000-000000000002",
                    "principal:test",
                    "request:observation-b",
                    DIGEST_B,
                    DIGEST_C,
                    "workspace:observation-b",
                    '{"private":"second workspace snapshot"}',
                    '{"command":"second private command"}',
                    DIGEST_D,
                    1_001,
                    "run",
                    None,
                    None,
                    0,
                ),
            ]
            connection.executemany(
                "INSERT INTO jobs(job_id,principal,client_request_id,request_digest,"
                "operation_digest,workspace_id,workspace_snapshot_json,execution_plan_json,"
                "execution_plan_digest,created_at_ms,desired_state,resolution,current_attempt_id,"
                "row_version) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                jobs,
            )
            attempts = [
                (
                    "attempt-019fd000-0000-7000-8000-000000000001",
                    jobs[0][0],
                    1,
                    "running",
                    "natural",
                    DIGEST_A,
                    "/private/bundle-a",
                    None,
                    None,
                    "ordivon-test-a.service",
                    None,
                    None,
                    None,
                    None,
                    None,
                    None,
                    None,
                    None,
                    1_000,
                    1_001,
                    None,
                    0,
                ),
                (
                    "attempt-019fd000-0000-7000-8000-000000000002",
                    jobs[1][0],
                    1,
                    "accepted",
                    "natural",
                    DIGEST_B,
                    "/private/bundle-b",
                    None,
                    None,
                    "ordivon-test-b.service",
                    None,
                    None,
                    None,
                    None,
                    None,
                    None,
                    None,
                    None,
                    1_001,
                    None,
                    None,
                    0,
                ),
            ]
            connection.executemany(
                "INSERT INTO attempts(attempt_id,job_id,attempt_number,state,termination_intent,"
                "launch_token_digest,bundle_path,bundle_digest,boot_id,unit_name,invocation_id,"
                "control_group,main_pid,process_start_identity,runner_start_digest,result_digest,"
                "exit_code,infrastructure_error_digest,created_at_ms,started_at_ms,finished_at_ms,"
                "row_version) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                attempts,
            )
            events = [
                (
                    "event:runtime:a:1",
                    jobs[0][0],
                    attempts[0][0],
                    1,
                    "JOB_ACCEPTED",
                    "SYSTEM_DERIVED",
                    None,
                    "accepted",
                    "REQUEST_ADMITTED",
                    '{"private":"runtime detail one"}',
                    DIGEST_A,
                    1_000,
                ),
                (
                    "event:runtime:a:2",
                    jobs[0][0],
                    attempts[0][0],
                    2,
                    "ATTEMPT_RUNNING",
                    "SYSTEM_OBSERVED",
                    "accepted",
                    "running",
                    "RUNNER_OBSERVED",
                    '{"stdout":"private runtime output"}',
                    DIGEST_B,
                    1_002,
                ),
                (
                    "event:runtime:b:1",
                    jobs[1][0],
                    attempts[1][0],
                    1,
                    "JOB_ACCEPTED",
                    "SYSTEM_DERIVED",
                    None,
                    "accepted",
                    "REQUEST_ADMITTED",
                    '{"environment":"private environment"}',
                    DIGEST_C,
                    1_001,
                ),
            ]
            connection.executemany(
                "INSERT INTO job_events(event_id,job_id,attempt_id,event_sequence,event_type,"
                "origin,previous_state,new_state,reason_code,detail_json,detail_digest,"
                "observed_at_ms) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
                events,
            )
            connection.execute(
                "INSERT INTO artifacts(artifact_id,job_id,attempt_id,kind,relative_path,digest,"
                "media_type,byte_length,truncated,created_at_ms) VALUES(?,?,?,?,?,?,?,?,?,?)",
                (
                    "artifact:runtime:a:stdout",
                    jobs[0][0],
                    attempts[0][0],
                    "stdout",
                    "private-output.txt",
                    DIGEST_D,
                    "text/plain",
                    10,
                    0,
                    1_003,
                ),
            )
            connection.commit()
        finally:
            connection.close()
        os.chmod(database, 0o600)
        return database

    def run_export(
        self,
        directory: str,
        *,
        event_limit_per_job: int = 256,
        job_limit: int = 100,
        fail_after_bundle: bool = False,
        exported_at_ms: int = 2_000,
        job_ids: tuple[str, ...] = (),
        dry_run: bool = False,
        human: bool = False,
    ) -> dict[str, object]:
        root = Path(directory)
        return runtime_export.export_runtime_observations(
            registry_root=root / "registry",
            instance_id=INSTANCE_ID,
            checkpoint_path=root / "sidecar" / "runtime.json",
            outbox_root=root / "outbox",
            owner_revision=OWNER_REVISION,
            exporter_revision=EXPORTER_REVISION,
            exported_at_ms=exported_at_ms,
            job_limit=job_limit,
            event_limit_per_job=event_limit_per_job,
            job_ids=job_ids,
            fail_after_bundle=fail_after_bundle,
            dry_run=dry_run,
            human=human,
        )

    def test_per_job_streams_metadata_only_gateway_and_replay(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            database = self.create_registry(root / "registry")
            before = database.read_bytes()
            result = self.run_export(directory)
            self.assertEqual(result["eventCount"], 3)
            self.assertEqual(result["streamCount"], 2)
            self.assertEqual(database.read_bytes(), before)
            bundle_path = Path(str(result["bundlePath"]))
            encoded = bundle_path.read_text(encoding="utf-8")
            for private in (
                "workspace snapshot must not be exported",
                "private command must not be exported",
                "runtime detail one",
                "private runtime output",
                "private environment",
                "private-output.txt",
            ):
                self.assertNotIn(private, encoded)
            bundle = observation.ObservationExportBundle.from_dict(json.loads(encoded))
            streams = {
                batch.stream_id: [event.source.sequence for event in batch.events]
                for batch in bundle.batches
            }
            self.assertEqual(
                streams["runtime-job:job-019fd000-0000-7000-8000-000000000001"],
                [1, 2],
            )
            self.assertEqual(
                streams["runtime-job:job-019fd000-0000-7000-8000-000000000002"],
                [1],
            )
            relations = [
                relation.to_dict()
                for batch in bundle.batches
                for event in batch.events
                for relation in event.relations
            ]
            self.assertTrue(
                any(
                    item["targetKind"] == "ordivon.runtime.client-request"
                    and item["targetId"] == "request:observation-a"
                    for item in relations
                )
            )
            self.assertTrue(
                any(
                    item["targetKind"] == "ordivon.runtime.request"
                    and item["targetId"] == "runtime-request-v1:" + DIGEST_A
                    and item["targetDigest"] == DIGEST_A
                    for item in relations
                )
            )
            self.assertTrue(
                any(
                    item["targetKind"] == "ordivon.runtime.attempt"
                    and item["targetId"]
                    == "attempt-019fd000-0000-7000-8000-000000000001"
                    for item in relations
                )
            )
            producer = observation.ObservationProducerIdentity(
                "ordivon-runtime", "runtime-registry", INSTANCE_ID
            )
            with observation.SQLiteObservationGateway.initialize(
                root / "gateway",
                gateway_instance_id="observation-gateway:runtime-test",
                producer_allowlist=(producer,),
                mapping_versions=(
                    ("ordivon-runtime", "runtime-registry", runtime_export.MAPPING_VERSION),
                ),
                created_at_ms=100,
            ) as gateway:
                accepted = sum(
                    gateway.ingest(batch, ingested_at_ms=3_000).accepted
                    for batch in bundle.batches
                )
                self.assertEqual(accepted, 3)
                self.assertTrue(gateway.doctor(full=True)["healthy"])
            self.assertEqual(
                self.run_export(directory, exported_at_ms=2_001)["status"],
                "no_events",
            )

    def test_per_job_pagination_and_bundle_failure_recover(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            self.create_registry(root / "registry")
            with self.assertRaisesRegex(
                runtime_export.RuntimeObservationExportError, "injected failure"
            ):
                self.run_export(
                    directory, event_limit_per_job=1, fail_after_bundle=True
                )
            self.assertFalse((root / "sidecar" / "runtime.json").exists())
            self.assertEqual(len(tuple((root / "outbox").glob("bundle-*.json"))), 1)
            first = self.run_export(directory, event_limit_per_job=1)
            self.assertEqual(first["eventCount"], 2)
            self.assertEqual(len(tuple((root / "outbox").glob("bundle-*.json"))), 1)
            second = self.run_export(
                directory, event_limit_per_job=1, exported_at_ms=2_001
            )
            self.assertEqual(second["eventCount"], 1)
            self.assertEqual(
                self.run_export(
                    directory, event_limit_per_job=1, exported_at_ms=2_002
                )["status"],
                "no_events",
            )

    def test_exact_job_selection_bypasses_unrelated_registry_size(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            self.create_registry(root / "registry")
            selected = "job-019fd000-0000-7000-8000-000000000001"
            result = self.run_export(
                directory,
                job_limit=1,
                job_ids=(selected,),
            )
            self.assertEqual(result["eventCount"], 2)
            self.assertEqual(result["streamCount"], 1)
            self.assertEqual(result["jobCount"], 1)
            self.assertEqual(result["registryJobCount"], 2)
            bundle = observation.ObservationExportBundle.from_dict(
                json.loads(Path(str(result["bundlePath"])).read_text(encoding="utf-8"))
            )
            self.assertEqual(
                {batch.stream_id for batch in bundle.batches},
                {f"runtime-job:{selected}"},
            )

    def test_exact_job_selection_fails_closed_for_missing_or_duplicate_identity(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            self.create_registry(root / "registry")
            with self.assertRaisesRegex(
                runtime_export.RuntimeObservationExportError, "are absent"
            ):
                self.run_export(
                    directory,
                    job_limit=1,
                    job_ids=("job-019fd000-0000-7000-8000-ffffffffffff",),
                )
            selected = "job-019fd000-0000-7000-8000-000000000001"
            with self.assertRaisesRegex(ValueError, "must be unique"):
                self.run_export(
                    directory,
                    job_limit=2,
                    job_ids=(selected, selected),
                )

    def test_job_bound_and_sidecar_boundaries_fail_closed(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            self.create_registry(root / "registry")
            with self.assertRaisesRegex(
                runtime_export.RuntimeObservationExportError, "exceeds"
            ):
                self.run_export(directory, job_limit=1)
            with self.assertRaisesRegex(
                runtime_export.RuntimeObservationExportError, "outside"
            ):
                runtime_export.export_runtime_observations(
                    registry_root=root / "registry",
                    instance_id=INSTANCE_ID,
                    checkpoint_path=root / "registry" / "checkpoint.json",
                    outbox_root=root / "outbox",
                    owner_revision=OWNER_REVISION,
                    exporter_revision=EXPORTER_REVISION,
                    exported_at_ms=2_000,
                )

    def test_dry_run_preview_writes_nothing(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            database = self.create_registry(root / "registry")
            before = database.read_bytes()
            result = self.run_export(directory, dry_run=True)
            self.assertEqual(result["status"], "preview")
            self.assertEqual(result["eventCount"], 3)
            self.assertEqual(result["streamCount"], 2)
            self.assertEqual(result["jobCount"], 2)
            self.assertEqual(result["registryJobCount"], 2)
            self.assertEqual(database.read_bytes(), before)
            self.assertFalse((root / "sidecar" / "runtime.json").exists())
            self.assertEqual(len(tuple((root / "outbox").glob("bundle-*.json"))), 0)

    def test_dry_run_previews_large_registry_without_failing(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            database = self.create_registry(root / "registry")
            before = database.read_bytes()
            result = self.run_export(directory, job_limit=1, dry_run=True)
            self.assertEqual(result["status"], "preview")
            self.assertEqual(result["jobCount"], 1)
            self.assertEqual(result["registryJobCount"], 2)
            self.assertEqual(result["streamCount"], 1)
            self.assertEqual(result["eventCount"], 2)
            self.assertEqual(database.read_bytes(), before)
            self.assertFalse((root / "sidecar" / "runtime.json").exists())
            self.assertEqual(len(tuple((root / "outbox").glob("bundle-*.json"))), 0)

    def test_human_preview_returns_timeline(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            self.create_registry(root / "registry")
            result = self.run_export(directory, human=True)
            self.assertEqual(result["status"], "preview")
            self.assertIn("timeline", result)
            timeline = str(result["timeline"])
            self.assertIn(
                "runtime-job:job-019fd000-0000-7000-8000-000000000001", timeline
            )
            self.assertIn("JOB_ACCEPTED", timeline)
            self.assertIn("ATTEMPT_RUNNING", timeline)
            self.assertFalse((root / "sidecar" / "runtime.json").exists())
            self.assertEqual(len(tuple((root / "outbox").glob("bundle-*.json"))), 0)


class RuntimeObservationExportHelpersTests(unittest.TestCase):
    def test_default_instance_id_nonempty(self) -> None:
        value = runtime_export._default_instance_id()
        self.assertTrue(value)
        self.assertEqual(value, value.strip())

    def test_git_head_is_40_char_revision(self) -> None:
        value = runtime_export._git_head(REPO)
        self.assertEqual(len(value), 40)
        self.assertTrue(all(ch in "0123456789abcdef" for ch in value))

    def test_render_timeline_empty(self) -> None:
        self.assertEqual(
            runtime_export._render_timeline(()),
            "(no Runtime Registry events to export)",
        )

    def test_render_timeline_orders_events_per_job(self) -> None:
        native = (
            (
                {
                    "jobId": "job-a",
                    "eventSequence": 1,
                    "observedAtMs": 1_000,
                    "eventType": "JOB_ACCEPTED",
                    "previousState": None,
                    "newState": "accepted",
                    "reasonCode": "REQUEST_ADMITTED",
                    "attemptId": None,
                },
                {
                    "jobId": "job-a",
                    "eventSequence": 2,
                    "observedAtMs": 1_002,
                    "eventType": "ATTEMPT_RUNNING",
                    "previousState": "accepted",
                    "newState": "running",
                    "reasonCode": "RUNNER_OBSERVED",
                    "attemptId": "attempt-a",
                },
            ),
        )
        text = runtime_export._render_timeline(native)
        self.assertIn("runtime-job:job-a", text)
        self.assertIn("JOB_ACCEPTED", text)
        self.assertIn("ATTEMPT_RUNNING", text)
        self.assertIn("attempt=attempt-a", text)
        self.assertLess(text.index("JOB_ACCEPTED"), text.index("ATTEMPT_RUNNING"))


if __name__ == "__main__":
    unittest.main()
