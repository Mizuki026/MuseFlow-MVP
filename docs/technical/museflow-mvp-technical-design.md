# MuseFlow MVP 技术方案

- 版本：v0.2
- 状态：MVP 发布验收完成
- 更新日期：2026-09-18
- 对应产品文档：[MuseFlow MVP 产品设计文档](../product/museflow-mvp-product-design.md)

## 1. 目标与背景

MuseFlow MVP 是一个面向开发者和面试评审者的 AI 异步任务可靠性作品集。系统只在本机或受信网络运行，使用一个真实图片生成 Provider 完成受控冒烟测试，并通过确定性的 `MockProvider` 验证日常行为和故障恢复。

本方案重点解决：

- HTTP 请求快速创建任务，图片生成由独立 Worker 执行。
- PostgreSQL 持久化任务、attempt、重试时间、事件、结果元数据和投递意图，并作为业务事实源。
- Redis 或 Worker 中断后，非终态任务能够自动恢复或进入可诊断终态。
- 对 HTTP 重放、消息重复投递、执行租约过期和旧 Worker 回写提供可验证保护。
- Provider 成功后，结果经安全下载、对象存储和数据库提交后才成为权威结果。
- 本地环境能够通过 Docker Compose 复现成功、限流、超时、临时失败、永久失败和进程中断。

项目属于阶段性 MVP，完整交付上限为一名开发者六周。第四周末必须形成可独立演示和测试的后端作品集；不为未经验证的规模提前引入微服务或复杂基础设施。

## 2. 范围与非目标

### 2.1 本方案范围

- React 单页应用及创建、详情、历史三个页面。
- FastAPI HTTP API 和独立查询 DTO。
- 单图文生图，请求只包含提示词和一个尺寸预设。
- 任务状态机、HTTP 幂等、自动重试和线性手动重试。
- PostgreSQL outbox、独立 Scheduler、Celery Worker 和重复消费保护。
- 执行 lease、fencing token、attempt 恢复和任务总截止时间。
- 一个真实 `GenerationProvider` Adapter 和一个 `MockProvider` Adapter。
- PostgreSQL、Redis 和 MinIO。
- 单元测试、集成测试、故障注入测试和关键端到端测试。
- Docker Compose 本地运行环境、迁移 job 和开发环境重置命令。

### 2.2 非目标

- 参考图、图生图、视频生成和多模态理解。
- 多图、任意宽高、复杂生成参数和质量评测。
- 多真实 Provider 路由或自动故障切换。
- 用户注册、认证、RBAC、用户隔离、配额和计费。
- 公网部署和公网滥用防护。
- 任务取消、删除 API 和自动数据 TTL。
- WebSocket、SSE 和事件流平台。
- 微服务、Kubernetes、跨地域部署和高可用。
- 备份恢复和灾难恢复承诺。
- 完整 tracing、metrics 和可观测平台。
- 未经压测的吞吐量、并发量和延迟承诺。
- Provider 缺少幂等或结果恢复能力时的外部 exactly-once。

## 3. 关键约束与不可妥协项

1. PostgreSQL 是任务状态、attempt、重试时间和投递意图的唯一业务事实源。
2. 任务主状态只能为 `QUEUED`、`RUNNING`、`RETRY_WAIT`、`SUCCEEDED` 或 `FAILED`。
3. 创建任务与写入 outbox 必须在一个数据库事务中完成。
4. Celery 和 Redis 只负责运输，不决定是否重试、何时重试或当前业务状态。
5. Provider 调用期间不得持有数据库行锁或长事务。
6. 每个未决 attempt 拥有稳定的 Provider request key；Worker 接管同一 attempt 时必须复用。
7. 完成写入必须携带当前 execution token，过期 Worker 不得覆盖新执行者。
8. 只有结果对象和资产元数据都确认后，任务才能进入 `SUCCEEDED`。
9. 自动重试只适用于明确分类的临时错误；永久错误不得自动重试。
10. 普通 CI 不调用收费 Provider；真实 Provider 只运行显式启用的受控冒烟测试。
11. 应用仅限本机或受信网络，README 必须声明不得直接暴露公网。
12. 所有可靠性声明必须对应可复现测试。

## 4. 总体架构

API、Worker 和 Scheduler 使用同一个 Python 代码库与领域实现，但作为不同进程角色运行。

