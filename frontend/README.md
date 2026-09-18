# MuseFlow frontend

正式前端提供三个路由：

- `/tasks/new`：创建单图、1280 × 1280 任务；
- `/tasks/:taskId`：状态、attempt、错误、时间线、重试和结果下载；
- `/tasks`：cursor 分页、状态筛选和任务卡片。

本地 API 默认地址为 `http://127.0.0.1:8000/api/v1`，可通过 `.env` 中的 `VITE_API_BASE_URL` 覆盖。前端只使用后端返回的稳定下载路径，不保存或记录 MinIO 签名 URL。

常用命令：

    npm run generate:api
    npm run dev
    npm run build
    npm run typecheck
    npm test
    npm run lint
    npm run test:e2e

`generate:api` 从 FastAPI OpenAPI 输出重新生成 `src/api/schema.generated.ts`。Playwright 流程要求 Compose 的 API、PostgreSQL、Redis、Worker、Scheduler 和 MinIO 已启动，并使用 Demo-only MockProvider 场景；普通测试不会调用真实 Provider。
