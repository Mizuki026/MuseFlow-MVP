# MuseFlow 最终版本技术方案

- 版本：v0.1
- 状态：待实施
- 更新日期：2026-09-23
- 对应产品文档：[MuseFlow 最终产品设计文档](../product/museflow-product-design.md)
- 基线方案：[MuseFlow MVP 技术方案](./museflow-mvp-technical-design.md)

## 1. 目标与背景

MuseFlow 已完成可靠文生图 MVP。本方案定义从 MVP 演进到最终作品集版本的技术路径，核心增量是一个可信的图生图纵向切片，以及对功能、可靠性和性能声明的证据化收尾。

本阶段不重写 MVP，也不通过堆叠基础设施展示技术栈。现有 PostgreSQL outbox、独立 Scheduler、Redis、Celery Worker、attempt、lease、fencing、MinIO、Provider Adapter 和前端查询链路继续作为执行基线。新增能力必须复用这些可靠性机制。

本阶段属于阶段性产品收尾，而非长期商业平台建设。方案优先考虑：

- 图生图与文生图共享统一任务语义，同时保持输入能力约束清晰。
- 参考图片从上传、校验、私有存储到任务引用形成完整可信链路。
- Provider 差异停留在 Adapter 内部，不扩散到任务模块和前端。
- 自动化测试和受控真实验收能够证明产品声明。
- 只在数据证明必要时引入 SSE、实时指标平台或第二个真实 Provider。

## 2. 范围与非目标

### 2.1 本方案范围

- 文生图与图生图的统一任务输入模型。
- 单张参考图片的服务端上传、校验、私有存储、预览和任务关联。
- `GenerationProvider` 对文生图与图生图能力的显式表达。
- `MockProvider` 的图生图成功与故障场景。
- DashScope 图生图 Adapter 和一次受控真实端到端验收。
- 任务历史按生成类型和状态筛选。
- 手动重试对规范化输入和参考素材的复用。
- 前端创建、详情和历史页面的图生图增量。
- 图生图单元、集成、端到端和故障恢复测试。
- 固定场景下的可复现压测、指标报告和交付证据。
- 参考素材与结果的基础保留和孤立对象清理规则。

### 2.2 非目标

- 多图融合、蒙版编辑、批量生成或自由画布。
- 视频、音频、模型训练、微调或质量评测。
- 智能 Provider 路由、自动故障转移和复杂熔断。
- 用户注册、认证、权限、配额、计费和公网部署。
- 分片上传、断点续传和客户端直传基础设施。
- 微服务、Kubernetes、Kafka、Temporal 或新的业务数据库。
- 为展示效果预先建设 WebSocket、SSE 或完整可观测平台。
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

## 4. 架构演进

总体架构保持模块化单体和多进程角色，不增加新的常驻基础设施。

```text
React 前端
  ├── 上传参考图片 ───────────────┐
  └── 创建/查询任务               │
          │                        │
          ▼                        ▼
FastAPI HTTP API ─────────── 素材模块
          │                 ├── Pillow 校验
          │                 └── MinIO Adapter
          ▼
PostgreSQL：任务、素材、attempt、事件、outbox
          │
          ▼
独立 Scheduler ── Redis ── Celery Worker
                               │
                               ├── 读取参考素材
                               ├── GenerationProvider
                               │     ├── MockProvider
                               │     └── DashScope Adapter
                               └── 验证并保存结果
```

相对 MVP 的变化集中在三个位置：

1. 素材模块从“只保存结果”深化为“管理参考素材和结果”。
2. 任务模块增加生成类型与参考素材约束，但执行可靠性语义不变。
3. `GenerationProvider` 接口接收明确区分的文生图或图生图输入。

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
| 对象存储 | MinIO | 参考图片和结果共用私有 bucket 与素材模块 |
| Provider HTTP | httpx | 继续显式管理超时、远端轮询和错误分类 |
| 前端 | React、TypeScript、Vite | 不切换 Next.js 或其他框架 |
| 前端数据 | React Router、TanStack Query、React Hook Form | 扩展现有创建、详情和历史流程 |
| 测试 | pytest、Vitest、Testing Library、Playwright | 增加图生图覆盖 |
| 本地交付 | Docker Compose | 保持单机、受信网络运行模型 |