```text
┌──────────────────────────────┐
│ React + TypeScript + Vite    │
│ React Router / TanStack Query│
└──────────────┬───────────────┘
               │ HTTP / JSON
┌──────────────▼───────────────┐
│ FastAPI API                  │
│ - 校验与查询 DTO             │
│ - 创建、查询、手动重试       │
│ - 结果下载重定向             │
└──────────────┬───────────────┘
               │
┌──────────────▼────────────────────────────┐
│ PostgreSQL                               │
│ task / attempt / event / asset / outbox  │
└───────────┬───────────────────┬───────────┘
            │                   │
      ┌─────▼──────┐     ┌──────▼────────────┐
      │ Scheduler  │     │ Application Use   │
      │ - outbox   │     │ Cases / Domain    │
      │ - retry due│     └───────────────────┘
      │ - lease    │
      └─────┬──────┘
            │ publish task_id
      ┌─────▼──────┐
      │ Redis      │
      │ Broker     │
      └─────┬──────┘
            │
      ┌─────▼─────────────────────┐
      │ Celery Worker Adapter     │
      │ → ExecuteGenerationAttempt│
      └─────┬──────────────┬──────┘
            │              │
┌───────────▼──────┐  ┌────▼───────────────┐
│ GenerationProvider│  │ ResultAssetStore   │
│ Real / Mock       │  │ MinIO / S3 Adapter │
└───────────────────┘  └────────────────────┘
```

Scheduler 是模块化单体的进程角色，不是独立微服务：它不拥有独立数据库、仓库或发布周期。默认 Compose 只运行一个 Scheduler，但数据库领取协议必须允许未来安全运行多个实例。

## 5. 技术栈

| 领域 | 选择 | 说明 |
| --- | --- | --- |
| 前端 | React、TypeScript、Vite | 三个页面，无 SSR 要求 |
| 前端路由 | React Router | 创建、详情和历史 |
| 服务端状态 | TanStack Query | 查询缓存和非终态轮询 |
| 表单 | React Hook Form | 输入与提交保护 |
| 样式 | CSS Modules 或现有设计令牌 | 不引入重量级 UI 套件 |
| HTTP API | FastAPI、Pydantic | OpenAPI 和独立查询 DTO |
| 数据访问 | SQLAlchemy 2.x、Alembic、psycopg | 事务、并发领取和迁移 |
| 后台任务 | Celery | 薄消息 Adapter |
| 消息代理 | Redis | 至少一次运输，不保存业务事实 |
| 数据库 | PostgreSQL | task、attempt、outbox 和业务状态 |
| 对象存储 | MinIO，使用 S3 兼容 API | 私有结果存储 |
| Provider 调用 | httpx | 显式超时和流式结果下载 |
| Python 工具链 | uv、Ruff、Pyright、pytest | 锁定依赖、格式、类型和测试 |
| 前端测试 | Vitest、Testing Library、Playwright | 组件与关键流程 |
| 本地编排 | Docker Compose | migration、API、Scheduler、Worker、依赖和前端 |

依赖锁定到经验证的版本。方案只固定主要版本线，不追逐未经验证的最新版本。

### 5.1 同步与异步选择

首版后端使用同步 SQLAlchemy 和同步 Provider Adapter：

- 产品的异步来自消息队列和独立 Worker，不要求全部 Python 代码采用 `asyncio`。
- API、Scheduler 和 Worker 可以共享同一事务实现。
- Provider 网络调用发生在数据库事务之外。
- 只有压测证明 API I/O 并发成为瓶颈后，才评估 SQLAlchemy asyncio；变化不得影响应用用例接口。

## 6. 配置、组合根与全局状态

### 6.1 配置分层

必须配置的外部依赖：

- Provider 凭据和端点；
- PostgreSQL、Redis 和 MinIO 连接；
- 允许的前端来源；
- 允许下载结果的 Provider/CDN 主机。

启动时读取并校验的运行策略：

- Provider 超时；
- 新任务默认最大 attempts；
- lease 时长与续租间隔；
- Scheduler 扫描间隔；
- 签名 URL 有效期。

固定代码契约：

- 状态机与错误分类；
- HTTP 幂等冲突规则；
- 请求字段；
- 允许文件类型与大小上限；
- 事件类型和 API 错误结构。

创建任务时必须保存 `max_attempts`、任务截止时间和 `policy_version`，避免重启后配置变化导致旧任务行为漂移。

### 6.2 组合根

- API、Worker 和 Scheduler 各有自己的 composition root。
- 环境变量只在进程启动时读取，settings 启动后不可变。
- 数据库 session 按用例创建，不使用模块级共享 session。
- HTTP client、S3 client 和 Provider Adapter 可以复用连接池，但必须通过构造参数注入应用服务。
- 时钟、ID 生成器和退避随机源通过小接口注入。
- FastAPI `Depends` 只负责取得依赖，不承载业务逻辑。
- Celery task 导入模块时不得隐式连接数据库或读取可变全局对象。

## 7. 核心模块与 seam

### 7.1 任务领域模块

任务模块拥有以下不变量：

- 状态转换合法性；
- HTTP 幂等和请求指纹；
- attempt sequence 与最大次数；
- 自动重试资格和时间；
- 手动重试线性谱系；
- execution lease 与 fencing token；
- 任务截止时间和终态不可逆。

