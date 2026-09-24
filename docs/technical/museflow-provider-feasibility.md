# MuseFlow MVP 图片生成 Provider 可行性记录

> 当前判定（2026-09-19）：第 5 窗口已完成一次受控真实 Provider E2E；提交、轮询、安全下载、PNG 校验、SHA-256、私有 MinIO 写入和签名下载均成功。最新证据见第 17 节。默认演示仍使用 MockProvider，外部调用 exactly-once 不成立。

## 1. 调查范围与结论状态

第 12 至 16 节保留第 5 窗口修复前后的历史核查过程；其中 真实 E2E 尚未完成等结论不再代表当前状态。当前真实链路结论以第 17 节为准。

- **查证日期**：2026-09-18
- **目标**：验证首个真实图片生成 Provider 是否满足 MuseFlow MVP 的地区、账号、成本、单图固定尺寸、故障恢复、结果下载和窄适配器约束。
- **候选**：阿里云 Model Studio（百炼）Wan 2.6、Replicate 官方 FLUX.1 Schnell、OpenAI Images GPT Image 2.5 Flare。
- **证据边界**：关键结论只使用 Provider 官方 API 文档、官方定价、官方地区或账号说明和官方 API reference。
- **真实调用状态**：第 5 窗口唯一一次获授权的真实异步请求使用 `n=1`、`size=1280*1280`、`prompt_extend=false`，完成提交、轮询、安全结果下载、PNG 文件头与 `1280×1280` 尺寸校验、SHA-256、私有 MinIO 写入和签名下载；匿名读取与过期签名均被拒绝。默认 Worker 仍使用 MockProvider，本窗口不重复发起真实请求。
- **阶段结论**：阶段 0 的官方调查、脱敏 fixture 验证和一次受控真实 Provider E2E 均已完成；真实 Provider 不支持可依赖的外部幂等或稳定 ID 恢复，因此只能保证一个权威本地结果，必须保留外部重复调用与重复费用的降级承诺。

本文用以下标记区分结论性质：

- **已确认**：官方资料直接说明，或由当前工作区直接观察到。
- **推断**：由已确认的协议事实推导，尚未经过目标账号的真实调用验证。
- **待验证**：必须使用目标地区、目标账号或真实请求确认。

## 2. MuseFlow 的硬性选型条件

首个 Provider 必须满足以下约束：

1. 目标部署地区和目标账号能够实际访问并完成计费调用。
2. 支持单张文生图，并能锁定一个稳定尺寸。
3. 单次费用适合受控作品集演示。
4. 提供客户端幂等键，或允许客户端预先确定稳定请求标识并按该标识查询结果，才能承诺外部调用去重。
5. 如果请求标识只在提交成功后由远端返回，则它不能消除“远端已受理、本地尚未保存 ID”的故障窗口。
6. 提交、轮询、超时、429、可恢复 5xx、永久 4xx 和内容安全拒绝必须能映射到明确的本地错误类别。
7. 结果必须能在过期前安全下载；下载主机、重定向、大小和媒体类型必须受到限制。
8. 协议应能通过 `httpx` 直接实现窄而稳定的适配器，不依赖重量级 SDK。
9. 若没有候选满足第 4 项，产品承诺必须降级为“本地只有一个权威结果，但外部可能发生重复调用和重复费用”。

## 3. 候选对比矩阵

| 维度 | 阿里云 Model Studio Wan 2.6 | Replicate FLUX.1 Schnell | OpenAI Images GPT Image 2.5 Flare |
| --- | --- | --- | --- |
| 地区和账号 | **已确认**：提供中国（北京）工作空间端点，API Key、端点和模型按地域隔离；需要阿里云账号、工作空间、对应地域 API Key 和有效计费。**已确认（用户提供）**：目标为华北 2（北京，`cn-beijing`），空间和 `wan2.6-t2i` 计费权限已具备；当前基线的唯一真实调用已提交并进入轮询，但未完成结果下载。 | **已确认**：需要 Replicate 账号、API token 和预付或有效付款方式；官方条款受美国出口和制裁规则约束。官方未发布中国大陆可用性矩阵。**待验证**：中国大陆网络、注册、付款和模型访问。 | **已确认**：仅支持官方列表内的国家和地区；中国大陆不在列表中。免费层不支持该模型。**待验证**：目标账号所在支持地区、组织权限和计费层级。 |
| 单张和稳定尺寸 | **已确认**：`wan2.6-t2i` 支持 `n=1`；`size=1280*1280` 是明确尺寸，建议阶段 0 固定为此值。 | **已确认**：`num_outputs=1`；`aspect_ratio=1:1` 与 `megapixels=1`。**限制**：公开 schema 将像素数描述为近似值，未承诺一个精确的像素尺寸。 | **已确认**：`n=1`；Images API 支持 `1024x1024` 等明确尺寸；GPT Image 返回 base64。 |
| 预计单次费用 | **已确认**：中国（北京）`wan2.6-t2i` 为 **¥0.20/成功图片（Alibaba Cloud 英文定价页约 US$0.028671，以账号控制台为准）**；失败请求不收费。 | **已确认**：官方模型页在查证日显示 **US$3/1000 张，即 US$0.003/张**。公共模型失败运行不收费；取消某些官方模型可能收费。 | **已确认**：按文本和图像 token 计费，输出图像 token 为 US$30/百万；官方计算器当前示例显示图像输出约 **US$0.00588/张**，不含文本输入。**待验证**：固定参数请求的实际 usage 和总价。 |
| 调用模型 | 异步创建任务，返回服务端 `task_id`，再按 ID 轮询；任务和结果保留 24 小时。 | 默认异步创建 prediction，返回服务端 prediction ID 后轮询；也可用 `Prefer: wait=n` 等待 1–60 秒，超时后仍转为异步查询。 | 同步 `POST /images/generations`，响应中直接返回 base64；没有远端作业查询 API。 |
| 客户端幂等或稳定 ID 查询 | **未发现官方支持**。创建请求 schema 没有幂等键或客户端指定任务 ID；只能按提交后返回的 `task_id` 查询。 | **未发现官方支持**。官方 OpenAPI 创建请求只有模型输入、webhook、stream 等字段以及 `Prefer`、`Cancel-After` 头；只能按提交后返回的 prediction ID 查询。 | **未发现 Images API 支持**。`X-Client-Request-Id` 仅用于日志和排障；Images API 没有按该 ID 查询结果的契约。SDK 对 `Idempotency-Key` 的重试行为不能替代 Images API 的服务端幂等保证。 |
| 外部重复调用承诺 | **不成立** | **不成立** | **不成立** |
| 结果 | PNG 临时 URL，官方说明 24 小时有效；示例主机为 `dashscope-result-bj.oss-cn-beijing.aliyuncs.com`。 | URI 数组，官方文件域名为 `replicate.delivery` 及其子域；API 生成文件 1 小时后自动删除。 | base64 内联数据；没有远端下载 URL 和 CDN 主机。 |
| 错误资料完整度 | 官方错误码覆盖 400、429、500、503 和内容检查，较适合建立窄错误映射。 | 创建限流、prediction 状态和部分运行错误码有文档；内容安全拒绝的精确终态和 HTTP 映射仍需实测。 | HTTP 错误码、限流、5xx 和图像内容安全类别有官方说明；同步超时后的结果不可恢复。 |
| `httpx` 适配度 | **高**：标准 JSON/HTTPS，创建加轮询，协议窄；需安全下载结果 URL。 | **中高**：标准 JSON/HTTPS，创建加轮询；结果生命周期短、精确尺寸和失败分类需补探针。 | **高**：标准 JSON/HTTPS，同步加 base64；但中国大陆地区约束和超时不可恢复使其不适合作为当前默认。 |
| 本轮建议 | **条件式首选；优先做真实探针** | 低成本备选 | 仅在部署与账号均位于官方支持地区时作为备选 |

## 4. 候选一：阿里云 Model Studio Wan 2.6

### 4.1 地区、账号和费用

**已确认**：Model Studio 提供中国（北京）工作空间端点。官方地区文档说明不同地域的 API Key、端点和可用模型不能混用，数据也按地域处理。创建 API Key 需要阿里云账号或具备相应权限的 RAM 用户。

**已确认**：中国（北京）`wan2.6-t2i` 为 ¥0.20/成功图片（Alibaba Cloud 英文定价页约 US$0.028671，以账号控制台为准），并且图片生成按成功生成的图片计费，失败调用不收费。阶段 0 固定 `n=1`，因此一次成功探针的预期图片费用为 ¥0.20（约 US$0.028671，以账号控制台为准）。

**已确认（用户提供）**：运行地区为华北 2（北京，`cn-beijing`），北京地域百炼业务空间、`wan2.6-t2i` 计费权限以及 `DASHSCOPE_API_KEY`、`DASHSCOPE_API_HOST` 环境变量均已就绪。

**本窗口未完成**：唯一一次获授权的真实请求完成提交并进入轮询，但最终轮询状态为 `SUCCEEDED` 时本地适配器未找到结果 URL，按 `PROVIDER_RESULT_URL_MISSING` fail-closed；未下载、未写入 MinIO，未重试。由于当时未保留脱敏响应摘要，不能把该调用描述为成功。

官方来源：

