# MuseFlow MVP 技术方案

- 版本：v0.1
- 状态：待实施
- 更新日期：2026-09-17
- 对应产品文档：[MuseFlow MVP 产品设计文档](../product/museflow-mvp-product-design.md)

## 1. 目标与背景

MuseFlow MVP 是一个 AI 图像生成与异步任务编排平台。本方案用于指导 MVP 的工程实现，重点解决以下问题：

- HTTP 请求应快速返回，图片生成在独立 Worker 中执行。
- PostgreSQL 持久化任务、重试、错误和文件元数据，并作为业务状态的唯一事实来源。
- 对第三方模型的超时、限流和临时故障执行有限次数的自动重试。
- 对重复提交和重复消费提供可验证的幂等保护。
- 参考图和生成结果通过对象存储持久化，并使用短期签名 URL 访问。
- 本地环境可通过 Docker Compose 一键启动，并能稳定复现成功、重试和永久失败场景。

本项目属于阶段性 MVP，但承担后端能力展示用途。因此方案优先保证核心链路可靠、模块职责清晰和测试可复现，不为尚未确认的规模提前引入微服务或复杂基础设施。

## 2. 范围与非目标

### 2.1 本方案范围

- React 单页应用及三个 MVP 页面。
- FastAPI HTTP API。
- 生成任务状态机、幂等创建和手动重试。
- Celery Worker、自动重试和重复消费保护。
- 一个真实图片生成 Provider 和一个 `MockProvider`。
- PostgreSQL、Redis 和 MinIO。
- 单元测试、集成测试及关键端到端测试。
- Docker Compose 本地运行环境。

### 2.2 非目标

- 微服务拆分、Kubernetes 和跨地域部署。
- 用户注册、RBAC、配额和计费。
- 多 Provider 智能路由。
- WebSocket、SSE 和事件流平台。
- 视频生成、质量评测和候选排序。
- 文件分片上传、断点续传和内容哈希去重。
- 未经过压测验证的吞吐量和延迟承诺。

## 3. 关键约束与不可妥协项

1. PostgreSQL 中的任务记录是业务状态的唯一事实来源；不得以 Celery 或 Redis 状态代替。
2. 所有任务状态变化必须经过任务模块，不允许路由或 Worker 任意写状态字段。
3. 自动重试只适用于明确分类为临时错误的失败；永久错误不得自动重试。
4. 相同幂等键和相同请求内容必须返回同一任务；相同幂等键但请求内容不同必须返回冲突。
5. Worker 重复收到同一消息时，最多只允许一个执行者获得当前执行权。
6. 对象默认私有，上传和下载只能通过短期签名 URL。
7. `MockProvider` 必须可确定地模拟成功、超时、限流、临时错误和永久错误。
8. 核心行为必须可通过自动化测试复现，不依赖人工观察 Celery 控制台。

## 4. 总体架构

MVP 采用模块化单体。FastAPI API 和 Celery Worker 使用同一个 Python 代码库与领域实现，但作为不同进程运行。

```text
┌──────────────────────────────┐
│ React + TypeScript + Vite    │
│ React Router / TanStack Query│
└──────────────┬───────────────┘
               │ HTTP / JSON
┌──────────────▼───────────────┐
│ FastAPI API                  │
│ - 参数校验                   │
│ - 任务创建、查询、手动重试   │
│ - 签名 URL                   │
└──────┬──────────┬────────────┘
       │          │ publish task_id
       │          ▼
       │    ┌──────────────┐
       │    │ Redis Broker │
       │    └──────┬───────┘
       │           ▼
       │    ┌─────────────────────┐
       │    │ Celery Worker       │
       │    │ - 抢占执行权        │
       │    │ - 调用 Provider     │
       │    │ - 分类错误与重试    │
       │    │ - 保存生成结果      │
       │    └──────┬──────────────┘
       │           │
┌──────▼───────────▼───────┐   ┌─────────────────┐
│ PostgreSQL               │   │ MinIO           │
│ 任务、尝试、事件、文件元数据│   │ 参考图、生成结果 │
└──────────────────────────┘   └─────────────────┘
                                │
                         ┌──────▼────────────┐
                         │ GenerationProvider│
                         │ Real / Mock       │
                         └───────────────────┘
```