状态转换、错误分类、指纹、退避计算和重试资格应实现为纯函数或无 I/O 的领域对象。HTTP 路由、Celery task 和 Scheduler 不能直接修改状态字段。

主状态机：

```text
QUEUED → RUNNING → SUCCEEDED
             ├──→ RETRY_WAIT → QUEUED
             └──→ FAILED
```

`QUEUED` 表示任务已被系统接受并承诺执行，不表达 Redis 内部状态。Worker lease 过期且任务仍可执行时，Scheduler 可以使 `RUNNING → QUEUED`；超过任务总截止时间则进入 `FAILED`。

### 7.2 应用用例

建议公开以下应用用例：

- `CreateTask`；
- `GetTaskDetail`；
- `ListTasks`；
- `RetryTask`；
- `ExecuteGenerationAttempt`；
- `DispatchOutbox`；
- `ScheduleDueRetries`；
- `RecoverExpiredLeases`。

`ExecuteGenerationAttempt` 负责编排任务、Provider 和结果存储：

1. 以短事务领取或接管 attempt，取得 execution token 和 lease。
2. 在事务外调用或恢复 Provider 请求。
3. 验证并保存结果对象。
4. 以短事务提交成功、临时失败或永久失败。
5. 所有完成写入校验 execution token。

Celery task 只解析稳定的 `task_id` 并调用该用例，不知道 Provider、MinIO 或状态迁移细节。

### 7.3 GenerationProvider seam

`GenerationProvider` 只接收规范化生成请求、稳定 Provider request key 和可选远端请求编号，返回规范化结果或错误：

```python
class GenerationProvider(Protocol):
    def generate(
        self,
        request: GenerationRequest,
        *,
        request_key: str,
        remote_request_id: str | None,
    ) -> GenerationResult: ...
```

Adapter 内部隐藏：

- 厂商参数转换、鉴权和请求头；
- 厂商同步或异步任务协议；
- 客户端幂等键，或按客户端预先确定的稳定请求标识恢复；
- 超时、轮询和响应解析；
- 厂商错误到 MuseFlow 错误类别的映射；
- 结果位置或二进制内容获取。

标准错误至少包括：

- `TransientProviderError`：网络错误、超时、HTTP 429 和可恢复 5xx；
- `PermanentProviderError`：参数错误、内容安全拒绝和不支持的请求；
- `ProviderConfigurationError`：凭据或服务配置问题。

应用领域策略决定错误是否自动重试；Celery Adapter 不解析厂商响应。

### 7.4 结果素材模块

MVP 素材模块只管理生成结果，不管理用户上传。职责包括：

- 生成确定性对象键；
- 安全获取 Provider 结果；
- 流式校验类型、大小和 SHA-256；
- 幂等写入私有对象存储；
- 提交资产元数据；
- 生成短期签名下载 URL。

对象键使用稳定标识，例如 `results/{task_id}/{attempt_id}/0`。实际扩展名由验证后的文件类型决定，不信任远端 URL 或文件名。

### 7.5 队列 Adapter

Celery 消息只包含 `task_id`。配置采用 late acknowledgment，并允许 Worker 丢失后重投。消息至少一次投递，所有重复保护来自 PostgreSQL task、attempt、lease 和 execution token，而不是 Celery 状态。

### 7.6 Scheduler

Scheduler 周期执行：

- 领取并发布未完成 outbox；
- 把到期的 `RETRY_WAIT` 转为 `QUEUED` 并写入 outbox；
- 回收过期 lease，恢复同一未决 attempt 或使任务超时失败。

领取使用条件更新或 `FOR UPDATE SKIP LOCKED`，每次小批量、短事务处理。默认单实例运行，但多个实例不会重复拥有同一数据库工作项。

## 8. 时间预算与重试策略

默认值：

- Provider 单次 attempt 总截止时间：180 秒；
- 最大 attempts：3 次，包括首次调用；
- 退避：2 秒起步、倍数 2、上限 30 秒、full jitter；
- 任务总截止时间：10 分钟；
- execution lease：Provider 截止时间加 60 秒，默认 240 秒；
- 必须续租时的间隔：30 秒；
- Scheduler 扫描间隔：本地环境 1 秒；
- 签名下载 URL：5 分钟。

阶段 0 可以根据真实 Provider 调整 Provider 截止时间，但必须保持：

- lease 长于单次调用截止时间；
- 任务总截止时间能够容纳允许的 attempts 和退避；
- 不安全的配置组合在启动时直接拒绝。

测试通过注入 clock、随机源和 policy 使用毫秒级时间，不进行真实长等待。

## 9. 数据设计

### 9.1 `generation_tasks`

主要字段：