### 5.2 核心新增依赖

| 依赖 | 用途 | 引入阶段 |
| --- | --- | --- |
| `python-multipart` | FastAPI 接收 `multipart/form-data` 参考图片 | 图生图纵向切片 |
| Pillow | 识别格式、校验文件完整性、尺寸、色彩模式和像素上限 | 图生图纵向切片 |
| Locust | 固定用户数、速率和时长的可复现压测 | 证据化收尾 |

Pillow 的解压炸弹警告应在上传校验中升级为错误。文件校验先限制传输字节数，再执行格式识别和完整解码，避免只读取图片头后接受损坏或恶意文件。

### 5.3 条件性依赖

`prometheus-client` 仅在需要展示运行中指标时引入。最终版本首先使用 PostgreSQL 中已有的任务和 attempt 时间字段生成可复查报告；只有需要实时 Counter、Gauge 或 Histogram 时才增加 `/metrics`，不默认加入 Prometheus Server 或 Grafana。

SSE、第二个真实 Provider 和对象存储生命周期规则均属于条件性扩展，不是图生图交付的组成部分。

## 6. 任务输入模型

### 6.1 生成类型

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
```

两种输入共用相同的任务状态、attempt、deadline、重试和结果语义。生成类型只决定输入约束和 Provider 能力，不创建第二套状态机。

### 6.2 领域规则

- 文生图不允许 `reference_asset_id`。
- 图生图必须有且仅有一个 `reference_asset_id`。
- 被引用素材必须是 `REFERENCE`、`READY` 且未过期或删除。
- 尺寸白名单由 MuseFlow 定义，再由 Provider Adapter 转换为厂商参数。
- 请求摘要包含生成类型、规范化提示词、尺寸和参考素材 SHA-256。
- 同一幂等键和相同摘要返回原任务；同键不同摘要返回冲突。
- 手动重试复制原任务的完整规范化输入，不接受覆盖参数。

## 7. 素材模块

### 7.1 模块职责

现有结果存储实现应深化为素材模块。它通过小接口隐藏 MinIO、临时文件、校验和签名 URL 细节，负责：

- 接收并校验参考图片。
- 将参考图片写入私有对象存储。
- 为 Worker 读取已确认的参考图片。
- 保存经过验证的 Provider 结果。
- 为参考图片和结果生成受控访问地址。
- 删除孤立或超过保留期的对象。

不把 MinIO SDK、对象键规则或签名 URL 暴露给任务模块和 Provider 接口。

### 7.2 上传策略

最终版本采用服务端代理上传，而不是浏览器直传 MinIO：

1. 前端以 `multipart/form-data` 上传一张图片。
2. API 流式读取并执行硬性字节上限，计算 SHA-256。
3. Pillow 校验真实格式、完整性、尺寸、像素数、帧数和色彩模式。
4. 通过校验后写入私有 MinIO。
5. PostgreSQL 将素材推进为 `READY`。
6. API 返回稳定的 `asset_id` 和 MuseFlow 预览路径。

该策略适合单张、最多 10 MB 的本地作品集场景，可减少预签名上传、隔离区和完成回调的协议复杂度。若未来出现公网大文件上传，再独立评估“预签名隔离区上传 → 服务端确认”的两阶段方案。

### 7.3 校验规则

- 只接受产品白名单中的 PNG、JPEG 和 WebP。
- 传输大小在完整读取前后都必须受限。
- 文件头、Pillow 识别格式和最终媒体类型必须一致。
- 图片必须能完整解码，不接受截断文件。
- 宽高、总像素、帧数和色彩模式必须满足产品及 Provider 约束。
- 当前真实 Provider 不支持带 alpha 的输入时，明确拒绝，不自动丢弃透明通道。
- 不保留 EXIF、文件名或其他非必要元数据作为业务事实。
- 保存实际宽高、字节数、SHA-256 和对象键。

### 7.4 上传一致性

素材使用 `STAGING → READY` 状态：

- 创建 `STAGING` 记录后再写对象。
- 对象写入和元数据确认后推进为 `READY`。
- 写入失败时记录失败并尽力删除对象。
- Scheduler 定期查找超过阈值的 `STAGING` 记录，确认后清理孤立对象或标记失败。

MinIO 与 PostgreSQL 无法形成单个事务，因此方案承认短暂不一致，并通过可重入恢复流程收敛，而不是宣称跨系统原子提交。

## 8. 数据模型与迁移

### 8.1 `assets`

将现有结果素材模型迁移为通用 `assets` 模型，保留所有既有结果行：

- `id`
- `kind`：`REFERENCE` 或 `RESULT`
- `status`：至少包含 `STAGING`、`READY`、`FAILED`、`DELETED`
- `task_id`：结果素材关联任务；参考素材可为空并由任务反向引用
- `object_key`
- `content_type`
- `size_bytes`
- `width`、`height`
- `sha256`
- `created_at`、`ready_at`、`deleted_at`

保持对象键唯一。结果素材继续保证一个任务最多一份权威结果。

### 8.2 `generation_tasks`

新增：

- `generation_type`，既有行回填为 `TEXT_TO_IMAGE`，之后设为非空。
- `reference_asset_id`，可空外键。

数据库约束和任务模块共同保证类型与参考素材组合合法。历史 cursor 的排序字段保持不变，避免迁移后分页语义变化。

### 8.3 兼容性

- 既有 HTTP 创建请求不传生成类型时继续解释为文生图，直到前端和外部调用方全部迁移。
- 既有任务和结果下载路径保持有效。
- OpenAPI 类型更新后，正式前端在同一逻辑提交中同步生成类型。
- 迁移必须在含既有 MVP 数据的数据库副本上验证升级，不以清空数据代替兼容性检查。

## 9. GenerationProvider seam

### 9.1 接口演进

`GenerationProvider` 继续作为真实 seam，生产 Adapter 和 `MockProvider` 都满足同一接口。规范化请求改为文生图或图生图区分联合，但调用方式保持统一：

```text
generate(request, request_key, remote_request_id, on_remote_request_id)
    → GenerationResult
