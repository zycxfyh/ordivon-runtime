use super::*;

#[tool_router(vis = "pub(super)")]
impl RuntimeServer {
    #[tool(
        name = "runtime.describe",
        description = "Project the current Runtime node's execution affordances without reconciling, dispatching, selecting, or mutating work. Returns operator ceilings, allowed executable roots, named immutable-input authorities without host authority paths, and per-target configured/available state including current Runtime-owned provider identity, whether explicit Host Dependency commitments are supported, and the exact continuity scope Runtime can witness for those commitments. The Linux scope runtime_host_namespace_path_witness means admission/pre-dispatch digest binding plus Runtime-host-namespace path/topology watching; it is not target mount/root namespace isolation or proof that target code consumed those bytes. Windows authority availability is probed read-only; this projection never becomes admission authority, and new Jobs independently bind current provider truth at admission.",
        output_schema = rmcp::handler::server::tool::schema_for_output::<ToolOutcome<RuntimeDescribeResult>>(),
        annotations(
            title = "Describe runtime affordances",
            read_only_hint = true,
            destructive_hint = false,
            idempotent_hint = true,
            open_world_hint = false
        )
    )]
    async fn runtime_describe(
        &self,
        Parameters(request): Parameters<RuntimeDescribeRequest>,
    ) -> ToolOutcome<RuntimeDescribeResult> {
        if request.schema_version != RUNTIME_SCHEMA_VERSION {
            return ToolOutcome::Error(ToolError::invalid(
                "schemaVersion must be 1",
                "schemaVersion",
            ));
        }
        let runtime = self.state.runtime.clone();
        let global_execution_limit = self.state.execution.global_limit;
        let structured_release_configured = self.state.release.is_some();
        self.run_core("runtime.describe", move || {
            Ok(RuntimeDescribeResult::from_capabilities(
                runtime.capabilities(),
                global_execution_limit,
                structured_release_configured,
            ))
        })
        .await
    }

    #[tool(
        name = "release.apply",
        description = "Admit one exact Runtime self-release as a structured reconciliable effect. The caller chooses a Workspace commit and exact candidate-manifest digest; operator configuration owns source/install/Registry/environment/receipt authority. Exact clientRequestId replay is resolved before consulting current Workspace or candidate state. New admission commits a durable release effect and an Accepted Runtime Job, then returns without directly dispatching it; normal Runtime reconciliation owns later at-most-once execution. A connection loss during self-replacement is not failure evidence: reconnect with release.get using the same clientRequestId. The deployment receipt, not process exit alone, is authoritative for whether the external release effect committed.",
        output_schema = rmcp::handler::server::tool::schema_for_output::<ToolOutcome<RuntimeReleaseAdmission>>(),
        annotations(
            title = "Apply structured Runtime release",
            read_only_hint = false,
            destructive_hint = true,
            idempotent_hint = true,
            open_world_hint = true
        )
    )]
    async fn release_apply(
        &self,
        Parameters(request): Parameters<RuntimeReleaseApplyToolRequest>,
    ) -> ToolOutcome<RuntimeReleaseAdmission> {
        let runtime = self.state.runtime.clone();
        let release_config = self.state.release.clone();
        let principal = self.state.execution.principal.clone();
        let global_limit = self.state.execution.global_limit;
        self.run_core("release.apply", move || {
            let release_request = RuntimeReleaseRequest {
                schema_version: request.schema_version,
                client_request_id: request.client_request_id.clone(),
                principal: principal.clone(),
                workspace_id: request.workspace_id.clone(),
                commit: request.commit.clone(),
                candidate_manifest_digest: request.candidate_manifest_digest.clone(),
                expected_tool_count: request.expected_tool_count,
            };

            // Exact replay is intentionally resolved before current operator/world state.
            if let Some(replay) = runtime
                .find_runtime_release_for_apply(&release_request)
                .map_err(ToolError::from)?
            {
                return Ok(replay);
            }

            let release = release_config.ok_or_else(|| {
                ToolError::invalid(
                    "structured Runtime release is not configured on this node",
                    "release",
                )
            })?;
            let workspace = runtime
                .get_workspace(&RuntimeWorkspaceGetRequest {
                    schema_version: RUNTIME_SCHEMA_VERSION,
                    workspace_id: request.workspace_id.clone(),
                })
                .map_err(ToolError::from)?;
            let configured_source = std::fs::canonicalize(&release.source_repo).map_err(|error| {
                ToolError::invalid(
                    format!("configured release source repository is unavailable: {error}"),
                    "release.sourceRepo",
                )
            })?;
            if workspace.source_repo != configured_source.to_string_lossy() {
                return Err(ToolError::invalid(
                    "Workspace source repository does not match operator-owned Runtime release source",
                    "workspaceId",
                ));
            }
            if workspace.current_head_revision != request.commit {
                return Err(ToolError::invalid(
                    "Workspace HEAD does not match the requested Runtime release commit",
                    "commit",
                ));
            }
            if workspace.dirty {
                return Err(ToolError::invalid(
                    "Runtime release Workspace must be clean",
                    "workspaceId",
                ));
            }

            let candidate_dir = release.candidate_dir(&request.commit);
            let candidate_manifest = candidate_dir.join("ordivon-deployment-manifest.json");
            let candidate_deployer = candidate_dir.join("ordivon-runtime-deploy");
            let manifest_metadata = std::fs::symlink_metadata(&candidate_manifest).map_err(|error| {
                ToolError::invalid(
                    format!("Runtime release candidate manifest is unavailable: {error}"),
                    "candidateManifestDigest",
                )
            })?;
            if manifest_metadata.file_type().is_symlink() || !manifest_metadata.is_file() {
                return Err(ToolError::invalid(
                    "Runtime release candidate manifest must be a regular non-symlink file",
                    "candidateManifestDigest",
                ));
            }
            if manifest_metadata.len() > 4 * 1024 * 1024 {
                return Err(ToolError::invalid(
                    "Runtime release candidate manifest is too large",
                    "candidateManifestDigest",
                ));
            }
            let manifest_bytes = std::fs::read(&candidate_manifest).map_err(|error| {
                ToolError::invalid(
                    format!("cannot read Runtime release candidate manifest: {error}"),
                    "candidateManifestDigest",
                )
            })?;
            let manifest_digest = format!("sha256:{}", hex::encode(Sha256::digest(&manifest_bytes)));
            if manifest_digest != request.candidate_manifest_digest {
                return Err(ToolError::invalid(
                    "Runtime release candidate manifest digest does not match current bytes",
                    "candidateManifestDigest",
                ));
            }
            let deployer_metadata = std::fs::symlink_metadata(&candidate_deployer).map_err(|error| {
                ToolError::invalid(
                    format!("Runtime release candidate deployer is unavailable: {error}"),
                    "commit",
                )
            })?;
            if deployer_metadata.file_type().is_symlink() || !deployer_metadata.is_file() {
                return Err(ToolError::invalid(
                    "Runtime release candidate deployer must be a regular non-symlink file",
                    "commit",
                ));
            }

            let effect_id = ordivon_runtime_core::runtime_release_effect_id(&release_request);
            let request_digest =
                ordivon_runtime_core::runtime_release_request_identity_digest(&release_request)
                    .map_err(ToolError::from)?;
            let receipt_path = release.receipt_root.join(format!("effect-{effect_id}"));
            let args = vec![
                "apply".to_string(),
                "--source-repo".to_string(),
                configured_source.to_string_lossy().into_owned(),
                "--commit".to_string(),
                request.commit.clone(),
                "--confirm-commit".to_string(),
                request.commit.clone(),
                "--candidate-dir".to_string(),
                candidate_dir.to_string_lossy().into_owned(),
                "--candidate-manifest".to_string(),
                candidate_manifest.to_string_lossy().into_owned(),
                "--install-dir".to_string(),
                release.install_dir.to_string_lossy().into_owned(),
                "--database".to_string(),
                release.database.to_string_lossy().into_owned(),
                "--env-file".to_string(),
                release.env_file.to_string_lossy().into_owned(),
                "--receipt-root".to_string(),
                release.receipt_root.to_string_lossy().into_owned(),
                "--expected-tool-count".to_string(),
                request.expected_tool_count.to_string(),
                "--require-ref".to_string(),
                release.required_ref.clone(),
                "--effect-id".to_string(),
                effect_id.clone(),
                "--effect-request-digest".to_string(),
                request_digest,
                "--candidate-manifest-digest".to_string(),
                request.candidate_manifest_digest.clone(),
                "--drain-seconds".to_string(),
                "30".to_string(),
            ];
            let proposal = TaskRunProposal {
                schema_version: RUNTIME_SCHEMA_VERSION,
                client_request_id: request.client_request_id.clone(),
                principal: principal.clone(),
                global_limit,
                execution: ExecutionProposal {
                    workspace_id: request.workspace_id.clone(),
                    executable: candidate_deployer.to_string_lossy().into_owned(),
                    args,
                    cwd_relative: ".".to_string(),
                    env: BTreeMap::new(),
                    timeout_ms: Some(release.timeout_ms),
                    stdout_limit_bytes: Some(262_144),
                    stderr_limit_bytes: Some(262_144),
                    steps: Vec::new(),
                    budget: ExecutionBudget::default(),
                    execution_profile: ExecutionProfile::TrustedLocal,
                    execution_target: ExecutionTarget::LocalLinux,
                    windows_authority: WindowsAuthority::Limited,
                    foreign_references: vec![ForeignReference {
                        namespace: "ordivon.runtime".to_string(),
                        reference_type: "runtime_release".to_string(),
                        id: effect_id,
                        generation: Some(request.commit.clone()),
                        digest: Some(request.candidate_manifest_digest.clone()),
                    }],
                    host_dependencies: Vec::new(),
                },
                wait_ms: 0,
                stdout_tail_bytes: 0,
                stderr_tail_bytes: 0,
            };
            runtime
                .admit_runtime_release_effect(&release_request, &proposal, &receipt_path)
                .map_err(ToolError::from)
        })
        .await
    }

    #[tool(
        name = "release.get",
        description = "Project one exact structured Runtime release effect by clientRequestId without reconciling, dispatching, retrying, or changing the external world. Joins durable Job/Attempt truth with the deterministic deployment receipt. Receipt truth is authoritative for deployed/not-committed/rolled-back release effects even when the generic execution channel was interrupted by Runtime self-replacement. Use this after any uncertain release.apply response; never infer release completion from transport loss or process status alone.",
        output_schema = rmcp::handler::server::tool::schema_for_output::<ToolOutcome<RuntimeReleaseProjection>>(),
        annotations(
            title = "Get structured Runtime release",
            read_only_hint = true,
            destructive_hint = false,
            idempotent_hint = true,
            open_world_hint = false
        )
    )]
    async fn release_get(
        &self,
        Parameters(request): Parameters<RuntimeReleaseGetToolRequest>,
    ) -> ToolOutcome<RuntimeReleaseProjection> {
        let runtime = self.state.runtime.clone();
        let principal = self.state.execution.principal.clone();
        self.run_core("release.get", move || {
            runtime
                .get_runtime_release_effect(&RuntimeReleaseGetRequest {
                    schema_version: request.schema_version,
                    principal,
                    client_request_id: request.client_request_id,
                })
                .map_err(ToolError::from)
        })
        .await
    }

    #[tool(
        name = "workspace.open",
        description = "Resolve a local revision and create one detached Git Workspace. Omit workspaceId for a server-generated immutable ws-* handle; provide an explicit unique workspaceId when deterministic response-loss reconciliation matters. Repeating workspace.open is not an idempotent replay: after an uncertain response, use workspace.get on the explicit ID. This tool does not fetch remote refs.",
        output_schema = rmcp::handler::server::tool::schema_for_output::<ToolOutcome<CompactWorkspaceOpenResult>>(),
        annotations(
            title = "Open isolated workspace",
            read_only_hint = false,
            destructive_hint = false,
            idempotent_hint = false,
            open_world_hint = false
        )
    )]
    async fn workspace_open(
        &self,
        Parameters(request): Parameters<WorkspaceOpenRequest>,
    ) -> ToolOutcome<CompactWorkspaceOpenResult> {
        let runtime = self.state.runtime.clone();
        let request = request.bind();
        self.run_core("workspace.open", move || {
            runtime.open_workspace(&request).map_err(ToolError::from)
        })
        .await
    }

    #[tool(
        name = "workspace.close",
        description = "Close one Workspace. By default, reject tracked or untracked changes; force=true may remove dirty files. expectedSourceStateDigest compare-and-closes only the exact committed source state and remains replayable through the closed tombstone. closureDisposition distinguishes removed, already_closed, already_absent, and recovered_missing; removed only says whether this call performed physical removal. Active or held Jobs and open Workspaces whose Git authority lives under paths this close would remove block closure without reconciliation or dispatch.",
        output_schema = rmcp::handler::server::tool::schema_for_output::<ToolOutcome<WorkspaceCloseResult>>(),
        annotations(
            title = "Close workspace",
            read_only_hint = false,
            destructive_hint = true,
            idempotent_hint = true,
            open_world_hint = false
        )
    )]
    async fn workspace_close(
        &self,
        Parameters(request): Parameters<WorkspaceCloseRequest>,
    ) -> ToolOutcome<WorkspaceCloseResult> {
        let runtime = self.state.runtime.clone();
        self.run_core("workspace.close", move || {
            runtime.close_workspace(&request).map_err(ToolError::from)
        })
        .await
    }

    #[tool(
        name = "workspace.get",
        description = "Return one Workspace's canonical sourceRepo, opening/base sourceRevision, exact currentHeadRevision, detached-head mode, dirty state, complete sourceStateDigest, creation time, and active Job identities. This is a projection-only read: it does not reconcile or dispatch Jobs. Use it after reconnecting or after an uncertain workspace.open instead of reconstructing Workspace identity or state from memory.",
        output_schema = rmcp::handler::server::tool::schema_for_output::<ToolOutcome<RuntimeWorkspaceSummary>>(),
        annotations(
            title = "Get workspace state",
            read_only_hint = true,
            destructive_hint = false,
            idempotent_hint = true,
            open_world_hint = false
        )
    )]
    async fn workspace_get(
        &self,
        Parameters(request): Parameters<RuntimeWorkspaceGetRequest>,
    ) -> ToolOutcome<RuntimeWorkspaceSummary> {
        let runtime = self.state.runtime.clone();
        self.run_core("workspace.get", move || {
            runtime.get_workspace(&request).map_err(ToolError::from)
        })
        .await
    }

    #[tool(
        name = "workspace.list",
        description = "List newest healthy open Workspaces using stable cursor pagination, with canonical sourceRepo, opening/base sourceRevision, and exact currentHeadRevision. Exact sourceStateDigest is omitted by default and may be requested explicitly; workspace.get remains the precise proof boundary. This is a projection-only read and does not reconcile or dispatch Jobs. Inventory is derived from current physical Workspace candidates rather than historical closed tombstones; current inventory and page-local projection failures are isolated with a machine-readable stage, while authority-wide failures still fail closed.",
        output_schema = rmcp::handler::server::tool::schema_for_output::<ToolOutcome<RuntimeWorkspaceListResult>>(),
        annotations(
            title = "List open workspaces",
            read_only_hint = true,
            destructive_hint = false,
            idempotent_hint = true,
            open_world_hint = false
        )
    )]
    async fn workspace_list(
        &self,
        Parameters(request): Parameters<RuntimeWorkspaceListRequest>,
    ) -> ToolOutcome<RuntimeWorkspaceListResult> {
        let runtime = self.state.runtime.clone();
        self.run_core("workspace.list", move || {
            runtime.list_workspaces(&request).map_err(ToolError::from)
        })
        .await
    }

    #[tool(
        name = "workspace.read",
        description = "Read bounded UTF-8 content from an isolated workspace in FULL or SLICE mode.",
        output_schema = rmcp::handler::server::tool::schema_for_output::<ToolOutcome<WorkspaceReadResult>>(),
        annotations(
            title = "Read workspace content",
            read_only_hint = true,
            destructive_hint = false,
            idempotent_hint = true,
            open_world_hint = false
        )
    )]
    async fn workspace_read(
        &self,
        Parameters(request): Parameters<WorkspaceReadRequest>,
    ) -> ToolOutcome<WorkspaceReadResult> {
        let config = self.state.executor.clone();
        self.run_core("workspace.read", move || match request.mode {
            WorkspaceReadMode::Full => {
                if request.offset != 0 {
                    return Err(ToolError::invalid(
                        "offset must be zero in FULL mode",
                        "offset",
                    ));
                }
                let result = read_workspace_text_compact(
                    &config,
                    &ExecWorkspaceReadRequest {
                        schema_version: request.schema_version,
                        workspace_id: request.workspace_id,
                        relative_path: request.relative_path,
                        max_bytes: request.max_bytes,
                    },
                )
                .map_err(ToolError::from)?;
                Ok(WorkspaceReadResult {
                    content: result.content,
                    digest: result.digest,
                    file_byte_length: None,
                    eof: None,
                })
            }
            WorkspaceReadMode::Slice => {
                let result = read_workspace_slice_compact(
                    &config,
                    &WorkspaceReadSliceRequest {
                        schema_version: request.schema_version,
                        workspace_id: request.workspace_id,
                        relative_path: request.relative_path,
                        offset: request.offset,
                        max_bytes: request.max_bytes,
                    },
                )
                .map_err(ToolError::from)?;
                Ok(WorkspaceReadResult {
                    content: result.content,
                    digest: result.file_digest,
                    file_byte_length: Some(result.file_byte_length),
                    eof: Some(result.eof),
                })
            }
        })
        .await
    }

    #[tool(
        name = "workspace.content",
        description = "Project one exact digest-bound Workspace image as native MCP image content. Runtime verifies the current file bytes against expectedDigest and validates PNG/JPEG signatures before transport; a changed file fails closed instead of silently changing the Agent's perceptual input.",
        output_schema = rmcp::handler::server::tool::schema_for_output::<ToolOutcome<WorkspaceContentMetadata>>(),
        annotations(
            title = "Read verified workspace media content",
            read_only_hint = true,
            destructive_hint = false,
            idempotent_hint = true,
            open_world_hint = false
        )
    )]
    async fn workspace_content(
        &self,
        Parameters(request): Parameters<WorkspaceContentRequest>,
    ) -> Result<CallToolResult, McpError> {
        let config = self.state.executor.clone();
        let outcome = self
            .run_core("workspace.content", move || {
                read_workspace_content(&config, &request).map_err(ToolError::from)
            })
            .await;
        workspace_content_call_result(outcome)
    }

    #[tool(
        name = "workspace.mutate",
        description = "Apply one atomic validated batch. mode must be exactly WRITE, APPEND, or REPLACE_EXACT; REPLACE_EXACT requires expectedText. expectedDigest is required when a target already exists and protects the complete file version. Active or held Jobs block mutation without being reconciled or dispatched by this call. This tool has no durable clientRequestId replay receipt: after an uncertain response, inspect current Workspace state before retrying. Prefer workspace.patch when response-loss reconciliation is required.",
        output_schema = rmcp::handler::server::tool::schema_for_output::<ToolOutcome<WorkspaceMutateResult>>(),
        annotations(
            title = "Mutate workspace files",
            read_only_hint = false,
            destructive_hint = true,
            idempotent_hint = false,
            open_world_hint = false
        )
    )]
    async fn workspace_mutate(
        &self,
        Parameters(request): Parameters<WorkspaceMutateRequest>,
    ) -> ToolOutcome<WorkspaceMutateResult> {
        let runtime = self.state.runtime.clone();
        self.run_core("workspace.mutate", move || {
            runtime.mutate_workspace(&request).map_err(ToolError::from)
        })
        .await
    }

    #[tool(
        name = "workspace.changes",
        description = "Page one exact Workspace change projection without unbounded path arrays. Entries expose atomic modified, added, deleted, and untracked changes in stable path/kind order; rename/copy interpretation stays on workspace.diff rather than adding similarity analysis to this large-change-set primitive. limit bounds entry count, maxBytes bounds encoded entry payload (not Git discovery I/O/RSS), totalEntries/remainingEntries expose traversal size, and nextCursor={changeSetDigest,afterPath,afterKind} fails closed if the projected change set changes. Use this for large change sets; workspace.diff remains the richer legacy convenience surface.",
        output_schema = rmcp::handler::server::tool::schema_for_output::<ToolOutcome<WorkspaceChangePageResult>>(),
        annotations(
            title = "Page workspace changes",
            read_only_hint = true,
            destructive_hint = false,
            idempotent_hint = true,
            open_world_hint = false
        )
    )]
    async fn workspace_changes(
        &self,
        Parameters(request): Parameters<WorkspaceChangesRequest>,
    ) -> ToolOutcome<WorkspaceChangePageResult> {
        let config = self.state.executor.clone();
        self.run_core("workspace.changes", move || {
            workspace_changes_page(
                &config,
                &ExecWorkspaceChangePageRequest {
                    schema_version: request.schema_version,
                    workspace_id: request.workspace_id,
                    limit: request.limit,
                    max_bytes: request.max_bytes,
                    cursor: request.cursor,
                },
            )
            .map_err(ToolError::from)
        })
        .await
    }

    #[tool(
        name = "workspace.patch",
        description = "Apply one digest-guarded atomic text patch under a durable clientRequestId. Active or held Jobs block mutation without being reconciled or dispatched by this call. Exact replay returns the committed receipt; changed input conflicts; uncertain mixed outcomes require reconciliation.",
        output_schema = rmcp::handler::server::tool::schema_for_output::<ToolOutcome<DurableWorkspacePatchResult>>(),
        annotations(
            title = "Apply durable workspace patch",
            read_only_hint = false,
            destructive_hint = true,
            idempotent_hint = true,
            open_world_hint = false
        )
    )]
    async fn workspace_patch(
        &self,
        Parameters(request): Parameters<WorkspacePatchToolRequest>,
    ) -> ToolOutcome<DurableWorkspacePatchResult> {
        let runtime = self.state.runtime.clone();
        let request = self.state.execution.bind_patch(request);
        self.run_core("workspace.patch", move || {
            runtime
                .patch_workspace_durable(&request)
                .map_err(ToolError::from)
        })
        .await
    }

    #[tool(
        name = "workspace.patch.get",
        description = "Reconcile one durable Workspace Patch receipt by exact clientRequestId without applying an uncommitted patch. This call may advance Runtime receipt state from prepared to committed or unknown after inspecting physical file state.",
        output_schema = rmcp::handler::server::tool::schema_for_output::<ToolOutcome<WorkspacePatchOperationStatus>>(),
        annotations(
            title = "Inspect durable workspace patch",
            read_only_hint = false,
            destructive_hint = false,
            idempotent_hint = true,
            open_world_hint = false
        )
    )]
    async fn workspace_patch_get(
        &self,
        Parameters(request): Parameters<WorkspacePatchStatusToolRequest>,
    ) -> ToolOutcome<WorkspacePatchOperationStatus> {
        let runtime = self.state.runtime.clone();
        let request = self.state.execution.bind_patch_status(request);
        self.run_core("workspace.patch.get", move || {
            runtime
                .workspace_patch_status(&request)
                .map_err(ToolError::from)
        })
        .await
    }

    #[tool(
        name = "workspace.diff",
        description = "Return Git diff text with bounded Git stdout capture plus the complete legacy structured changed, modified, added, deleted, renamed, and untracked path projection. maxBytes bounds diff text only; the structured path arrays remain complete and are not response-bounded. Prefer workspace.changes for large or paged change sets.",
        output_schema = rmcp::handler::server::tool::schema_for_output::<ToolOutcome<CompactWorkspaceDiffResult>>(),
        annotations(
            title = "Inspect workspace diff",
            read_only_hint = true,
            destructive_hint = false,
            idempotent_hint = true,
            open_world_hint = false
        )
    )]
    async fn workspace_diff(
        &self,
        Parameters(request): Parameters<WorkspaceDiffRequest>,
    ) -> ToolOutcome<CompactWorkspaceDiffResult> {
        let config = self.state.executor.clone();
        self.run_core("workspace.diff", move || {
            workspace_diff_compact(
                &config,
                &ExecWorkspaceDiffRequest {
                    schema_version: request.schema_version,
                    workspace_id: request.workspace_id,
                    max_bytes: request.max_bytes,
                },
            )
            .map_err(ToolError::from)
        })
        .await
    }

    #[tool(
        name = "workspace.exec",
        description = "Run one effect-opaque command inside a workspace with the installed service user's trusted-local authority. execution.executable must be an absolute host path and execution.cwdRelative must be relative to the Workspace root. For trusted_local local_linux only, execution.hostDependencies may declare known absolute regular host prerequisite files with exact SHA-256 digests; Runtime binds them into operation identity, validates them at admission and before dispatch, then Runner establishes path/topology drift witnesses before its final digest checks and keeps those witnesses active through the Attempt. Runtime likewise witnesses the target executable path through each step while preserving normal pathname semantics. A witnessed Runtime-host-namespace write/replace/rename/delete fails closed instead of being reported as a successful committed realization. Host Dependency continuity evidence carries scope runtime_host_namespace_path_witness: trusted_local target code retains its authority and may intentionally establish another mount/root namespace view, so this witness is not target namespace isolation or proof that the target consumed the committed bytes. This is an explicit partial prerequisite/path-continuity contract, not automatic dependency discovery, immutability, or a complete environment snapshot. Duplicate clientRequestId admission is idempotent and exact replay resolves the existing Job before consulting current dependency bytes. Omitted waitMs performs only a brief 2-second observation after durable admission; long work returns the same Job for task.observe rather than holding the MCP request open. Results expose exact Attempt state, execution and delivery disposition, recovery requirement, and explicitly do not claim semantic completion or external-effect idempotency.",
        output_schema = rmcp::handler::server::tool::schema_for_output::<ToolOutcome<TaskObservation>>(),
        annotations(
            title = "Execute transactional workspace job",
            read_only_hint = false,
            destructive_hint = true,
            idempotent_hint = false,
            open_world_hint = true
        )
    )]
    async fn workspace_exec(
        &self,
        Parameters(request): Parameters<WorkspaceExecRequest>,
    ) -> ToolOutcome<TaskObservation> {
        let runtime = self.state.runtime.clone();
        let request = self.state.execution.bind(request);
        self.run_core("workspace.exec", move || match request {
            BoundTaskRun::Legacy(request) => runtime.run_task(&request).map_err(ToolError::from),
            BoundTaskRun::Proposal(proposal) => runtime
                .run_task_proposal(&proposal)
                .map_err(ToolError::from),
        })
        .await
    }

    #[tool(
        name = "workspace.execBound",
        description = "Admit one execution with exact immutable inputs from operator-configured named authorities. local_linux uses contained_local and a read-only /run/ordivon/inputs bind. windows_native uses trusted_local with the limited Windows token only and a provider-owned native read-only input presentation; elevated Windows input-bound execution is rejected. Each input names only an authority, relative object, expected SHA-256 digest, and presentation-relative path. Runtime resolves and copies bytes only on new admission, freezes effective input commitments into the Job, and exact replay returns the historical Job before consulting current authority state. Omitted waitMs performs only a brief 2-second observation after durable admission; long work returns the same Job for task.observe. This is physical execution evidence only and does not imply domain semantic completion or target-byte isolation from separate elevated host authority.",
        output_schema = rmcp::handler::server::tool::schema_for_output::<ToolOutcome<TaskObservation>>(),
        annotations(
            title = "Execute with immutable inputs",
            read_only_hint = false,
            destructive_hint = true,
            idempotent_hint = false,
            open_world_hint = false
        )
    )]
    async fn workspace_exec_bound(
        &self,
        Parameters(request): Parameters<WorkspaceExecBoundRequest>,
    ) -> ToolOutcome<TaskObservation> {
        let runtime = self.state.runtime.clone();
        let (proposal, inputs) = self.state.execution.bind_bound(request);
        self.run_core("workspace.execBound", move || {
            runtime
                .run_task_proposal_with_inputs(&proposal, &inputs)
                .map_err(ToolError::from)
        })
        .await
    }

    #[tool(
        name = "workspace.execPlan",
        description = "Run an ordered structured execution plan inside one Workspace. Steps use absolute executables and explicit args, run sequentially, stop on the first failure by default, and continue only when that step explicitly sets continueOnError. For trusted_local local_linux only, execution.hostDependencies may bind known absolute regular host prerequisite files by exact SHA-256 across the whole Job. Runtime validates them at admission and before dispatch; Runner then establishes path/topology drift witnesses before final digest validation and keeps them active across the complete plan, while each target executable path is independently witnessed through its step. Runtime fails closed on witnessed runtime drift without pretending that the files are immutable or that it inferred a complete environment closure. Exact replay resolves the committed Job before current dependency checks. Omitted waitMs performs only a brief 2-second observation after durable admission; long work returns the same Job for task.observe. The Job exposes step progress plus exact Attempt state, execution and delivery disposition, and recovery requirement without asking the caller to infer them from output.",
        output_schema = rmcp::handler::server::tool::schema_for_output::<ToolOutcome<TaskObservation>>(),
        annotations(
            title = "Execute fail-fast workspace plan",
            read_only_hint = false,
            destructive_hint = true,
            idempotent_hint = false,
            open_world_hint = true
        )
    )]
    async fn workspace_exec_plan(
        &self,
        Parameters(request): Parameters<WorkspaceExecPlanRequest>,
    ) -> ToolOutcome<TaskObservation> {
        let runtime = self.state.runtime.clone();
        let request = match self.state.execution.bind_plan(request) {
            Ok(request) => request,
            Err(error) => return ToolOutcome::Error(error),
        };
        self.run_core("workspace.execPlan", move || match request {
            BoundTaskRun::Legacy(request) => runtime.run_task(&request).map_err(ToolError::from),
            BoundTaskRun::Proposal(proposal) => runtime
                .run_task_proposal(&proposal)
                .map_err(ToolError::from),
        })
        .await
    }

    #[tool(
        name = "task.get",
        description = "Read one exact durable Job as a projection-only Runtime inspection. This never reconciles, dispatches, cancels, or otherwise advances the Job. It returns bounded Attempt history, mechanical convergence, Artifact/episode summaries, and a bounded event timeline with event detail omitted; use artifact.read for retained stdout/stderr/results and task.observe only when targeted reconciliation or waiting is intended.",
        output_schema = rmcp::handler::server::tool::schema_for_output::<ToolOutcome<RuntimeJobInspection>>(),
        annotations(
            title = "Get transactional job",
            read_only_hint = true,
            destructive_hint = false,
            idempotent_hint = true,
            open_world_hint = false
        )
    )]
    async fn task_get(
        &self,
        Parameters(request): Parameters<TaskGetRequest>,
    ) -> ToolOutcome<RuntimeJobInspection> {
        if request.schema_version != 1 {
            return ToolOutcome::Error(ToolError::invalid(
                "schemaVersion must be 1",
                "schemaVersion",
            ));
        }
        let runtime = self.state.runtime.clone();
        self.run_core("task.get", move || {
            runtime
                .inspect_job(&request.job_id, request.event_limit)
                .map_err(ToolError::from)
        })
        .await
    }

    #[tool(
        name = "task.observe",
        description = "Observe or briefly await one exact Job and reconcile that Job before projection. If the durable Job is still accepted with desiredState=run, this call may dispatch that already-committed execution intent; it never creates a new Job. Exact Attempt state, terminal execution disposition, delivery certainty, recovery requirement, result availability, and semanticCompletionEvaluated=false are projected explicitly. Omit offsets for tail mode, or pass stdoutOffset/stderrOffset with at least 4 tail bytes to read only new retained UTF-8 text and continue from returned next offsets.",
        output_schema = rmcp::handler::server::tool::schema_for_output::<ToolOutcome<TaskObservation>>(),
        annotations(
            title = "Observe transactional job",
            read_only_hint = false,
            destructive_hint = true,
            idempotent_hint = true,
            open_world_hint = true
        )
    )]
    async fn task_observe(
        &self,
        Parameters(request): Parameters<TaskObserveRequest>,
    ) -> ToolOutcome<TaskObservation> {
        let runtime = self.state.runtime.clone();
        self.run_core("task.observe", move || {
            runtime.observe_task(&request).map_err(ToolError::from)
        })
        .await
    }

    #[tool(
        name = "task.cancel",
        description = "Persist cancellation intent, stop the cgroup-owned process tree, and reconcile the Job.",
        output_schema = rmcp::handler::server::tool::schema_for_output::<ToolOutcome<TaskObservation>>(),
        annotations(
            title = "Cancel transactional job",
            read_only_hint = false,
            destructive_hint = true,
            idempotent_hint = true,
            open_world_hint = false
        )
    )]
    async fn task_cancel(
        &self,
        Parameters(request): Parameters<TaskCancelRequest>,
    ) -> ToolOutcome<TaskObservation> {
        let runtime = self.state.runtime.clone();
        self.run_core("task.cancel", move || {
            runtime.cancel_task(&request).map_err(ToolError::from)
        })
        .await
    }

    #[tool(
        name = "task.list",
        description = "List newest Jobs first from the current durable Registry projection with request identity, Workspace, command summary, exact Attempt state, execution and delivery disposition, recovery requirement, timestamps, duration, and Artifact count using a stable cursor. Optionally filter by exact workspaceId, clientRequestId, or their intersection so a reconnecting caller can recover historical Jobs without scanning the global ledger. This call does not reconcile or dispatch Jobs; use task.observe for targeted reconciliation. Runtime never claims Task/domain semantic completion.",
        output_schema = rmcp::handler::server::tool::schema_for_output::<ToolOutcome<RuntimeJobListResult>>(),
        annotations(
            title = "List transactional jobs",
            read_only_hint = true,
            destructive_hint = false,
            idempotent_hint = true,
            open_world_hint = false
        )
    )]
    async fn task_list(
        &self,
        Parameters(request): Parameters<RuntimeJobListRequest>,
    ) -> ToolOutcome<RuntimeJobListResult> {
        let runtime = self.state.runtime.clone();
        self.run_core("task.list", move || {
            runtime.list_jobs(&request).map_err(ToolError::from)
        })
        .await
    }

    #[tool(
        name = "artifact.read",
        description = "Read a bounded verified range from one Job Artifact by Job and Artifact identity.",
        output_schema = rmcp::handler::server::tool::schema_for_output::<ToolOutcome<ArtifactReadResult>>(),
        annotations(
            title = "Read transactional job artifact",
            read_only_hint = true,
            destructive_hint = false,
            idempotent_hint = true,
            open_world_hint = false
        )
    )]
    async fn artifact_read(
        &self,
        Parameters(request): Parameters<ArtifactReadRequest>,
    ) -> ToolOutcome<ArtifactReadResult> {
        let runtime = self.state.runtime.clone();
        self.run_core("artifact.read", move || {
            runtime.read_artifact(&request).map_err(ToolError::from)
        })
        .await
    }
}