- `id`：UUID；
- `idempotency_key`：全局唯一；
- `request_fingerprint`：应用默认值后的规范化请求摘要；
- `prompt`、`size_preset`；
- `execution_profile`：仅 Demo 模式使用，可空；
- `status`；
- `max_attempts`、`policy_version`；
- `deadline_at`、`next_attempt_at`；
- `error_code`、`error_message`；
- `retried_from_task_id`：可空且唯一，形成线性手动重试链；
- `created_at`、`queued_at`、`started_at`、`completed_at`；
- `version`：乐观并发控制。

提示词原样保存。请求指纹基于规范化结构计算，但不得擅自 trim、改写或 Unicode 归一化提示词。

### 9.2 `generation_attempts`

- `id`、`task_id`、`sequence`；
- `status`、`phase`；
- `provider_name`；
- `provider_request_key`、`provider_request_id`；
- `execution_token`、`lease_expires_at`；
- `error_code`、`error_message`；
- `started_at`、`finished_at`。

`task_id + sequence` 必须唯一。attempt 的内部阶段至少能够区分 Provider 调用和结果持久化。Worker 接管未决 attempt 时更新 execution token，但复用 sequence 与 Provider request key。

### 9.3 `assets`

- `id`、`task_id`、`attempt_id`；
- `role`：MVP 只允许 `RESULT`；
- `object_key`：唯一；
- `content_type`、`size_bytes`、`sha256`；
- `created_at`。

对象存储桶保持私有。单个对象默认不超过 20 MiB，只允许 PNG、JPEG 和 WEBP。

### 9.4 `task_events`

保存用于审计和前端时间线的追加事件，例如创建、排队、开始执行、临时失败、等待重试、attempt 接管、结果保存、成功和失败。

事件与对应状态更新在同一事务中写入，但不反向计算当前状态；MuseFlow 不是事件溯源系统。

### 9.5 `outbox_messages`

主要字段：

- `id`、`message_type`、`aggregate_id`；
- `payload`：只含稳定标识；
- `available_at`；
- `published_at`；
- 领取与重试所需的版本或时间字段；
- `created_at`。

MVP 只实现任务执行所需的少量内部消息，不建设通用事件平台。

## 10. 幂等、投递与执行语义

### 10.1 HTTP 创建幂等

创建任务和手动重试都要求 `Idempotency-Key`：

- 键不存在：在同一事务中创建 `QUEUED` 任务、事件和 outbox，返回 201；
- 键存在且指纹一致：返回原任务和 `idempotency_replayed: true`，返回 200；
- 键存在但指纹不同：返回 409 与 `IDEMPOTENCY_KEY_CONFLICT`。

数据库唯一约束是最终保护。HTTP Key 不得直接作为 Provider request key。

### 10.2 Outbox 投递

任务事务提交后，Scheduler 发布只含 `task_id` 的消息。若 Redis 不可用，outbox 保留；恢复后自动重试。发布成功但 `published_at` 提交失败可能造成重复消息，Worker 必须安全处理。

`QUEUED` 的业务含义是系统已经承诺执行，不表示 Redis 内一定存在消息，因此不需要公开 `CREATED` 状态。

### 10.3 Attempt、lease 与 fencing

Worker 通过短事务领取任务：

1. 若存在未决 attempt，则接管同一 attempt；否则创建下一个 sequence。
2. 生成新的 execution token 并设置 lease。
3. 事务提交后才调用 Provider。
4. 完成或失败写入必须匹配当前 token。

lease 过期后，Scheduler 使任务重新可执行。旧 Worker 即使稍后返回，也因 token 不匹配而不能提交权威结果。

### 10.4 外部调用去重边界

同一 attempt 的 Provider request key 必须稳定。Provider 支持客户端幂等时透传；Provider 允许按客户端稳定请求标识查询时使用同一标识恢复。服务端返回的 remote request ID 在成功保存后可以加速恢复，但如果该 ID 只能在提交后获得且无法重建，就不能独立提供外部去重保证。

若候选 Provider 不支持客户端幂等，也不能按客户端稳定标识查询，只能保证 MuseFlow 内部一份权威结果，不能承诺外部调用 exactly-once。阶段 0 必须记录并触发产品承诺降级。

## 11. Provider 结果持久化

Provider 返回成功后，attempt 进入内部 `PROVIDER_SUCCEEDED` 阶段，task 对外仍为 `RUNNING`：

1. 从允许的 HTTPS 主机流式取得结果。
2. 禁止重定向；若 Provider 必须重定向，每跳都重新校验目标。
3. 拒绝 loopback、链路本地和私有网段，测试配置显式例外。
4. 校验总超时、最大 20 MiB、响应类型和文件头。
5. 计算 SHA-256，并以确定性对象键幂等写入 MinIO。
6. 在数据库事务中写入 asset、事件和任务成功状态。

对象上传成功但数据库提交前崩溃时，接管者恢复同一 attempt 并安全覆盖或确认同一对象。结果保存失败不创建新的 Provider attempt；只有超过任务截止时间后才进入可诊断失败。