```

Provider 接口只接收规范化输入和已验证参考图片，不接收 MinIO URL、数据库模型或 FastAPI DTO。

新增能力查询应保持简单，例如返回支持的生成类型和输入限制。任务创建前用它拒绝不支持的能力；不要为未来未知模型提前建设通用参数描述语言。

### 9.2 DashScope Adapter

- 文生图继续使用已经验收的 `wan2.6-t2i` 协议。
- 图生图使用 `wan2.6-image`，`enable_interleave=false`、单张参考图、`n=1`。
- Worker 从私有 MinIO 读取参考图片，Adapter 将其编码为 Provider 支持的 Base64 data URL；不生成面向第三方的公开 MinIO URL。
- Adapter 继续负责请求转换、远端任务 ID、轮询、错误归一化和结果 URL 定位。
- 创建请求不在 Adapter 内自动重发；平台 attempt 和现有 retry 策略继续拥有重试决定。
- 结果继续经过现有精确主机、逐跳重定向、DNS、大小、媒体类型、文件头和尺寸校验后才能保存。

真实图生图调用需要单独的 Provider 可行性记录，确认地域、模型权限、单次费用、输入限制、轮询协议、结果主机和外部幂等边界。

### 9.3 MockProvider

Mock 场景继续确定性运行，并为文生图和图生图区分结果摘要。图生图结果摘要至少纳入参考素材 SHA-256，使测试能够证明 Worker 使用了预期素材，而不是只传递了一个 ID。

现有成功、临时失败后成功、限流、超时和永久失败场景对两种生成类型均可运行，不复制两套故障实现。

## 10. Worker 与可靠性链路

图生图 Worker 流程：

1. 按现有规则抢占任务并获得 execution token。
2. 在短事务外读取 `READY` 参考素材。
3. 校验素材数据库摘要与对象内容一致。
4. 构造规范化图生图请求并调用 Provider。
5. 安全下载和验证结果。
6. 保存结果对象与素材元数据。
7. 携带 execution token 提交成功；过期 Worker 的写入被 fencing 拒绝。

读取参考素材和调用 Provider 期间不得持有数据库行锁。素材缺失、摘要不一致或能力不支持属于永久错误；MinIO 临时不可用按现有错误策略分类为可恢复基础设施错误。

过期 Worker 可能已经向外部 Provider 发起请求，外部 exactly-once 降级语义与 MVP 保持一致。MuseFlow 只承诺一个权威任务和一个权威结果。

## 11. HTTP API 增量

保留 `/api/v1` 前缀和现有错误结构。

| 方法 | 路径 | 用途 |
| --- | --- | --- |
| `POST` | `/assets/references` | 上传并校验单张参考图片 |
| `GET` | `/assets/{asset_id}` | 查询参考素材元数据 |
| `GET` | `/assets/{asset_id}/download` | 获取稳定的受控预览或下载入口 |
| `POST` | `/tasks` | 增加生成类型和可选参考素材 ID |
| `GET` | `/tasks` | 增加生成类型筛选，保留稳定 cursor |
| `GET` | `/tasks/{task_id}` | 返回输入摘要、参考素材和重试链 |
| `POST` | `/tasks/{task_id}/retry` | 继续只接收幂等键，不允许覆盖原输入 |

上传接口成功后返回 `201`。格式、大小、尺寸或能力约束不满足时返回稳定的 4xx 错误码；对象存储暂时不可用返回可诊断的 503，不把未完成素材返回给调用者。

## 12. 前端增量

正式前端继续使用当前三个页面。

### 12.1 创建任务

- 选择文生图或图生图。
- 图生图显示文件选择、大小/格式提示和本地预览。
- 使用浏览器 `File` 和 `URL.createObjectURL` 实现提交前预览，并在替换或卸载时释放 URL。
- 先上传参考素材并获得 `asset_id`，再创建图生图任务。
- 上传成功但任务创建失败时保留素材 ID，允许用户重试创建而不重复上传。

### 12.2 任务详情

- 展示生成类型、参考图片、规范化尺寸、attempt、时间线和重试链。
- 参考图片和结果都使用 MuseFlow 稳定路径，不缓存 MinIO 签名 URL。
- 继续只轮询非终态任务。

### 12.3 任务历史

- 增加生成类型筛选并与状态筛选组合。
- cursor 继续由后端提供，前端不自行推导分页位置。
- 缩略图仍通过受控下载入口加载。

OpenAPI 继续生成 TypeScript 类型；不得在前端手写与后端重复的生成类型枚举。

## 13. 保留与清理

最终核心版本先采用明确、简单的规则：

- 被任务引用的参考素材与任务一起保留。
- 权威结果与成功任务一起保留。
- 超过阈值仍处于 `STAGING` 的素材进入恢复或清理流程。
- 未被任何任务引用且超过宽限期的 `READY` 参考素材可被清理。
- 数据库先记录删除意图或状态，再删除对象；失败可由 Scheduler 重试。

清理扫描复用现有 Scheduler，不引入 Celery Beat。只有确定保留期限和删除产品行为后，才启用自动清理；在此之前只提供 dry-run 报告和显式管理命令。

## 14. 测试策略

测试继续以模块接口为主要表面，避免穿透接口断言实现细节。

### 14.1 单元测试

- 文生图和图生图区分联合的合法与非法组合。
- 幂等摘要包含生成类型和参考素材摘要。
- 文件大小、格式、文件头、尺寸、透明通道、损坏图片和解压炸弹。
- Provider 能力检查和参数转换。
- `MockProvider` 对参考素材内容的确定性使用。
- 手动重试复制完整输入，不允许覆盖。

### 14.2 PostgreSQL 与 MinIO 集成测试

- 既有 MVP 数据迁移后仍能查询和下载。
- 上传成功形成 `READY` 素材及私有对象。
- 中途失败产生的 `STAGING` 记录可以恢复或清理。
- 图生图任务不能引用无效、失败或已删除素材。
- 引用素材和结果的匿名读取均失败，受控访问成功。
- 对象内容与数据库 SHA-256 一致。

### 14.3 Scheduler、Redis 与 Worker 集成测试

- 图生图复用 outbox 投递、attempt、lease 和 fencing。
- 重复消息不会产生多个权威结果。
- Worker 在 Provider 调用前后中断时，任务可以恢复或进入可诊断终态。
- Redis 短暂离线不会丢失 PostgreSQL 中的投递意图。
- 过期 Worker 不能覆盖接管者写入的图生图结果。

### 14.4 端到端测试

Playwright 使用 `MockProvider` 覆盖：

1. 文生图原流程回归。
2. 上传参考图并完成图生图。
3. 无效文件和不支持图片被明确拒绝。
4. 图生图临时失败后自动恢复。
5. 图生图永久失败和线性手动重试。
6. 历史按生成类型和状态筛选。
7. 详情展示参考图片、attempt、事件和结果。

### 14.5 真实 Provider 验收

普通测试、CI 和 Compose Demo 保持 `MockProvider`。真实图生图验收必须：

- 单独确认 API Key、地域、模型权限和计费上限。
- 固定单张输入、单张输出和受控参数。
- 禁止 Adapter 自动创建重试。
- 记录脱敏请求轨迹、结果媒体类型、尺寸、大小、SHA-256、MinIO 写入和访问控制结果。
- 不保存签名 URL、完整原始响应、密钥或账号标识。

## 15. 证据化与可观测性

### 15.1 事实记录

优先从现有 PostgreSQL 数据生成报告：

- 排队时间：`started_at - queued_at`。
- 执行总时间：`completed_at - started_at`。
- attempt 数量和每次耗时。
- 按生成类型、终态和错误码分组的数量。
- 自动重试恢复率。

日志使用现有 Python logging，补齐稳定结构字段：`task_id`、`attempt_id`、`generation_type`、`provider_name`、脱敏远端请求 ID、阶段、错误码和耗时。不得把任务 ID 等高基数值作为 Prometheus label。

### 15.2 压测

Locust 作为后端开发依赖，压测只在固定环境中运行：

- 使用 `MockProvider`，隔离真实 Provider 波动和费用。
- 明确 Compose 资源、Worker 并发、用户数、增长速率、持续时间和数据规模。
- 分开测试创建、历史查询、详情轮询和混合负载。
- 保存 Locust 配置、原始结果、数据库版本和 Git commit。
- 通过重复运行确认结果稳定后，才能对外声明吞吐量或 P95。

真实 Provider 只记录少量受控端到端耗时，不纳入平台并发基准。

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
6. `docker compose config` 和镜像构建。

真实 Provider 冒烟和 Locust 压测不在普通 CI 自动执行，使用显式人工触发和独立证据记录。

## 17. 实施阶段

### 阶段 1：输入模型与兼容迁移

- 增加生成类型和参考素材概念。
- 迁移通用素材表及任务外键。
- 保持既有创建、查询和下载行为兼容。

验收：既有文生图测试全绿，包含历史数据的迁移验证通过。

### 阶段 2：参考素材纵向切片

- 加入 `python-multipart` 和 Pillow。
- 实现上传、校验、MinIO 私有存储、稳定预览与恢复清理。
- 增加素材模块接口与集成测试。

验收：有效参考图形成 `READY` 素材；恶意、损坏或不兼容文件被拒绝；匿名读取失败。

### 阶段 3：图生图任务与 MockProvider

- 扩展任务创建、幂等摘要、历史筛选和手动重试。
- 扩展 `GenerationProvider` 与 `MockProvider`。
- 让图生图完整复用可靠执行链路。

验收：Mock 图生图成功和全部故障场景在真实 PostgreSQL、Redis、Worker、Scheduler 和 MinIO 链路中通过。

### 阶段 4：正式前端

- 实现生成类型选择、文件上传、预览、详情和历史筛选。
- 重新生成 OpenAPI 类型。
- 补齐 Vitest 和 Playwright。

验收：正式前端可完成文生图和图生图，MVP 文生图流程无回归。

### 阶段 5：真实图生图 Provider

- 完成 `wan2.6-image` Adapter 与可行性记录。
- 运行一次单独授权的真实端到端验收。
- 验证安全结果下载、私有 MinIO 和稳定下载入口。

验收：一条真实图生图任务成功，证据可复查且不泄露敏感信息。

### 阶段 6：证据化收尾

- 完善结构化日志和数据报告。
- 使用 Locust 执行固定场景压测。
- 完成 README、架构图、演示脚本和对外声明复核。
- 运行完整测试、构建、迁移和 Compose 验证。

验收：最终产品设计中的 13 项验收标准逐项存在实现或验证证据。

## 18. 风险与取舍

| 风险 | 当前处理 | 后续触发条件 |
| --- | --- | --- |
| 恶意图片消耗内存或触发解码漏洞 | 先限字节、Pillow 像素上限、完整解码、依赖锁定 | 公网阶段再增加内容扫描和隔离进程 |
| MinIO 写入与数据库提交不原子 | `STAGING` 状态、可重入恢复和孤立对象清理 | 对象规模显著增加时再评估更强生命周期自动化 |
| Base64 增加内存和请求体积 | 输入上限 10 MB、单图、Worker 内短期持有 | 大文件或多图出现后评估 Provider 可访问的短期上传机制 |
| 图生图模型协议与文生图不同 | 保持统一外部接口，差异封装在 DashScope Adapter 内 | 第二 Provider 到来时用同一契约验证 seam |
| 迁移影响既有结果数据 | Alembic 保留行、默认回填、历史数据库升级测试 | 不允许用清空卷替代迁移修复 |
| 压测混入 Provider 波动 | 平台压测固定使用 MockProvider | 真实 Provider 只做受控单请求验收 |
| 指标系统扩大部署复杂度 | 首先生成数据库报告，Prometheus 保持可选 | 确有实时展示或长期采集需求时启用 |
| 自动清理误删有效素材 | 首先 dry-run、引用检查、宽限期和删除状态 | 保留规则确认并通过恢复测试后启用 |

## 19. 验收标准

- 干净环境可以按 README 启动完整系统。
- 既有文生图数据、接口和前端流程保持兼容。
- 正式前端可以完成文生图和图生图。
- 参考图片经过服务端大小、格式、文件头、完整性、尺寸和像素限制校验。
- 参考图片和结果保存在私有 MinIO，匿名读取失败。
- 文生图和图生图复用同一 outbox、Scheduler、attempt、lease、fencing 和 retry 机制。
- 相同幂等请求不创建多个逻辑任务；重复消息和过期 Worker 不产生多个权威结果。
- 手动重试复用原任务的规范化输入和参考素材，并形成线性重试链。
- `MockProvider` 可确定性复现两类任务的成功和主要故障场景。
- Redis 离线、Worker 中断和进程重启后，图生图任务可恢复或进入可诊断终态。
- 至少一条真实图生图任务完成安全下载、结果校验、MinIO 写入和受控访问。
- 后端和前端相关测试、lint、类型检查、迁移检查、构建和 Compose 验证全部通过。
- 任何公开性能数字都有固定环境、固定场景、原始结果和对应 commit。

## 20. 当前进度与下一步

当前已完成：

- 可靠文生图 MVP 及其发布验收。
- PostgreSQL outbox、Scheduler、Redis、Celery、attempt、lease 和 fencing。
- 真实文生图 Provider、`MockProvider`、安全结果下载和私有 MinIO。
- 创建、详情、历史三个正式前端页面及 OpenAPI 类型生成。
- MVP 单元、集成、故障恢复和端到端测试。

下一步从阶段 1 开始：先定义生成类型、参考素材领域规则和兼容数据库迁移，再实现上传。Provider 和前端改动不应早于输入模型与素材生命周期稳定。

本方案实施前没有必须由用户补充的产品决策。真实图生图请求仍需要在阶段 5 单独确认账号、地域、费用和单次授权。

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
