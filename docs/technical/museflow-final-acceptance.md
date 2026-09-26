# MuseFlow 最终发布验收追踪

- 验收更新日期：2026-09-26
- 当前实现验证基线：`f710b2e9423203c201b0352c055cc0ddec872c96`（结果下载修复）；Provider 默认值切换提交为 `9374c6d`。下文原阶段 8 记录仍对应 `33986b5f6af673e89ed69a631af7f1bf2eb60352`；本页末尾补充了后续两项变更的验证结果。P-13 另外引用文生图提交 `2636c4e57e8254347222338ec89fc013ab6b5b11` 和图生图提交 `d9e1809fe14bad85e3d19c13c82b3c494a4096cf`。
- 验收结论：本地验收通过；限制见各条与“总体限制”。
- 当前范围：最终产品设计 P-01 至 P-18、README、演示步骤、迁移、资源边界、日志/报告和发布门禁。

## 总体验证结果

阶段 8 的完整门禁运行从 `frontend` 目录执行 `npm run test:e2e:compose -- --backend-check --full-backend-gates`。脚本使用独立 Compose project、宿主机端口、数据库和对象存储卷及 MockProvider；完整前后端健康，Chromium Playwright `6 passed`，独立数据库升级至 `0007_reference_operation_leases`，`alembic check` 输出 `No new upgrade operations detected`，后端 `296 passed, 10 skipped, 2 warnings`。10 项 gated 服务测试随后全部单独运行通过：Compose worker/Redis/MinIO/maintenance `6 passed`、模拟 DashScope `1 passed`、结果发布 `3 passed`；栈停止/重启持久化检查通过。之后阶段 8 最终工作树运行 `npm run test:e2e:compose -- --backend-check --skip-restart` 得到 Playwright `6 passed`、后端 `297 passed, 10 skipped, 2 warnings`，当时 unit suite 为 `220 passed`。2026-09-25 至 26 下载修复后的追加检查见下文；两条既有弃用警告来自 Starlette/httpx TestClient 与 AnyIO。

阶段 8 代码检查命令：

```text
uv run --directory backend ruff check .
uv run --directory backend pyright
uv run --directory backend python -m compileall -q src tests migrations
```

结果分别为 Ruff 全通过、Pyright `0 errors, 0 warnings, 0 informations`、compileall 退出码 0。后端 unit suite `uv run --directory backend pytest -q tests/unit --tb=short` 为 `220 passed`。任务创建、手动重试和 STAGING 恢复日志调用点在隔离 PostgreSQL 的一次性测试库里额外执行，`5 passed`。

前端在 `frontend` 目录运行 `npm run check:api-generated`、`npm run typecheck`、`npm run lint`、`npm test -- --reporter=dot` 和 `npm run build`，均通过；Vitest `19 passed`。`docker compose config --quiet` 与 `node --check frontend/scripts/compose-e2e.mjs` 通过。隔离 Compose 栈重启后，Playwright 成功重新读取已完成任务与私有 MinIO 结果。

另用一个只启动 PostgreSQL 的独立 Compose project 对空数据库执行完整 Alembic upgrade，并创建第二个测试数据库运行 Alembic head 迁移；该项目的 `docker compose ps` 只有 PostgreSQL，没有 MinIO。兼容迁移集成用例另覆盖历史任务回填。所有临时项目、容器、网络和命名卷均已清理。仓库默认 `museflow` 项目和数据卷没有被连接、迁移或删除。

## P-01：一条 Compose 命令启动完整前后端

- **实现**：`compose.yaml`、`frontend/Dockerfile`、`frontend/deploy/nginx.conf`、`frontend/scripts/compose-e2e.mjs`、`README.md`。
- **自动化证据**：`frontend/tests/e2e/task-flow.spec.ts` 中全部 6 个正式 Web UI 流程；服务健康、迁移、MinIO 初始化、Web/API 代理和栈重启检查。
- **命令与结果**：`docker compose config --quiet` 通过；`cd frontend; npm run test:e2e:compose -- --backend-check --full-backend-gates` 启动完整隔离栈，Playwright `6 passed`，重启持久化检查通过。
- **状态/限制**：通过。为避免触碰默认数据库卷，实际启动使用 E2E 脚本创建的隔离 Compose project；仓库根目录的 `docker compose up --build` 命令已对照 Compose 配置和同一组镜像/服务验证，没有在默认 project 上直接执行。