## 12. HTTP API 与查询契约

统一使用 `/api/v1` 前缀。

| 方法 | 路径 | 用途 |
| --- | --- | --- |
| `POST` | `/tasks` | 幂等创建生成任务 |
| `POST` | `/demo/tasks` | 仅 Demo 模式注册的场景任务 |
| `GET` | `/tasks` | cursor 分页查询任务历史 |
| `GET` | `/tasks/{task_id}` | 查询任务、attempt、时间线、谱系和结果 |
| `POST` | `/tasks/{task_id}/retry` | 幂等创建线性手动重试任务 |
| `GET` | `/assets/{asset_id}/download` | 临时重定向到短期签名 URL |
| `GET` | `/health/live` | API 进程存活 |
| `GET` | `/health/ready` | API 数据库与迁移就绪 |

### 12.1 查询 DTO

API DTO 与 SQLAlchemy ORM 分离。任务详情可以公开：

- ID、规范化请求、主状态和时间戳；
- 当前阶段、attempt 次数、最大次数和 `next_retry_at`；
- 脱敏错误代码、用户消息和 `can_retry`；
- 结果资产的 MuseFlow 下载路径；
- `retried_from_task_id` 和 `retry_task_id`；
- 精简的 attempt 和事件时间线。

不得公开 execution token、lease owner、Provider request key、outbox、堆栈或厂商原始响应。

### 12.2 错误通道

同步 HTTP 错误使用稳定 envelope：

```json
{
  "error": {
    "code": "IDEMPOTENCY_KEY_CONFLICT",
    "message": "此幂等键已用于不同请求",
    "request_id": "..."
  }
}
```

异步任务失败由详情接口正常返回 200，并在任务数据中表达 `FAILED`、稳定错误码、用户消息和 `retryable`。错误码属于契约，消息可以调整。

### 12.3 历史分页

- 固定按 `(created_at DESC, id DESC)` 排序；
- 参数为 `limit`、可选 `cursor` 和可选 `status`；
- cursor 不透明，前端不得解析；
- 响应包含 `items` 和 `next_cursor`；
- 固定最大 limit；
- 不实现总数统计、复杂搜索或任意排序。

### 12.4 下载响应

详情接口只返回稳定的 MuseFlow 下载路径。下载端点生成有效期 5 分钟的签名 URL，使用临时重定向，并设置 `Cache-Control: no-store`。

## 13. Demo 模式与 MockProvider

Demo 模式必须显式启用。只有此时才注册 `/api/v1/demo/tasks`，支持以下确定性场景：

- `success`；
- `transient_then_success`；
- `rate_limited`；
- `timeout`；
- `permanent_failure`。

场景作为内部 execution profile 保存，不通过魔法提示词触发。非 Demo 模式下路由不存在。自动化测试也可以绕过 HTTP，直接注入脚本化 Mock、clock 和随机源。

## 14. 前端方案

正式前端使用 React 和 TypeScript。现有 `demo/` 只作布局和交互参考，不直接演化为正式代码。

页面：

- 创建任务：提示词、提交保护和新操作的幂等键；
- 任务详情：主状态、当前阶段、attempt、时间线、错误、重试链和结果；
- 任务历史：cursor 分页、状态筛选、详情入口和允许的手动重试。

TanStack Query 负责：

- 对 `QUEUED`、`RUNNING` 和 `RETRY_WAIT` 轮询；
- 终态后停止轮询；
- 页面重新获得焦点时刷新非终态任务；
- 创建或重试成功后刷新历史。

前端按钮禁用不能替代后端约束。OpenAPI 生成前端类型，禁止手写第二套状态枚举和错误结构。

## 15. 安全与敏感信息

- 密钥、数据库连接和对象存储凭据通过环境变量注入，不提交到仓库。
- 提供不含真实密钥的 `.env.example`。
- MinIO bucket 默认私有。
- 结果下载只允许 HTTPS 和配置的 Provider/CDN 主机；Mock 与本地测试显式例外。
- 默认不跟随重定向；必须跟随时逐跳重新验证目标。
- 禁止请求 loopback、链路本地和私有网段，测试配置显式例外。
- 流式下载并限制连接、读取、总超时和最大字节数。
- 同时校验响应类型和文件头。
- 提示词和错误消息在前端按文本输出，禁止拼接未转义 HTML。
- CORS 只允许配置的本地或受信前端来源。
- 日志不得记录鉴权头、密钥、完整签名 URL、提示词、execution token 或原始厂商响应。
- README 明确声明当前版本不得暴露公网。

## 16. 健康检查、迁移与关闭

### 16.1 健康检查

