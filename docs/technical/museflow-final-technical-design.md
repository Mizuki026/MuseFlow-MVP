# MuseFlow 最终版本技术方案

- 版本：v0.2
- 状态：待实施
- 更新日期：2026-09-24
- 对应产品文档：[MuseFlow 最终产品设计文档](../product/museflow-product-design.md)
- 基线方案：[MuseFlow MVP 技术方案](./museflow-mvp-technical-design.md)

## 1. 目标与背景

MuseFlow 已完成可靠文生图 MVP。本方案定义从 MVP 演进到最终作品集版本的技术路径，核心增量是一个可信的图生图纵向切片，以及对功能、可靠性和性能声明的证据化收尾。

本阶段不重写 MVP，也不通过堆叠基础设施展示技术栈。现有 PostgreSQL outbox、独立 Scheduler、Redis、Celery Worker、attempt、lease、fencing、MinIO、Provider Adapter 和前端查询链路继续作为执行基线。新增能力必须复用这些可靠性机制，但“复用”不意味着保留已经发现的缺口：固定结果对象键无法被数据库 fencing 保护，固定 lease 也无法覆盖新增的图生图 I/O，因此必须先完成可靠性加固。

本阶段属于阶段性产品收尾，而非长期商业平台建设。方案优先考虑：

- 图生图与文生图共享统一任务语义，同时保持输入能力约束清晰。
- 参考图片从上传、校验、私有存储到任务引用形成完整可信链路。
- Provider 差异停留在 Adapter 内部，不扩散到任务模块和前端。
- 一个 attempt 表示一次逻辑 Provider 生成调用；lease 接管和已有远端任务恢复不创建新 attempt。
- 结果先写入不可变候选对象，数据库只发布当前 execution token 对应的唯一权威结果。
- 自动化测试和受控真实验收能够证明产品声明。
- 只在数据证明必要时引入 SSE、实时指标平台或第二个真实 Provider。

## 2. 范围与非目标

### 2.1 本方案范围

- 文生图与图生图的统一任务输入模型。
- 单张参考图片的服务端上传、校验、私有存储、预览和任务关联。
- 参考素材上传幂等、状态机、超时 `STAGING` 恢复和显式清理。
- `GenerationProvider` 对文生图与图生图能力的显式表达。
- `MockProvider` 的图生图成功与故障场景。
- DashScope 图生图 Adapter 和一次受控真实端到端验收。
- 任务历史按生成类型和状态筛选。
- 手动重试对规范化输入和参考素材的复用。
- 前端创建、详情和历史页面的图生图增量。
- 图生图单元、集成、端到端和故障恢复测试。
- 固定场景下的运行证据和验收追踪；只有对外公开性能数字时才执行可复现压测。
- 不可变候选结果、lease heartbeat、维护任务隔离和基础孤立对象清理规则。
- 一条 Docker Compose 命令启动完整前后端。

### 2.2 非目标

- 多图融合、蒙版编辑、批量生成或自由画布。
- 视频、音频、模型训练、微调或质量评测。
- 智能 Provider 路由、自动故障转移和复杂熔断。
- 用户注册、认证、权限、配额、计费和公网部署。
- 分片上传、断点续传和客户端直传基础设施。
- 把参考素材与结果强行合并为一张通用素材表。
- 未引用 `READY` 素材的自动 TTL 删除、用户删除 API 和任务删除。
- 动态 Provider 管理后台、运行时路由和自动 Provider 切换。
- 微服务、Kubernetes、Kafka、Temporal 或新的业务数据库。
- 为展示效果预先建设 WebSocket、SSE 或完整可观测平台。
- 在不公开吞吐量、并发量或 P95 时，把 Locust 压测作为发布硬门。
- 外部 Provider exactly-once 承诺。

## 3. 关键约束与不可妥协项

1. PostgreSQL 继续作为任务、attempt、投递意图、素材元数据和权威结果的唯一业务事实源。
2. 图生图必须复用现有 outbox、Scheduler、lease、fencing、自动重试和结果提交链路，不建立旁路 Worker。
3. 参考图片只有完成服务端校验和私有对象写入后才能进入 `READY`，只有 `READY` 素材可以创建任务。
4. 文件扩展名、浏览器声明的媒体类型和 Provider 响应声明均不可信，必须校验文件头、尺寸和实际解码结果。
5. 文生图任务不得错误关联参考图片；图生图任务必须关联一张有效参考图片。
6. 幂等请求摘要必须包含生成类型、规范化参数和参考素材身份。
7. 手动重试复用原任务的规范化输入和参考素材；修改任一输入属于新建任务。
8. Provider 不支持图生图时必须在创建任务前返回能力错误，不能执行时静默降级为文生图。
9. Provider Adapter 不执行会绕过平台 attempt 语义的隐式创建重试。
10. 参考图片和结果对象保持私有，前端只持有稳定的 MuseFlow 访问路径。
11. 普通测试和 CI 不调用收费 Provider；每次真实请求都必须显式授权并限制为一张图片。
12. 未完成固定环境压测前，不对外声明吞吐量、并发量或 P95。
13. 除非阶段 0 证据明确触发版本化变更，继续使用 MVP 默认最多 3 个 attempt 和 10 分钟任务总截止时间；创建任务时保存策略快照。
14. 图生图 schema、上传和前端实现开始前，必须先通过真实 Provider 阶段 0；失败时停止实施并更换 Provider 或缩减产品承诺。
15. 结果对象键必须不可变。对象写入不受数据库事务保护，因此数据库只能发布候选对象指针，不能依赖覆盖固定键实现幂等。
16. lease 使用 execution token 条件 heartbeat；所有阶段仍受硬超时和任务总截止时间约束。
17. API 与 Worker 使用同一份代码定义的 Provider capability registry，并在任务上冻结 profile、模型、能力版本和策略版本。
18. Scheduler 核心循环只执行短数据库事务和消息发布，不直接执行 MinIO 网络 I/O。
19. 最终版本保持本机或受信网络边界；匿名直读 MinIO 必须失败，但 MuseFlow API 不提供用户级授权。

## 4. 架构演进

总体架构保持模块化单体和多进程角色，不增加新的常驻基础设施。