该架构保持一个部署单元内的代码一致性，同时将耗时任务从 HTTP 请求进程中隔离。MVP 不拆微服务，避免引入分布式调用、独立发布和跨服务一致性成本。

## 5. 技术栈

| 领域 | 选择 | 说明 |
| --- | --- | --- |
| 前端 | React、TypeScript、Vite | 无 SEO 或服务端渲染要求，单页应用足够 |
| 前端路由 | React Router | 三个页面及任务详情路由 |
| 服务端状态 | TanStack Query | 请求缓存、失效处理和按任务状态动态轮询 |
| 表单 | React Hook Form | 控制输入、校验和提交状态 |
| 样式 | CSS Modules 或现有设计令牌加普通 CSS | 保留定制视觉，不引入重量级 UI 套件 |
| HTTP API | FastAPI、Pydantic | 请求校验、OpenAPI 和清晰的数据契约 |
| 数据访问 | SQLAlchemy 2.x、Alembic、psycopg | PostgreSQL 映射、事务和迁移 |
| 后台任务 | Celery | 独立 Worker、重试与任务投递 |
| 消息代理 | Redis | MVP 部署简单；不保存业务事实 |
| 数据库 | PostgreSQL | 任务状态、幂等约束和文件元数据 |
| 对象存储 | MinIO，使用 S3 兼容访问方式 | 本地可复现，未来可替换为兼容对象存储 |
| Provider 调用 | httpx | 显式配置连接、读取和总超时 |
| Python 工具链 | uv、Ruff、Pyright、pytest | 依赖锁定、格式检查、类型检查和测试 |
| 前端测试 | Vitest、Testing Library、Playwright | 模块行为与关键用户流程 |
| 本地编排 | Docker Compose | API、Worker、PostgreSQL、Redis、MinIO 和前端 |

依赖应锁定到经验证的版本。技术方案只固定主要版本线，不追逐未经验证的最新版本。

### 5.1 同步与异步选择

首版后端采用同步 SQLAlchemy 和同步 Provider 客户端：

- 产品的核心“异步”来自消息队列和独立 Worker，而不是要求所有 Python 代码使用 `asyncio`。
- FastAPI 可以安全运行同步路由，Celery 本身也以同步任务为主。
- API 与 Worker 共享同一套事务实现，降低会话管理和测试复杂度。
- MVP 为单用户演示模式，轮询流量不足以证明引入异步数据库访问的收益。

只有在真实压测表明 HTTP API 的 I/O 并发成为瓶颈后，才评估 SQLAlchemy asyncio；该变化不应影响任务模块的外部接口。

## 6. 核心模块与 seam

### 6.1 任务模块

任务模块是业务核心，应把状态机、幂等规则、执行权抢占、自动重试结果和手动重试关联隐藏在一个小接口后面。HTTP 路由和 Celery task 都只是调用者。

建议对外暴露的行为：

- 创建或返回幂等任务。
- 查询任务详情与分页历史。
- 为 Worker 原子抢占执行权。
- 记录某次执行成功、临时失败或永久失败。
- 从失败任务创建有关联的新重试任务。

任务模块必须拒绝非法状态转换。状态主流程保持为：

```text
CREATED → QUEUED → RUNNING → SUCCEEDED
                           ↘ FAILED
```

自动重试是同一任务下的新执行尝试，不创建新的业务任务。等待自动重试期间主任务保持 `RUNNING`，详细过程由执行尝试和任务事件表达。手动重试则创建新的业务任务，并通过 `original_task_id` 关联原失败任务；原任务记录不可被覆盖。

### 6.2 GenerationProvider seam

`GenerationProvider` 是真实 seam，因为 MVP 同时存在真实 Adapter 和 `MockProvider` Adapter。其接口应保持小而稳定，例如只接收规范化生成请求并返回规范化结果：

```python
class GenerationProvider(Protocol):
    def generate(self, request: GenerationRequest) -> GenerationResult: ...
```

该模块内部隐藏：

