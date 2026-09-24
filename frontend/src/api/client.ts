import type {
  ApiErrorEnvelope,
  CreateTaskInput,
  ReferenceAsset,
  Task,
  TaskList,
  GenerationType,
  TaskStatus,
} from './types'

const configuredBaseUrl = import.meta.env.VITE_API_BASE_URL ?? '/api/v1'
export const apiBaseUrl = configuredBaseUrl.replace(/\/$/, '')

export class ApiError extends Error {
  readonly status: number
  readonly code: string
  readonly requestId?: string

  constructor(
    status: number,
    code: string,
    message: string,
    requestId?: string,
  ) {
    super(message)
    this.name = 'ApiError'
    this.status = status
    this.code = code
    this.requestId = requestId
  }
}

function isErrorEnvelope(value: unknown): value is ApiErrorEnvelope {
  if (!value || typeof value !== 'object' || !('error' in value)) return false
  const error = (value as { error?: unknown }).error
  return Boolean(
    error &&
      typeof error === 'object' &&
      'code' in error &&
      'message' in error &&
      typeof (error as { code: unknown }).code === 'string' &&
      typeof (error as { message: unknown }).message === 'string',
  )
}

async function request<T>(path: string, init?: RequestInit): Promise<T> {
  const headers = new Headers(init?.headers)
  headers.set('Accept', 'application/json')
  if (typeof init?.body === 'string' && !headers.has('Content-Type')) {
    headers.set('Content-Type', 'application/json')
  }
  let response: Response
  try {
    response = await fetch(`${apiBaseUrl}${path}`, { ...init, headers })
  } catch (error) {
    if (error instanceof DOMException && error.name === 'AbortError') throw error
    throw new ApiError(0, 'NETWORK_OUTCOME_UNKNOWN', 'network response is unknown')
  }
  const body: unknown = await response.json().catch(() => null)
  if (!response.ok) {
    if (isErrorEnvelope(body)) {
      throw new ApiError(
        response.status,
        body.error.code,
        body.error.message,
        body.error.request_id,
      )
    }
    throw new ApiError(response.status, 'UNEXPECTED_RESPONSE', '服务暂时不可用，请稍后重试。')
  }
  return body as T
}

export const tasksApi = {
  create(body: CreateTaskInput, idempotencyKey: string) {
    return request<Task>('/tasks', {
      method: 'POST',
      headers: { 'Idempotency-Key': idempotencyKey },
      body: JSON.stringify(body),
    })
  },
  get(taskId: string, signal?: AbortSignal) {
    return request<Task>(`/tasks/${encodeURIComponent(taskId)}`, { signal })
  },
  list(params: {
    cursor?: string
    limit?: number
    status?: TaskStatus
    generationType?: GenerationType
  }, signal?: AbortSignal) {
    const query = new URLSearchParams()
    query.set('limit', String(params.limit ?? 9))
    if (params.cursor) query.set('cursor', params.cursor)
    if (params.status) query.set('status', params.status)
    if (params.generationType) query.set('generation_type', params.generationType)
    return request<TaskList>(`/tasks?${query}`, { signal })
  },
  retry(taskId: string, idempotencyKey: string) {
    return request<Task>(`/tasks/${encodeURIComponent(taskId)}/retry`, {
      method: 'POST',
      headers: { 'Idempotency-Key': idempotencyKey },
    })
  },
}

export const referenceAssetsApi = {
  upload(file: File, idempotencyKey: string) {
    const form = new FormData()
    form.append('file', file, file.name)
    return request<ReferenceAsset>('/assets', {
      method: 'POST',
      headers: { 'Idempotency-Key': idempotencyKey },
      body: form,
    })
  },
  get(assetId: string, signal?: AbortSignal) {
    return request<ReferenceAsset>(`/assets/${encodeURIComponent(assetId)}`, { signal })
  },
}

export function resultUrl(path: string | null | undefined): string | undefined {
  if (!path?.startsWith('/api/v1/assets/') || !path.endsWith('/download')) return undefined
  return path
}

export function referenceAssetUrl(assetId: string | null | undefined): string | undefined {
  if (!assetId || !/^[0-9a-f-]{36}$/i.test(assetId)) return undefined
  return `/api/v1/assets/${encodeURIComponent(assetId)}/download`
}
