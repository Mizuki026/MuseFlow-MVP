import type {
  ApiErrorEnvelope,
  CreateTaskBody,
  Task,
  TaskList,
  TaskStatus,
} from './types'

const configuredBaseUrl = import.meta.env.VITE_API_BASE_URL ?? 'http://127.0.0.1:8000/api/v1'
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
  const response = await fetch(`${apiBaseUrl}${path}`, {
    ...init,
    headers: {
      Accept: 'application/json',
      ...(init?.body ? { 'Content-Type': 'application/json' } : {}),
      ...init?.headers,
    },
  })
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
  create(body: CreateTaskBody, idempotencyKey: string) {
    return request<Task>('/tasks', {
      method: 'POST',
      headers: { 'Idempotency-Key': idempotencyKey },
      body: JSON.stringify(body),
    })
  },
  get(taskId: string) {
    return request<Task>(`/tasks/${encodeURIComponent(taskId)}`)
  },
  list(params: { cursor?: string; limit?: number; status?: TaskStatus }) {
    const query = new URLSearchParams()
    query.set('limit', String(params.limit ?? 9))
    if (params.cursor) query.set('cursor', params.cursor)
    if (params.status) query.set('status', params.status)
    return request<TaskList>(`/tasks?${query}`)
  },
  retry(taskId: string, idempotencyKey: string) {
    return request<Task>(`/tasks/${encodeURIComponent(taskId)}/retry`, {
      method: 'POST',
      headers: { 'Idempotency-Key': idempotencyKey },
    })
  },
}

export function resultUrl(path: string | null | undefined): string | undefined {
  if (!path?.startsWith('/api/v1/assets/') || !path.endsWith('/download')) return undefined
  return `${new URL(apiBaseUrl).origin}${path}`
}