```text
React 前端
  ├── 上传参考图片 ───────────────┐
  └── 创建/查询任务               │
          │                        │
          ▼                        ▼
FastAPI HTTP API ─────────── 参考素材应用用例
          │                 ├── ImageInspector
          │                 └── BlobStore
          ▼
PostgreSQL：任务、reference_assets、result_assets、attempt、事件、outbox
          │
          ▼
独立 Scheduler ── Redis ── Celery Worker
                               │
                               ├── LeaseGuard / 读取已验证参考素材
                               ├── GenerationProvider
                               │     ├── MockProvider
                               │     └── DashScope Adapter
                               └── 写入不可变候选并发布权威结果

Scheduler ── maintenance outbox ── Celery maintenance queue
                                      ├── 恢复超时 STAGING
                                      └── 清理未引用候选对象
```

相对 MVP 的变化集中在三个位置：

1. 保留现有结果素材边界，新增独立参考素材模型；两者在代码层复用窄的图片和对象存储端口，而不是共享一张通用表。
2. 任务模块增加生成类型、参考素材约束和冻结的 Provider profile，但状态、attempt 与重试仍共同组成高内聚的任务生命周期。
3. `GenerationProvider` 接收短生命周期的 Provider 请求对象，不接收数据库 ID、ORM、MinIO URL 或 FastAPI DTO。
4. 结果从“覆盖确定性对象键”改为“写不可变候选，再由数据库 fencing 发布权威指针”。
5. Scheduler 只协调数据库状态和消息投递；所有对象存储维护 I/O 在隔离的 Celery maintenance queue 中执行。

## 5. 技术栈决策

### 5.1 继续使用

| 领域 | 技术 | 决策 |
| --- | --- | --- |
| Python 运行时 | Python 3.13、uv | 保持现有版本和锁文件 |
| HTTP API | FastAPI、Pydantic | 继续使用同步路由和显式 DTO |
| 数据访问 | SQLAlchemy 2.x、Alembic、psycopg | 通过迁移扩展现有模型 |
| 业务事实源 | PostgreSQL | 不引入第二业务数据库 |
| 可靠投递 | PostgreSQL outbox、独立 Scheduler | 继续负责投递、到期重试和 lease 回收 |
| 后台执行 | Celery、Redis | 继续只承担消息运输和 Worker 执行 |
| 对象存储 | MinIO | 参考图片和结果共用私有 bucket 与底层 BlobStore，但使用独立领域模型和前缀 |
| Provider HTTP | httpx | 继续显式管理超时、远端轮询和错误分类 |
| 前端 | React、TypeScript、Vite | 不切换 Next.js 或其他框架 |
| 前端数据 | React Router、TanStack Query、React Hook Form | 扩展现有创建、详情和历史流程 |
| 测试 | pytest、Vitest、Testing Library、Playwright | 增加图生图覆盖 |
| 本地交付 | Docker Compose、静态 Web 容器 | 多阶段构建前端，统一代理 `/api`，一条命令启动完整前后端 |

### 5.2 核心新增依赖

| 依赖 | 用途 | 引入阶段 |
| --- | --- | --- |
| `python-multipart` | FastAPI 接收 `multipart/form-data` 参考图片 | 图生图纵向切片 |
| Pillow | 识别格式、校验文件完整性、尺寸、色彩模式和像素上限 | 图生图纵向切片 |

Pillow 的解压炸弹警告应在上传校验中升级为错误。文件校验先限制传输字节数，再执行格式识别和完整解码，避免只读取图片头后接受损坏或恶意文件。

### 5.3 条件性依赖

`prometheus-client` 仅在需要展示运行中指标时引入。最终版本首先使用 PostgreSQL 中已有的任务和 attempt 时间字段生成可复查报告；只有需要实时 Counter、Gauge 或 Histogram 时才增加 `/metrics`，不默认加入 Prometheus Server 或 Grafana。

Locust 仅在决定公开吞吐量、并发量或 P95 时作为开发依赖引入，不属于核心依赖。

SSE、第二个真实 Provider、未引用 `READY` 素材的自动 TTL 和用户删除 API 均属于条件性扩展，不是图生图交付的组成部分。超时 `STAGING` 恢复、候选结果清理和显式素材清理命令属于核心可靠性范围。

## 6. 任务输入与执行输入

### 6.1 可持久化任务输入

新增稳定枚举：

- `TEXT_TO_IMAGE`
- `IMAGE_TO_IMAGE`

任务模块使用区分联合表达规范化输入：

```text
TextToImageInput
  prompt
  size_preset

ImageToImageInput
  prompt
  size_preset
  reference_asset_id
  reference_sha256
```

两种输入共用相同的任务状态、attempt、deadline、重试和结果语义。生成类型只决定输入约束和 Provider 能力，不创建第二套状态机。

### 6.2 短生命周期 Provider 输入

数据库输入不能直接传给 Provider。Worker 通过 `ReferenceAssetReader` 读取并再次核对内容后，构造：

```text
VerifiedReferenceImage
  content
  content_type
  width
  height
  sha256

ProviderGenerationRequest
  input: TextToImageProviderInput | ImageToImageProviderInput
  deadline_at
```

`ProviderGenerationRequest` 不包含数据库模型、FastAPI DTO、MinIO URL 或对象键。参考图片字节只在当前执行阶段短期存在。

### 6.3 领域规则与指纹

- 文生图不允许 `reference_asset_id` 或参考摘要。
- 图生图必须有且仅有一个 `reference_asset_id`，并保存创建时验证到的不可变 SHA-256。
- 创建任务在同一数据库事务中锁定 reference row，验证其仍为 `READY`，再插入任务和 outbox。
- 尺寸、文件和像素上限由 MuseFlow 在 Provider 阶段 0 后冻结；Provider profile 只能声明其支持的子集。
- 请求摘要包含生成类型、规范化提示词、尺寸、`reference_asset_id` 和参考素材 SHA-256。
- 同一任务幂等键和相同摘要返回原任务；同键不同摘要返回冲突。
- 手动重试复制原任务的完整规范化输入、Provider profile 和策略快照，不接受覆盖参数。
- 既有 `/api/v1` 请求未传生成类型时长期解释为 `TEXT_TO_IMAGE`；移除该默认值需要新的 API 主版本。