- API liveness：进程能够响应。
- API readiness：PostgreSQL 可访问且迁移版本兼容；Redis 和 Provider 不作为 API ready 前置条件。
- Scheduler readiness：PostgreSQL 与 Redis 可访问。
- Worker readiness：PostgreSQL、Redis 和 MinIO 可访问，不主动调用收费 Provider。
- Docker Compose 分别检查各服务，不用一个聚合端点掩盖具体故障。

Redis 离线时 API 仍可创建 `QUEUED` 任务，outbox 在 Redis 恢复后发布。

### 16.2 数据库迁移

- Compose 使用一次性 migration job。
- migration 成功后，API、Scheduler 和 Worker 才启动。
- 应用进程不自动修改 schema，只检查 revision。
- 测试从空数据库运行全部 Alembic 升级。
- CI 检查模型与 migration 没有未生成差异。
- 禁止用 `create_all()` 代替正式迁移。
- MVP 承诺升级到当前版本，不承诺自动 downgrade。

### 16.3 优雅关闭

- API 停止接收新请求并完成正在处理的短请求。
- Worker 停止领取新消息，并给当前任务有限宽限期。
- 超过宽限期时直接退出，不把任务擅自写成永久失败；由 lease 和 Scheduler 恢复。
- Scheduler 完成当前短事务后退出，所有领取状态必须可过期或重新领取。
- Compose 停止宽限期默认 30 秒，不等待最长 Provider 调用结束。

## 17. 可观测性

MVP 使用 JSON 结构化日志，不引入完整 tracing 或 metrics 平台。

允许字段白名单：

- `timestamp`、`level`、`event`；
- `request_id`、`task_id`、`attempt_id`；
- `provider_name`；
- 状态转换或规范化错误码；
- 耗时、attempt sequence；
- 进程角色和应用版本。

禁止记录提示词、密钥、鉴权头、完整签名 URL、execution token、图片内容和原始厂商响应。日志只用于诊断，不能成为业务事实源。

## 18. 测试策略

### 18.1 单元测试

- 所有合法与非法状态转换；
- 请求指纹和 HTTP 幂等分支；
- Provider 错误分类；
- 自动与手动重试资格；
- attempt 次数、退避、jitter 和总截止时间；
- 线性重试谱系；
- cursor 编解码；
- Demo 场景确定性。

### 18.2 契约测试

- `GenerationProvider` 的成功、临时失败、永久失败和恢复语义；
- `ResultAssetStore` 的幂等写入、类型、大小和 checksum；
- API 错误 envelope 与 OpenAPI DTO。

### 18.3 PostgreSQL 集成测试

- 同键并发创建只生成一个任务与一条有效投递意图；
- outbox 并发领取与重复发布；
- attempt 领取、lease 过期接管和 fencing token；
- 过期 Worker 不能提交；
- 手动重试唯一约束；
- migration 从空库成功执行。

### 18.4 Worker 与故障注入测试

- 重复消息只有一个执行者获得有效租约；
- Worker 在 Provider 调用前中断；
- Provider 接受请求后 Worker 中断；
- 对象上传后、数据库提交前 Worker 中断；
- Redis 离线后恢复，outbox 自动发布；
- 两次临时失败后成功；
- 永久错误不重试；
- 任务截止时间后进入可诊断终态；
- API、Worker、Scheduler 和 Redis 重启后自动收敛。

Celery eager mode 可用于快速测试，但不能代替至少一组真实 Redis 和 Worker 的集成测试。

### 18.5 MinIO 集成测试

- 私有 bucket 不可匿名读取；
- 确定性对象键重复写入安全；
- 类型、文件头、大小和 checksum 校验；
- 签名 URL 有效期与下载重定向；
- API 重启后结果仍可访问。

### 18.6 端到端测试

使用 Playwright 和 `MockProvider` 覆盖：

1. 文生图成功并下载结果；
2. 临时失败后自动恢复；
3. 永久错误直接失败；
4. 自动重试耗尽后创建线性手动重试；
5. 历史 cursor 分页、状态筛选和详情时间线。

真实 Provider 测试单独标记，只有显式提供密钥时运行。普通 CI 不产生费用，也不依赖第三方稳定性。

### 18.7 发布检查

- Ruff、Pyright、前后端 lint 和类型检查；
- 单元、契约、集成、故障注入和端到端测试；
- 前后端构建；
- migration 差异检查；
- 日志扫描，确认不含敏感字段；
- README 演示步骤在干净环境复现。

不得声称未自动化或未实际运行的保证已经通过。

## 19. 建议目录结构

