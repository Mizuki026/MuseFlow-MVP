# MuseFlow MVP 图片生成 Provider 可行性记录

## 1. 调查范围与结论状态

- **查证日期**：2026-09-18
- **目标**：验证首个真实图片生成 Provider 是否满足 MuseFlow MVP 的地区、账号、成本、单图固定尺寸、故障恢复、结果下载和窄适配器约束。
- **候选**：阿里云 Model Studio（百炼）Wan 2.6、Replicate 官方 FLUX.1 Schnell、OpenAI Images GPT Image 2.5 Flare。
- **证据边界**：关键结论只使用 Provider 官方 API 文档、官方定价、官方地区或账号说明和官方 API reference。
- **真实调用状态**：前两次分别因 Host/路径配置返回 HTTP 404、旧 API Key 返回 HTTP 401 `InvalidApiKey`，均未获得 `task_id` 并按停止条件结束。用户更换 API Key 后，第三次授权的真实异步提交使用 `n=1`、`size=1280*1280`、`prompt_extend=false`：提交 HTTP 200，任务轮询经历 `RUNNING → SUCCEEDED`，结果下载 HTTP 200；PNG 签名、媒体类型和尺寸验证通过，尺寸为 `1280×1280`，字节数为 1,828,507。完整签名 URL 未写入日志；结果主机以摘要 `0bd1575e39cb` 记录。全程没有重试。
- **阶段结论**：官方资料调查和一次脱敏成功探针均已完成，阶段 0 **已通过**。外部调用 exactly-once 仍不成立，必须保留产品降级承诺；第 1 个实现窗口可以开始。

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
| 地区和账号 | **已确认**：提供中国（北京）工作空间端点，API Key、端点和模型按地域隔离；需要阿里云账号、工作空间、对应地域 API Key 和有效计费。**已确认（用户提供）**：目标为华北 2（北京，`cn-beijing`），空间和 `wan2.6-t2i` 计费权限已具备；真实提交仍因 HTTP 404 未进入任务流程。 | **已确认**：需要 Replicate 账号、API token 和预付或有效付款方式；官方条款受美国出口和制裁规则约束。官方未发布中国大陆可用性矩阵。**待验证**：中国大陆网络、注册、付款和模型访问。 | **已确认**：仅支持官方列表内的国家和地区；中国大陆不在列表中。免费层不支持该模型。**待验证**：目标账号所在支持地区、组织权限和计费层级。 |
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

**已验证**：更换后的 API Key 可被北京 Workspace 接受；真实提交、轮询和结果下载已完成。

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

**已实测**：成功结果 URL 带签名查询参数，下载返回 HTTP 200，PNG `Content-Type`、文件签名和 `1280×1280` 尺寸均通过验证；完整 URL 未记录，主机以摘要 `0bd1575e39cb` 脱敏保存。官方契约仍规定 URL 有效期 24 小时，正式适配器必须成功后立即下载。

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
- 已记录两次分别获授权的真实提交：首次 HTTP 404；环境修正后的第二次 HTTP 401 `InvalidApiKey`。两次均未返回可用 `task_id`，均按停止条件立即结束，没有轮询、下载或重试，也没有产生可确认的图片费用。
- 当前运行环境此前对 DashScope 公共图像生成端点执行过不带凭据、不会生成图片的 HEAD 连通性探测，返回 HTTP 401；这只证明 DNS、TLS 和 HTTP 可达，不证明工作空间、账号、模型或计费可用。
- 已记录三次分别获授权的真实提交：首次 HTTP 404；第二次 HTTP 401 `InvalidApiKey`；更换 API Key 后第三次 HTTP 200 并成功完成 `task_id` 轮询和结果下载。第三次没有重试，PNG 尺寸为 `1280×1280`、字节数为 1,828,507。

### 10.2 尚未验证

- 429、5xx 和客户端超时没有被主动制造；其行为和重试边界依据官方错误/限流契约记录，正式适配器应使用受控模拟测试验证映射。
- 内容安全拒绝未被主动制造；官方错误码和永久失败边界已记录。
- 完整签名 URL 未进入日志，因此实际结果主机以摘要保存；正式适配器仍需使用精确 allowlist 和即时下载。


### 10.3 阶段 0 判定

**阶段 0 已通过，第 1 个实现窗口可以开始。**

已满足：官方候选调查、地区与费用核实、单图固定尺寸、异步提交、任务轮询、结果下载和媒体尺寸验证。三候选均不提供可依赖的外部幂等/稳定查询标识，因此产品外部重复调用降级承诺必须保留。429、5xx、超时和内容安全拒绝未被主动制造，正式适配器实现时应按本文官方契约和错误分类建立受控模拟测试。