### 6.4 输入资源预算

阶段 0 后必须记录并验证传输字节、宽高、总像素、帧数、色彩模式、Base64 后请求体上限、单 Worker 峰值内存、Celery 并发和容器内存预算。限制同时存在于：

1. ASGI 或前置 Web 层的请求体与 multipart part 上限；
2. 应用流式读取时的 chunk 与累计字节上限；
3. Pillow 完整解码时的尺寸、像素、帧数和解压炸弹保护。

只在取得 `UploadFile` 后检查文件大小不能被描述为传输层限制。

阶段 0 已冻结 DashScope 图生图 profile：PNG/JPEG/WebP、单帧 RGB、无 alpha、原图≤6,000,000 bytes、每边 240–2,048 px、≤4,194,304 pixels、比例 1:4–4:1；Base64 JSON body 本地 guard 为 8,100,000 bytes。Celery 并发冻结为 1，单 attempt 预留 128 MiB、容器预算至少 1 GiB。Provider 未保证此 JSON body 上限，本次实测仅证明 978,936-byte 请求体可用；边界拒绝时必须 fail closed。依据与安全余量见[阶段 0 报告](museflow-provider-feasibility.md)。

## 7. 参考素材与对象存储边界

### 7.1 窄端口

参考素材和结果属于同一图片存储上下文，但不使用一个包办全部行为的胖接口。应用用例组合以下角色端口：

- `ImageInspector`：无业务 I/O 的格式、完整性、尺寸、像素、帧数、色彩模式和摘要检查；
- `BlobStore`：按对象键写入、读取、查询和删除私有对象；
- `ReferenceAssetReader`：读取 `READY` 参考素材并返回 `VerifiedReferenceImage`；
- `AssetAccessSigner`：为数据库确认的对象生成短期访问入口；
- `SafeArtifactFetcher`：由需要远端 URL 的 Provider Adapter 组合，用于逐跳网络和图片验证。

任务领域和 Provider 契约都不得看到 MinIO SDK、ORM、签名 URL 或对象键规则。跨 PostgreSQL 与 MinIO 的一致性由明确的应用用例编排。

### 7.2 幂等上传

最终版本采用服务端代理上传：

1. 前端为一次逻辑上传生成稳定 `Idempotency-Key`，并以 `multipart/form-data` 上传一张图片。
2. Web 层先应用请求体和 multipart part 上限；应用流式读取到临时文件并计算 SHA-256。
3. `ImageInspector` 完成真实格式、完整解码和资源限制检查。
4. 同一上传幂等键与相同图片摘要返回原素材；同键不同摘要返回 409。
5. 创建包含完整预期元数据的 `STAGING` 记录，再以不可变对象键写入私有 MinIO。
6. 对象确认后将素材推进为 `READY`，API 返回稳定的 `asset_id` 和 MuseFlow 预览路径。

该策略适合单张、小文件的本地作品集场景。具体上限在 Provider 阶段 0 后冻结，不在证据完成前把 10 MB 等暂定值写成最终产品契约。

### 7.3 校验规则

- 只接受产品白名单中的 PNG、JPEG 和 WebP。
- 文件头、Pillow 识别格式和最终媒体类型必须一致。
- 图片必须能完整解码，不接受截断文件。
- 宽高、总像素、帧数和色彩模式必须同时满足 MuseFlow 与冻结 Provider profile 的约束。
- Provider 不支持带 alpha 的输入时明确拒绝，不自动丢弃透明通道。
- 不保留 EXIF、原始文件名或其他非必要元数据作为业务事实。
- 保存实际宽高、字节数、SHA-256 和对象键。

### 7.4 生命周期与恢复

参考素材状态机：

```text
STAGING → READY
    └──→ FAILED

READY → DELETE_PENDING → DELETED
```

- 超过 1 小时的 `STAGING` 必须由维护任务恢复、标记失败或清理孤立对象。
- 被任务引用的 `READY` 素材随任务保留。
- 未引用且超过 24 小时的 `READY` 只进入 dry-run 报告和显式管理命令；核心版本不自动执行 TTL 删除。
- 显式删除在短事务中使用 `FOR UPDATE SKIP LOCKED` 领取候选，重新确认没有任务引用，再转为 `DELETE_PENDING`。
- 对象删除成功后进入 `DELETED`；失败时保留 `DELETE_PENDING` 并由维护任务重试。
- 任务外键使用 `ON DELETE RESTRICT`，创建任务只接受锁定后的 `READY` 素材。

MinIO 与 PostgreSQL 无法形成单个事务。方案承认短暂不一致，并通过可重入恢复流程收敛，而不是宣称跨系统原子提交。

### 7.5 维护任务隔离

Scheduler 只扫描数据库、领取维护意图并投递消息。MinIO 查询、恢复和删除由独立 Celery maintenance queue 执行，使用独立并发、批量、超时和错误隔离。MinIO 故障不能阻塞 outbox 发布、到期重试或 lease 回收，也不应改变 Scheduler 的核心 readiness。

## 8. 数据模型与迁移

### 8.1 `reference_assets`

新增独立表：

- `id`
- `idempotency_key`，唯一
- `request_fingerprint`，包含最终图片 SHA-256 与规范化媒体属性
- `status`：`STAGING`、`READY`、`FAILED`、`DELETE_PENDING`、`DELETED`
- `object_key`，唯一
- `content_type`
- `size_bytes`
- `width`、`height`
- `sha256`
- `created_at`、`ready_at`、`delete_pending_at`、`deleted_at`
- `error_code`、`error_message`

参考素材与结果素材共享值对象和底层端口，但生命周期不同，因此不迁移到一张通用 `assets` 表。

### 8.2 `result_assets`

保留现有 `result_assets` 及其 `task_id`、`attempt_id` 和唯一约束。新增：

- `width`、`height`，迁移时先允许既有行为空；
- 新写入路径必须始终提供实际宽高；
- 既有行由可重入管理命令从 MinIO 读取并回填，缺失或损坏对象生成报告；
- Alembic schema 升级不得因为 MinIO 不可访问而失败。

每个任务最多一份权威结果，每个结果必须保留产出它的逻辑 attempt。数据库只记录获胜候选，不新增 `result_candidates` 表。