- 厂商参数与 MuseFlow 参数之间的转换。
- 鉴权和请求头。
- 厂商异步任务的提交与轮询。
- 超时设置和响应解析。
- 厂商错误到 MuseFlow 错误类别的映射。
- 结果 URL 或二进制内容的获取。

标准错误类别至少包括：

- `TransientProviderError`：网络错误、超时、HTTP 429、可恢复的 5xx。
- `PermanentProviderError`：参数错误、内容安全拒绝、认证失败和不支持的请求。

Celery task 不应解析厂商响应，也不应包含供应商特有分支。

### 6.3 素材模块

素材模块负责对象键生成、上传确认、结果保存和签名访问。其接口应隐藏 MinIO SDK 细节，防止对象存储调用散落到路由和 Worker。

参考图上传流程：

1. 前端请求创建上传记录。
2. API 校验声明的类型和大小，生成随机对象键及短期签名上传 URL。
3. 前端直接上传到 MinIO。
4. 前端通知 API 完成上传。
5. API 使用 `HEAD` 检查真实对象大小和元数据，确认后标记素材可用。

生成结果由 Worker 上传，不经过浏览器。下载接口返回短期签名 URL，而不是公开桶或长期 URL。

### 6.4 队列 Adapter

Celery 是任务投递 Adapter，不拥有业务状态。消息体只包含稳定标识，例如 `task_id`，不复制提示词、状态等业务数据。Worker 始终从 PostgreSQL 读取最新任务。

Celery task 本身保持很薄：

1. 调用任务模块抢占执行权。
2. 调用 `GenerationProvider`。
3. 调用素材模块保存结果。
4. 将规范化结果或错误交还任务模块。
5. 仅在任务模块判定可重试时调用 Celery retry。

## 7. 数据设计

### 7.1 `generation_tasks`

主要字段：

- `id`：UUID。
- `idempotency_key`：创建请求幂等键，唯一索引。
- `request_fingerprint`：规范化请求内容的摘要。
- `prompt`、`width`、`height`、`image_count`。
- `reference_asset_id`：可空。
- `status`。
- `retry_count`、`max_retries`。
- `error_code`、`error_message`：面向系统诊断的规范化错误。
- `original_task_id`：手动重试时关联原任务，可空。
- `created_at`、`queued_at`、`started_at`、`completed_at`。
- `version`：用于乐观并发控制。

### 7.2 `generation_attempts`

每一次真实 Provider 调用对应一条记录：

- `id`、`task_id`、`sequence`。
- `provider_name`。
- `provider_request_key`、`provider_request_id`。
- `status`、`error_code`、`error_message`。
- `started_at`、`finished_at`。

`task_id + sequence` 必须唯一。该表用于解释自动重试过程，不替代任务主状态。

### 7.3 `assets`

- `id`、`task_id`。
- `role`：`REFERENCE` 或 `RESULT`。
- `object_key`、`content_type`、`size_bytes`。
- `status`：上传中的参考图只有确认后才能使用。
- `created_at`。

对象键由服务端生成，不使用用户文件名。对象存储桶保持私有。

### 7.4 `task_events`

保存对用户或开发者有诊断价值的追加事件，例如创建、入队、开始执行、准备重试、执行成功和执行失败。事件只用于展示和诊断，不反向计算任务当前状态。

## 8. 幂等、并发与投递语义

### 8.1 创建请求幂等

前端为每次明确的用户提交生成 `Idempotency-Key`。后端在同一事务中保存幂等键和请求摘要：

- 键不存在：创建任务。
- 键存在且摘要一致：返回既有任务。
- 键存在但摘要不同：返回 HTTP 409。

数据库唯一约束是最终保护，不能只依赖应用进程内锁。

### 8.2 重复消费保护

Worker 使用条件更新或行锁原子抢占执行权。没有获得执行权的重复消息直接结束，不调用 Provider。

任务消息采用至少一次投递语义。MuseFlow 可以保证：

- 正常重复消息不会并行调用 Provider。
- 一个任务只接受一组有效结果。
- 终态任务的重复消息不会再次执行。

