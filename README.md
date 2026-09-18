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

`compose.yaml` 当前只覆盖 PostgreSQL、一次性 migration job 和 API，服务端口绑定前应保持本机或受信网络范围。Docker Desktop 尚未作为本机前置依赖安装时，不要把 Compose 作为当前测试的唯一入口。

Redis、MinIO、Scheduler、Worker 和 Provider 适配器属于后续窗口；日常测试继续使用 Mock，真实 Provider 只做受控冒烟测试。

## 后续环境

- Scheduler/Worker 窗口开始前：安装并启动 Docker Desktop，提供 Redis，并设置 `REDIS_URL`。
- 结果持久化窗口开始前：提供私有 MinIO bucket，并设置 `MINIO_ENDPOINT`、访问凭据和 `MINIO_BUCKET`。
- Provider 冒烟测试前：在本地配置 `DASHSCOPE_API_KEY` 和 `DASHSCOPE_API_HOST`，不得提交真实值。
- 前端窗口开始前：建立 `frontend/` 工作区并固定 Node/package manager 版本。