候选对象键使用 attempt 范围内的内容寻址形式：

```text
results/{task_id}/{attempt_id}/candidates/{sha256}.{ext}
```

相同内容的重复写入安全，不同内容不会互相覆盖。维护任务按 task/attempt 前缀和宽限期列举对象，并与权威 `result_assets.object_key` 对比后清理未引用候选。

### 8.3 `generation_tasks`

新增：

- `generation_type`，既有行回填为 `TEXT_TO_IMAGE`，之后非空；
- `reference_asset_id`，可空且使用 `ON DELETE RESTRICT`；
- `reference_sha256`，图生图非空；
- `provider_profile`、`provider_name`、`model_name`、`capability_version`；
- 继续保存 `policy_version`，并保证其能够定位创建时的 attempts、deadline、退避和 lease 策略。

数据库 CHECK 约束类型与参考字段的空值组合；跨表 `READY`、摘要和删除状态由锁定事务保证。历史 cursor 排序字段保持不变。

### 8.4 迁移顺序与兼容性

1. 新建 `reference_assets`，为 `result_assets` 增加 nullable 宽高，为任务增加 nullable 新字段。
2. 将既有任务回填为 `TEXT_TO_IMAGE`，并回填当前默认 Provider profile、模型和能力版本。
3. 部署兼容读写代码；新前端显式发送生成类型。
4. 验证含真实 MVP 数据的数据库升级、旧任务查询和旧下载路径。
5. 对已完成回填且能在数据库内证明的字段增加非空和 CHECK；历史结果宽高保持兼容 nullable，由管理命令独立回填。

`/api/v1` 中未传生成类型的既有创建请求长期解释为文生图。迁移不得清空数据或要求 MinIO 在线，不提供自动 downgrade。

## 9. GenerationProvider seam

### 9.1 接口演进

`GenerationProvider` 继续作为真实 seam，生产 Adapter 和 `MockProvider` 都满足同一接口。调用方式保持统一：

```text
generate(provider_request, request_key, remote_request_id, on_remote_request_id, lease_guard)
    → GenerationResult
```

Provider 接口只接收 `ProviderGenerationRequest`。图生图请求包含 `VerifiedReferenceImage`，不接收 reference asset ID、MinIO URL、数据库模型或 FastAPI DTO。

能力目录由代码定义的不可变 Provider profile registry 提供，包含 `provider_name`、`model_name`、`capability_version`、支持的生成类型和输入限制。API 与 Worker 从同一包加载并在启动时校验。任务创建前用它拒绝不支持的能力；不要为未来未知模型提前建设动态管理后台或通用参数描述语言。

任务冻结 profile 标识而不保存凭据。Worker 必须按冻结 profile 执行；profile 不存在或配置不可用时返回稳定配置错误，不能自动切换 Provider。

### 9.2 DashScope Adapter

- 文生图继续使用已经验收的 `wan2.6-t2i` 协议。
- 图生图使用冻结 profile `dashscope-wan2.6-image-cn-beijing-edit`，模型 `wan2.6-image`，北京 Workspace endpoint，`enable_interleave=false`、单张 RGB 参考图、`n=1`、`size=1K`，输出 PNG 最大边 1,440 px。
- Worker 通过 `ReferenceAssetReader` 获得已验证图片，Adapter 将其编码为 Provider 支持的 Base64 data URL；不生成面向第三方的公开 MinIO URL。
- Adapter 负责请求转换、远端任务 ID、轮询和错误归一化。需要下载远端结果时，它组合注入的 `SafeArtifactFetcher`，返回经过精确 host、逐跳 redirect、DNS/IP 公网判定并绑定实际连接、大小、媒体类型、完整图片解码和尺寸验证的 bytes 与元数据。阶段 0 只观测到一个允许的结果 host，host 变化须 fail-closed 并重新核验。
- 创建请求不在 Adapter 内自动重发；平台 attempt 和现有 retry 策略继续拥有重试决定。
- Adapter 在提交前、轮询间隔和结果下载前检查 `LeaseGuard`。失去 ownership 后尽快停止本地工作；已发出的外部请求仍保留 exactly-once 降级语义。

真实图生图 Provider 阶段 0 已于 2026-09-24 完成一次单独授权的成功探针，结论为 `CONDITIONAL GO`。能力矩阵、脱敏证据和冻结限制见[阶段 0 报告](museflow-provider-feasibility.md)。可以按冻结契约继续实现；生产 SafeArtifactFetcher 的 DNS 连接绑定、单 attempt 单创建和 body 未验证上界等上线条件仍必须落实。

### 9.3 MockProvider

Mock 场景继续确定性运行，并为文生图和图生图区分结果摘要。图生图结果摘要至少纳入参考素材 SHA-256，使测试能够证明 Worker 使用了预期字节，而不是只传递了一个 ID。

现有成功、临时失败后成功、限流、超时和永久失败场景对两种生成类型均可运行，不复制两套故障实现。

MockProvider 还应记录创建调用次数和 remote request 恢复次数，使测试可以证明 lease 接管、轮询恢复和结果持久化不会错误创建新的逻辑 Provider attempt。

## 10. Worker 与可靠性链路

### 10.1 Attempt 与 execution claim

一个 attempt 表示一次逻辑 Provider 生成调用。每次 Worker 领取或接管产生新的 execution claim 和 execution token，但接管同一 attempt 时必须复用 provider request key 与已保存的 remote request ID。

只有应用策略决定再次创建远端生成请求时才创建下一 sequence。以下恢复留在原 attempt：

- 参考素材存储暂时不可用；
- 已有 remote request ID 后的轮询中断；
- 安全结果下载暂时失败；
- 候选对象写入或数据库权威发布暂时失败；
- lease 过期后的接管。

### 10.2 稳定执行阶段

Attempt 使用稳定 phase：

```text
INPUT_LOADING
  → PROVIDER_SUBMITTING
  → PROVIDER_RUNNING
  → RESULT_FETCHING
  → RESULT_PERSISTING
  → COMPLETED
```

`PROVIDER_SUBMITTING` 明确表达远端可能已经受理但本地尚未得到 ID 的窗口。内部 phase 可以映射为前端友好文案，但值本身属于诊断契约。