```text
MuseFlow/
├── backend/
│   ├── pyproject.toml
│   ├── src/museflow/
│   │   ├── api/
│   │   │   ├── app.py
│   │   │   ├── dependencies.py
│   │   │   └── routes/
│   │   ├── config.py
│   │   ├── db/
│   │   ├── tasks/
│   │   │   ├── domain.py
│   │   │   ├── application.py
│   │   │   ├── repository.py
│   │   │   └── dto.py
│   │   ├── generation/
│   │   │   ├── interface.py
│   │   │   └── adapters/
│   │   │       ├── mock.py
│   │   │       └── real_provider.py
│   │   ├── assets/
│   │   ├── outbox/
│   │   ├── scheduler/
│   │   ├── workers/
│   │   └── composition/
│   └── tests/
├── frontend/
│   ├── src/
│   │   ├── app/
│   │   ├── features/tasks/
│   │   └── shared/
│   └── tests/
├── demo/                       # 可丢弃交互原型
├── docs/
└── compose.yaml
```

目录只是初始导航。不要为每个数据表机械创建层，也不要增加没有变化理由的空 seam。

## 20. 实施阶段

### 阶段 0：Provider 可行性探针，1–2 天

- 验证地区、账号、费用、固定尺寸和单图生成。
- 验证客户端幂等键，或按客户端预先确定的稳定请求标识查询结果；仅有提交后返回的 remote request ID 不满足外部去重条件。
- 实测提交、轮询、超时、429、5xx、永久 4xx、结果获取和结果主机。
- 形成不含凭据的简短选型记录。

验收：确认 Provider 满足可靠性约束；否则记录并启用产品承诺降级。此阶段不编写正式 Adapter。

### 第 1 周：任务核心与 HTTP API

- 建立工程、质量工具、依赖锁和 migration job。
- 实现纯领域规则、数据表、创建/查询 API 和 HTTP 幂等。
- 使用真实 PostgreSQL 验证唯一约束和并发创建。

验收：任务能够以 `QUEUED` 创建和查询，幂等分支全部有自动化测试。

### 第 2 周：异步成功纵向切片

- 实现 outbox、Scheduler、Redis、Celery Adapter 和 `MockProvider`。
- 实现 `ExecuteGenerationAttempt` 与异步成功路径。
- 从 API 创建到 Worker 完成形成可运行切片。

验收：消息重复发布不产生多个权威结果，Redis 短暂离线后自动恢复。

### 第 3 周：attempt 与故障恢复

- 实现数据库重试时间、lease、execution token 和任务截止时间。
- 实现临时与永久错误、接管同一 attempt 和线性手动重试。
- 完成关键 Worker 崩溃与重启测试。

验收：约定的非终态任务无需人工命令即可自动收敛。

### 第 4 周：结果存储与后端交付点

- 实现安全结果下载、确定性对象键、MinIO 保存和签名下载。
- 完成错误契约、查询 DTO、时间线、健康检查和结构化日志。
- 提供最小演示页面或 API 演示脚本。

验收：形成可独立交付的后端作品集，完整后端测试通过。

### 第 5 周：正式前端

- 实现创建、详情和历史三个页面。
- 接入轮询、cursor 分页、时间线、错误和手动重试。
- 使用 OpenAPI 生成或共享类型。

验收：使用 `MockProvider` 完成全部 Playwright 关键流程。

### 第 6 周：真实 Provider 与交付收尾

- 实现阶段 0 已验证的真实 Provider Adapter。
- 运行受控真实 Provider 冒烟测试。
- 完成 README、演示脚本、重置命令和验证记录。
- 运行完整测试、构建、lint 和类型检查。

验收：产品文档中的发布标准全部有实现或可复现证据。

## 21. 风险与取舍

| 风险 | 当前处理 | 边界或触发条件 |
| --- | --- | --- |
| PostgreSQL 与 Redis 双写 | 事务 outbox，允许重复发布 | 不引入通用事件平台 |
| Worker 调用期间崩溃 | 稳定 attempt、lease、request key 和 fencing | 外部去重仍依赖 Provider |
| Provider 不支持客户端幂等或稳定标识恢复 | 阶段 0 硬性验证 | 不满足时降级外部重复调用承诺 |
| 结果上传后数据库未提交 | 确定性对象键并恢复同一 attempt | 不为存储失败创建新 Provider attempt |
| Redis 重启丢失消息 | PostgreSQL outbox 与 Scheduler 重发 | Redis 不保存业务事实 |
| 无认证系统暴露公网 | 限定本机或受信网络并在 README 警告 | 公网部署必须进入新阶段 |
| 第三方结果 URL 不可信 | 主机白名单、网络地址、重定向、大小和文件头校验 | 测试配置显式例外 |
| 前端轮询产生额外请求 | 只轮询非终态，终态停止 | 活跃量显著增长后评估 SSE |
| 真实 Provider 波动和费用 | 普通 CI 使用 Mock，真实测试显式启用 | 不做持续真实 Provider CI |
| 六周范围过大 | 第四周后端停点，前端和真实 Adapter 顺延 | 不删除可靠性测试伪装完成 |

## 22. 发布验收标准

发布门槛与产品文档保持一致，至少验证：