若 Worker 在 Provider 已接收请求、但本地尚未记录结果时崩溃，严格避免外部重复调用需要真实 Provider 支持幂等键或可恢复查询的远端请求编号。首个真实 Provider 的选型必须满足至少一项：

1. 支持客户端幂等键；或
2. 提交后返回稳定请求编号，并允许按编号查询原请求结果。

若候选 Provider 两项都不支持，只能保证 MuseFlow 内部最多接受一份结果，无法诚实承诺外部调用 exactly-once；此时必须在实现前重新确认产品验收口径。

### 8.3 数据库提交与消息发布

MVP 采用“先提交任务、再发布消息”的顺序：

1. 创建 `CREATED` 任务并提交。
2. 发布只含 `task_id` 的 Celery 消息。
3. 发布成功后将任务推进为 `QUEUED`。

若发布失败，任务保留在 `CREATED`，相同幂等请求可以再次尝试发布。另提供可测试的恢复命令扫描超过阈值的 `CREATED` 任务并重新发布。

该方案不提供完全原子的数据库与 Redis 双写。只有在实际运行证明无人值守恢复是必要需求时，才增加 PostgreSQL Outbox 和独立 Dispatcher；MVP 不预先承担这部分复杂度。

## 9. HTTP API 草案

统一使用 `/api/v1` 前缀。

| 方法 | 路径 | 用途 |
| --- | --- | --- |
| `POST` | `/assets/uploads` | 创建参考图上传记录和签名 URL |
| `POST` | `/assets/{asset_id}/complete` | 确认上传并校验对象 |
| `POST` | `/tasks` | 幂等创建生成任务 |
| `GET` | `/tasks` | 分页查询任务历史 |
| `GET` | `/tasks/{task_id}` | 查询任务、尝试、事件和结果 |
| `POST` | `/tasks/{task_id}/retry` | 从允许重试的失败任务创建新任务 |
| `GET` | `/assets/{asset_id}/download` | 获取短期签名下载 URL |
| `GET` | `/health/live` | 进程存活检查 |
| `GET` | `/health/ready` | 数据库等必要依赖就绪检查 |

创建任务和手动重试接口都使用 `Idempotency-Key` 请求头。错误响应使用稳定错误码和可展示消息，内部异常细节只写入日志。

## 10. 前端方案

正式前端重新使用 React 和 TypeScript 实现。现有 `demo/` 仅作为布局、文案和交互参考，不直接演化为正式代码。

页面结构：

- 创建任务：提示词、必要参数、可选参考图、上传状态和提交保护。
- 任务详情：主状态、自动重试次数、执行记录、错误和生成结果。
- 任务历史：分页、状态筛选、进入详情和手动重试。

TanStack Query 负责服务端状态：

- `CREATED`、`QUEUED`、`RUNNING` 状态下轮询详情。
- `SUCCEEDED` 或 `FAILED` 后自动停止轮询。
- 页面重新获得焦点时刷新非终态任务。
- 创建或重试成功后使历史列表失效并刷新。

前端的按钮禁用只能改善交互，不能替代后端幂等约束。OpenAPI 契约可生成前端类型，避免手写两套状态枚举和响应结构。

## 11. 错误处理与重试

默认自动重试策略：

- 最多 3 次 Provider 调用尝试，包括首次调用。
- 指数退避并启用随机抖动。
- 每次调用设置明确的连接、读取和总超时。
- 429 可尊重合理的 `Retry-After`，但仍受最大等待时间限制。
- 参数错误、认证失败、内容安全拒绝和明确的 4xx 不重试。

错误处理顺序为：Provider Adapter 规范化厂商错误，任务模块决定该类别是否可重试，Celery Adapter 只执行已经做出的重试决定。

用户可见错误不得包含密钥、完整厂商响应、内部堆栈或对象存储凭据。

## 12. 安全与配置