### 10.3 LeaseGuard

Worker 领取 attempt 后启动 `LeaseGuard`：

- heartbeat 使用独立 session 和短事务，仅在 execution token 仍匹配时延长 lease；
- heartbeat 间隔、lease 时长和各阶段超时在启动时验证，不能形成 lease 早于合法阶段截止的组合；
- Adapter 在提交前、轮询间隔和结果下载前检查 ownership；
- 失去 ownership 后不再推进数据库权威状态，也不覆盖任何对象；
- 进程崩溃时 heartbeat 自然停止，由 Scheduler 在 lease 到期后接管。

Provider 调用和对象存储 I/O 期间不得持有数据库行锁或长事务。

### 10.4 图生图执行与结果发布

1. 按现有规则领取或接管逻辑 attempt，获得 execution token 并启动 `LeaseGuard`。
2. 在短事务外通过 `ReferenceAssetReader` 读取 `READY` 参考素材，校验数据库摘要与对象内容一致。
3. 构造 `ProviderGenerationRequest`，提交或恢复冻结 profile 对应的远端请求。
4. Adapter 返回经过验证的结果 bytes、媒体类型、尺寸和 SHA-256。
5. 使用 `results/{task_id}/{attempt_id}/candidates/{sha256}.{ext}` 写入不可变候选对象。
6. 在短数据库事务中锁定 task 与 attempt，验证 execution token、lease 和非终态状态。
7. 若验证成功，插入唯一 `result_assets` 行并指向候选对象，同时写事件、完成 attempt 和任务；若验证失败，候选保持非权威，等待维护任务清理。

数据库唯一约束是“每个任务一份权威结果”的最终保护。旧 Worker 即使在失去 lease 后完成外部调用，也只能产生不同或相同的不可变候选，不能覆盖获胜对象。数据库 checksum 必须始终与其指向的对象一致。

### 10.5 错误与重试矩阵

| 错误码 | 责任域 | 自动行为 | 新 attempt | 手动重试 |
| --- | --- | --- | --- | --- |
| `REFERENCE_ASSET_NOT_READY` | 创建请求 | 同步拒绝 | 否 | 修改选择后新建 |
| `REFERENCE_ASSET_MISSING` | 平台完整性 | 终止自动执行 | 否 | 仅修复数据后的运维重放 |
| `REFERENCE_ASSET_CORRUPT` | 平台完整性 | 终止自动执行 | 否 | 仅修复数据后的运维重放 |
| `ASSET_STORE_UNAVAILABLE` | 基础设施 | 原 attempt 恢复 | 否 | 截止时间后允许 |
| `PROVIDER_CAPABILITY_UNSUPPORTED` | 创建请求 | 同步 4xx 拒绝 | 否 | 修改输入或 profile 后新建 |
| `PROVIDER_PROFILE_UNAVAILABLE` | 部署配置 | 创建时 503；执行时停止自动重试 | 否 | 修复配置后允许 |
| `PROVIDER_CONTENT_REJECTED` | Provider 永久错误 | 任务失败 | 否 | 不允许原输入 |
| `PROVIDER_UNAVAILABLE` | Provider 临时错误 | 需要重新提交时按策略退避 | 是 | 自动耗尽或 deadline 后允许 |
| `RESULT_INVALID` | Provider/安全边界 | 任务失败 | 否 | 默认不允许 |
| `RESULT_STORAGE_ERROR` | 基础设施 | 原 attempt 恢复 | 否 | 截止时间后允许 |

`can_retry` 由稳定错误码和任务状态的纯函数计算，前端不自行推断。未知异常不得默认伪装成 Provider 故障；必须先按发生阶段归入平台、存储或 Provider 责任域。

过期 Worker 可能已经向外部 Provider 发起请求。MuseFlow 承诺一个权威本地任务和一个权威结果，但不承诺外部调用或费用 exactly-once。

## 11. HTTP API 增量

保留 `/api/v1` 前缀和现有错误结构。

| 方法 | 路径 | 用途 |
| --- | --- | --- |
| `POST` | `/assets/references` | 使用上传幂等键上传并校验单张参考图片 |
| `GET` | `/assets/{asset_id}` | 查询参考素材元数据 |
| `GET` | `/assets/{asset_id}/download` | 获取稳定的受控预览或下载入口 |
| `POST` | `/tasks` | 增加生成类型和可选参考素材 ID |
| `GET` | `/tasks` | 增加生成类型筛选，保留稳定 cursor |
| `GET` | `/tasks/{task_id}` | 返回输入摘要、参考素材和重试链 |
| `POST` | `/tasks/{task_id}/retry` | 继续只接收幂等键，不允许覆盖原输入 |

上传接口要求 `Idempotency-Key`。首次成功返回 `201`，同键同文件重放返回 `200` 和 `idempotency_replayed: true`，同键不同文件返回 `409`。格式、大小或尺寸不满足时返回稳定 4xx；对象存储暂时不可用返回可诊断 503，不把 `STAGING` 素材返回给调用者。

任务创建时，静态能力不支持返回 `PROVIDER_CAPABILITY_UNSUPPORTED` 4xx；当前部署缺少冻结 profile 所需配置返回 `PROVIDER_PROFILE_UNAVAILABLE` 503。异步执行错误继续通过任务详情中的稳定错误码表达。

## 12. 前端增量

正式前端继续使用当前三个页面。

### 12.1 创建任务

- 选择文生图或图生图。
- 图生图显示文件选择、大小/格式提示和本地预览。
- 使用浏览器 `File` 和 `URL.createObjectURL` 实现提交前预览，并在替换或卸载时释放 URL。
- 先上传参考素材并获得 `asset_id`，再创建图生图任务。
- 上传成功但任务创建失败时保留素材 ID，允许用户重试创建而不重复上传。
- 文件选择或逻辑上传开始时生成稳定上传幂等键；网络状态未知时复用同一键和同一文件，不生成新键。

### 12.2 任务详情

- 展示生成类型、参考图片、规范化尺寸、attempt、时间线和重试链。
- 参考图片和结果都使用 MuseFlow 稳定路径，不缓存 MinIO 签名 URL。
- 继续只轮询非终态任务。

