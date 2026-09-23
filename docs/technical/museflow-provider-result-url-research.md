# MuseFlow Provider 结果 URL 官方文档核对

> 研究范围：仅核对阿里云百炼（Model Studio）官方文档；未发起请求、未读取凭据、未修改代码。访问日期：2026-09-19。

## 结论摘要

- **文档事实**：Wan2.6 文生图异步 API 不定义顶层 `result_url` 字段。
- **文档事实**：当前 Wan2.6 API 参考中的异步成功响应，把 PNG 结果 URL 放在 `output.choices[].message.content[].image`。
- **文档事实**：另一份官方《Wan 文生图 V2 API 参考》的异步示例，把结果放在 `output.results[].url`。这说明官方文档存在版本/协议展示差异，不能仅凭字段名猜测。
- **代码事实**：当前适配器已兼容 `output.results[].url` 与 `output.choices[].message.content[].image` 两种已记录结构。此前“只读取 `output.results[0].url`”描述的是修复前状态，不代表当前代码。
- **历史故障判断**：如果历史真实响应采用当前 Wan2.6 文档展示的 `choices/message/content/image` 结构，则当时的 `PROVIDER_RESULT_URL_MISSING` 是适配器解析契约不匹配，而不是 Provider 没有生成结果；但原始响应未保存，无法确认本次历史响应实际采用哪种结构。
- **尚不能确认**：本研究没有读取或恢复真实请求的原始/脱敏响应，因此不能证明本次真实响应实际采用了哪一种官方结构，也不能据此确认账号、区域或权限限制。

## 官方异步契约

### 请求与提交响应

北京业务空间专属端点使用以下形式（`{WorkspaceId}` 为文档占位符，不是本项目凭据）：

```text
POST https://{WorkspaceId}.cn-beijing.maas.aliyuncs.com/api/v1/services/aigc/image-generation/generation
```

异步请求必须带 `X-DashScope-Async: enable`，模型为 `wan2.6-t2i`；请求体使用 `input.messages`，图像数量、尺寸和提示词扩展参数位于 `parameters`。官方示例展示了 `n`、`size`、`prompt_extend` 等字段。

提交响应只承诺返回任务标识，例如：

```json
{
  "output": {
    "task_status": "PENDING",
    "task_id": "<task-id>"
  },
  "request_id": "<request-id>"
}
```

因此，提交阶段没有结果 URL 是正常行为，不应在提交响应中寻找 `result_url`。

### 任务查询与状态

查询端点为：

```text
GET https://{WorkspaceId}.cn-beijing.maas.aliyuncs.com/api/v1/tasks/{task_id}
```

官方列出的状态为 `PENDING`、`RUNNING`、`SUCCEEDED`、`FAILED`、`CANCELED`、`UNKNOWN`；正常流转为 `PENDING → RUNNING → SUCCEEDED/FAILED`。`task_id` 和任务数据通常保留 24 小时，查询接口默认 RPS 为 20，官方建议图像任务以约 10 秒间隔轮询。

## 结果 URL 字段核对

### 当前 Wan2.6 API 参考中的结构

官方 Wan2.6 API 参考的异步查询成功示例为：

```json
{
  "output": {
    "task_status": "SUCCEEDED",
    "finished": true,
    "choices": [
      {
        "message": {
          "role": "assistant",
          "content": [
            {
              "type": "image",
              "image": "<temporary-png-url>"
            }
          ]
        }
      }
    ]
  }
}
```

这里的结果字段是 `output.choices[].message.content[].image`；官方说明该 URL 指向 PNG，且有效期为 24 小时，应及时下载到永久存储。

### 另一份官方 V2 文档中的结构

官方《Wan 文生图 V2 API 参考》的 Wan2.6 异步示例展示了另一种结果结构：

```json
{
  "output": {
    "task_status": "SUCCEEDED",
    "results": [
      {
        "url": "<temporary-image-url>"
      }
    ]
  }
}
```

