# MuseFlow UI Demo（可丢弃原型）

验证问题：创建任务、查看状态与失败重试是否顺手？哪一种页面结构适合 MuseFlow？

## 正式产品演示

本目录仍是可丢弃的独立静态原型；正式产品使用仓库根目录的 Docker Compose。默认 Compose 调用 DashScope 真实 Provider，创建任务可能产生费用。若只做离线体验，可在 PowerShell 会话中设置 `$env:MUSEFLOW_PROVIDER='mock'` 后启动。

1. 安装 Docker Desktop、Docker Compose、Node.js 24 和 `uv`，在仓库根目录运行 `docker compose up --build`。
2. 打开 `http://127.0.0.1:5173/tasks/new`，确认 Web、API、Scheduler、generation Worker、maintenance Worker、PostgreSQL、Redis 和 MinIO 均健康。
3. 输入文生图提示词和尺寸，提交后会调用真实 Provider；在详情页观察 `QUEUED`、`RUNNING` 到终态的变化。
4. 刷新详情页，确认状态、attempt、时间线和结果仍从后端恢复。
5. 回到新建页上传 PNG、JPEG 或 WebP 参考图，等待素材校验通过并出现预览。
6. 选择图生图，提交提示词和参考图；这也会调用真实 Provider。检查详情页显示生成类型、冻结 Provider profile 和结果。
7. 在任务历史按类型、状态和提示词筛选，并用分页加载更多任务。
8. 从成功任务复用参数，或从失败任务执行手动重试，检查新任务与原任务的关联。
9. 运行 `npm run test:e2e:compose -- --backend-check --full-backend-gates`，Playwright 会在隔离 Compose 项目中覆盖成功、确定性失败、幂等重放、重试、浏览器错误监控、服务集成门禁和重启持久化。
10. 检查命令末尾的 Playwright / pytest 汇总；脚本只清理它创建的 Compose project 和 volumes。自动化脚本强制使用 MockProvider，不需要真实凭证、不产生 Provider 费用；reference maintenance 隔离和清理覆盖 `tests/integration/test_real_reference_maintenance_worker.py` 与 `tests/integration/test_reference_asset_minio.py`。

失败注入走显式启用的 Demo API 入口，并继续经正式任务创建用例、PostgreSQL outbox、Scheduler、Redis 和 Worker 生命周期；它不直接写数据库，也不替代正式 UI 的成功流程。默认 Compose 不启用 Demo 入口。

## 启动

在仓库根目录运行：

```sh
node demo/server.mjs
```

浏览器打开 http://127.0.0.1:4173 。无需安装依赖；也可直接打开 `demo/index.html`。

## 三种布局

- `?variant=A`：创作工作台，左侧输入，右侧作品与任务卡片。
- `?variant=B`：引导式创建，以单次创作和灵感预览为中心。
- `?variant=C`：任务控制台，状态统计、任务表格和弹窗创建。

底部按钮或方向键切换布局，输入框中方向键保持正常编辑行为。

## 体验流程

1. 使用默认提示词，或切换示例；选择比例、参考图后生成。
2. 点击任务卡片查看排队、生成、结果与执行记录。
3. 打开“演示设置”，选择“两次超时后自动恢复”或“永久错误”再提交。
4. 打开预置失败任务，点击“重新尝试”，查看新任务与原记录的关联。
5. 在任务历史搜索、筛选和分页；完成任务可以下载示例图或复用参数。
6. 通过“演示设置”查看当前内存状态、重置数据。

## 限制与待验证事项

全部生成、图片、耗时与恢复行为均为模拟，不接入模型、数据库或账号。
图片为本地原创 SVG 插画，不会根据提示词或比例实际生成/变换。
可上传 PNG/JPG/WEBP 作为当前浏览器中的参考图预览，不上传至服务端。
数据仅保存在内存中，刷新恢复初始数据；快速重复点击仅做前端提交保护，不代表后端幂等保证。
原型静态服务器只监听本机；底部切换器仅随此独立 Demo 存在，不属于正式应用。

原型保存在 `prototype/museflow-ui` 分支。用户尚未选定布局；不将原型视为已验证的产品决策。
正式实现应在选定布局后重新构建，并遵循 MVP 产品文档。

## 浏览器验证

启动 Demo 后，如已安装 Playwright 和 Microsoft Edge，可运行：

```sh
node demo/check-demo.cjs
```

Playwright 不在默认模块路径时，通过 `PLAYWRIGHT_MODULE` 环境变量指定其绝对路径。
脚本检查创建、成功下载、自动重试、永久失败后的编辑入口、手动重试关联、参数复用、参考图、历史筛选分页、布局切换和手机宽度溢出。
验收截图写入系统临时目录，不进入仓库。