### 12.3 任务历史

- 增加生成类型筛选并与状态筛选组合。
- cursor 继续由后端提供，前端不自行推导分页位置。
- 缩略图仍通过受控下载入口加载。

OpenAPI 继续生成 TypeScript 类型；不得在前端手写与后端重复的生成类型枚举。

### 12.4 Compose 交付

前端使用多阶段镜像构建静态资源，由轻量静态 Web 容器提供并统一代理 `/api`。最终交付不使用 Vite dev server。Compose 所有公开端口继续绑定 `127.0.0.1`，README 只要求一条 Compose 启动命令即可进入完整 UI；本地前端开发命令保留为开发者可选路径，不属于评审者启动前置条件。

## 13. 保留与清理

最终核心版本采用以下规则：

- 被任务引用的参考素材与任务一起保留。
- 权威结果与成功任务一起保留。
- 超过 1 小时仍处于 `STAGING` 的素材自动进入恢复、失败或孤立对象清理流程。
- 未被任何任务引用且超过 24 小时的 `READY` 素材进入 dry-run 报告；只有显式管理命令会把它推进到 `DELETE_PENDING`。
- 非权威结果候选超过宽限期后，由维护任务在核对数据库权威指针后清理。
- 删除失败保留 `DELETE_PENDING` 并重试；对象确认删除后才进入 `DELETED`。

Scheduler 只产生维护意图，不执行 MinIO I/O，也不引入 Celery Beat。未引用 `READY` 素材的自动 TTL、用户删除 API 和任务删除属于后续产品阶段。

## 14. 测试策略

测试继续以模块接口为主要表面，避免穿透接口断言实现细节。

### 14.1 单元测试

- 文生图和图生图区分联合的合法与非法组合。
- 上传幂等、任务幂等以及任务摘要包含 reference asset ID 与摘要。
- 文件大小、格式、文件头、尺寸、透明通道、损坏图片和解压炸弹。
- Provider profile registry、能力检查、快照和参数转换。
- `MockProvider` 对参考素材内容的确定性使用。
- 手动重试复制完整输入，不允许覆盖。
- attempt phase、错误责任域、自动重试、新 attempt 资格和 `can_retry` 纯函数。
- 候选对象键由验证后的内容摘要生成。

### 14.2 PostgreSQL 与 MinIO 集成测试

- 既有 MVP 数据迁移后仍能查询和下载。
- 旧 `result_assets` 保留 attempt 关联；历史宽高为空时不阻断升级，并可由管理命令回填。
- 上传成功形成 `READY` 素材及私有对象。
- 中途失败产生的 `STAGING` 记录可以恢复或清理。
- 上传响应丢失后的同键重放返回原素材，同键不同内容冲突。
- 图生图任务不能引用无效、失败、`DELETE_PENDING` 或已删除素材。
- 创建任务与显式删除素材并发时，锁协议和 `ON DELETE RESTRICT` 保持引用完整性。
- 匿名直读 MinIO 中的参考素材和结果失败；无认证 MuseFlow API 在受信网络内可以生成短期入口。
- 对象内容与数据库 SHA-256 一致。

### 14.3 Scheduler、Redis 与 Worker 集成测试

- 图生图复用 outbox 投递、attempt、lease 和 fencing。
- 重复消息不会产生多个权威结果。
- Worker 在 Provider 调用前后中断时，任务可以恢复或进入可诊断终态。
- Redis 短暂离线不会丢失 PostgreSQL 中的投递意图。
- 新旧 Worker 返回不同结果字节并交错写入时，过期 Worker 不能覆盖接管者的权威对象，数据库 checksum 与获胜对象一致。
- lease 在候选写入与数据库提交之间过期时，旧 Worker 只能留下非权威候选。
- heartbeat 跨越原 lease 时维持 ownership；token 不匹配后停止续租和权威提交。
- 已有 remote request ID 的轮询恢复、素材存储暂时不可用和结果持久化恢复不会创建新 attempt。
- MinIO 故障或 maintenance task 失败不阻塞 outbox、retry 和 lease 回收。
- API 与 Worker capability registry 或 profile 配置不一致时启动失败或返回稳定配置错误，不静默切换。

### 14.4 端到端测试

Playwright 使用 `MockProvider` 覆盖：

1. 文生图原流程回归。
2. 上传参考图并完成图生图。
3. 无效文件、不支持图片和超过原始 multipart 限制的请求被明确拒绝。
4. 图生图临时失败后自动恢复。
5. 图生图永久失败和线性手动重试。
6. 历史按生成类型和状态筛选。
7. 详情展示参考图片、attempt、事件和结果。

### 14.5 真实 Provider 验收

普通测试、CI 和 Compose Demo 保持 `MockProvider`。真实图生图验收必须：

- 单独确认 API Key、地域、模型权限和计费上限。
- 固定单张输入、单张输出和受控参数。
- 禁止 Adapter 自动创建重试。
- 记录 Provider profile、model 和 capability version，并核对 Worker 使用冻结快照。
- 记录脱敏请求轨迹、结果媒体类型、尺寸、大小、SHA-256、MinIO 写入和访问控制结果。
- 不保存签名 URL、完整原始响应、密钥或账号标识。

## 15. 证据化与可观测性

### 15.1 事实记录

优先从现有 PostgreSQL 数据生成报告：

- 排队时间：`started_at - queued_at`。
- 端到端任务时间：`completed_at - queued_at`，包含排队、退避和恢复等待。
- attempt 数量和每次耗时。
- Provider 执行、结果获取和结果持久化阶段耗时。
- 按生成类型、终态和错误码分组的数量。
- 自动重试恢复率。

日志使用现有 Python logging，补齐稳定结构字段：`task_id`、`attempt_id`、`generation_type`、`provider_name`、脱敏远端请求 ID、阶段、错误码和耗时。不得把任务 ID 等高基数值作为 Prometheus label。

### 15.2 压测

最终核心版本不公开吞吐量、并发量或 P95，因此 Locust 不是发布门。只有 README、简历或演示决定公开性能数字时，才把 Locust 加入后端开发依赖并在固定环境中运行：