该页面同时说明 `results[].url` 是图像 URL，并展示了部分成功时的结果列表。两份官方页面对 Wan2.6 异步结果字段的展示不一致；当前适配器已经兼容这两种已记录的嵌套结构，但仍应以实际使用的官方协议版本和脱敏响应 fixture 为依据，不能把顶层 `result_url` 当作官方字段。

## 北京 Host、权限与计费

- **文档事实**：北京地域可使用业务空间专属域名 `{WorkspaceId}.cn-beijing.maas.aliyuncs.com`；官方也列出旧的共享 DashScope 域名。地域的 API Key、端点和模型列表不可混用，跨地域调用可能导致鉴权失败或服务错误。
- **文档事实**：API Key 的权限由所属 Workspace 决定；自定义权限可以限制可访问模型或 IP。被限制的模型、Workspace、IP 或地域不应通过切换端点规避。
- **文档事实**：官方 `wan2.6-t2i` 模型页列出北京原价为 **0.20 元/张**；文档说明 `n` 会影响图像数量和费用，测试应将 `n` 设为 1。价格页面不代表当前账号一定有免费额度，优惠和额度应以控制台/账单为准。
- **文档事实**：API Key 持有人可以代表账号发起计费请求，官方要求妥善保管 API Key。

这些事实可以解释鉴权、权限、地域和计费失败，但**不能解释一个已经进入 `SUCCEEDED` 的任务为何没有 URL**；官方文档对成功任务明确承诺会返回图像 URL。

## 对 `PROVIDER_RESULT_URL_MISSING` 的判断

| 判断 | 结论 | 依据 |
| --- | --- | --- |
| A. 适配器解析错误 | **历史故障中的较强推断，原始响应仍待确认** | 修复前适配器只读取 `output.results[0].url`；当前适配器已兼容 `output.results[].url` 和 `output.choices[].message.content[].image`。 |
| B. Host/区域/权限/模型限制 | **无法由该错误确认** | 官方文档说明这些限制通常表现为鉴权/服务错误；未提供本次真实响应。 |
| C. Provider 成功但没有可下载结果 | **不符合官方成功契约** | 官方明确写明 `SUCCEEDED` 响应包含图像 URL。 |
| D. 产品契约与 Provider 能力不一致 | **已确认存在字段契约风险** | 产品内部使用 `result_url` 概念，但官方返回的是嵌套字段；且官方不同页面展示了两种嵌套形态。 |

本文件不把失败请求描述为成功，也不建议把缺少结果 URL 改成成功；在获得脱敏真实响应前，fail-closed 是正确行为。

## 官方来源

1. [Wan2.6 - image generation and editing API reference（官方英文文档）](https://help.aliyun.com/en/model-studio/wan-image-generation-api-reference) — 当前 Wan2.6 异步请求、状态和 `choices[].message.content[].image` 结构。
2. [Wan 2.1 text-to-image V2 API reference（官方英文文档）](https://help.aliyun.com/en/model-studio/text-to-image-v2-api-reference) — Wan2.6 异步端点及 `results[].url` 示例、任务状态和 URL 有效期。
3. [万相-图像生成与编辑2.6 API参考（官方中文文档）](https://help.aliyun.com/zh/model-studio/wan-image-generation-api-reference) — 北京端点、状态枚举、PNG 结果 URL 和 24 小时有效期。
4. [Regions and endpoints（官方英文文档）](https://help.aliyun.com/en/model-studio/regions/) — 地域、业务空间端点、API Key 和模型列表的地域隔离。
5. [How to obtain an API key（官方英文文档）](https://help.aliyun.com/en/model-studio/get-api-key) — Workspace 权限、模型/IP 访问范围和密钥计费风险。
6. [wan2.6-t2i Model Info（官方英文文档）](https://help.aliyun.com/en/model-studio/wan2-6-t2i) — 北京地域价格与限流信息。
7. [Manage asynchronous tasks（官方英文文档）](https://help.aliyun.com/en/model-studio/manage-asynchronous-tasks) — 异步任务查询、保留期限和查询限流说明。
