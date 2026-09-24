# MuseFlow frontend

正式前端提供三个路由：

- `/tasks/new`：创建单图、1280 × 1280 任务；
- `/tasks/:taskId`：状态、Provider 快照、attempt、错误、事件时间线、手动重试和结果下载；
- `/tasks`：cursor 分页、生成方式与状态组合筛选、已加载提示词搜索和任务卡片。

创建页支持文生图和单参考图图生图。参考素材先在浏览器预览，再通过幂等上传 API 校验；页面刷新可恢复提示词、幂等键和 READY 素材 ID，不会持久化原始图片或对象 URL。前端只使用后端返回的稳定下载路径，不保存或记录 MinIO 签名 URL。

本地 Vite 开发时，`/api` 代理到 `http://127.0.0.1:8000`，API 路径使用 `/api/v1`。完整本地产品建议按仓库根目录 README 使用 Compose 启动；生产 Web 容器使用 Nginx 静态文件和同源 `/api/` 代理。

常用命令：

    npm run generate:api
    npm run dev
    npm run build
    npm run typecheck
    npm test
    npm run lint
    npm run test:e2e
    npm run test:e2e:compose
    npm run test:e2e:compose -- --backend-check

`generate:api` 从 FastAPI OpenAPI 输出重新生成 `src/api/schema.generated.ts`。`npm run test:e2e:compose` 会自行构建并启动隔离 Compose project、动态分配本机端口，使用 MockProvider 运行生产静态前端和真实 API/私有 MinIO 的 Chromium E2E，重启服务检查持久化后只移除自己的容器、网络与 volume。`--backend-check` 还会在同一隔离 PostgreSQL 服务创建独立测试库，执行 Alembic 检查和完整后端 pytest。测试不会调用真实 Provider，也不会触碰默认 Compose 项目。