1. 干净环境完成迁移并启动全部进程。
2. Mock 成功路径保存一张可下载结果。
3. HTTP 幂等同键重放和冲突均符合契约。
4. Redis 离线期间任务不丢失，恢复后自动执行。
5. 重复消息和过期 Worker 不产生多个权威结果。
6. 三个关键 Worker 中断窗口均自动收敛。
7. 临时、永久、重试耗尽和总截止时间行为正确。
8. 手动重试形成唯一线性谱系。
9. 进程重启后任务、attempt、事件和结果保持一致。
10. MinIO 私有访问、结果校验和短期签名下载正确。
11. 日志不包含约定的敏感信息。
12. 前后端测试、故障测试、构建、lint、类型检查和 migration 检查通过。
13. 真实 Provider 冒烟测试有日期、版本和结果记录。
14. README 明确受信网络边界，并能在干净环境复现演示。

## 23. 当前进度与下一步

当前已完成：

- 产品定位、信任边界、范围和非目标已经确认；
- 阶段 0 Provider 可行性探针已完成，选定北京地域 `wan2.6-t2i`，并记录外部 exactly-once 降级承诺；
- 第 1 个实现窗口已完成任务领域规则、PostgreSQL 三表持久化、Alembic migration、创建/详情/历史 API 和 HTTP 幂等；
- 已使用真实 PostgreSQL 验证同事务写入、唯一约束、并发创建、事务回滚和稳定 cursor；
- 已通过第 1、2 窗口的领域、API、PostgreSQL、Redis/Celery 集成测试，以及 Ruff、Pyright 和 Alembic check；
- 已完成第 3 窗口阶段 A：数据库驱动的临时/永久错误分类、指数退避、`next_attempt_at`、attempt lease、过期接管、execution token fencing、任务截止时间、Worker 恢复和线性手动重试；
- 已完成第 3 窗口阶段 B：`ResultAssetStore`、MinIO Adapter、确定性对象键、PNG/JPEG/WEBP 文件校验、大小限制、SHA-256、私有 bucket、短期签名下载和结果 DTO；
- 已完成 API、Scheduler、Worker 的分离 readiness，并在真实 Compose Redis、PostgreSQL 和 MinIO 上验证成功链路、私有访问和签名 URL 过期；
- 第 4 个实现窗口已完成正式 React 前端：创建、详情、历史、非终态轮询、cursor 分页、状态筛选、事件时间线、错误展示、手动重试和稳定结果下载入口；
- 前端状态与错误契约由 FastAPI OpenAPI 生成，服务端状态由 TanStack Query 管理，创建与重试使用稳定的业务提交幂等键；
- 已补齐仅在 Demo 模式注册的场景任务入口、内部 execution profile、受信本地 CORS 和历史缩略图契约，并通过真实 Compose MockProvider Playwright 流程验证成功、临时恢复、永久失败、手动重试、分页、筛选、下载和刷新恢复；
- 当前迁移已升级到 0004_demo_execution_profiles；第 6 窗口最终验收后后端 67 passed, 0 skipped，前端 Vitest 17 passed、Playwright 4 passed，前端 build/typecheck/lint、Ruff、Pyright、compileall、Alembic check、Compose config 和 diff check 均通过。

第 5 个窗口已完成一次受控真实 Provider E2E；第 6 窗口完成了默认 MockProvider 正式演示、可靠性边界、Compose 健康检查和交付文档验收。当前版本仍不承诺公网部署或外部 Provider exactly-once。

## 24. 参考资料

- [FastAPI Documentation](https://fastapi.tiangolo.com/)
- [Celery Tasks](https://docs.celeryq.dev/en/stable/userguide/tasks.html)
- [Celery with Redis](https://docs.celeryq.dev/en/stable/getting-started/backends-and-brokers/redis.html)
- [SQLAlchemy 2.0 Documentation](https://docs.sqlalchemy.org/en/20/)
- [PostgreSQL Explicit Locking](https://www.postgresql.org/docs/current/explicit-locking.html)
- [React with TypeScript](https://react.dev/learn/typescript)
- [TanStack Query Documentation](https://tanstack.com/query/latest)

第 5 个窗口结果主机故障（历史记录）：此前一次真实请求已完成提交、轮询到 `SUCCEEDED` 并解析结果 URL，但在精确主机白名单处以 `RESULT_INVALID: result host is not allowed` 停止，未下载 PNG、未写入 MinIO。随后通过厂商支持确认精确主机与 Bucket 地域，并只补入该主机；Mock Provider 继续作为默认实现。

最终状态：2026-09-19，MuseFlow MVP 发布验收完成；真实 Provider 仅有一次受控成功证据，默认演示仍使用 MockProvider，所有公网认证、限流、用户隔离、费用保护和外部 exactly-once 能力仍明确不在 MVP 范围。
