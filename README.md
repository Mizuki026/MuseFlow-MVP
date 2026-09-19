# MuseFlow MVP

MuseFlow 是面向开发者和面试评审者的可靠性作品集项目。MVP 只允许在本机或受信网络运行，**不得直接暴露到公网**。

## 第 1 窗口：任务核心与 HTTP API

当前后端工具链使用 Python 3.13、uv、FastAPI、SQLAlchemy、Alembic 和 PostgreSQL。依赖锁文件位于 `backend/uv.lock`。

### 本机 PostgreSQL 测试环境

仓库当前已有一个临时 PostgreSQL 实例监听 `127.0.0.1:55432`，数据库为 `museflow`，并已升级到当前 Alembic revision。它适合第 1 窗口的集成测试，不应被当作长期开发数据库。

在 PowerShell 中，为当前终端设置变量：

```powershell
$env:MUSEFLOW_TEST_DATABASE_URL = "postgresql+psycopg://postgres@127.0.0.1:55432/museflow"
$env:MUSEFLOW_DATABASE_URL = $env:MUSEFLOW_TEST_DATABASE_URL
```

然后运行迁移、测试和 API：

```powershell
Push-Location backend
uv run alembic upgrade head
uv run pytest
uv run uvicorn museflow.api.main:app --host 127.0.0.1 --port 8000
Pop-Location
```

`MUSEFLOW_DATABASE_URL` 应在使用正式本机数据库时替换为实际连接串；不要把密码写入仓库、日志或对话。当前代码直接读取进程环境变量，不会自动加载 `.env` 文件。

### Compose

Docker Desktop + Compose is the local infrastructure path. Redis and MinIO are now part of compose.yaml and bind only to 127.0.0.1.

Run:

    docker compose up -d redis minio minio-init
    docker compose ps

The local endpoints are:

- Redis: redis://127.0.0.1:6379/0
- MinIO API: http://127.0.0.1:9000
- MinIO Console: http://127.0.0.1:9001
- Private bucket: museflow-results

The Compose init job creates the bucket and explicitly keeps it private. Local default credentials are only for this trusted development environment; never reuse them outside it.

### 当前环境审核与窗口边界

Docker Desktop、Docker Engine 和 Docker Compose 已可用；Redis 与 MinIO 已通过 Compose 启动并完成健康检查。第 1 窗口的业务代码保持 PostgreSQL、任务领域和 HTTP API 边界不变，不因为这些依赖已就绪而提前引入 Scheduler、Celery、Worker、Provider 或对象存储逻辑。

`127.0.0.1:55432` 的隔离 PostgreSQL 只作为第 1 窗口集成测试的可复现测试数据库保留，不是 Redis 或 MinIO 的替代方案，也不与 Compose 依赖冲突。第 2 窗口可以直接使用 Compose 内部地址 `redis://redis:6379/0`；后续结果持久化窗口可以直接使用 `http://minio:9000` 和私有 bucket `museflow-results`。Provider 仍需显式配置和受控验证，不存在自动替代承诺。
## 前端环境

前端使用 React、TypeScript、Vite、React Router、TanStack Query 和 React Hook Form；测试工具已安装 Vitest、Testing Library 和 Playwright。

在 frontend 目录运行：

    npm run dev
    npm run generate:api
    npm run build
    npm run typecheck
    npm test
    npm run lint
    npm run test:e2e

frontend/.env 已配置本机 API 地址 http://127.0.0.1:8000/api/v1，并由 Git 忽略。
正式前端提供 `/tasks/new`、`/tasks/:taskId` 和 `/tasks`。前端类型由 FastAPI OpenAPI 生成；Playwright 使用显式启用的 Demo API 和确定性 MockProvider，不调用真实 Provider。
## 后续环境

- Scheduler/Worker 窗口可直接使用已启动的 Redis，并读取 REDIS_URL。
- 结果持久化窗口可直接使用已启动的私有 MinIO bucket，并读取 MINIO_ENDPOINT、访问凭据和 MINIO_BUCKET。
- Provider 冒烟测试前：在本地配置 `DASHSCOPE_API_KEY` 和 `DASHSCOPE_API_HOST`，不得提交真实值。
- 前端工作区已建立于 frontend/，Node 版本固定为 24.18.0，使用 npm。


## 真实 Provider 与受控冒烟测试

正式 Adapter 位于 backend/src/museflow/providers.py，通过 GenerationProvider 接口由 Worker 调用。默认 MUSEFLOW_PROVIDER=mock，普通测试和 Demo 不访问真实 Provider；需要本地受控运行时才设置 MUSEFLOW_PROVIDER=dashscope。

DashScope 只从本地进程环境读取 DASHSCOPE_API_KEY 和 DASHSCOPE_API_HOST，不要写入源码、日志、文档或提交。Adapter 固定使用 wan2.6-t2i、n=1、size=1280*1280 和 prompt_extend=false，只执行一次创建请求；429、5xx、网络超时和轮询超时交给现有应用重试语义，不能在 Adapter 内部重发创建请求。

远端任务 ID 由执行层关联到 attempt。结果 URL 经过主机白名单、重定向逐跳校验、网络地址拒绝、流式大小限制、Content-Type、文件头和尺寸校验后才写入私有 MinIO。Provider 不提供 exactly-once；远端已受理但响应丢失时仍可能发生重复调用和费用。

受控冒烟测试不会因为环境变量存在而自动运行。先运行 uv run --directory backend python -m museflow.dashscope_smoke，该命令只检查变量存在并明确不发请求。只有再次确认北京地域/工作空间、模型权限、计费权限、单次最多约 ¥0.20、不得自动重试和遇错立即停止后，才显式追加 --authorize-real-request。日常 pytest、Compose Demo 和 CI 继续只使用 MockProvider。