## P-02：正式前端完成文生图与图生图

- **实现**：`frontend/src` 创建、详情、历史页面；`backend/src/museflow` 共享任务执行路径。
- **精确 E2E**：`creates text-to-image from the static frontend, shows the result and history, and survives refresh`；`uploads one real image, creates image-to-image, filters it in history, and keeps MinIO private`。两条流程均点击“下载结果”，断言浏览器下载事件、建议文件名、PNG 签名和预览仍可见。
- **命令与结果**：阶段 8 隔离全栈命令中 Chromium `6 passed`；2026-09-25 至 26 两种生成流程的下载 E2E 各 `1 passed`。正式前端静态容器通过真实 Compose API 代理连接 PostgreSQL、Worker 和 MinIO。
- **状态/限制**：通过。UI/自动化 Compose 流程使用 MockProvider；默认 Compose 已切换为 DashScope，真实 Provider 受控证据见 P-13。浏览器下载目录由浏览器设置控制。

## P-03：参考图片输入安全校验

- **实现**：`backend/src/museflow/api/app.py`、`backend/src/museflow/reference_assets`、`backend/src/museflow/safe_artifacts.py`；原始 body 最多 6,500,000 bytes，单文件最多 6,000,000 bytes，64 KiB 分块并写受控临时文件；PNG/JPEG/WebP 单帧 RGB、单边 240–2048、像素数最多 4,194,304、比例 1:4–4:1。
- **精确测试**：`tests/api/test_reference_assets_api.py::test_upload_rejects_declared_type_mismatch_and_stream_limit`、`::test_small_raw_body_guard_rejects_before_multipart_or_storage`；`tests/unit/test_reference_image_inspector.py::test_accepts_fully_decoded_single_frame_rgb_images`、`::test_content_type_is_checked_against_decoded_format`；`tests/integration/test_reference_asset_minio.py::test_reference_asset_upload_is_private_and_verified_through_api`。
- **命令与结果**：上述测试包含在完整后端套件和服务门禁中；API/Inspector/MinIO 检查通过，Chromium 上传与真实 API 错误场景通过。
- **状态/限制**：通过。Provider JSON body 的 8,100,000-byte 值只是 MuseFlow 本地 guard，Provider 没承诺接受该上限。

## P-04：上传幂等与并发

- **实现**：`backend/src/museflow/reference_assets/service.py`、`backend/src/museflow/reference_assets/repository.py` 与 reference API。
- **精确测试**：`tests/api/test_reference_assets_api.py::test_upload_is_idempotent_and_download_uses_verified_stable_path`、`::test_same_key_different_image_returns_stable_conflict`、`::test_concurrent_same_key_uploads_reserve_one_asset_and_one_object`、`::test_concurrent_same_key_different_images_conflict_without_second_object`。
- **命令与结果**：隔离 PostgreSQL/MinIO 服务门禁 `6 passed`，包含参考素材私有上传集成；全套 API tests 通过。
- **状态/限制**：通过。同 key 同内容复用素材，同 key 不同内容返回冲突。

## P-05：两种生成类型复用同一任务生命周期

- **实现**：`backend/src/museflow/tasks`、`backend/src/museflow/worker.py`、`backend/src/museflow/scheduler.py`；两类输入共享 outbox、attempt、lease、fencing、重试和结果发布。
- **精确测试**：`tests/integration/test_task_persistence.py::test_create_persists_task_event_and_execution_outbox_in_one_transaction`；`tests/integration/test_real_compose_image_to_image_e2e.py::test_real_compose_api_upload_to_mock_image_to_image_result`；`tests/integration/test_lease_execution.py::test_heartbeat_keeps_long_provider_execution_owned_while_scheduler_scans`。
- **命令与结果**：隔离完整后端套件与 Compose 服务门禁通过；图生图真实 Compose 服务链 `1 passed`，文生图/图生图 UI 均通过。
- **状态/限制**：通过。Redis 是消息传输；PostgreSQL 保持任务和结果权威事实源。

