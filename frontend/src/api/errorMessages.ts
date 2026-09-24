import { ApiError } from './client'

const errorMessages: Record<string, string> = {
  ALPHA_NOT_ALLOWED: '参考图片不能包含透明通道。',
  ASSET_NOT_FOUND: '图片素材不存在，请重新上传。',
  DEADLINE_EXCEEDED: '任务已超过执行时限。',
  IDEMPOTENCY_KEY_CONFLICT: '同一个操作标识对应了不同输入。请修改输入，或重新选择图片后再试。',
  IMAGE_ALPHA_NOT_ALLOWED: '参考图片不能包含透明通道。',
  IMAGE_ANIMATED_NOT_ALLOWED: '暂不支持动图，请选择单帧图片。',
  IMAGE_ASPECT_RATIO_UNSUPPORTED: '图片长宽比例不受支持，请换一张图片。',
  IMAGE_COLOR_MODE_UNSUPPORTED: '图片必须是 RGB 格式。',
  IMAGE_CONTENT_TYPE_MISMATCH: '文件声明的格式与图片实际格式不一致。',
  IMAGE_CORRUPT: '图片无法完整读取，可能已损坏。',
  IMAGE_DECOMPRESSION_BOMB: '图片尺寸超出安全解码范围。',
  IMAGE_DIMENSIONS_UNSUPPORTED: '图片宽高需在 240 至 2048 像素之间。',
  IMAGE_FORMAT_UNSUPPORTED: '只支持 PNG、JPEG 和 WebP 图片。',
  IMAGE_PIXEL_LIMIT_EXCEEDED: '图片像素总量超出限制，请换一张较小的图片。',
  INVALID_GENERATION_OPTIONS: '当前生成方式不支持所选参数。',
  INVALID_MULTIPART: '图片上传请求无效，请重新选择一张图片。',
  INVALID_PROMPT: '提示词需要包含有效内容，且不能超过 2000 个字符。',
  INVALID_REQUEST: '提交内容无法识别，请检查后重试。',
  MULTIPART_BODY_TOO_LARGE: '图片上传请求超过服务端限制。',
  OBJECT_STORE_UNAVAILABLE: '图片存储暂时不可用，可以稍后使用同一张图片重试。',
  OWNERSHIP_LOST: '任务正在由另一个执行单元恢复，请稍后刷新状态。',
  PROVIDER_ACCOUNT_NOT_READY: '生成服务账户或额度暂不可用，请检查部署配置。',
  PROVIDER_AUTHENTICATION: '生成服务凭据未通过验证，请检查部署配置。',
  PROVIDER_CAPABILITY_UNSUPPORTED: '当前生成服务不支持这种生成方式或尺寸。',
  PROVIDER_CONTENT_REJECTED: '生成服务因内容审核拒绝了本次请求。',
  PROVIDER_INVALID_REQUEST: '生成服务拒绝了请求，请检查提示词和图片。',
  PROVIDER_PERMISSION_DENIED: '当前部署没有使用所选生成服务的权限。',
  PROVIDER_PROFILE_UNAVAILABLE: '生成服务配置不可用，请检查服务端 Provider profile 和凭据。',
  PROVIDER_REJECTED: '生成服务永久拒绝了本次请求。',
  PROVIDER_RATE_LIMITED: '生成服务暂时繁忙，系统会按任务策略恢复。',
  PROVIDER_SUBMISSION_UNKNOWN: '生成服务是否接收了请求暂时无法确认；任务不会自动重复提交。',
  PROVIDER_TIMEOUT: '生成服务执行超时，系统将按任务策略处理。',
  PROVIDER_UNAVAILABLE: '生成服务暂时不可用，系统会按任务策略恢复。',
  REFERENCE_ASSET_INVALID: '参考素材校验状态无效，请重新上传。',
  REFERENCE_ASSET_NOT_FOUND: '参考素材不存在，请重新上传。',
  REFERENCE_ASSET_NOT_READY: '参考素材尚未就绪，请稍后重试或重新上传。',
  REFERENCE_ASSET_NOT_REUSABLE: '该参考素材不能再次使用，请重新上传。',
  REFERENCE_ASSET_PROCESSING: '参考素材仍在处理，请稍后重试。',
  REFERENCE_ASSET_STATE_UPDATE_FAILED: '参考素材状态暂时不可用，请重新上传或检查服务状态。',
  REFERENCE_ASSET_UNAVAILABLE: '参考素材暂时不可用，请检查素材状态或重新上传。',
  REFERENCE_OBJECT_MISSING: '参考图片暂时无法读取，请重新上传或检查存储服务。',
  REFERENCE_OBJECT_MISMATCH: '参考图片完整性校验失败，请重新上传。',
  RESULT_FETCH_UNAVAILABLE: '结果图片暂时无法读取，请稍后查看任务。',
  RESULT_INVALID: '生成服务返回的图片无法使用。',
  RESULT_NOT_READY: '结果图片尚未就绪，请稍后查看任务。',
  RESULT_STORAGE_ERROR: '结果图片暂时无法保存，系统会按任务策略恢复。',
  RESULT_STORE_UNAVAILABLE: '结果图片存储暂时不可用，请稍后重试。',
  RETRY_NOT_ALLOWED: '该任务当前不满足手动重试条件。',
  SERVICE_NOT_READY: '创作服务仍在启动，请稍后重试。',
  TASK_NOT_FOUND: '任务不存在或暂时无法访问。',
}

export function messageForError(error: unknown, fallback: string): string {
  if (!(error instanceof ApiError)) return fallback
  if (error.status === 413) return '图片超过服务端允许的大小限制（6 MB）。'
  return errorMessages[error.code] ?? (
    error.code === 'NETWORK_OUTCOME_UNKNOWN'
      ? '网络响应状态未知；再次尝试会复用本次幂等标识。'
      : error.status >= 500
        ? '服务暂时不可用，请稍后重试或检查部署配置。'
        : fallback
  )
}

export function describeTaskError(code: string | null | undefined): string | undefined {
  if (!code) return undefined
  return errorMessages[code] ?? '任务遇到未识别的问题；请查看服务状态后再重试。'
}