- 密钥、数据库连接和对象存储凭据通过环境变量注入，不提交到仓库。
- 提供不含真实密钥的 `.env.example`。
- MinIO 桶默认私有，签名 URL 有较短有效期。
- 上传类型限制为 PNG、JPEG 和 WEBP，并同时校验声明类型、实际对象大小和文件头。
- 提示词、错误消息和文件名在前端输出时按文本处理，禁止拼接未转义 HTML。
- CORS 只允许配置的前端来源。
- 日志对鉴权头、签名 URL 和 Provider 密钥进行脱敏。
- 单用户 MVP 不实现认证，但 HTTP 接口设计不得依赖硬编码用户 ID 才能工作。

## 13. 可观测性

MVP 使用结构化日志，不强制引入完整可观测平台。每条关键日志至少包含：

- `task_id`。
- `attempt_id`。
- `provider_name`。
- `provider_request_id`，如存在。
- 状态变化或规范化错误码。
- 耗时。

API 和 Worker 使用同一关联字段。禁止把提示词全文、签名 URL 或密钥作为常规日志字段。

## 14. 测试策略

测试以模块接口为主要表面，不穿透接口断言内部实现。

### 14.1 单元测试

- 合法和非法状态转换。
- Provider 错误分类。
- 自动重试次数与退避参数计算。
- 相同幂等键的相同和不同请求。
- 手动重试关系。
- `MockProvider` 各种确定性场景。

### 14.2 集成测试

- 使用真实 PostgreSQL 验证唯一约束、事务和并发抢占。
- 使用真实 Redis 和 Celery Worker 验证消息投递与自动重试。
- 使用真实 MinIO 验证签名上传、上传确认、结果保存和下载。
- 并发提交相同幂等键时只生成一个任务。
- 同一任务重复投递时只有一个 Worker 获得执行权。
- API 重启后任务历史和结果仍可访问。

Celery eager mode 可用于快速测试，但不能代替至少一组真实 broker 和 Worker 的集成测试。

### 14.3 端到端测试

使用 Playwright 和 `MockProvider` 覆盖：

1. 文生图成功。
2. 上传参考图后成功。
3. 两次临时失败后自动恢复。
4. 永久错误直接失败且不自动重试。
5. 自动重试耗尽后手动创建关联重试任务。
6. 历史分页、筛选、详情和下载。

真实 Provider 测试单独标记，只有显式提供密钥时运行，避免普通 CI 产生费用或受外部波动影响。

## 15. 建议目录结构

```text
MuseFlow/
├── backend/
│   ├── pyproject.toml
│   ├── src/museflow/
│   │   ├── app.py
│   │   ├── config.py
│   │   ├── db/
│   │   ├── tasks/
│   │   │   ├── domain.py
│   │   │   ├── application.py
│   │   │   ├── repository.py
│   │   │   └── routes.py
│   │   ├── generation/
│   │   │   ├── interface.py
│   │   │   └── adapters/
│   │   │       ├── mock.py
│   │   │       └── real_provider.py
│   │   ├── assets/
│   │   └── workers/
│   └── tests/
├── frontend/
│   ├── src/
│   │   ├── app/
│   │   ├── features/tasks/
│   │   ├── features/assets/
│   │   └── shared/
│   └── tests/
├── demo/                       # 可丢弃交互原型
├── docs/
└── compose.yaml
```

目录只是初始导航结构。不要为每个数据库表机械创建一层，也不要增加只有一个实现且没有变化理由的空 seam。

## 16. 实施阶段

### 阶段 1：工程骨架与基础设施

- 建立前后端工程、质量工具和依赖锁文件。
- 建立 Docker Compose，启动 PostgreSQL、Redis、MinIO、API 和 Worker。
- 加入数据库迁移和健康检查。

验收：新环境可以一条命令启动，各进程就绪检查通过。

### 阶段 2：任务核心与 MockProvider

- 实现数据表、任务状态机和幂等创建。
- 实现 Celery 投递、执行权抢占和错误分类。
- 实现可确定配置的 `MockProvider`。

验收：自动化测试稳定覆盖成功、临时失败重试、永久失败和重复消费。

### 阶段 3：对象存储

- 实现参考图签名上传与上传确认。
- 实现 Worker 保存结果和签名下载。
- 完成文件类型、大小和私有访问验证。

验收：参考图和结果在 API 重启后仍可访问，未签名对象不可公开读取。