## P-06：MockProvider 故障演示

- **实现**：`backend/src/museflow/mock_provider.py`、`backend/src/museflow/provider_profiles.py`、前端 Compose E2E 场景。
- **精确测试**：`tests/unit/test_mock_provider.py::test_image_to_image_mock_uses_verified_bytes_and_is_deterministic`、`::test_image_to_image_remote_request_recovery_does_not_create_again`、`::test_rate_limit_scenario_fails_before_creating_a_remote_request`、`::test_timeout_scenario_is_transient_after_mock_submission`；Playwright `recovers transient failures and creates an immutable manual retry chain after permanent failure`。
- **命令与结果**：单元 suite `220 passed`；Playwright `6 passed`，其中重试/限流场景通过。
- **状态/限制**：通过。模拟行为证明平台故障处理；不作为真实 Provider 行为证据。

## P-07：唯一权威结果

- **实现**：`backend/src/museflow/tasks/execution.py` 与结果素材发布服务；每次候选对象键不可变，数据库 fencing 决定唯一权威指针。
- **精确测试**：`tests/integration/test_result_publication.py::test_new_worker_result_stays_authoritative_when_stale_worker_writes_later`、`::test_expired_lease_between_candidate_write_and_publish_leaves_no_result_pointer`、`::test_database_publication_failure_does_not_mark_task_succeeded_and_recovers_same_attempt`。
- **命令与结果**：在隔离 PostgreSQL/MinIO 内单独运行结果发布门禁，`3 passed`；stage 6 真实结果数据库 checksum 与私有 MinIO 下载内容摘要相同。
- **状态/限制**：通过。数据库 fencing 保证本地权威结果；不承诺 Provider 外部 exactly-once。

## P-08：故障后有界恢复

- **实现**：`backend/src/museflow/tasks/lease_guard.py`、`execution.py`、`dispatcher.py`、`scheduler.py`、`backend/src/museflow/worker.py`。
- **精确测试**：`tests/integration/test_lease_execution.py::test_heartbeat_is_fenced_by_token_and_task_status`、`::test_token_takeover_stops_old_worker_before_result_persistence`、`::test_remote_request_recovery_keeps_one_attempt_and_never_resubmits`；`tests/integration/test_real_redis_worker.py::test_real_redis_scheduler_worker_path`；`tests/integration/test_real_compose_simulated_dashscope_e2e.py::test_simulated_dashscope_edit_runs_through_real_compose_services`。
- **命令与结果**：全后端 broad run `297 passed, 10 skipped`（此前 full-gate run 为 `296 passed`）；Redis/Worker/maintenance Compose 门禁 `6 passed`；模拟 DashScope `1 passed`；重启后的持久化检查通过。
- **状态/限制**：通过。自动重试最多 3 个 attempt，总任务截止时间 600 秒；未知 Provider 创建结果不会自动重发。

## P-09：任务创建与素材显式删除并发安全

- **实现**：`backend/src/museflow/reference_assets/repository.py`、`maintenance.py` 与任务创建用例；行锁、引用复查、`ON DELETE RESTRICT`。
- **精确测试**：`tests/integration/test_reference_asset_lifecycle.py::test_reference_lock_and_delete_claim_are_serialized_and_fk_restricts_delete`；`tests/api/test_reference_assets_api.py::test_concurrent_same_key_uploads_reserve_one_asset_and_one_object`。
- **命令与结果**：隔离 PostgreSQL/MinIO 全量后端和 service gates 通过；锁/外键集成测试通过。
- **状态/限制**：通过。未引用 READY 素材仍要求 dry-run 和显式管理命令；无自动 TTL 删除。

## P-10：STAGING 恢复与显式清理