- 使用 `MockProvider`，隔离真实 Provider 波动和费用。
- 明确 Compose 资源、Worker 并发、用户数、增长速率、持续时间和数据规模。
- 分开测试创建、历史查询、详情轮询和混合负载。
- 若声明异步任务端到端 P95，场景必须轮询到终态并与同步 API 响应延迟分开报告。
- 保存 Locust 配置、原始结果、数据库版本和 Git commit。
- 通过重复运行确认结果稳定后，才能对外声明吞吐量或 P95。

真实 Provider 只记录少量受控端到端耗时，不纳入平台并发基准。失败任务、超时任务和恢复任务是否进入分位数及其分母必须在报告中明确。

### 15.3 条件性实时指标

若需要现场展示运行指标，可增加 `prometheus-client`：

- Counter：任务创建、终态、错误类别。
- Gauge：待投递任务和正在执行 attempt 数量。
- Histogram：排队时间和执行时间。

只有确认需要长期采集或图表后才把 Prometheus Server/Grafana 加入独立 Compose profile；最终核心版本不依赖它们启动。

## 16. CI 与交付验证

建议使用 GitHub Actions 执行：

1. 后端 Ruff、Pyright、compileall 和 pytest。
2. Alembic 从空数据库升级、`alembic check` 和既有数据迁移测试。
3. 前端 OpenAPI 生成差异检查、typecheck、lint、Vitest 和构建。
4. PostgreSQL、Redis 和 MinIO 集成测试。
5. 使用 `MockProvider` 的 Playwright 关键流程。
6. 新旧 Worker 交错、lease 过期、创建与清理并发、上传重放和含历史数据迁移的发布阻断测试。
7. `docker compose config`、API/Worker profile 一致性检查、后端和静态前端镜像构建。
8. 使用一条 Compose 命令启动完整前后端后的健康检查和最小 UI 验收。

真实 Provider 冒烟不在普通 CI 自动执行，使用显式人工触发和独立证据记录。Locust 只有在公开性能数字时执行。

交付维护一张验收追踪表，把最终产品文档的每项验收映射到自动化测试、受控真实 Provider 记录、Compose 演示或人工检查，并记录对应 commit。历史测试数字只能作为历史记录，不能代表当前工作树。

## 17. 实施阶段

每个阶段必须形成独立、已验证且可回滚的逻辑提交；不得把可靠性修复、迁移、上传、Provider 和前端压入一个大型提交。

### 阶段 0：真实图生图 Provider 门禁

- 核对官方地域、权限、模型、费用、输入限制、Base64 上限、异步协议、结果主机和幂等边界。
- 在逐次授权和费用上限内执行一次单参考图成功探针。
- 冻结本版本 MuseFlow 输入白名单、资源预算和 Provider profile。

验收：成功路径和能力矩阵有脱敏记录。阶段 0 已获 `CONDITIONAL GO`；继续实施须遵守 Provider profile 与报告列明的部署前置条件。若实际部署触碰未保证的协议边界或安全条件不成立，停止并重新选型或缩减承诺。

### 阶段 1：不可变结果与 fencing

- 把固定结果对象键改为内容寻址候选。
- 由数据库 execution token 事务发布唯一权威结果。
- 增加新旧 Worker 返回不同字节并交错写入的回归测试。

验收：旧 Worker 只能留下非权威候选，数据库 checksum 与获胜对象始终一致。

### 阶段 2：Lease heartbeat 与执行语义

- 实现 `LeaseGuard`、稳定 attempt phase 和错误责任域。
- 明确同 attempt 恢复与新 Provider attempt 的分界。
- 验证 heartbeat、阶段超时、任务 deadline 和 Worker 退出。

验收：跨越原 lease 的执行保持 ownership；token 失效后停止权威提交；已有远端请求恢复不重复创建。

### 阶段 3：兼容数据迁移

- 新建 `reference_assets`，扩展 `result_assets` 与 `generation_tasks`。
- 回填既有任务类型和 Provider profile。
- 保持旧 API 默认、历史 cursor 和下载路径。

验收：既有文生图测试全绿，含真实 MVP 数据的数据库升级通过，MinIO 离线不阻断 Alembic。

### 阶段 4：参考素材纵向切片

- 加入 `python-multipart` 和 Pillow。
- 实现上传幂等、三层大小限制、完整校验、MinIO 私有存储和稳定预览。
- 实现 `STAGING` 恢复、dry-run 报告、显式删除和隔离 maintenance queue。

验收：有效参考图形成 `READY`；恶意、损坏、超限文件被拒绝；上传重放、创建与删除并发、MinIO 故障隔离测试通过。

### 阶段 5：图生图领域与 Mock 链路

- 扩展任务创建、指纹、历史筛选和手动重试。
- 扩展 `GenerationProvider`、Provider registry 与 `MockProvider`。
- 让图生图完整复用加固后的执行链路。

验收：Mock 图生图成功及全部故障场景在真实 PostgreSQL、Redis、Worker、Scheduler 和 MinIO 链路中通过。

### 阶段 6：正式图生图 Adapter

- 按阶段 0 冻结的 profile 实现 `wan2.6-image` Adapter。
- 组合 `SafeArtifactFetcher`，禁止 Adapter 自动创建重试。
- 运行一次新的、单独授权的真实端到端验收。

验收：一条真实图生图任务完成输入摘要核对、安全结果获取、不可变候选、权威发布和短期访问，证据可复查且不泄露敏感信息。

### 阶段 7：前端与 Compose

- 实现生成类型选择、幂等上传、预览、详情和历史筛选。
- 重新生成 OpenAPI 类型并补齐 Vitest 与 Playwright。
- 使用多阶段前端镜像和静态 Web 容器加入 Compose。

验收：一条 Compose 命令启动完整 UI；正式前端完成文生图和图生图，MVP 流程无回归。

### 阶段 8：证据化收尾

- 完善结构化日志、资源预算、数据报告和演示脚本。
- 完成 README、架构图、验收追踪表和对外声明复核。
- 运行完整测试、构建、迁移和 Compose 验证。
- 仅在决定公开性能数字时执行 Locust 并保存原始证据。

验收：最终产品设计中的全部验收条目逐项存在当前 commit 对应的实现或验证证据。

## 18. 风险与取舍