- [Model Studio 地域和端点](https://www.alibabacloud.com/help/en/model-studio/regions)
- [创建与管理 API Key](https://www.alibabacloud.com/help/en/model-studio/get-api-key)
- [Model Studio 模型定价](https://www.alibabacloud.com/help/en/model-studio/model-pricing)

### 4.2 单图、尺寸和协议

**已确认**：Wan 2.6 文生图 API 支持 `n=1`，`size` 可以明确设置为 `1280*1280`。建议阶段 0 使用以下固定参数：

```json
{
  "model": "wan2.6-t2i",
  "input": {
    "messages": [
      {
        "role": "user",
        "content": [{ "text": "<prompt>" }]
      }
    ]
  },
  "parameters": {
    "size": "1280*1280",
    "n": 1,
    "prompt_extend": false
  }
}
```

关闭 `prompt_extend` 的理由属于**设计推断**：官方说明该开关会改写提示词并增加延迟；MuseFlow 首个适配器应尽量保持提交语义稳定。是否需要开启应在后续质量评估中单独决定，不属于阶段 0 的可达性验证。

**已确认**：异步创建使用 `POST .../api/v1/services/aigc/image-generation/generation`，请求头包含 `X-DashScope-Async: enable`。创建成功返回服务端生成的 `task_id`；随后使用 `GET .../api/v1/tasks/{task_id}` 查询状态。任务 ID 和任务结果保留 24 小时。

官方来源：

- [Wan 文生图 V2 API reference](https://www.alibabacloud.com/help/en/model-studio/text-to-image-v2-api-reference)
- [管理异步任务](https://www.alibabacloud.com/help/en/model-studio/manage-asynchronous-tasks)

### 4.3 幂等和恢复语义

**已确认**：官方创建请求的字段和请求头中没有客户端幂等键，也没有客户端预先指定 `task_id` 的字段。查询接口只接受创建成功后由服务端返回的 `task_id`。官方建议不要重复创建任务，而应使用既有 `task_id` 轮询；这一建议以客户端已经收到并保存 ID 为前提。

**推断**：如果 Provider 已受理并创建任务，但连接在响应到达前中断，MuseFlow 没有稳定客户端标识可以查询这次受理结果。重试创建可能产生第二个任务和第二笔费用。因此：

- `provider_request_key` 只能作为 MuseFlow 本地关联键，不能被描述为 Provider 端幂等键；
- 收到 `task_id` 后应尽快持久化，再开始轮询；
- 即使持久化动作足够快，仍无法消除“远端已受理、本地尚未保存 ID”的窗口；
- 产品必须采用外部重复调用降级承诺。

### 4.4 错误和重试映射

| 场景 | 官方行为 | MuseFlow 建议 |
| --- | --- | --- |
| 创建或轮询网络超时 | HTTP 客户端观察到超时；官方还定义图像处理 `RequestTimeout`。 | 若已有 `task_id`，只轮询，不重新创建。若创建响应未知且没有 ID，将本次标记为“受理状态未知”；允许受控重试，但记录可能重复计费。 |
| 429 `Throttling.RateQuota`、`Throttling.BurstRate` | 官方描述为速率或突发限流。 | 可恢复，指数退避并尊重服务端提示；计入总截止时间。 |
| 429 额度、计费或商品状态类错误 | 官方错误表还包含余额、额度、商品状态等 429。 | 需要账号干预，不应把所有 429 一律自动重试。 |
| 500 `InternalError`、`SystemError`、`ModelServiceFailed`，503 `ServiceUnavailable`、`ModelUnavailable` | 官方建议稍后重试。 | 可恢复；有 `task_id` 时优先轮询原任务，无 ID 时重试仍有重复调用风险。 |
| 400 参数、模型或权限错误 | 官方定义为参数、模型、鉴权或资源配置问题。 | 永久失败或配置失败；不自动重试同一参数。 |
| 400 `DataInspectionFailed` / `data_inspection_failed` | 官方说明输入或输出未通过内容检查。 | 内容安全拒绝；永久失败，不自动改写提示词或重试。 |

**待验证**：目标账号真实响应中的 HTTP 状态、错误码字段、`Retry-After` 是否存在、任务状态转换、最长耗时和任务失败时的完整响应结构。

官方来源：

- [Model Studio 错误码](https://www.alibabacloud.com/help/en/model-studio/error-code)
- [Model Studio 限流说明](https://www.alibabacloud.com/help/en/model-studio/rate-limit)
- [图像生成常见问题](https://www.alibabacloud.com/help/en/model-studio/image-generation-faq)

### 4.5 结果 URL 和安全下载

**已确认**：成功结果是 PNG URL，官方说明 URL 24 小时有效。北京地域示例使用 `dashscope-result-bj.oss-cn-beijing.aliyuncs.com`；其他地域示例会使用不同 OSS 主机。

**推断**：正式实现应在收到成功状态后立即下载并写入 MinIO，不把临时 URL 当作持久资源。下载器应：

- 用阶段 0 实测到的精确主机建立 allowlist；未完成实测前，不把整个 OSS 域名空间加入宽泛白名单；
- 默认禁止重定向，并在每次连接前解析和拒绝私网、环回、链路本地地址；
- 使用独立连接、首字节和总下载超时；流式计算字节数并在 20 MiB 上限前终止；
- 同时检查 HTTP `Content-Type` 和文件签名，只接收 PNG、JPEG 或 WebP；
- 日志只记录脱敏后的主机、路径摘要和 Provider request ID，不记录完整临时 URL 或查询参数。

**本窗口的非付费验证**：安全下载器使用脱敏 fixture 验证了结果主机白名单、PNG 校验和下载链路；Compose MinIO 验证了私有 bucket、签名下载和过期行为。唯一一次真实调用未到达下载阶段，因此没有真实 PNG、尺寸、checksum 或 MinIO 写入记录。

## 5. 候选二：Replicate 官方 FLUX.1 Schnell

### 5.1 地区、账号、费用和模型能力

**已确认**：Replicate API 需要 Bearer token。官方计费文档说明使用预付额度或有效付款方式；预付额度有有效期。官方条款禁止受制裁地区或受限制主体使用服务，但没有发布按国家列出的 API 可用性矩阵。

**待验证**：中国大陆环境能否稳定访问 API 和 `replicate.delivery`、目标用户能否完成注册与付款、目标账号是否可调用该官方模型。不能把“官方条款未明确排除中国大陆”推断为“当前地区一定可用”。

**已确认**：官方 FLUX.1 Schnell 模型页在查证日显示 US$3/1000 张，即 US$0.003/张。schema 支持 `num_outputs=1`、`aspect_ratio=1:1`、`megapixels=1`、WebP/PNG/JPEG 输出，安全检查默认开启。

**限制**：`megapixels=1` 是近似像素规模而非明确的 `1024x1024` 契约。若 MuseFlow 将“稳定尺寸”解释为精确像素尺寸，必须先实测固定输入的重复结果，或者选择 schema 明确接受宽高的其他官方模型。

官方来源：

- [FLUX.1 Schnell 官方 API schema](https://replicate.com/black-forest-labs/flux-schnell/api/schema)
- [Replicate 计费](https://replicate.com/docs/topics/billing)
- [预付额度](https://replicate.com/docs/topics/billing/prepaid-credit)
- [Replicate 服务条款](https://replicate.com/terms/)

### 5.2 提交、轮询和超时

**已确认**：默认创建 prediction 后立即返回服务端 prediction ID，客户端通过返回的 `urls.get` 或 GET prediction API 轮询。prediction 状态包括排队、处理中、成功、失败和取消等终态。

**已确认**：`Prefer: wait=n` 可让创建请求同步等待 1–60 秒；若在期限内未完成，响应仍返回可轮询的 prediction。`Cancel-After` 可以设置运行时限。它是取消远端执行的控制，不是 HTTP 客户端的连接超时。

官方来源：

- [创建 prediction](https://replicate.com/docs/topics/predictions/create-a-prediction)
- [Replicate HTTP API reference](https://replicate.com/docs/reference/http)
- [官方 OpenAPI schema](https://api.replicate.com/openapi.json)

### 5.3 幂等、错误和内容安全

**已确认**：官方 OpenAPI 的创建 prediction 契约没有客户端幂等键或客户端指定 prediction ID。ID 在创建后由服务端返回，查询依赖该 ID。因此它具有与 Wan 相同的受理未知窗口，外部去重承诺不成立。

**已确认**：官方限流文档列出创建 prediction 和其他 API 的默认速率，并说明超限返回 429 JSON 错误。信用额度较低或未绑定付款方式的账号可能受到更严格限制。

**已确认**：终态 prediction 通过 `status` 和 `error` 表达运行失败。官方运行错误页公开了部分错误码，例如未知瞬时错误、启动超时、显存不足和上传失败。

**已确认**：FLUX schema 默认启用安全检查，API 允许显式关闭。MuseFlow 不应关闭该检查。

**待验证**：安全拒绝时的精确 `status`、`error` 结构和是否收费；模型级 4xx、5xx 与 prediction 运行错误的稳定映射；服务端是否返回 `Retry-After`；取消和超时对计费的影响。由于官方文档没有给出完整、稳定的逐码映射，Replicate 适配器在真实探针前不能宣称已完成错误分类。

官方来源：

- [Prediction 限流](https://replicate.com/docs/topics/predictions/rate-limits)
- [运行错误码](https://replicate.com/docs/reference/error-codes)
- [安全检查](https://replicate.com/docs/topics/predictions/safety-checking)

### 5.4 结果和下载主机

**已确认**：图像输出是 URI 数组；API 生成的输出文件在 1 小时后自动删除。官方说明文件由 `replicate.delivery` 及其子域提供。

**推断**：MuseFlow 必须在成功后立即下载，并将 `replicate.delivery` 和实际观测到的子域纳入精确 allowlist。仍须应用禁止重定向、DNS/IP 校验、20 MiB 上限、流式下载、媒体类型和文件签名校验。完整 URL 不得进入日志。

**待验证**：真实响应主机、是否重定向、是否需要鉴权头、Content-Type、实际像素尺寸和安全拒绝是否产生输出 URL。

官方来源：

- [Prediction 输出文件](https://replicate.com/docs/topics/predictions/output-files)

## 6. 候选三：OpenAI Images GPT Image 2.5 Flare

### 6.1 地区、账号和费用

**已确认**：OpenAI API 只支持官方列表内的国家和地区。中国大陆不在该列表中；官方说明从不受支持地区访问可能导致账号被阻止或暂停。因此，如果 MuseFlow 的运行环境或账号实际位于中国大陆，该候选不应作为默认 Provider。

**已确认**：GPT Image 2.5 Flare 免费层不支持。模型按文本输入、图像输入和图像输出 token 计费；官方模型页列出图像输出为 US$30/百万 token。官方图像指南的当前计算器示例显示图像输出约 US$0.00588/张，不含文本输入费用。

**待验证**：目标账号是否位于支持地区、是否有该模型权限和有效计费层；固定 `1024x1024`、目标质量和实际提示词产生的 usage 与总费用。

官方来源：

- [OpenAI API 支持的国家和地区](https://help.openai.com/zh-hans-cn/articles/5347006)
- [GPT Image 2.5 Flare 模型页](https://developers.openai.com/api/docs/models/gpt-image-2.5-flare)
- [OpenAI 图像生成指南与费用计算器](https://developers.openai.com/api/docs/guides/image-generation)

### 6.2 调用、结果和恢复语义

**已确认**：Images API 是同步 `POST /images/generations`。可以请求 `n=1` 和明确的 `1024x1024`，GPT Image 模型的结果以 base64 数据返回。没有提交后可轮询的远端作业资源，也没有临时结果 URL。

**推断**：base64 结果免除了外部 CDN 下载和 URL 过期问题，但响应体可能较大。适配器必须限制响应体和解码后字节数，验证图片签名与尺寸，再写入对象存储。

**已确认**：OpenAI 支持客户端提供 `X-Client-Request-Id` 用于排障和日志关联；官方没有说明 Images API 会按它去重，也没有按它查询图像结果的 API。SDK 文档中“携带 `Idempotency-Key` 的请求可自动重试”描述的是客户端重试策略，不构成 Images API 的服务端幂等和结果恢复契约。

**推断**：如果服务端完成了生成但同步响应在客户端超时，MuseFlow 无法查询原结果。重发请求可能再次生成并计费。因此外部去重承诺仍不成立，而且 OpenAI 同步协议比可轮询协议更难在超时后恢复。

官方来源：

- [Images generate API reference](https://developers.openai.com/api/reference/cli/resources/images/methods/generate)
- [OpenAI API 请求 ID](https://platform.openai.com/docs/api-reference/authentication)

### 6.3 错误和内容安全

| 场景 | 官方行为 | MuseFlow 建议 |
| --- | --- | --- |
| HTTP/读取超时 | 客户端超时；没有远端图像作业可查询。 | 状态记为“结果未知”；重试需明确接受重复计费风险。 |
| 429 速率限制、`slow_down` | 官方列为限流。 | 可恢复，指数退避并尊重 `Retry-After`。 |
| 429 余额、支出上限或额度 | 官方错误说明包含计费和 quota 类原因。 | 账号配置失败，等待人工处理。 |
| 500、503 | 官方建议稍后重试，503 可带 `Retry-After`。 | 可恢复，但同步重试可能重复生成。 |
| 400、401、403 | 参数、鉴权、权限或不支持地区。 | 永久失败或配置失败；不重试同一请求。 |
| 内容安全拒绝 | 官方说明输入和输出均经过过滤，并提供输入、输出或未知阶段等 moderation 细节。 | 内容安全永久失败；保留脱敏分类，不记录原始敏感内容。 |

**待验证**：目标模型当前版本的完整 moderation 错误 JSON、实际 HTTP 超时范围、429 分支字段和大响应的传输行为。

官方来源：

- [OpenAI API 错误码](https://developers.openai.com/api/docs/guides/error-codes)
- [图像生成安全与 moderation](https://developers.openai.com/api/docs/guides/image-generation)

## 7. 推荐选型

### 7.1 条件式推荐

**推荐先验证阿里云 Model Studio 中国（北京）`wan2.6-t2i` 异步 API。** 该结论以 MuseFlow 计划从中国大陆运行、并能使用中国（北京）阿里云账号和计费为条件。

理由：

1. 官方明确提供中国（北京）地域和专属工作空间端点，地区结论比 Replicate 的公开资料更确定，也避开 OpenAI 对中国大陆的官方地区限制。
2. `n=1` 和 `1280*1280` 是明确的单图固定尺寸契约。
3. 一张成功图片 ¥0.20（Alibaba Cloud 英文定价页约 US$0.028671，以账号控制台为准），适合严格限定次数的作品集探针。
4. 创建、轮询、任务状态、错误码和 24 小时结果 URL 都有官方协议，可以用 `httpx` 实现窄适配器。
5. 异步 `task_id` 在本地成功保存后可以恢复轮询，优于完全同步且无查询资源的协议。

这个推荐不等于账号可用性已验证。若实际部署和账号位于 OpenAI 官方支持地区，OpenAI 的同步 base64 协议可作为更简单的备选；若 Replicate 在目标网络和账号上可用，FLUX.1 Schnell 的单张成本明显更低，但精确尺寸、地区访问和错误映射仍需实测。

### 7.2 外部调用去重承诺

三个候选都没有在其官方图像生成契约中提供可依赖的客户端幂等键，也没有允许客户端预先确定稳定请求 ID 并按该 ID 查询结果。它们的远端 ID 都只在提交后返回，OpenAI Images 甚至没有可查询的远端作业。

因此 MuseFlow **不得承诺外部调用 exactly-once 或严格去重**。应启用产品文档规定的降级承诺：

> MuseFlow 对用户只保留并展示一个权威本地结果；在“远端已受理、本地未保存远端 ID”或同步响应丢失的窗口内，恢复重试可能触发重复的外部调用和费用。

## 8. 对正式 `GenerationProvider` 的最小影响

阶段 0 不应为了三个不同协议提前建设通用框架。首个正式适配器只需表达真实 Wan 协议：

1. `submit`：接收规范化 prompt 和固定 `1280*1280`，创建任务并返回服务端 `task_id`。
2. `poll`：按 `task_id` 返回 pending、running、succeeded、content_rejected、permanent_failed 或 retryable_failed。
3. `result`：成功时返回临时结果 URL及媒体元数据，由独立安全下载器落入对象存储。
4. `provider_request_key`：继续作为本地 attempt 关联键，但接口和文档必须明确它不提供 Provider 端幂等。
5. 错误分类：保留 HTTP 状态、Provider 错误码和安全分类的脱敏摘要，统一映射为可恢复、永久、内容安全和账号配置错误。

**推断**：如果未来切换到 OpenAI，同步 base64 可以由另一个适配器在一次 `generate` 内直接返回字节；没有必要让首个 Wan 适配器假装同步，也不应让抽象接口隐藏异步任务 ID。

在正式实现中，收到远端 ID 后应立即进行短事务持久化。即便如此，这只缩短故障窗口，不改变外部去重承诺。

## 9. 最小真实探针方案

### 9.1 调用前需要用户一次性确认的信息（本轮已提供）

1. 实际运行地区是否为中国大陆，以及计划使用的阿里云账号和 Model Studio 工作空间是否位于中国（北京）。
2. 环境变量中是否已经配置该地域凭据和工作空间标识。凭据只能由本地环境提供，不得粘贴到对话、日志或文档。
3. 账号是否已开通 `wan2.6-t2i` 和有效计费。
4. 是否明确授权一次最多 ¥0.20（Alibaba Cloud 英文定价页约 US$0.028671，以账号控制台为准）的成功图片调用，以及是否授权为故障窗口验证增加一次同价调用。

### 9.2 推荐的首个付费请求

- Provider/模型：中国（北京）Model Studio `wan2.6-t2i`
- 参数：一个无争议提示词，`n=1`，`size=1280*1280`，`prompt_extend=false`
- 预计成功费用：¥0.20/成功图片（Alibaba Cloud 英文定价页约 US$0.028671，以账号控制台为准）
- 目的：验证目标账号的地区与权限、异步创建、`task_id` 持久化形态、状态轮询、成功耗时、结果主机、下载响应头、PNG 签名和实际尺寸。
- 停止条件：完成一张成功图片并下载验证后立即停止；若遇到鉴权、地区、模型权限、余额或计费错误，也立即停止，不扩大调用次数。

在执行前必须再次展示费用、请求和停止条件，并获得该次付费调用的明确确认。

### 9.3 免费或不应主动制造的验证

- 可以用缺少必填字段的请求验证 400 结构，但必须确保它不会排队生成任务。
- 不应通过高频请求主动制造 429，也不应试图制造 Provider 5xx；这些分支应先依据官方契约实现，再通过受控 mock 验证本地分类。
- 内容安全拒绝的精确响应仍需一次经用户同意的温和边界探针；不应使用违法或真实人物伤害内容。
- 网络超时探针可能在远端已受理后仍产生费用。只有用户明确授权额外一次潜在收费调用时才能执行。

## 10. 已验证、未验证与阶段门禁

### 10.1 已验证

- 三个候选的官方地区或账号要求、定价方式、单图能力、调用协议和结果形态。
- Wan 2.6 的中国（北京）端点、`n=1`、`1280*1280`、异步 `task_id`、24 小时任务/结果生命周期及主要错误码。
- Replicate 的 prediction 创建和轮询、限流、1 小时输出生命周期、`replicate.delivery` 主机范围和官方模型价格。
- OpenAI 的官方支持地区约束、同步 Images API、明确尺寸、base64 结果和 HTTP 错误类别。
- 三个候选都没有足以支撑 MuseFlow 外部调用去重承诺的官方契约。
- 用户确认运行地区为华北 2（北京，`cn-beijing`），北京地域百炼业务空间、`wan2.6-t2i` 计费权限和两项 DashScope 环境变量已就绪。
- 本窗口只纳入当前基线记录的真实调用事实：一次提交后进入轮询，最终因 `PROVIDER_RESULT_URL_MISSING` 停止；未重试、未下载、未写入 MinIO。
- 当前运行环境此前对 DashScope 公共图像生成端点执行过不带凭据、不会生成图片的 HEAD 连通性探测，返回 HTTP 401；这只证明 DNS、TLS 和 HTTP 可达，不证明工作空间、账号、模型或计费可用。
- 本窗口未保留原始或脱敏轮询响应正文；官方契约与新增 fixture 显示，Wan2.6 新异步协议的结果字段为 `output.choices[].message.content[].image`，适配器已兼容该结构，同时保留旧 `output.results[].url`，缺失结果仍 fail-closed。

### 10.2 尚未验证

- 429、5xx 和客户端超时没有被主动制造；其行为和重试边界依据官方错误/限流契约记录，正式适配器应使用受控模拟测试验证映射。
- 内容安全拒绝未被主动制造；官方错误码和永久失败边界已记录。
- 完整签名 URL 未进入日志，因此实际结果主机以摘要保存；正式适配器仍需使用精确 allowlist 和即时下载。


### 10.3 阶段 0 判定

**阶段 0 的官方候选调查和协议核对已完成，第 1 个实现窗口可以开始；真实 Provider 端到端成功不因文档样例而视为完成。**

已满足：官方候选调查、地区与费用核实、单图固定尺寸、异步提交、任务轮询和基于 fixture 的结果下载/媒体尺寸验证。三候选均不提供可依赖的外部幂等/稳定查询标识，因此产品外部重复调用降级承诺必须保留。真实链路仍需单独授权的成功冒烟测试。


## 11. 正式 Adapter 实现状态

**正式 Adapter 已实现**：`backend/src/museflow/providers.py` 提供 DashScope `wan2.6-t2i` 异步创建、任务轮询、状态归一化和安全结果下载；Worker 通过现有 `GenerationProvider` 接口选择 MockProvider 或显式配置的 DashScopeProvider。Adapter 不写 PostgreSQL、不修改任务状态、不调用 MinIO SDK，也不实现业务重试。

实现固定北京地域实测契约：`n=1`、`size=1280*1280`、`prompt_extend=false`。收到远端 `task_id` 后由执行层写入当前 attempt，轮询使用单调截止时间；429、5xx、网络错误和轮询超时交给应用层重试，Provider 创建请求不自动重发。已受理但响应丢失的外部调用仍可能重复计费，MuseFlow 只保留一个权威本地结果，不宣称 Provider exactly-once。

结果下载已集中在安全下载器：固定主机白名单、HTTPS、DNS 解析后的私网/环回/链路本地拒绝、重定向逐跳校验、连接/读取超时、流式 20 MiB 上限、Content-Type、PNG/JPEG/WEBP 文件头和 `1280×1280` 尺寸校验。验证后的字节继续通过现有确定性对象键和私有 MinIO ResultAssetStore 持久化。

日常 Mock/契约/集成测试不访问真实 Provider，也不产生费用。真实冒烟测试必须由操作者显式运行并重新授权一次最多约 ¥0.20 的单次请求；环境变量存在本身不会触发请求。

## 12. 第 5 个窗口结果 URL 核查（2026-09-19）

本窗口没有再次发起真实 Provider 请求。基于官方 Wan2.6 异步响应样例、脱敏 fixture 和现有失败事实，根因判断为适配器解析契约不完整，而不是将缺少 URL 当作成功：

- 官方当前 Wan2.6 异步协议的成功结果位于 `output.choices[].message.content[].image`；另一份官方 V2 文档仍展示 `output.results[].url`。适配器现已兼容两种已记录结构。
- 回归测试先复现了 `PROVIDER_RESULT_URL_MISSING`，再验证新协议字段能够进入安全下载器；旧协议字段和缺字段 fail-closed 语义均保留。
- 上一次唯一真实请求的脱敏响应正文没有保存，因而不能声称已用真实正文完成解析验证；它仍只能记录为提交并进入轮询后因缺少本地可解析结果 URL 而失败。
- 本窗口不降低“至少一次真实 Provider 端到端生成”的产品成功定义，也不把失败请求描述为成功。适配器修复后的真实链路仍需要一次新的、单独授权的冒烟请求；在获得授权前不得运行真实命令。

本窗口的实现与验证不改变应用层 retry、lease、execution fencing、MinIO 私有 bucket 或 exactly-once 降级语义。

## 13. 第 5 个窗口结果主机白名单复核（2026-09-19）

官方 Wan2.6 文档的北京成功响应示例使用精确主机 `dashscope-result-bj.oss-cn-beijing.aliyuncs.com`；该主机已经存在于 `DEFAULT_RESULT_HOSTS`，并通过安全下载器的 HTTPS、精确主机、逐跳重定向和 DNS 网络安全检查。官方文档没有授权将任意 OSS 域名或后缀匹配视为安全。

本次授权冒烟请求实际走到了结果 URL 下载入口，但在白名单检查处返回 `RESULT_INVALID: result host is not allowed`。工作区没有保留完整签名 URL、原始响应或可用于识别实际主机的脱敏值，因此无法证明实际返回主机是什么，也不能安全增加新的白名单成员。当前行为保持 fail-closed；该失败记录为“结果主机无法安全确认”，而不是 Provider 成功。

本轮没有再次发起真实 Provider 请求。适配器现在可在下一次受控请求前输出不含主机明文的精确白名单命中结果和主机摘要；这不会扩大白名单，也不会绕过 SSRF 或重定向校验。只有在获得脱敏的真实主机证据或新的明确授权并确认配置后，才可重新进行一次受控真实冒烟；在此之前，真实 Provider 不纳入 MVP 成功承诺，Mock Provider 仍是默认实现。
- **第 5 个窗口最新状态**：唯一一次真实请求已提交并轮询到 `SUCCEEDED`，但结果 URL 在安全下载器的精确主机白名单处被拒绝；未下载 PNG、未写入 MinIO，真实 Provider E2E 仍未成功。


## 14. 结果主机摘要离线核对（2026-09-19）

本轮仅核对已有脱敏摘要、Git 历史和阿里云官方精确域名，没有发起真实 Provider 请求。运行环境的 API Key 存在，API Host 形态属于北京请求端点；这两个布尔结论不能证明结果 URL 的主机。旧探针记录曾以相同摘要记载 PNG 下载 HTTP 200、1280×1280 和 1,828,507 字节，但没有保留明文主机或可复核的脱敏 fixture；该记录不能作为扩大生产下载白名单的依据。

按 `SecureResultDownloader.diagnose_url` 的算法，对 URL 主机小写后取 SHA-256 前 12 个十六进制字符。目标摘要为 `0bd1575e39cb`。[阿里云万相图片输出存储域名表](https://help.aliyun.com/zh/model-studio/text-to-image-api-reference)列出以下精确主机：

| 地域 | 精确结果主机 | 摘要 | 匹配 |
| --- | --- | --- | --- |
| 北京 | `dashscope-result-bj.oss-cn-beijing.aliyuncs.com` | `b5afac5a68d1` | 否 |
| 杭州 | `dashscope-result-hz.oss-cn-hangzhou.aliyuncs.com` | `e09fd63c7ae2` | 否 |
| 上海 | `dashscope-result-sh.oss-cn-shanghai.aliyuncs.com` | `a16310289cc1` | 否 |
| 乌兰察布 | `dashscope-result-wlcb.oss-cn-wulanchabu.aliyuncs.com` | `a941e736157c` | 否 |
| 张家口 | `dashscope-result-zjk.oss-cn-zhangjiakou.aliyuncs.com` | `c7032766eeb1` | 否 |
| 深圳 | `dashscope-result-sz.oss-cn-shenzhen.aliyuncs.com` | `3cea30557767` | 否 |
| 河源 | `dashscope-result-hy.oss-cn-heyuan.aliyuncs.com` | `dd6c2dc29087` | 否 |
| 成都 | `dashscope-result-cd.oss-cn-chengdu.aliyuncs.com` | `f848c86cb854` | 否 |
| 广州 | `dashscope-result-gz.oss-cn-guangzhou.aliyuncs.com` | `6de737f78828` | 否 |
| 乌兰察布 ACDR | `dashscope-result-wlcb-acdr-1.oss-cn-wulanchabu-acdr-1.aliyuncs.com` | `7e812c9e57d5` | 否 |

合法尾随点形式也逐个离线比对，均不匹配。[Wan2.6 官方响应样例](https://help.aliyun.com/en/model-studio/wan-image-generation-api-reference)中的北京和上海精确结果主机均包含在上表，也不匹配。当前证据无法确定实际结果主机的明文、官方归属或适用地域；白名单维持不变，真实 Provider E2E 仍未完成。若要解锁，需要提供不含签名参数的可信主机证据及其官方归属证明，或由厂商确认该摘要对应的精确结果主机；不得为猜测再次生成图片。

受控 smoke 现已具备单次 Provider 结果下载后的 MinIO 私有写入、签名下载 200、匿名 403 和过期 403 验收路径。下一次真实请求前必须先轮换已在截图中暴露片段的 API Key，并重新取得该次明确授权。

## 15. 第 5 个窗口结果主机摘要的官方来源复核（2026-09-19）

以下记录工单答复之前的证据边界；后续确认与修复见第 16 节。

对阿里云[万相 2.7 图生视频 API 参考](https://help.aliyun.com/zh/model-studio/image-to-video-general-api-reference)成功响应样例中的精确主机 `dashscope-a717.oss-accelerate.aliyuncs.com` 离线计算 `SHA-256(lowercase(urlparse.hostname))[:12]`，得到 `0bd1575e39cb`，与第 14 节保存的摘要相同。官方样例明确列出该 HTTPS 主机，但结果是 MP4 视频；12 位十六进制摘要也不是可逆、唯一的主机证明，因此这只能确定一个命中的官方候选，不能还原原始结果 URL 的主机。

**地域与 Wan2.6 边界。** 上述万相 2.7 文档将示例标为北京地域调用，并在其成功结果中展示该主机，故可确认它适用于官方的北京调用样例。阿里云[OSS 域名说明](https://help.aliyun.com/zh/oss/user-guide/access-oss-via-bucket-domain-name)把 `<bucket-name>.oss-accelerate.aliyuncs.com` 定义为传输加速 Bucket 域名；[传输加速说明](https://help.aliyun.com/zh/oss/user-guide/transfer-acceleration)说明请求会路由到目标 Bucket 所在地域。该后缀本身不证明 Bucket 的物理地域，也不证明仅限北京。阿里云[Wan2.6 文生图 V2 API 参考](https://help.aliyun.com/zh/model-studio/text-to-image-v2-api-reference)给出的北京 `wan2.6-t2i` 异步成功结果位于 `output.choices[].message.content[].image`，其示例主机为 `dashscope-result-bj.oss-cn-beijing.aliyuncs.com`；[Wan2.6 图像 API 参考](https://help.aliyun.com/zh/model-studio/wan-image-generation-api-reference)同样展示该主机，两页均未列出 `dashscope-a717.oss-accelerate.aliyuncs.com`。[Z-Image 图像 API 参考](https://help.aliyun.com/zh/model-studio/z-image-api-reference)将 `dashscope-a717` 列为可能变化的图像结果 Bucket，并给出加速域名格式，但它适用于另一模型，且明确不提供固定 OSS 白名单、建议向客户经理索取最新列表。原有 Wan2.6 失败记录的摘要与本候选吻合；仍缺少原响应的无签名主机证据或阿里云对北京 `wan2.6-t2i` PNG 结果的明确确认。白名单维持不变，真实 E2E 仍未成功。

**待发送的厂商支持问询（尚未提交）：**“我们在华北 2（北京）调用 Model Studio `wan2.6-t2i` 异步文生图，成功任务返回的结果 URL 主机经小写规范化后，SHA-256 前 12 位为 `0bd1575e39cb`。公开文档中的候选精确主机 `dashscope-a717.oss-accelerate.aliyuncs.com` 具有相同摘要，但示例属于 Wan2.7 视频。请确认该候选是否也是北京地域 `wan2.6-t2i` PNG 结果的官方支持域名、其存储 Bucket 的地域；若不是，请提供该摘要对应的精确主机，或告知通过历史任务 ID 私下核查的渠道。若存储主机会变化，请提供当前可核验的精确主机清单或正式获取渠道。”问询只含公开主机、摘要、地域和模型，不附 API Key、账号资料、完整或签名 URL。阿里云[百炼平台售后服务范围说明](https://help.aliyun.com/zh/model-studio/after-sales-service-scope)将模型服务技术问题和 API 故障诊断纳入官网/在线/标准工单支持；此处仅准备文本，未提交工单。

## 16. 第 5 个窗口：工单核对与精确主机修复（2026-09-19）

用户提供的阿里云售后工程师答复，按北京地域 `wan2.6-t2i` 审计记录中的请求标识核对到与受控 smoke 相同提示词的成功图片结果；该 PNG 结果 URL 的精确主机为 `dashscope-a717.oss-accelerate.aliyuncs.com`。按下载器算法计算，其摘要为 `0bd1575e39cb`，与此前失败诊断一致。这是针对该具体模型调用的厂商响应证据，不再仅是第 15 节基于摘要和其他模型文档提出的候选。工单原文含完整签名 URL；本仓库只记录裸主机和摘要，不保存路径、查询参数、请求标识、账号信息或原始响应，也未访问该链接。

这一证据足以将上述**单个精确主机**加入 `DEFAULT_RESULT_HOSTS`，同时保留北京公开样例主机。未知主机、伪造后缀和跳转目标仍被拒绝；DNS 私网/环回/异常、响应大小、Content-Type、文件头、尺寸与 SHA-256 校验均保持不变。工程师后续确认 `dashscope-a717` Bucket 实际存储于北京，该结论来自厂商答复，而非从加速域名推断。未来结果主机是否会变化仍未确认，因此不允许 OSS 后缀通配，也不宣称该主机是长期完整清单。若后续返回其他主机，继续 fail-closed 并逐个取证。

回归测试先在 `DashScopeProvider.generate()` 的合成成功响应上复现 `RESULT_INVALID: result host is not allowed`，再以精确白名单修复通过；测试 fixture 不含真实签名 URL。聚焦 Provider/安全测试为 29 passed。非付费全量 pytest 为 67 passed、0 skipped；运行期间临时停用共享 Scheduler 以隔离固定过去时间的 lease/fencing 测试，结束后已恢复。首次使用旧临时数据库 URL 的运行失败；修正为当前 Compose 数据库配置后，Scheduler 并发运行时有一次既有 fencing 竞态失败，隔离运行全绿。仓库全量 Ruff、Pyright、compileall、Alembic check、Compose config 与改动文件格式检查通过。

本轮没有发起真实 Provider 请求，也没有使用工单中的签名 URL。真实 Provider 仍未完成 PNG 安全下载、checksum、MinIO 写入和签名下载，故**不满足第 6 窗口前置条件**。此前截图暴露过 API Key 片段，建议轮换；若用户选择继续使用，仍需在下一次请求前明确确认计费并取得新的单次授权。Worker 默认继续使用 MockProvider。

## 17. 第 5 个窗口：单次真实 Provider E2E 验收（2026-09-19）

在新的明确授权下，对提交 `2636c4e57e8254347222338ec89fc013ab6b5b11` 执行**一次**受控冒烟：北京地域 `wan2.6-t2i`、`n=1`、`1280*1280`、`prompt_extend=false`。API Key 与 Host 仅核对存在性，Host 符合官方北京业务空间专属端点格式；Worker 保持 `MUSEFLOW_PROVIDER=mock`。Adapter 无创建请求自动重试；本次没有再次提交，也没有使用工单中的签名 URL。

提交成功（单独的提交 HTTP 状态码未保留）；轮询 HTTP 状态为 `200,200,200,200,200`，任务状态轨迹为 `RUNNING → SUCCEEDED`。结果 URL 主机命中精确白名单，主机摘要 `0bd1575e39cb`；安全下载返回 HTTP 200，Content-Type 为 `image/png`，PNG 文件头及 `1280×1280` 尺寸校验通过，实际大小 `2,004,922` 字节，SHA-256 为 `c8bedca14b2f5e80f97a027612bd57424c403ffd284c9594ccc8079ab933aa67`。图片写入私有 MinIO；签名下载 HTTP 200 且下载字节 checksum 相同，匿名读取 403，过期签名 403。命令退出码 0，未记录完整结果 URL、签名 URL、原始厂商响应、API Key、完整 API Host、业务空间 ID 或账号信息。

本次执行前非付费验证为 Provider/安全测试 `31 passed`、全量 pytest `67 passed, 0 skipped`（两条既有依赖弃用警告）、真实 Compose Mock→Redis→Worker→MinIO 测试 `3 passed`；仓库全量 Ruff、Pyright、compileall、Alembic check、Compose config 与 diff check 均通过。第 5 个窗口的真实 Provider E2E 前置条件已满足，可以在后续单独开始第 6 个窗口；这不等同于宣布整个 MVP 的全部发布验收条目已经完成。默认 MockProvider 与外部 exactly-once 降级承诺不变。

## 18. 最终版阶段 0：wan2.6-image 单参考图 Provider 门禁（2026-09-24）

### 18.1 当前状态

本节验证最终版候选 `wan2.6-image` 的单参考图编辑能力。第 17 节真实请求只调用了 `wan2.6-t2i`，不作为本节图生图成功证据。

**最终结论：`CONDITIONAL GO`。** 2026-09-24 获得本次单次授权后，隔离探针恰好发送一个创建 POST，HTTP 200；同一远端任务两次 HTTP 200 轮询，从 `RUNNING` 到 `SUCCEEDED`，结果由允许主机安全获取并经 Pillow 完整解码。单张参考图编辑能力、北京 Workspace 权限、异步任务和 PNG 结果链路已获真实证据支持。可按本节冻结契约开始后续 schema、上传、领域模型和窄 Adapter 设计；正式部署前仍须满足 18.6 的安全和费用条件。不得把单次探针外推为模型版本稳定、任意输入上限或外部 exactly-once 保证。

标记含义：**官方保证**指当前官方文档明确记载；**仓库事实**指代码/命令可复查；**冻结策略**指比 Provider 能力更窄的 MuseFlow 拒绝边界；**未保证/未覆盖**指官方未承诺或本次单个真实样本没有覆盖。本节引用的官方资料均于 **2026-09-24** 访问。

### 18.2 官方能力矩阵

| 项目 | 已查明事实 | 状态与阶段 0 含义 |
| --- | --- | --- |
| Provider / 模型 | Model Studio，模型 ID `wan2.6-image`；官方能力页列为支持文生图和编辑，最多 4 张输出。 | 模型标识存在；未找到不可变模型 revision 或别名稳定期限，服务端模型行为的版本稳定性**未保证**。本地 capability version 只能标记契约快照日期。 |
| 地域 / endpoint | 模型列于中国（北京）、新加坡、德国（法兰克福）、美国（弗吉尼亚）。北京异步创建：`POST https://{WorkspaceId}.cn-beijing.maas.aliyuncs.com/api/v1/services/aigc/image-generation/generation`，要求 `X-DashScope-Async: enable`。任务查询：同域 `GET /api/v1/tasks/{task_id}`。 | 地域、endpoint 形式和模型有官方资料。北京 API Key/Host 地域绑定，不能混用。本机只确认 `DASHSCOPE_API_HOST` 是北京 workspace 域名形态，未读取/打印 Workspace ID。 |
| 账号权限 | 需要 Model Studio API Key。Key 权限由 Workspace 决定，可配置模型访问范围、IP 白名单等；免费额度/额度耗尽策略会影响调用。 | 本次创建及查询成功，证明当前配置的北京 Workspace、Key 与本模型在本次请求时可用；不证明其他 Workspace/Key。 |
| 单参考图语义 | `enable_interleave=false` 明确为 image editing mode，支持基于输入图编辑、风格迁移与主体一致性；编辑模式要求 1–4 张输入图。 | 固定单张参考图真实请求完成。探针不评测语义质量，不承诺视觉相似度、主体保持质量或任意 prompt 的成功率。 |
| 输入格式 / bytes | JPEG/JPG、PNG、BMP、WebP；PNG alpha 不支持；Wan2.6 API 页面最大 10 MB。通用 Base64 图片传输指导建议原图小于 7 MB。 | 冻结子集 PNG/JPEG/WebP、RGB、最大 6,000,000 bytes；比通用建议值低约 14%，比 10 MB 输入限制留 40% 余量。真实样本为 733,914 bytes；Wan2.6 专属 Base64 JSON body 上限**未保证**，本地 8,100,000-byte guard 不等于 Provider 接受上限。 |
| 输入宽高 / pixels / ratio | 官方编辑指南：宽、高各 240–8,000 px；输入 aspect ratio 为 Unlimited；未说明 Wan2.6 总像素限制。 | 冻结每边 240–2,048 px、≤4,194,304 pixels、ratio 1:4–4:1。各边界较 Provider 最大边长低 74.4%，总像素是本地解码资源上限。 |
| 帧 / 色彩 / alpha | alpha 不支持；未找到 Wan2.6 对动画帧数、色彩模式、色域、ICC、位深的完整保证。 | 冻结只接受 Pillow 完整解码后的单帧 RGB、无 alpha；拒绝灰度、palette 和动画。ICC/色域输入不作为产品承诺，上传处理需规范化或去除元数据。 |
| Prompt | `messages` 为单轮 `user`；编辑 prompt 必填，最多 2,000 字符；negative prompt 最多 500 字符。 | 探针 prompt 固定 163 字符，不发送 negative prompt。探针本地拒绝超长输入，不依赖 Provider 截断。 |
| 参数 / 输出 | 编辑 `size=1K` 约 1280×1280 总像素且保持最后一张输入比例；`2K` 约 2048×2048。直接宽高方式总像素约 768²–2048²、比例 1:4–4:1，边长取 16 倍数。`n` 编辑模式 1–4、默认 4；文档建议测试设 1。`prompt_extend` 默认 true；可关闭。输出格式 PNG。 | 固定参数真实返回 1280×1280 PNG，符合模型信息页的 1440×1440 与编辑 API 的 2K 口径。本契约采用两份官方资料中的较严格 1440px 上限。 |
| 图片传递 / Base64 | 支持公网 HTTP(S) URL 或 `data:{MIME};base64,...`；通用说明称 Base64 会扩大 payload 并建议原图 <7 MB。未找到 Wan2.6 HTTP JSON body 硬上限。 | 探针采用本地字节的 Base64 Data URL；不传 MinIO/公网图 URL、不用 Provider 文件服务。当前脚本 body ceiling 8,100,000 bytes 是本地 guard，不是官方保证，也不因较小成功请求而证明边界。 |
| 异步响应 / 状态 | 创建响应示例 `output.task_id`、`task_status=PENDING`、顶层 `request_id`。查询状态包括 PENDING、RUNNING、SUCCEEDED、FAILED、CANCELED、UNKNOWN。成功示例结果 URL 位于 `output.choices[].message.content[].image`。 | 真实创建响应和两次任务查询均为已文档化结构，任务从 `RUNNING` 到 `SUCCEEDED`，恰好返回一个结果 URL。未知结构记为协议错误，不静默套用 T2I Adapter 逻辑。 |
| ID / 结果保留 | 创建返回服务端 task ID；状态和 URL 有效 24 小时，可据 ID 查询。结果样例是带签名参数的 OSS HTTPS URL，官方要求尽快下载。 | 实际创建取得 task ID 并在同一执行流程轮询成功；本地状态支持在 23 小时 TTL 内恢复。创建没有客户端指定 task ID 或文档定义的幂等键。提交响应丢失时不能按本地 request key 恢复。 |
| Result host / redirects | 官方北京样例 host `dashscope-result-bj.oss-cn-beijing.aliyuncs.com`，有效期 24 小时。没有固定主机清单、重定向或重定向目标保证。 | 成功响应 host 的脱敏 SHA-256 前 12 位为 `0bd1575e39cb`，与真实调用前两台允许主机之一 `dashscope-a717.oss-accelerate.aliyuncs.com` 的摘要匹配。原始 URL/host 未保留，因此这是基于 48-bit 脱敏摘要与封闭候选集的匹配记录，不是原始 URL 的完整证据。部署契约暂只冻结该一台 HTTPS host、拒绝所有 redirect；官方未保证后续 host 恒定，变化时必须停止并重新核验。 |
| 幂等 / 不确定创建 | 创建 schema 没有客户端幂等键和客户端 task ID；官方建议收到 ID 后轮询而不要重复创建。 | Provider 不具可依赖的原生幂等保障。服务已受理、响应未到客户端时，重复 POST 可能再次生成并计费。MuseFlow 不承诺外部 exactly-once。 |
| 错误 / 限流 | 官方错误表含 InvalidApiKey、Workspace/Endpoint.AccessDenied、429 限流和额度类、DataInspectionFailed、IPInfringementSuspect、500/503 服务类错误。北京 `wan2.6-image` 限流表列 5 RPS/5 并发；任务查询 API 默认 RPS 20。 | 可做初步分类，但官方没有承诺码表永不变化或所有错误具统一 retry 语义；429 不能全部当作短期限流。创建失败不自动重试。 |
| 费用 | 北京 `wan2.6-image` 标价 CNY 0.20/张；官方计费规则按成功生成图片数，失败调用不收费。 | 本次 `n=1` 的已知最大图像生成费用 CNY 0.20；优惠/免费额度/最终账单看账号控制台。状态未知后另发 POST 可能再有一笔最多 CNY 0.20 的生成费用。 |

官方资料（均 2026-09-24 访问）：[Wan2.6 图像生成与编辑 API 参考](https://help.aliyun.com/en/model-studio/wan-image-generation-api-reference)、[Wan 图像编辑指南](https://help.aliyun.com/en/model-studio/wan-image-edit)、[wan2.6-image 模型信息](https://help.aliyun.com/zh/model-studio/wan2-6-image)、[图像模型清单](https://help.aliyun.com/en/model-studio/image-model/)、[Model Studio 模型价格](https://help.aliyun.com/en/model-studio/model-pricing)、[地域与 endpoint](https://help.aliyun.com/en/model-studio/regions)、[API Key 和权限](https://help.aliyun.com/en/model-studio/get-api-key)、[异步任务查询](https://help.aliyun.com/en/model-studio/manage-asynchronous-tasks)、[错误码](https://help.aliyun.com/en/model-studio/error-code)、[限流](https://help.aliyun.com/en/model-studio/rate-limit)。

### 18.3 隔离探针与验证证据

探针位于 `.scratch/provider-feasibility/`，没有接入生产 Provider、DB、Celery、Scheduler、Worker、MinIO 或 UI。`--check-only` 不读取凭证也不访问网络。真实创建要求对话单次授权、`--authorize-real-request`、`--confirm-max-cost-cny 0.20`、`DASHSCOPE_API_KEY` 和北京 workspace 专属 `DASHSCOPE_API_HOST`。

固定图片由探针在内存中生成，不落盘：PNG、1024×1024、RGB、单帧、无 alpha、733,914 bytes，SHA-256 `0bc68705bb0e638b70c0fbc9e6c841c8388d9bbb4830c14ea0c24101395fa744`；Data URL 序列化 JSON body 978,936 bytes。固定 163 字符 prompt、单张输入、`wan2.6-image`、编辑模式、`n=1` 和 `size=1K`。没有自动创建重试；创建连接中断按“状态未知”结束。取得 task ID 后只轮询同一 task，并在本地临时文件保留 task ID 以便恢复；成功后删除。超时 connect/read/write/pool 分别 5/30/30/5 秒，轮询间隔 10 秒，总运行 600 秒，最多 60 次 poll。

真实调用时探针仅接受 HTTPS allowlist 中两台既有已知主机，拒绝 userinfo/非标准端口，检查公网 IP，禁止重定向，限 HTTP 200、`image/png`、≤20 MiB，Pillow `verify()` 后完整 `load()`；当次实际结果为单帧 1280×1280 PNG，且命中后来冻结的 1440px 界限。真实调用后，探针已把 host allowlist 收窄到唯一实测主机，并把输入、输出边长限制移到完整解码之前检查；当前版本只接受该单主机、≤1440px。URL、签名参数、图片和完整响应只在进程内短暂存在，不写日志/证据。脱敏证据只存稳定状态类别、HTTP 状态、计时、输入/输出 MIME/宽高/字节数/SHA-256 与 remote/task/host hash，不记录 Key、账号/Workspace ID、完整 Host、签名 URL 或完整响应。

调用前只核对两个凭证环境变量存在，Host 仅判定为北京 workspace endpoint 形态；未读/输出变量值。随后创建和轮询成功，已证明本次请求所用北京 workspace/key 有 `wan2.6-image` 权限。本阶段不提交图片文件。

隐私复核发现，第一次授权执行的 preflight 曾输出 workspace host SHA-256 前 12 位（没有输出原始 host 或账号标识）。该稳定摘要不应作为诊断字段保留；已从最终证据 JSON 中移除，并从探针当前 preflight/evidence 代码中删除。当前诊断只报告 `region=cn-beijing` 与 `workspace_endpoint=dedicated`。Provider 错误只在内存中映射为稳定类别，原始错误码不写证据或日志。

| 非收费命令 | 结果 |
| --- | --- |
| `uv run --directory backend --with Pillow pytest E:/MuseFlow/.scratch/provider-feasibility/test_wan26_i2i_probe.py` | 最终 `33 passed`；覆盖参数规范化、字节/像素/比例/模式/格式限制、JPEG/WebP 输入、动画拒绝、完整解码、Base64 body、错误分类、任务状态/ID、URL/host、媒体、endpoint、私网 IP、超时、ID 脱敏和恢复 TTL。 |
| `uv run --directory backend --with Pillow python E:/MuseFlow/.scratch/provider-feasibility/wan26_i2i_probe.py --check-only` | exit 0；生成、完整解码并摘要固定 RGB PNG；未发 HTTP 请求。 |
| `uv run --directory backend ruff check E:/MuseFlow/.scratch/provider-feasibility/wan26_i2i_probe.py E:/MuseFlow/.scratch/provider-feasibility/test_wan26_i2i_probe.py` | exit 0；All checks passed。 |
| `uv run --directory backend ruff format --check E:/MuseFlow/.scratch/provider-feasibility/wan26_i2i_probe.py E:/MuseFlow/.scratch/provider-feasibility/test_wan26_i2i_probe.py` | exit 0；两个文件已格式化。 |
| `uv run --directory backend python -m compileall -q E:/MuseFlow/.scratch/provider-feasibility/wan26_i2i_probe.py E:/MuseFlow/.scratch/provider-feasibility/test_wan26_i2i_probe.py` | exit 0。 |
| `uv run --directory backend python E:/MuseFlow/.scratch/provider-feasibility/wan26_i2i_probe.py` | exit 2；缺少 CLI 双重显式确认，明确输出 `No request sent`，未发 HTTP 请求。 |
| `git diff --check` | exit 0。 |
| 敏感模式扫描 | 最终证据未发现 Key、签名 URL 或真实 workspace host；扫描命中报告中的 `<workspace>` 掩码以及测试代码里的 `workspace` / `sensitive-workspace` 合成 fixture 域名。 |

真实调用获本轮单独授权后执行一次：创建 HTTP 200，`create_requests=1`；同一 task 两次查询均 HTTP 200，轨迹 `RUNNING → SUCCEEDED`；总耗时 13.077 秒。最终 PNG 为 RGB 单帧 1280×1280、2,700,108 bytes、SHA-256 `2921676e41abd5136c03c04726dfe59d55c5295f6b283289f8fba31d6b873253`，完成完整解码。结果主机哈希 `0bd1575e39cb` 与调用前两台 allowlist 候选中的 `dashscope-a717.oss-accelerate.aliyuncs.com` 前 12 位摘要匹配；由于原始 URL/host 未保存，按脱敏摘要证据记录，不声称完整 host 可从记录独立复原。脱敏记录为 `[i2i-probe.json](../../.scratch/provider-feasibility/evidence/i2i-probe.json)`；原图和结果图都未保存，成功后本地 task ID 状态文件已删除。实际只创建了一次，无自动重试。本次未读取账单控制台，实际扣款/免费额度状态未知。

### 18.4 架构适配性与风险

仓库当前 `[providers.py](../../backend/src/museflow/providers.py)` 的 `GenerationProvider.generate()` 接收短生命周期 Provider request、`request_key`、`remote_request_id` 和回调；当前 `GenerationRequest` 尚无参考图片，阶段 0 未修改接口。最终技术设计已有 `VerifiedReferenceImage`/`ProviderGenerationRequest` seam。Wan 协议可让窄 Adapter 负责 Data URL、创建、ID、轮询和稳定错误转换，结果下载通过独立安全 fetcher 注入；不需要 ORM、事务或 MinIO SDK，差异可留在 Adapter 内。

创建请求只能一次。已知 ID 后 GET 查询与官方 24 小时期限符合“同一 attempt 恢复轮询”；未来 Worker 必须先保存 ID，再 poll。当前 repo 的 `[assets.py](../../backend/src/museflow/assets.py)` `SecureResultDownloader` 有精确 host、HTTPS、逐跳 redirect 检查、DNS/IP 公网检查、大小、MIME、文件头和基本尺寸校验，但**没有 Pillow 完整解码，也未把 DNS 解析结果 pin 到实际 socket 连接**。因此当前代码不能被描述为已经满足全部 SafeArtifactFetcher 安全要求；正式 fetcher 必须补全图片完整解码，并评估 DNS 重绑定连接风险。本次探针会独立执行完整解码和 fail-closed 检查。

**创建响应不确定窗口**：Provider 接受 POST 后，如果连接在客户端收到 `task_id` 前断开，MuseFlow 没有远端 ID 或幂等键可查。按平台策略再提交会有重复调用/重复计费可能。`provider_request_key` 只是本地键。只能承诺一个权威本地 task/result；不得承诺外部 exactly-once。Adapter 不隐式 retry；是否由平台策略创建新 attempt，必须显式记录状态未知及外部费用风险。

冻结的错误分类初映射：

| DashScope 现象 | 探针类别 | MuseFlow 初步类别 / 行为 |
| --- | --- | --- |
| 缺凭证、401 InvalidApiKey、403 Workspace/Endpoint.AccessDenied | `configuration_or_permission` | Profile/凭证配置错误，永久停止，不自动 retry。 |
| 400 参数/图片格式/尺寸问题 | `invalid_input_or_parameters` | `PROVIDER_INVALID_REQUEST`，永久失败。 |
| DataInspectionFailed、IPInfringementSuspect | `content_safety_rejected` | `PROVIDER_CONTENT_REJECTED`，不改写输入、不 retry。 |
| 429 RateQuota/BurstRate | `rate_limited` | `PROVIDER_RATE_LIMITED`；平台退避可创建新 attempt，探针不重发 POST。 |
| 余额、free-tier only、allocation/account quota | `billing_or_account_quota` 或 `rate_or_account_quota_unclassified` | 账号阻断，不当作短暂限流自动重试；未分类 quota 保持阻断态。 |
| 500/503、ModelServiceFailed/ServiceUnavailable | `provider_service_error` | `PROVIDER_UNAVAILABLE` 候选；已有 task ID 则继续原 task 轮询。 |
| 创建 timeout/连接中断、成功响应无 task ID | `submission_state_unknown` | `PROVIDER_SUBMISSION_UNKNOWN` 候选；不得 Adapter 自动 POST 重试。 |
| JSON/状态/结果字段不符 | `*_protocol_error` | `PROVIDER_INVALID_RESPONSE` 或 `RESULT_INVALID`，未知响应 fail-closed。 |
| 未知 host、redirect、私网 IP、MIME/大小/PNG 解码错误 | `result_*_rejected` | `RESULT_INVALID`，不保存结果。 |

错误映射由官方码表和本地纯函数测试支撑；具体错误是否稳定及是否适合重试未获 Provider 保证。未知异常不默认归为可重试 Provider 故障。

### 18.5 冻结的 Provider profile、输入白名单与资源预算

下列值是 MuseFlow 本地契约，不宣称为 Provider 服务端上限。输入格式/边界取官方范围更严格的一侧并附安全余量；一张真实样本验证了调用链路，但没有验证所有输入/请求体边界。超出范围必须在调用前拒绝。每次模型 alias 或服务协议有实质变化时，先更新 capability version 并重新验证。

| 字段 | 冻结值 | 依据、实测与安全余量 |
| --- | --- | --- |
| `provider_name` / `provider_profile` | `dashscope` / `dashscope-wan2.6-image-cn-beijing-edit` | 本次北京 Workspace 真实创建、轮询及结果下载成功。 |
| `model_name` / `capability_version` | `wan2.6-image` / `official-doc-snapshot-2026-09-24` | 官方无 immutable revision 或 alias 稳定期限保证；上线前若服务目录/行为变化，重新核验，不声称模型输出可复现。 |
| 地域 / endpoint | `cn-beijing` / Workspace 专属 `https://{WorkspaceId}.cn-beijing.maas.aliyuncs.com`，HTTPS 443 | 实际创建 HTTP 200；凭证 Key 与地域按 workspace 配置。不得跨地域重用该 profile。 |
| 输入格式 / 色彩 / 帧 / alpha | `image/png`、`image/jpeg`、`image/webp`；单帧 `RGB`；无 alpha；其他模式拒绝 | 是 Provider 格式集合的子集；PNG alpha 官方不支持。Pillow 完整解码；在解码前检查维度；上传边界规范化并剥离不支持/不需要的 ICC、EXIF 元数据。 |
| 输入字节 | ≤6,000,000 bytes | Provider API 10 MB 和通用 Base64 指导原图 <7 MB；本地值较 7,000,000 bytes 低 14.3%、较 10,000,000 bytes 低 40%。实测输入 733,914 bytes，未实测上边界。 |
| 输入宽高 / pixels / ratio | 每边 240–2,048 px；总像素≤4,194,304；宽高比 1:4–4:1 | 官方边长 240–8,000 且 ratio Unlimited；每边本地上限低 74.4%，总像素限制管理解码内存。拒绝极端长图。 |
| Base64 JSON 请求体 | ≤8,100,000 bytes | 由 6,000,000 bytes 原图编码约 8,000,000 bytes，加 prompt/JSON 余量形成。Wan2.6 body 硬上限未保证；本次成功实测 978,936 bytes，故本地限制是保守 guard，不能声称 Provider 已接受到该上限。若在此上限内遇请求体拒绝，阻断输入并重新评估。 |
| Prompt / 模式 | prompt 1–2,000 字符；单轮 `user`；`enable_interleave=false` | 官方编辑限制；本次 163 字符，明确为 image editing mode。拒绝空白和超长 prompt。 |
| 输出数 / 参数 / 格式 | `n=1`、`size=1K`、`prompt_extend=false`、`watermark=false`、PNG、最大边≤1,440 px | 实测 1280×1280、RGB、单帧 PNG。采用官方模型清单 1440×1440 与编辑 API 2K≈2048 的较严格值；输出仍比上限留 160px 余量。超出时拒绝并重核口径。 |
| Provider 响应体 | ≤1,048,576 bytes | 本地防止无界响应；未发现官方响应体上限。响应需要完整 JSON 且只取已文档化字段。 |
| Result bytes / media | `image/png`；≤20 MiB；完整 Pillow `verify()` + `load()`；宽高≤1,440；单帧 | 实测 2,700,108 bytes，即约 7.8 倍字节余量；拒绝其他 MIME、超长和解码失败。结果主机只允许实测命中的精确 host，重定向 0。 |
| Result host | `dashscope-a717.oss-accelerate.aliyuncs.com`，HTTPS 443；DNS 必须只解析为公网 IP；redirect=0 | 实测脱敏 host digest 与此 host 匹配。Provider 未保证 host 集合稳定；Host 变化 fail-closed、重新验证后再改 allowlist。生产 fetcher 还须把 DNS/IP 验证绑定至实际连接。 |
| 创建 / 轮询 / 总超时 | connect 5s、read 30s、write 30s、pool 5s；poll 10s；总 600s、最多60次 GET | 10s 间隔、10分钟总限时；本次 2 次 poll、13.077s 完成。task ID 本地恢复状态 TTL 23h，比官方 24h 保留期短 1h。创建只尝试一次。 |
| 每个在途 attempt 内存预算 | 预留 128 MiB | 静态高位估算：6 MB 输入、Base64 与 JSON 副本约 24 MB、20 MiB 下载缓冲及拼接约40 MB、输入/输出各约16 MiB 解码工作区，再留约25%余量。此为容量预算，不是实测峰值 RSS。 |
| Worker / 容器预算 | Celery 并发 1；容器内存至少 1 GiB | 至少为128 MiB单 attempt 预算的8倍并容纳运行时/队列开销；上线前通过实际部署监控调整，不在阶段0启动并发扩容。 |

### 18.6 真实调用记录、风险与最终门禁

| 证据项 | 结果 |
| --- | --- |
| 调用授权 / 创建 | 用户在本窗口单独授权最高 ¥0.20；只提交 1 次 POST，HTTP 200，无自动创建重试。 |
| 任务轮询 | 同一 remote task 查询 2 次，均 HTTP 200；`RUNNING → SUCCEEDED`。总耗时 13.077 秒。 |
| 输入 | 探针内存生成 PNG RGB 单帧、无 alpha、1024×1024、733,914 bytes；SHA-256 `0bc68705bb0e638b70c0fbc9e6c841c8388d9bbb4830c14ea0c24101395fa744`。 |
| 输出获取 / 解码 | HTTP 200、`image/png`；Pillow `verify()` 与完整 `load()` 通过；RGB 单帧 1280×1280、2,700,108 bytes；SHA-256 `2921676e41abd5136c03c04726dfe59d55c5295f6b283289f8fba31d6b873253`。 |
| Result host | host SHA-256 前 12 位 `0bd1575e39cb`；与当次 allowlist 的唯一匹配候选 `dashscope-a717.oss-accelerate.aliyuncs.com` 相同。原始 URL 不保存；下载期间未发生重定向。 |
| 脱敏 Provider IDs | request ID digest `cfd766447235`；remote task ID digest `2397992d058f`。不保存 workspace 派生标识。 |
| 费用 | 官方单张标价上限 CNY 0.20；未读取账单控制台，故不声称实际扣款金额。 |
| 记录文件 | `[脱敏探针证据](../../.scratch/provider-feasibility/evidence/i2i-probe.json)`。只保存状态、HTTP、时长、图像摘要及脱敏 ID/hash；不保存 Key、workspace、完整 host、签名 URL、远端原响应或图片。成功后 task 状态文件已删除。 |

**结论：`CONDITIONAL GO`。** 真实成功足以允许按 18.5 的 `wan2.6-image` 北京 profile 开始后续 schema、上传、领域模型和窄 Adapter 实施；产品承诺限定为白名单内的一张 RGB 单参考图输入，返回至多一张符合冻结限制的 PNG 编辑结果。不承诺视觉质量、模型 alias 长期稳定、Provider 接受超出实测样本的任意 JSON body，也不承诺外部 exactly-once。

正式部署前的硬条件：

1. 正式 `SafeArtifactFetcher` 必须对下载执行 HTTPS/精确 host/逐跳 redirect/DNS 公网地址、响应大小、MIME 和完整图片解码校验，并防止 DNS 检查结果与实际连接目标脱节（连接 pin 或等效机制）。当前 `SecureResultDownloader` 尚未满足完整解码与 DNS pin 条件。
2. 每个 attempt 最多一个逻辑创建 POST。不得对超时或响应不确定状态自动重发；先持久化 task ID，再在同一 attempt 内恢复轮询。新建 attempt 可能重复计费，不得宣传外部 exactly-once。
3. 输入在本地白名单和字节/像素/比例上限内拒绝超限。8,100,000-byte 请求体上限是本地 guard；本次 Provider 只证实 978,936-byte body 成功。任何此上限内的 request-size 拒绝必须停止扩展并重新核验，不能默默降低 guard 后称边界已验证。
4. Result host 固定为实测命中的一台。未来返回新 host 或任何 redirect 均 fail-closed，须先通过一轮新的官方文档和单独授权探针确认后才能放行。
5. 上线前复核模型目录、地域/价格、alias 行为和当前额度；若 API 字段或模型行为发生实质变化，更新 capability version 并重新评估门禁。

如产品必须支持无状态未知窗口自动恢复、未授权重复请求也绝不可能产生重复费用、任意上传尺寸或 Provider 保证模型固定版本，则本 Conditional GO 不适用，应改选具有对应合同保障的 Provider，或收缩产品承诺。

## 19. 第 6 个窗口：正式 Adapter 与非收费端到端验收（2026-09-24）

本节记录阶段 6 实施及新验证，**不改写**第 18 节阶段 0 的探针、授权和真实调用历史。第 18.4–18.5 节对“生产 SafeArtifactFetcher 尚未实现”的表述是阶段 0 当时状态；阶段 6 已完成下述实现，但真实 Provider 系统 E2E 仍待新的单次授权。

### 19.1 官方协议复核

重新核对 [Wan 图像生成 API 文档](https://help.aliyun.com/en/model-studio/wan-image-generation-api-reference)、[Wan2.6 Image 模型与计费](https://help.aliyun.com/zh/model-studio/wan2-6-image) 和[限流建议](https://help.aliyun.com/en/model-studio/rate-limiting-best-practices)。文档仍支持阶段 0 冻结的 profile：北京 Workspace 异步创建 endpoint、`X-DashScope-Async: enable`、查询 `/api/v1/tasks/{task_id}`、`PENDING/RUNNING/SUCCEEDED/FAILED` 及终态 `CANCELED/UNKNOWN`；轮询仍建议按约 10 秒间隔。返回 URL 在示例中出现过北京 OSS host，但官方没有承诺结果 host 永久固定，因此代码只允许阶段 0 脱敏证据实际命中的一个精确 host，不因官方示例扩展 allowlist，重定向设为 0。

北京 `wan2.6-image` 单张输出标价仍为 ¥0.20。阶段 6 没有发出真实 Provider 请求，因此没有真实任务 ID、输出图或账单记录；此处价格只用于后续单次授权上限说明，不代表发生扣费。

### 19.2 实现与当前安全边界

- `backend/src/museflow/dashscope_image_adapter.py` 实现独立 `DashScopeWan26ImageAdapter`；profile 为 `dashscope-wan2.6-image-cn-beijing-edit`，model `wan2.6-image`，capability version `official-doc-snapshot-2026-09-24`。Workspace origin 必须是单层北京 MaaS HTTPS host，端口 443，拒绝 userinfo、路径、query、fragment 和相似后缀 host。
- 输入再次核对实际 PNG/JPEG/WebP MIME 与 bytes；限制为单张、RGB、单帧、无 alpha、≤6,000,000 bytes、每边 240–2,048 px、≤4,194,304 pixels、宽高比 1:4–4:1。Data URL 使用真实 MIME。JSON 请求固定模型字段与 `enable_interleave=false`、`n=1`、`size=1K`、`prompt_extend=false`、`watermark=false`，序列化后 body ≤8,100,000 bytes。
- 每 attempt 最多一个 POST；HTTPX 不跟随 redirect、不读取代理环境、不自动重试。远端 task ID 解析后立即进入 `on_remote_request_id`；已知 ID 只 GET 原 task。超时、响应读取失败、task ID 保存失败和保守处理的 5xx 会记录为 submission 状态未知并阻止自动创建重试。轮询检查 lease/deadline，10 秒间隔、最多 60 次、总超时 600 秒；只接受冻结状态集合及一个输出图 URL。
- `backend/src/museflow/safe_artifacts.py` 通过窄 httpcore transport 将已验证并选定的公网 DNS 地址直接用于 socket 连接，同时 URL、HTTP Host 和 TLS SNI 保留规范化后的原始 host。每个请求/redirect hop 独立建立 pool（无连接复用、无重试）；TLS context 要求主机名校验及 `CERT_REQUIRED`。Resolver 返回的所有地址都必须为公网地址；混合公网/非公网、私网、回环、保留、文档测试网段、IPv4-mapped unsafe IPv6 和 zone ID 均拒绝。Resolver 与 transport 注入仅用于测试，不全局 monkeypatch DNS。
- 通用 fetcher 对每跳重新校验 HTTPS、端口、userinfo、精确 allowlist、DNS/IP 和循环；协议 profile 设 `max_redirects=0`。流式读取前检查 `Content-Length`，实际累计字节始终受 20 MiB 限制，禁止压缩响应，deadline/ownership 在读取时持续检查，失败路径关闭 stream/client。输出只接受 PNG/RGB/单帧/无 alpha，最大边 1,440、总像素≤1,440²；验证 Content-Type、PNG 签名、Pillow `verify()` 和重新打开后的完整 `load()`，计算实际 SHA-256。Result URL 不进入数据库或 API 响应，Adapter 不接触 ORM/MinIO。
- 稳定错误码区分配置、请求/内容拒绝、认证/权限、配额和可退避的 RateQuota/BurstRate、Provider 5xx/网络、创建未知、轮询协议、结果 URL、安全 DNS、下载大小/MIME/解码与 deadline；429 allocation/account quota 是永久账号阻断，不映射为普通可退避限流。

### 19.3 非收费验证记录

| 验证 | 结果 |
| --- | --- |
| 修改前直接相关基线 | `39 passed, 1 skipped`；Starlette/httpx 与 AnyIO 两条既有弃用警告。 |
| SafeArtifactFetcher、Adapter、profile 测试 | `105 passed`；全 unit suite `214 passed`（ownership/deadline、严格结果结构、多个选择项、控制字符 URL 与空白凭据用例均纳入）。 |
| 独立模拟 Provider 全链路 | `1 passed`：隔离 PostgreSQL、Redis、MinIO；正式 Scheduler iteration；Celery Worker test runner；API 上传/任务创建/outbox、单次模拟 POST、远端 ID、轮询、受控安全下载、不可变候选、DB/MinIO SHA 与短期签名访问。Provider API 和 ArtifactFetcher transport 使用受控模拟，无网络 Provider POST。 |
| 阶段 5 完整 Compose 文生图及 Mock 图生图 | `2 passed`；独立 Compose 的 PostgreSQL、Redis、Scheduler、generation Worker、MinIO 均实际运行。 |
| PostgreSQL/MinIO 发布 fencing、MinIO 对象及参考上传集成 | `5 passed`。 |
| 短 lease 的真实 Redis Worker / heartbeat | `1 passed`；实际 Worker 运行超过 2 秒 lease；因该历史探针主动派发 outbox，测试期间暂停 Scheduler 避免其抢先消费。maintenance Scheduler→Worker `1 passed`，并由模拟 E2E 单独覆盖正式调度迭代。 |
| 完整后端测试 | 隔离服务下 `292 passed, 9 skipped, 2 warnings`；模拟 DashScope E2E 门禁已启用，其余 9 个 gated 测试按对应服务设置另行运行，详见最终技术方案阶段 6 验证记录。 |
| Ruff / Pyright / compileall | 本阶段涉及文件 Ruff 全绿；Pyright `0 errors, 0 warnings, 0 informations`；compileall 通过。全仓 Ruff 仍只有 `migrations/versions/0006_compatibility_constraints.py:45-47` 的 3 条已知历史 E501。 |
| Alembic / OpenAPI / 前端 / Compose | 隔离 PostgreSQL head `0007_reference_operation_leases`，`alembic check` 无新操作；OpenAPI 与已提交 JSON 对象一致；前端 typecheck、lint、17 个 Vitest、build 通过；Compose config 与 backend API/Scheduler/worker/maintenance-worker 镜像构建通过。 |

所有容器和卷使用独立 Compose project `museflow-stage6-simulation` 及独立命名 volume/network/bucket；默认 Compose 项目/数据卷未连接、迁移或删除。完整 pytest 执行时暂停后台消费者，避免与手动执行型集成测试争抢 outbox。2 条弃用警告与本阶段无关。

### 19.4 阶段 6 状态与门禁

**阶段 6：有条件完成。** SafeArtifactFetcher、正式 Adapter/profile、模拟 Provider 的真实基础设施端到端和无收费回归已实现并提交，代码提交为 `d9e1809fe14bad85e3d19c13c82b3c494a4096cf`。未取得新的真实调用授权：真实 POST 数 `0`，真实任务/状态轨迹/数据库与 MinIO 输出 SHA/匿名访问/短期访问证据均未验证，实际账单没有本窗口真实调用记录。下一步必须先完成提示词中列出的脱敏授权前清单，再等待用户逐次明确授权；未授权不得发送创建请求。模型 alias 稳定性和 8,100,000-byte request guard 仍分别属于 Provider 未承诺与本地限制；外部 exactly-once 仍不保证。