- **实现**：`backend/src/museflow/reference_assets/maintenance.py`、`scheduler.py`、`worker.py`；维护任务与生成任务分队列。
- **精确测试**：`tests/integration/test_reference_asset_lifecycle.py::test_staging_recovery_readies_valid_object_and_fails_missing_or_invalid_objects`、`::test_minio_failure_keeps_staging_and_dry_run_does_not_mutate_ready_asset`、`::test_explicit_deletion_claims_old_unreferenced_asset_then_worker_marks_deleted`、`::test_delete_failure_remains_retryable_and_two_workers_cannot_claim_same_object`、`::test_maintenance_scheduler_and_outbox_dispatch_are_separate_from_core_outbox`。
- **命令与结果**：隔离 PostgreSQL/Redis/MinIO maintenance 门禁在 `6 passed` 组内通过；另有 scheduler → 独立 maintenance Worker 历史 E2E 记录。
- **状态/限制**：通过。MinIO I/O 不在 Scheduler 核心循环里执行。

## P-11：私有对象存储与认证边界

- **实现**：`compose.yaml` bucket 初始化强制 private；API 短期签名下载；`README.md` 明确无用户认证、仅供本机或受信网络。
- **精确测试/记录**：`tests/integration/test_reference_asset_minio.py::test_reference_asset_upload_is_private_and_verified_through_api`；`tests/integration/test_real_compose_mock_e2e.py::test_compose_mock_result_reaches_private_minio_and_signed_download`；`tests/api/test_tasks_api.py::test_historical_result_asset_still_downloads_from_its_saved_object_key` 覆盖预览路径和 `attachment=true` 的响应头；stage 6 真实结果匿名访问 403、签名访问成功。
- **命令与结果**：隔离 MinIO/Compose gates 通过；Playwright 验证 MinIO 私有、稳定 MuseFlow 路径和浏览器附件下载；历史真实 Provider 记录包含匿名拒绝。
- **状态/限制**：通过。MuseFlow API 本身没有用户级认证、授权或公网滥用防护，禁止直接暴露公网。

## P-12：Provider profile 冻结

- **实现**：`backend/src/museflow/provider_profiles.py`、任务快照和 API/Worker 启动校验。
- **精确测试**：`tests/unit/test_provider_profiles.py::test_registry_freezes_capabilities_sizes_and_adapter_availability`、`::test_mock_profile_supports_both_types_and_dashscope_rejects_i2i_before_queue`、`::test_unavailable_frozen_profile_fails_without_switching_provider`；`tests/integration/test_lease_execution.py::test_task_lease_policy_snapshot_survives_worker_environment_drift`。
- **命令与结果**：220 单测、297 项完整后端套件和 Compose service gates 通过。
- **状态/限制**：通过。模型 alias 稳定性仍由 Provider 控制；profile 不可用时显式失败。

## P-13：真实 Provider 文生图和图生图记录

