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
    npm run build
    npm run typecheck
    npm test
    npm run lint

frontend/.env 已配置本机 API 地址 http://127.0.0.1:8000/api/v1，并由 Git 忽略。
## 后续环境

- Scheduler/Worker 窗口可直接使用已启动的 Redis，并读取 REDIS_URL。
- 结果持久化窗口可直接使用已启动的私有 MinIO bucket，并读取 MINIO_ENDPOINT、访问凭据和 MINIO_BUCKET。
- Provider 冒烟测试前：在本地配置 `DASHSCOPE_API_KEY` 和 `DASHSCOPE_API_HOST`，不得提交真实值。
- 前端工作区已建立于 frontend/，Node 版本固定为 24.18.0，使用 npm。