| 风险 | 当前处理 | 后续触发条件 |
| --- | --- | --- |
| 恶意图片消耗内存或触发解码漏洞 | Web/multipart、流式读取和完整解码三层限制；依赖锁定 | 公网阶段再增加内容扫描和隔离进程 |
| 结果对象在数据库 fencing 前被旧 Worker 覆盖 | 内容寻址不可变候选，数据库 token 事务只发布获胜指针 | 对象规模显著增加时再评估候选 ledger |
| MinIO 写入与数据库提交不原子 | 参考素材状态机、结果候选、可重入维护任务 | 对象规模显著增加时再评估更强生命周期自动化 |
| Base64 增加内存和请求体积 | 阶段 0 冻结输入上限、单图、Worker 并发和容器内存预算 | 大文件或多图出现后评估 Provider 可访问的短期上传机制 |
| 图生图模型协议与文生图不同 | 保持统一外部接口，差异封装在 DashScope Adapter 内 | 第二 Provider 到来时用同一契约验证 seam |
| 迁移影响既有结果数据 | Alembic 保留行、默认回填、历史数据库升级测试 | 不允许用清空卷替代迁移修复 |
| 图生图 Provider 假设过早固化 | 在 schema 和 UI 前执行真实阶段 0 | 探针失败时停止并重新选型或缩减承诺 |
| lease 小于完整执行窗口 | token 条件 heartbeat、阶段超时和启动配置校验 | 更长任务出现后评估拆分提交与轮询 |
| 压测混入 Provider 波动 | 只有公开性能数字时才压测，且固定使用 MockProvider | 真实 Provider 只做受控单请求验收 |
| 指标系统扩大部署复杂度 | 首先生成数据库报告，Prometheus 保持可选 | 确有实时展示或长期采集需求时启用 |
| 自动清理误删有效素材 | 核心版不自动删除未引用 READY；显式删除使用锁、`DELETE_PENDING` 和引用复查 | 通过并发测试后再评估自动 TTL |
| maintenance I/O 阻塞核心 Scheduler | Scheduler 只投递，隔离 Celery queue 执行 MinIO I/O | 维护规模上升后独立调节队列和并发 |

## 19. 验收标准

- 干净环境可以按 README 使用一条 Compose 命令启动完整前后端。
- 既有文生图数据、`/api/v1` 默认语义、历史 cursor、下载路径和前端流程保持兼容。
- 正式前端可以完成文生图和单参考图条件生成。
- 参考图片经过三层大小限制、实际格式、文件头、完整解码、尺寸、像素、帧数和色彩模式校验。
- 上传幂等的首次、重放和冲突分支符合 201/200/409 契约。
- 参考图片和结果保存在私有 MinIO，匿名直读失败；README 明确 MuseFlow API 无用户级授权。
- 文生图和图生图复用同一 outbox、逻辑 attempt、lease heartbeat、fencing 和 retry 机制。
- 相同任务幂等请求不创建多个逻辑任务；重复消息和过期 Worker 不破坏唯一权威结果。
- 新旧 Worker 返回不同字节并交错时，数据库 checksum 与获胜候选对象一致。
- 创建任务与显式素材删除并发时不产生悬空引用。
- 手动重试复用原任务输入、参考素材、Provider profile 和策略快照，并形成线性重试链。
- `MockProvider` 可确定性复现两类任务的成功、主要故障和已有远端请求恢复。
- Redis 离线、MinIO 暂时不可用、Worker 中断和进程重启后，任务可在策略上限内恢复或进入可诊断终态。
- maintenance queue 故障不阻塞 outbox、retry 和 lease 回收。
- 至少一条真实图生图任务完成冻结 profile 核对、安全结果获取、不可变候选、权威发布和短期访问。
- 后端和前端相关测试、lint、类型检查、迁移检查、构建和 Compose 验证全部通过。
- 验收追踪表将最终产品标准逐项映射到当前 commit 的证据。
- 只有公开性能数字时才要求固定环境、固定场景、原始结果和对应 commit。

## 20. 当前进度与下一步

当前已完成：

- 可靠文生图 MVP 及其发布验收。
- PostgreSQL outbox、Scheduler、Redis、Celery、attempt、lease 和 fencing。
- 真实文生图 Provider、`MockProvider`、安全结果下载和私有 MinIO。
- 创建、详情、历史三个正式前端页面及 OpenAPI 类型生成。
- MVP 单元、集成、故障恢复和端到端测试。

阶段 0 已完成并得出 `CONDITIONAL GO`。下一步从阶段 1 开始实施不可变结果候选与 fencing、lease heartbeat；按实施阶段再进入数据库迁移、参考素材和 Provider Adapter。正式接入真实结果下载前必须先实现满足阶段 0 报告安全条件的 `SafeArtifactFetcher`，不得在 P0 可靠性缺口修复前扩展图生图任务。

产品与架构决策已经在 2026-09-24 的审查中收敛。每次真实图生图请求仍需要单独确认账号、地域、费用、请求参数和单次授权；文档结论本身不构成付费调用授权。

## 21. 验证记录

本版本为技术方案文档，不包含行为代码修改。交付时应完成：

- Markdown 格式和行尾空白检查。
- 本地文档链接存在性检查。
- Git diff 检查，确保不包含无关改动。

各实施阶段的代码、测试和真实 Provider 验收结果应在完成对应阶段后更新本节或相关专项记录，不预先声称通过。

## 22. 参考资料

- [FastAPI 文件上传](https://fastapi.tiangolo.com/tutorial/request-files/)
- [Pillow `Image.verify`](https://pillow.readthedocs.io/en/stable/reference/Image.html#PIL.Image.Image.verify)
- [Pillow 解压炸弹保护](https://pillow.readthedocs.io/en/stable/reference/Image.html#decompression-bombs)
- [DashScope Wan2.6 图像生成与编辑接口](https://help.aliyun.com/en/model-studio/wan-image-generation-api-reference)
- [Locust 无界面运行](https://docs.locust.io/en/stable/running-without-web-ui.html)
- [Prometheus Python Histogram](https://prometheus.github.io/client_python/instrumenting/histogram/)
- [GitHub Actions 容器化依赖](https://docs.github.com/en/actions/tutorials/use-containerized-services)
