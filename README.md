# MuseFlow

MuseFlow 是一个面向本机和受信网络的图像生成工作流演示项目。它包含任务恢复、参考素材校验、任务历史、不可变结果和 Provider 适配器。默认 Compose 使用 `MockProvider`，不调用真实 Provider，也不需要 API Key。MuseFlow 没有用户认证或公网滥用防护，**不得直接暴露到公网**。

## 一条命令启动

需要 Docker Desktop 与 Docker Compose。首次构建还需要 Docker Hub 和 Quay 镜像仓库可达。

在仓库根目录执行：

```powershell
docker compose up --build
```

打开 [http://127.0.0.1:5173/tasks/new](http://127.0.0.1:5173/tasks/new)。正式 UI、`/api` 代理、API、Scheduler、generation Worker、maintenance Worker、PostgreSQL、Redis 和私有 MinIO 会一起启动。数据库迁移与 MinIO bucket 初始化由一次性 Compose 服务自动完成。

默认入口只需访问 Web UI。API 通过 `http://127.0.0.1:5173/api/v1` 代理，也保留仅绑定本机的 `http://127.0.0.1:8000/api/v1` 调试端口。PostgreSQL `55432`、Redis `6379`、MinIO API `9000` 与 MinIO Console `9001` 也只绑定 `127.0.0.1`。可将 `.env.example` 复制为 `.env` 后覆盖端口和本机开发凭据。默认示例凭据不能用于共享或公网环境。

检查服务：

```powershell
docker compose ps --all
docker compose logs --tail=100 web api scheduler worker maintenance-worker
```

按 `Ctrl+C` 停止前台服务。也可以在另一个终端运行 `docker compose stop`；`docker compose down` 会移除容器和网络并保留数据库、Redis、MinIO 数据卷。不要用 `docker compose down -v` 重置有用数据。

## 已验证的资源边界与系统限制

- 上传请求体最多 6,500,000 bytes；应用单张参考图最多 6,000,000 bytes，以 64 KiB 分块读取并先落到受控临时文件。只接受单帧 PNG/JPEG/WebP 的 RGB 图像，单边 240–2048 px、总像素最多 4,194,304、宽高比 1:4 至 4:1。
- Wan 图生图 JSON 请求体另有 8,100,000-byte 本地安全上限。这是 MuseFlow 的序列化请求限制，不是 Provider 承诺的限额；base64 会增加请求体大小。
- Provider 结果最多 20 MiB，当前 profile 只接受 PNG，最多 1440×1440、单帧 RGB；下载不跟随重定向。generation Worker 并发为 1，Compose 为它设置 1 GiB 内存上限；任务最多 3 个 attempt，总截止时间 600 秒。
- 外部 Provider 不提供可依赖的 exactly-once 创建契约。创建结果不确定时任务不会自动重发 POST；外部服务可能已接受该请求。另行新建任务仍可能产生重复调用或费用。Provider 模型 alias 的长期稳定性也不受 MuseFlow 保证；冻结 profile 不可用时任务明确失败，不静默切换。
- 历史结果的宽高字段保持 nullable，不能把“旧数据没有尺寸”解释为零尺寸或完整校验通过。项目没有发布吞吐量、并发量或 P95 性能数字。

PostgreSQL 任务数据和私有 MinIO 对象是持久事实；Redis 只用于消息传输。日志使用固定字段并只记录远端请求 ID 摘要。下面的只读命令生成最近 24 小时任务统计（延迟、阶段耗时、终态和自动重试恢复率）；需要 `MUSEFLOW_DATABASE_URL` 指向可读的 MuseFlow PostgreSQL：

    uv run --directory backend python -m museflow.task_report
    uv run --directory backend python -m museflow.task_report --since 2026-09-23T00:00:00Z --until 2026-09-24T00:00:00Z --limit 1000

报告不会输出提示词、远端 Provider 请求 ID、对象键、签名 URL 或结果正文。它只读任务、attempt 和阶段事件；样本被 `--limit` 截断时会标记 `truncated`。

## 数据库与私有存储

当前 Alembic head 为 `0007_reference_operation_leases`。干净数据库会由 `migrate` 自动升级到当前 head；仓库 Compose 的 PostgreSQL、Redis 和 MinIO 数据分别保存在独立命名 volume 中。

升级已有数据库前先记录当前 revision，并把可恢复备份保存在仓库之外。例如：

    docker compose exec -T postgres pg_dump -U museflow -d museflow --format=plain --no-owner --file=/tmp/museflow-before-migration.sql
    $postgresContainer = docker compose ps -q postgres
    docker cp ($postgresContainer + ':/tmp/museflow-before-migration.sql') (Join-Path $env:TEMP 'museflow-before-migration.sql')

迁移前先在已有表上核对行数，例如：

    docker compose exec -T postgres psql -U museflow -d museflow -c "SELECT 'generation_tasks' AS table_name, count(*) FROM generation_tasks UNION ALL SELECT 'generation_attempts', count(*) FROM generation_attempts UNION ALL SELECT 'result_assets', count(*) FROM result_assets"

执行迁移后再核对新增素材表并比对旧表行数：

    docker compose exec -T postgres psql -U museflow -d museflow -c "SELECT 'generation_tasks' AS table_name, count(*) FROM generation_tasks UNION ALL SELECT 'generation_attempts', count(*) FROM generation_attempts UNION ALL SELECT 'result_assets', count(*) FROM result_assets UNION ALL SELECT 'reference_assets', count(*) FROM reference_assets"
    docker compose run --rm migrate uv run --no-dev alembic current

同时抽查旧任务状态、旧创建请求默认类型和结果下载路径。用 `docker compose run --rm migrate` 执行迁移，再检查迁移容器的退出状态和 `alembic current`；迁移只依赖 PostgreSQL，不要求 MinIO 在线。当前迁移不提供自动 downgrade，部分历史步骤明确不可逆。失败时从备份恢复到隔离副本排查，不要在未备份的默认 Compose 数据卷上试迁移，也不要通过 downgrade 回退数据库。

MinIO bucket 默认保持私有。浏览器图片使用 MuseFlow 的稳定 `/api/v1/assets/{id}/download` 路径，后端需要时再签发短期访问能力；前端不保存对象键或签名 URL。

MuseFlow API 没有用户级认证、授权或限流，只适用于本机或受信网络。Compose 将所有宿主机端口限制到 `127.0.0.1`；不要把当前 Compose 直接暴露到公网。

## 可选：本地 Vite 开发

正式 Compose 前端是由 Node 构建、Nginx 提供静态文件的 Web 容器。日常开发可以让 Vite 把 `/api` 代理到本机 API：

```powershell
docker compose up -d postgres redis minio minio-init migrate api scheduler worker maintenance-worker
Push-Location frontend
npm ci
npm run dev -- --host 127.0.0.1
Pop-Location
```

开发 UI 地址为 `http://127.0.0.1:5173/tasks/new`。前端使用 React、TypeScript、Vite、React Router、TanStack Query 和 React Hook Form；类型由 FastAPI OpenAPI 生成。

## 显式启用真实 Provider

默认 Compose 在 API 和 Worker 中固定使用 `MockProvider`。如已逐次取得真实调用授权，并接受 Wan Provider 的调用费用，先在当前 PowerShell 会话安全设置 `DASHSCOPE_API_KEY` 与获准的 `DASHSCOPE_API_HOST`，再明确使用覆盖文件：

```powershell
docker compose -f compose.yaml -f compose.real-provider.yaml up --build
```

覆盖文件缺少这两个变量时会拒绝启动。启用后创建图像任务会访问真实 Provider 并可能产生费用；不要用它运行常规测试或 Compose UI E2E。每次实际 Provider 请求都需要单独授权。历史任务如果记录了不可用的 Provider profile，会明确失败，不会静默切换。阶段 6 的真实图生图轨迹和脱敏 JSON 见[最终技术方案阶段 6 验证记录](docs/technical/museflow-final-technical-design.md#阶段-6-验证记录)和[受控 Provider 证据](.scratch/provider-feasibility/evidence/stage6-real-e2e.json)。同时保留的真实文生图记录见[Provider 可行性报告](docs/technical/museflow-provider-feasibility.md)；账单控制台未核账，实际扣款状态未知。

## 验证与开发命令

```powershell
Push-Location frontend
npm run generate:api
npm run check:api-generated
npm run typecheck
npm run lint
npm test
npm run build
npm run test:e2e:compose
Pop-Location
```

`npm run test:e2e:compose` 使用独立 Compose project 名、隔离 volume 和临时本机端口启动完整 MockProvider 系统，运行 Chromium Playwright，再检查栈重启后的持久化并只清理它创建的项目资源。需要本机已安装 Playwright Chromium。前端静态构建、真实 Compose 服务、API 与私有 MinIO 请求都会参与 E2E，不会调用真实 Provider。添加 `--backend-check` 会在该隔离栈内再运行 Alembic 检查和完整后端 pytest；再加 `--full-backend-gates` 会对隔离 Compose 的 PostgreSQL、Redis 和 MinIO 启用服务集成门禁，全部使用 MockProvider，不发送真实 Provider 请求。Playwright 另行收集并检查浏览器 console、页面错误和 HTTP 失败。

完整本地发布验证命令：

    uv run --directory backend ruff check .
    uv run --directory backend pyright
    uv run --directory backend python -m compileall -q src tests migrations
    docker compose config --quiet
    Push-Location frontend
    npm ci
    npm run check:api-generated
    npm run typecheck
    npm run lint
    npm test
    npm run build
    npx playwright install chromium
    npm run test:e2e:compose -- --backend-check --full-backend-gates
    Pop-Location

仓库 GitHub Actions 在 push、pull request 和手动触发时运行同一组静态检查、前端检查及隔离 Compose 全栈回归。逐项发布验收状态和证据索引见[最终发布验收追踪表](docs/technical/museflow-final-acceptance.md)。

后端命令在 `backend` 目录由 `uv` 管理，完整回归为 `uv run pytest -q`；静态检查包括 `uv run ruff check .`、`uv run pyright`、`uv run python -m compileall -q src tests` 和 Alembic 检查。主 Compose 的 `migrate` 会执行 `alembic upgrade head`。

## 前端路由

- `/tasks/new`：文生图和单参考图图生图。
- `/tasks/:taskId`：任务状态、Provider 快照、Attempt、时间线、参考素材、结果与手动重试链。
- `/tasks`：任务历史、generation type 与状态组合筛选，以及后端 cursor 分页。