- **实现/证据**：真实文生图见 [`museflow-provider-feasibility.md` 第 17 节](museflow-provider-feasibility.md#17-第-5-个窗口单次真实-provider-e2e-验收2026-09-19)；真实图生图见 [`museflow-final-technical-design.md` 阶段 6 验证记录](museflow-final-technical-design.md#阶段-6-验证记录) 和脱敏记录 [stage6-real-e2e.json](../../.scratch/provider-feasibility/evidence/stage6-real-e2e.json)。
- **已存在真实证据**：文生图成功生成、结果安全下载、PNG/尺寸/摘要验证、私有 MinIO 写入与匿名访问拒绝；图生图在 `d9e1809fe14bad85e3d19c13c82b3c494a4096cf` 实现提交上的一次授权运行完成单次创建、轮询、结果校验、私有存储和访问控制。记录包含脱敏远端标识摘要，不含凭证、签名 URL 或完整远端 ID。
- **阶段 8 命令与结果**：本阶段只运行 MockProvider、fixture 和模拟 DashScope；没有再次调用付费 Provider。
- **状态/限制**：通过既有受控真实证据。账单控制台未核账，实际扣款状态未知；估算单次上限 ¥0.20 不代表已核实账单。Provider 不保证外部 exactly-once。

## P-14：MVP 兼容与历史数据迁移

- **实现**：Alembic `backend/migrations/versions/0005_expand_compatibility_schema.py` 至 `0007_reference_operation_leases.py`，以及兼容 API/下载路径。
- **精确测试**：`tests/integration/test_compatibility_migration.py::test_empty_database_upgrades_to_head_repeatedly_without_runtime_services`、`::test_stage_two_history_is_preserved_and_backfilled_deterministically`、`::test_generation_and_reference_constraints_preserve_asset_lifecycles`。
- **命令与结果**：Compose 隔离测试数据库升到 `0007_reference_operation_leases (head)`；`alembic check` 为 `No new upgrade operations detected`。独立只含 PostgreSQL 的迁移项目成功，无 MinIO 服务。
- **状态/限制**：通过。没有在默认 Compose 数据库上运行迁移；发布前仍需按 README 备份目标库、升级并比对迁移前后数据行数。不提供自动 downgrade。

## P-15：发布阻断竞态和输入边界

- **实现**：结果 fencing、lease heartbeat、上传幂等、reference deletion lock、冻结 profile 和 multipart 限制；位置见 P-03/04/07/08/09/12。
- **精确测试集合**：旧 Worker 晚写 `test_new_worker_result_stays_authoritative_when_stale_worker_writes_later`；lease 接管 `test_token_takeover_stops_old_worker_before_result_persistence`；创建/删除 `test_reference_lock_and_delete_claim_are_serialized_and_fk_restricts_delete`；上传重放与竞争 `test_upload_is_idempotent_and_download_uses_verified_stable_path`、`test_concurrent_same_key_uploads_reserve_one_asset_and_one_object`；Provider drift `test_task_lease_policy_snapshot_survives_worker_environment_drift`；超大 body `test_small_raw_body_guard_rejects_before_multipart_or_storage`；历史迁移 `test_stage_two_history_is_preserved_and_backfilled_deterministically`。
- **命令与结果**：完整后端与隔离 PostgreSQL/Redis/MinIO service gates 全部通过；隔离 Compose 命令总计执行 307 个通过的后端用例。
- **状态/限制**：通过。每条竞态由对应数据库/服务集成测试覆盖，未用清库规避迁移。

## P-16：工程质量门

- **实现**：`.github/workflows/ci.yml` 在 push、pull request 和手动触发时执行后端 Ruff/Pyright/compileall、前端生成类型/类型检查/lint/unit/build、Compose config 与完整隔离 Compose E2E。
- **精确命令与结果**：阶段 8 的后端 `ruff check .`、Pyright `0 errors`、compileall 通过；前端生成类型、typecheck、lint、Vitest `19 passed`、build 通过；full-gate Compose 运行 Playwright `6 passed`、后端 `296 passed + 10 gated skipped`，另将 10 个 gated 用例分别实际运行且全部通过。阶段 8 最终树执行 `--backend-check --skip-restart`，Playwright `6 passed`、后端 `297 passed + 10 gated skipped`。2026-09-25 至 26 的追加验证在隔离 Compose 中得后端 `298 passed, 10 skipped, 2 warnings`；本次没有重跑 10 个 gated 用例。前端生成类型、typecheck、lint 和 Vitest `19 passed` 通过。`ci.yml` 由 `js-yaml` 成功解析，迁移检查通过。
- **状态/限制**：相关本地检查通过；阶段 8 的完整质量门仍有历史记录，最近下载修复后的记录是定向 E2E 和完整非-gated后端套件，并非重新执行所有 service gates。GitHub-hosted Actions 尚未因这些本地提交而触发，远端运行状态未验证。两条既有弃用警告没有被隐藏或改称为失败。

## P-17：README 可复现性

- **人工检查**：`README.md` 覆盖一条启动命令、入口、服务检查、资源边界、只读报告、数据库备份和升级、私有存储、DashScope 默认启用、浏览器下载行为、无认证边界及已知限制；`demo/README.md` 给出 Mock 端到端演示流程。
- **自动证据**：README 声明的 `docker compose config --quiet` 与完整隔离 Compose E2E 均通过；下载修复后的 Chromium 流程验证浏览器接收文件。数据库迁移步骤在隔离 PostgreSQL 项目中实测，默认数据卷未接触。本地 Vite 开发命令返回 HTTP 200，`/tasks/new` 路由正常。
- **状态/限制**：通过。首次启动需要 Docker 和镜像仓库连通；Chromium E2E 另需安装 Playwright Chromium。

## P-18：对外声明与实施证据一致

- **人工核对**：产品设计 P-01 至 P-18 均链接本追踪表；README 的 DashScope 默认值、图生图、结果下载、资源上限、重试和本机安全边界已对照代码。未发布吞吐量、并发量或 P95 数字；没有 Locust 结果就不作性能保证。
- **实施证据**：`backend/src/museflow/safe_logging.py` 仅写固定字段并对远端请求 ID 做 12 位 SHA-256 摘要；`backend/src/museflow/task_report.py` 只读、限时窗/条数，报告阶段/队列/端到端观察延迟及重试恢复，不输出提示词、对象键、URL、远端 ID 或正文。单元测试 `tests/unit/test_safe_logging.py::test_task_event_logs_fixed_fields_and_only_remote_id_digest` 与 `tests/unit/test_task_report.py::test_task_report_calculates_phase_timings_and_retry_recovery_without_private_fields` 通过。
- **资源口径**：Worker concurrency 1、Compose memory limit 1 GiB、单 attempt 128 MiB 是配置/容量预算；没有采集实际进程峰值 RSS，不声称为实测内存结果。task report 的百分位只描述所选数据库样本窗，不是服务等级或性能承诺。
- **状态/限制**：通过。账单状态、实际峰值内存和 GitHub-hosted CI 尚未核验，均未写成已确认事实。

## 2026-09-25 至 2026-09-26 追加验证

- **Provider 默认值**：`9374c6d` 将 Compose/API/Worker 默认 Provider 切换为 DashScope；离线演示可显式设为 MockProvider，自动化 E2E 仍显式使用 Mock。本次没有发起新的真实 Provider 请求，账单状态未核验。
- **浏览器下载**：`f710b2e` 增加 `?attachment=true` 附件响应。文生图与图生图 Chromium 流程各 `1 passed`，均确认建议文件名 `museflow-result.png`、下载无失败、内容为 PNG 且详情预览仍可见。API 测试同时确认原稳定预览路径保持兼容。
- **后端与前端检查**：隔离 Compose 命令 `npm run test:e2e:compose -- --grep "creates text-to-image" --skip-restart --backend-check` 的后端结果为 `298 passed, 10 skipped, 2 warnings`；被跳过的 10 个服务门禁已在阶段 8 记录中分别运行通过，但本次没有重跑。前端 `npm run check:api-generated`、`npm run typecheck`、`npm run lint` 和 Vitest `19 passed` 通过；受影响后端 Ruff/Pyright 检查通过。
- **范围与限制**：没有重跑完整 `--full-backend-gates`，没有再次调用付费 Provider，也未触发 GitHub-hosted Actions。详细实现及验证说明见[最终技术方案第 21 节](museflow-final-technical-design.md#2026-09-25-至-2026-09-26-最终行为更新验证)。

## 总体限制

1. MuseFlow 面向本机或受信网络，没有用户认证、授权、限流或公网防护。
2. 当前状态/迁移目标为 `0007_reference_operation_leases`；历史结果宽高保持 nullable。部署者必须先备份并核对行数，不能通过清空 volume 或 downgrade 回退。
3. Provider 创建请求的不确定结果不自动重发，但外部系统仍可能已接受请求；新的独立任务可能导致重复费用。
4. 历史真实 Provider 请求没有在阶段 8 重跑；账单控制台未核查。
5. 真实内存峰值、吞吐量、并发量、P95 和 GitHub-hosted Workflow 运行结果尚未测量/触发。
6. 本阶段对工作区和 Git 历史执行高置信凭证/签名 URL 扫描：未发现私钥、Provider/API token、Bearer token 或签名 URL 值；没有跟踪 `.env`（示例文件除外）、Playwright trace/video、数据库 dump 或超过 100 KB 的媒体/归档。扫描只保存文件名和类别，不输出提示词或匹配值。
