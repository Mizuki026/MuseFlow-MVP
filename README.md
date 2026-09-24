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

## 数据库与私有存储

当前 Alembic head 为 `0007_reference_operation_leases`。干净数据库会由 `migrate` 自动升级到当前 head；仓库 Compose 的 PostgreSQL、Redis 和 MinIO 数据分别保存在独立命名 volume 中。

把现有数据库交给新版本之前，先完成可恢复备份，再核对关键表迁移前后的行数和业务数据。迁移不提供自动 downgrade，部分历史迁移明确不可逆。默认 Compose 项目可能包含旧数据，不要在未备份的数据库上试迁移。

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

覆盖文件缺少这两个变量时会拒绝启动。启用后创建图像任务会访问真实 Provider 并可能产生费用；不要用它运行常规测试或 Compose UI E2E。每次实际 Provider 请求都需要单独授权。历史任务如果记录了不可用的 Provider profile，会明确失败，不会静默切换。

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

`npm run test:e2e:compose` 使用独立 Compose project 名、隔离 volume 和临时本机端口启动完整 MockProvider 系统，运行 Chromium Playwright，再检查栈重启后的持久化并只清理它创建的项目资源。需要本机已安装 Playwright Chromium。前端静态构建、真实 Compose 服务、API 与私有 MinIO 请求都会参与 E2E，不会调用真实 Provider。添加 `--backend-check` 会在该隔离栈内再运行 Alembic 检查和完整后端 pytest。

后端命令在 `backend` 目录由 `uv` 管理，完整回归为 `uv run pytest -q`；静态检查包括 `uv run ruff check .`、`uv run pyright`、`uv run python -m compileall -q src tests` 和 Alembic 检查。主 Compose 的 `migrate` 会执行 `alembic upgrade head`。

## 前端路由

- `/tasks/new`：文生图和单参考图图生图。
- `/tasks/:taskId`：任务状态、Provider 快照、Attempt、时间线、参考素材、结果与手动重试链。
- `/tasks`：任务历史、generation type 与状态组合筛选，以及后端 cursor 分页。