### 阶段 4：正式前端

- 根据最终选定的原型布局实现三个页面。
- 接入轮询、错误展示、历史分页和手动重试。
- 生成或共享 HTTP 契约类型。

验收：使用 `MockProvider` 完成全部关键端到端流程。

### 阶段 5：真实 Provider

- 按选型约束接入一个真实 Adapter。
- 记录远端请求 ID、错误映射和调用耗时。
- 增加显式启用的真实 Provider 冒烟测试。

验收：完成一次真实端到端生成，结果保存到 MinIO，失败不会暴露敏感响应。

### 阶段 6：可靠性与交付收尾

- 验证并发幂等、重复投递、进程重启和恢复命令。
- 完成 README、演示步骤和验证记录。
- 运行完整测试、构建、lint 和类型检查。

验收：产品文档中的 MVP 完成标准全部有实现或可复现的测试记录。

## 17. 风险与取舍

| 风险 | 当前处理 | 后续触发条件 |
| --- | --- | --- |
| Redis 重启导致未持久消息丢失 | 开启 AOF、禁止淘汰队列键、保留 `CREATED` 恢复命令 | 需要更强无人值守恢复时引入 Outbox Dispatcher |
| 外部 Provider 不支持幂等 | 把幂等或可恢复远端请求 ID 作为首个 Provider 的选型条件 | 无法满足时重新确认 exactly-once 验收口径 |
| 前端轮询产生额外请求 | 仅轮询非终态任务，终态立即停止 | 真实活跃任务量明显增长时评估 SSE |
| Celery 配置复杂 | 集中配置并用真实 broker 集成测试 | 队列类型和路由显著增加时再评估专用消息代理 |
| 原型代码污染正式实现 | `demo/` 只作参考，正式前端独立构建 | 最终布局选定后可归档原型 |
| 真实 Provider 外部波动和费用 | 普通 CI 使用 `MockProvider`，真实测试显式启用 | 上线前增加限额、告警和成本统计 |

## 18. 验收标准

- `docker compose up` 能启动并完成必要初始化。
- 创建任务快速返回 `task_id`，生成过程不阻塞 HTTP 请求。
- `MockProvider` 的成功、临时失败恢复、永久失败和重试耗尽场景均可重复演示。
- 相同幂等键的并发请求只创建一个任务。
- 重复投递不会产生并行 Provider 调用或多份有效结果。
- 自动重试遵守错误分类、次数上限、指数退避和随机抖动。
- 手动重试创建新任务并保留原任务关联。
- 参考图和结果存入 MinIO，数据库只保存元数据和对象标识。
- API 重启后历史、状态和结果仍存在。
- 后端测试、前端测试、集成测试、端到端测试、构建、lint 和类型检查全部通过。
- README 包含启动、配置、演示、测试和常见故障说明。

## 19. 当前进度与下一步

当前已完成：

- MVP 产品范围和完成标准已经确认。
- 技术栈、总体架构、核心模块 seam 和可靠性策略已经形成方案。
- 可丢弃 UI 原型可供正式前端参考，但最终布局尚未确认。

实施前仍需确认的一项外部选择：

- 首个真实图片生成 Provider。选型需要综合可用地区、账号与费用、参考图能力、超时模型，以及是否支持幂等键或远端请求恢复。

下一步建议从阶段 1 开始建立工程骨架和 Docker Compose，并在阶段 2 先用 `MockProvider` 闭合任务可靠性链路，再接真实 Provider。

## 20. 参考资料

- [FastAPI Background Tasks](https://fastapi.tiangolo.com/tutorial/background-tasks/)
- [Celery Tasks and Retries](https://docs.celeryq.dev/en/stable/userguide/tasks.html)
- [Celery with Redis](https://docs.celeryq.dev/en/stable/getting-started/backends-and-brokers/redis.html)
- [SQLAlchemy 2.0 Documentation](https://docs.sqlalchemy.org/en/20/)
- [React with TypeScript](https://react.dev/learn/typescript)
- [TanStack Query Polling](https://tanstack.com/query/latest/docs/framework/react/guides/polling)
