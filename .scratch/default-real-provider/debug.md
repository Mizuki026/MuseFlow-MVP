# 默认 Provider 偏离记录

- 日期：2026-09-25
- 检查点提交：`12f3877`（修改前空提交，工作区此前干净）
- 范围：默认 Compose 与未显式设置 `MUSEFLOW_PROVIDER` 时，文生图/图生图应选择真实 DashScope profile。
- 凭据处理：仅检查环境变量是否存在；未读取、记录或输出密钥与工作区值。

## 修改前复现证据

- 当前 PowerShell 进程中 `DASHSCOPE_API_KEY` 与 `DASHSCOPE_API_HOST` 均存在；`MUSEFLOW_PROVIDER` 未设置，仓库根目录没有 `.env` 文件。
- `docker compose config --format json` 的服务环境摘要：API `MUSEFLOW_PROVIDER=mock`，generation Worker `MUSEFLOW_PROVIDER=mock`；API/Worker 容器配置均未包含 DashScope key/host。
- 未显式设置 Provider 时运行 `configured_profile()`，文生图返回 `mock-text-to-image-v1`，图生图返回 `mock-image-generation-v2`。
- 因此默认用户操作路径（新建页提交提示词；或上传有效参考图后提交图生图）在任务创建阶段就会冻结 Mock profile。Worker 后续按冻结 profile 创建 MockProvider。第一个偏离点在 `CreateTask` 解析 Provider 快照，而不是网络、DashScope endpoint、结果下载或数据库持久化。
- 此偏离是错误 Provider 被选中，没有伴随异常堆栈或远端 HTTP 请求；本次没有为复现提交真实生成请求。

## 边界与根因

- API 创建路径在 `backend/src/museflow/tasks/application.py` 的 `_resolve_provider_snapshot()` 调用 `configured_profile()` 并冻结 profile；`backend/src/museflow/worker.py` 根据已保存的 task profile 构造 Adapter。
- 根因是两个相互独立的 Mock 默认：`backend/src/museflow/provider_profiles.py` 在环境变量缺失时回退到 `mock`，`compose.yaml` 又在 API/Worker 环境里显式固定 `mock`，且没有转发 DashScope 配置。宿主机已有凭据并不会自动进入容器。

## 修改后验证

- 未指定 `MUSEFLOW_PROVIDER` 时，两个 generation type 的 profile 默认都选择 DashScope；Compose 的 API 与 Worker 均解析为 `dashscope`，key/host 也从当前进程环境注入（验证输出只记录存在性）。
- `uv run --directory backend pytest -q tests/unit/test_provider_profiles.py tests/unit/test_dashscope_provider.py tests/unit/test_dashscope_image_adapter.py`：`76 passed`。
- `npm run test:e2e:compose -- --backend-check --full-backend-gates`：Playwright `6 passed`；后端 `298 passed, 10 skipped`；之后独立运行的服务集成 `6 passed`、模拟 DashScope E2E `1 passed`、结果发布集成 `3 passed`；栈重启持久化检查通过。完整回归只在隔离 Compose project/volume 运行，脚本已清理该项目。
- 测试默认强制 MockProvider 并移除继承的真实 key/host；模拟 DashScope 集成使用模拟 HTTP 服务。没有发起新的收费 Provider 请求。
- Ruff 对修改的后端源文件和测试文件通过；`docker compose --env-file .env.example config --quiet` 通过。

## 尚未覆盖

本次证明了默认配置选择、凭据传递、文生图/图生图任务 UI 流程和模拟 Adapter/服务链；没有向真实 DashScope 创建新任务，也没有核验新账单。真实模型实际返回仍以用户首次发起的真实任务为准。
