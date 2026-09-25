import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { render, screen, waitFor } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { MemoryRouter } from 'react-router-dom'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import App from './App'
import type { ReferenceAsset, Task, TaskList, TaskStatus } from './api/types'

const TASK_ID = '11111111-1111-4111-8111-111111111111'
const NEXT_ID = '22222222-2222-4222-8222-222222222222'
const ASSET_ID = '33333333-3333-4333-8333-333333333333'
const NOW = '2026-09-18T12:00:00Z'

function task(overrides: Partial<Task> = {}): Task {
  return {
    id: TASK_ID,
    prompt: '雾中的木屋',
    size_preset: '1280*1280',
    generation_type: 'TEXT_TO_IMAGE',
    status: 'QUEUED',
    max_attempts: 3,
    deadline_at: '2026-09-18T12:10:00Z',
    created_at: NOW,
    queued_at: NOW,
    started_at: null,
    completed_at: null,
    next_attempt_at: null,
    error_code: null,
    error_message: null,
    retried_from_task_id: null,
    thumbnail_url: null,
    idempotency_replayed: false,
    events: [],
    attempts: [],
    result: null,
    retry_task_id: null,
    input_summary: null,
    reference_asset_id: null,
    reference_sha256: null,
    reference_download_url: null,
    provider_profile: 'mock-text-to-image-v1',
    provider_name: 'mock',
    model_name: 'mock-deterministic-image',
    capability_version: 'text-to-image-v1',
    policy_snapshot: null,
    ...overrides,
  }
}

function asset(overrides: Partial<ReferenceAsset> = {}): ReferenceAsset {
  return {
    asset_id: ASSET_ID,
    status: 'READY',
    content_type: 'image/png',
    width: 512,
    height: 512,
    size_bytes: 128,
    sha256: 'a'.repeat(64),
    download_url: `/api/v1/assets/${ASSET_ID}/download`,
    idempotency_replayed: false,
    ...overrides,
  }
}

function response(body: unknown, status = 200): Response {
  return new Response(JSON.stringify(body), {
    status,
    headers: { 'Content-Type': 'application/json' },
  })
}

function errorResponse(status: number, code: string): Response {
  return response({ error: { code, message: 'internal raw detail', request_id: 'request-1' } }, status)
}

function renderRoute(path: string) {
  const client = new QueryClient({
    defaultOptions: { queries: { retry: false, gcTime: Infinity }, mutations: { retry: false } },
  })
  return render(
    <QueryClientProvider client={client}>
      <MemoryRouter initialEntries={[path]}><App /></MemoryRouter>
    </QueryClientProvider>,
  )
}

function uploadFile(name = 'reference.png', type = 'image/png') {
  return new File([new Uint8Array([1, 2, 3, 4])], name, { type, lastModified: 1234 })
}

function initAt(index: number) {
  return fetchMock.mock.calls[index]?.[1]
}

let fetchMock: ReturnType<typeof vi.fn<typeof fetch>>
let originalCreateObjectURL: typeof URL.createObjectURL | undefined
let originalRevokeObjectURL: typeof URL.revokeObjectURL | undefined

describe('MuseFlow task experience', () => {
  beforeEach(() => {
    fetchMock = vi.fn<typeof fetch>()
    vi.stubGlobal('fetch', fetchMock)
    sessionStorage.clear()
    originalCreateObjectURL = URL.createObjectURL
    originalRevokeObjectURL = URL.revokeObjectURL
    Object.defineProperty(URL, 'createObjectURL', { configurable: true, value: vi.fn(() => `blob:test-${crypto.randomUUID()}`) })
    Object.defineProperty(URL, 'revokeObjectURL', { configurable: true, value: vi.fn() })
  })

  afterEach(() => {
    vi.useRealTimers()
    vi.unstubAllGlobals()
    if (originalCreateObjectURL) Object.defineProperty(URL, 'createObjectURL', { configurable: true, value: originalCreateObjectURL })
    else Reflect.deleteProperty(URL, 'createObjectURL')
    if (originalRevokeObjectURL) Object.defineProperty(URL, 'revokeObjectURL', { configurable: true, value: originalRevokeObjectURL })
    else Reflect.deleteProperty(URL, 'revokeObjectURL')
  })

  it('defaults to text-to-image and never sends a reference field', async () => {
    fetchMock.mockImplementation(() => Promise.resolve(response(task({ status: 'QUEUED' }), 201)))
    const user = userEvent.setup()
    renderRoute('/tasks/new')

    expect(screen.getByRole('radio', { name: /文生图/ })).toBeChecked()
    await user.type(screen.getByLabelText('创作提示词'), '星光下的森林')
    await user.click(screen.getByRole('button', { name: '开始创作' }))

    expect(await screen.findByRole('heading', { name: '排队中' })).toBeInTheDocument()
    const [url, init] = fetchMock.mock.calls[0] ?? []
    expect(String(url)).toContain('/tasks')
    expect(JSON.parse(String(init?.body))).toMatchObject({
      prompt: '星光下的森林',
      generation_type: 'TEXT_TO_IMAGE',
    })
    expect(JSON.parse(String(init?.body))).not.toHaveProperty('reference_asset_id')
    expect(new Headers(init?.headers).get('Idempotency-Key')).toBeTruthy()
  })

  it('requires an uploaded reference before creating an image-to-image task', async () => {
    const user = userEvent.setup()
    renderRoute('/tasks/new')
    await user.click(screen.getByRole('radio', { name: /图生图/ }))
    await user.type(screen.getByLabelText('创作提示词'), '改成冬日清晨')
    await user.click(screen.getByRole('button', { name: '开始创作' }))

    expect(await screen.findByText('请先上传并完成校验参考图片，再创建图生图任务。')).toBeInTheDocument()
    expect(fetchMock).not.toHaveBeenCalled()
  })

  it.each([201, 200])('accepts a %s upload response with a multipart body and stable upload key', async (status) => {
    fetchMock.mockResolvedValue(response(asset({ idempotency_replayed: status === 200 }), status))
    const user = userEvent.setup()
    renderRoute('/tasks/new')
    await user.click(screen.getByRole('radio', { name: /图生图/ }))
    await user.upload(screen.getByLabelText('参考图片文件'), uploadFile())
    await user.click(screen.getByRole('button', { name: '上传参考图片' }))

    expect(await screen.findByText('上传完成')).toBeInTheDocument()
    const init = initAt(0)
    expect(String(fetchMock.mock.calls[0]?.[0])).toContain('/assets')
    expect(new Headers(init?.headers).get('Idempotency-Key')).toBeTruthy()
    expect(new Headers(init?.headers).has('Content-Type')).toBe(false)
    expect(init?.body).toBeInstanceOf(FormData)
  })

  it('reuses an upload idempotency key after an unknown network outcome', async () => {
    fetchMock.mockRejectedValueOnce(new TypeError('connection closed')).mockResolvedValueOnce(response(asset(), 201))
    const user = userEvent.setup()
    renderRoute('/tasks/new')
    await user.click(screen.getByRole('radio', { name: /图生图/ }))
    await user.upload(screen.getByLabelText('参考图片文件'), uploadFile())
    await user.click(screen.getByRole('button', { name: '上传参考图片' }))
    expect(await screen.findByText('重试会继续使用同一上传标识，避免重复创建素材。')).toBeInTheDocument()
    await user.click(screen.getByRole('button', { name: '重试上传' }))
    expect(await screen.findByText('上传完成')).toBeInTheDocument()

    const firstKey = new Headers(initAt(0)?.headers).get('Idempotency-Key')
    const secondKey = new Headers(initAt(1)?.headers).get('Idempotency-Key')
    expect(firstKey).toBeTruthy()
    expect(secondKey).toBe(firstKey)
  })

  it('uses a new upload key after replacing a file and surfaces a 409 conflict safely', async () => {
    fetchMock.mockResolvedValueOnce(response(asset(), 201)).mockResolvedValueOnce(errorResponse(409, 'IDEMPOTENCY_KEY_CONFLICT'))
    const user = userEvent.setup()
    renderRoute('/tasks/new')
    await user.click(screen.getByRole('radio', { name: /图生图/ }))
    const picker = screen.getByLabelText('参考图片文件')
    await user.upload(picker, uploadFile('first.png'))
    await user.click(screen.getByRole('button', { name: '上传参考图片' }))
    expect(await screen.findByText('上传完成')).toBeInTheDocument()
    await user.click(screen.getByRole('button', { name: '替换图片' }))
    await user.upload(picker, uploadFile('second.png'))
    await user.click(screen.getByRole('button', { name: '上传参考图片' }))

    expect(await screen.findByText(/同一个操作标识对应了不同输入/)).toBeInTheDocument()
    expect(new Headers(initAt(1)?.headers).get('Idempotency-Key')).not.toBe(new Headers(initAt(0)?.headers).get('Idempotency-Key'))
    expect(screen.queryByText('internal raw detail')).not.toBeInTheDocument()
  })

  it('clears and creates object URLs across replacement, clear, and unmount', async () => {
    const createObjectURL = vi.fn(() => `blob:reference-${crypto.randomUUID()}`)
    const revokeObjectURL = vi.fn()
    Object.defineProperty(URL, 'createObjectURL', { configurable: true, value: createObjectURL })
    Object.defineProperty(URL, 'revokeObjectURL', { configurable: true, value: revokeObjectURL })
    const user = userEvent.setup()
    const view = renderRoute('/tasks/new')
    await user.click(screen.getByRole('radio', { name: /图生图/ }))
    const picker = screen.getByLabelText('参考图片文件')
    await user.upload(picker, uploadFile('first.png'))
    expect(await screen.findByRole('img', { name: '所选参考图片预览' })).toBeInTheDocument()
    const firstUrl = createObjectURL.mock.results[0]?.value
    await user.click(screen.getByRole('button', { name: '替换图片' }))
    await user.upload(picker, uploadFile('second.png'))
    await waitFor(() => expect(revokeObjectURL).toHaveBeenCalledWith(firstUrl))
    const secondUrl = createObjectURL.mock.results[1]?.value
    await user.click(screen.getByRole('button', { name: '清除' }))
    await waitFor(() => expect(revokeObjectURL).toHaveBeenCalledWith(secondUrl))

    await user.upload(picker, uploadFile('third.png'))
    const thirdUrl = createObjectURL.mock.results[2]?.value
    view.unmount()
    expect(revokeObjectURL).toHaveBeenCalledWith(thirdUrl)
  })

  it('keeps the uploaded asset and task key after task creation fails, without uploading twice', async () => {
    fetchMock
      .mockResolvedValueOnce(response(asset(), 201))
      .mockResolvedValueOnce(errorResponse(503, 'SERVICE_NOT_READY'))
      .mockImplementation(() => Promise.resolve(response(task({
        generation_type: 'IMAGE_TO_IMAGE',
        reference_asset_id: ASSET_ID,
        reference_download_url: `/api/v1/assets/${ASSET_ID}/download`,
        status: 'QUEUED',
      }), 201)))
    const user = userEvent.setup()
    renderRoute('/tasks/new')
    await user.click(screen.getByRole('radio', { name: /图生图/ }))
    await user.upload(screen.getByLabelText('参考图片文件'), uploadFile())
    await user.type(screen.getByLabelText('创作提示词'), '给木屋增加雪景')
    await user.click(screen.getByRole('button', { name: '上传参考图片' }))
    expect(await screen.findByText('上传完成')).toBeInTheDocument()
    await user.click(screen.getByRole('button', { name: '开始创作' }))
    expect(await screen.findByText('创作服务仍在启动，请稍后重试。')).toBeInTheDocument()
    expect(screen.getByText('上传完成')).toBeInTheDocument()
    await user.click(screen.getByRole('button', { name: '重试创建任务' }))
    expect(await screen.findByText('雾中的木屋')).toBeInTheDocument()

    const uploadCalls = fetchMock.mock.calls.filter(([url]) => String(url).includes('/assets'))
    const createCalls = fetchMock.mock.calls.filter(([url]) => String(url).endsWith('/tasks'))
    expect(uploadCalls).toHaveLength(1)
    expect(createCalls).toHaveLength(2)
    expect(JSON.parse(String(createCalls[0]?.[1]?.body))).toMatchObject({
      generation_type: 'IMAGE_TO_IMAGE',
      reference_asset_id: ASSET_ID,
    })
    expect(JSON.parse(String(createCalls[0]?.[1]?.body))).not.toHaveProperty('reference_sha256')
    expect(new Headers(createCalls[0]?.[1]?.headers).get('Idempotency-Key'))
      .toBe(new Headers(createCalls[1]?.[1]?.headers).get('Idempotency-Key'))
    expect(new Headers(uploadCalls[0]?.[1]?.headers).get('Idempotency-Key'))
      .not.toBe(new Headers(createCalls[0]?.[1]?.headers).get('Idempotency-Key'))
  })

  it('rotates the task key after input changes but keeps it stable through rerenders and unknown outcomes', async () => {
    fetchMock.mockRejectedValueOnce(new TypeError('connection closed')).mockImplementation(() => Promise.resolve(response(task(), 201)))
    const user = userEvent.setup()
    renderRoute('/tasks/new')
    const prompt = screen.getByLabelText('创作提示词')
    await user.type(prompt, '雾中的旧城')
    await user.click(screen.getByRole('button', { name: '开始创作' }))
    expect(await screen.findByText(/沿用本次任务幂等标识/)).toBeInTheDocument()
    const firstKey = new Headers(initAt(0)?.headers).get('Idempotency-Key')
    await user.clear(prompt)
    await user.type(prompt, '雾中的新城')
    await user.click(screen.getByRole('button', { name: '开始创作' }))
    expect(await screen.findByText('雾中的木屋')).toBeInTheDocument()
    const secondKey = new Headers(initAt(1)?.headers).get('Idempotency-Key')
    expect(firstKey).toBeTruthy()
    expect(secondKey).not.toBe(firstKey)
    expect(JSON.parse(String(initAt(1)?.body))).toHaveProperty('prompt', '雾中的新城')
  })

  it('supports generation type and status together and restarts pagination from the backend cursor', async () => {
    const first: TaskList = { items: [task()], next_cursor: 'cursor-from-server' }
    const second: TaskList = { items: [task({ id: NEXT_ID, prompt: '第二页任务' })], next_cursor: null }
    fetchMock.mockResolvedValueOnce(response(first)).mockResolvedValueOnce(response(second)).mockResolvedValueOnce(response({ items: [], next_cursor: null })).mockResolvedValueOnce(response({ items: [], next_cursor: null }))
    const user = userEvent.setup()
    renderRoute('/tasks')

    expect(await screen.findByText('雾中的木屋')).toBeInTheDocument()
    await user.click(screen.getByRole('button', { name: '载入更多' }))
    expect(await screen.findByText('第二页任务')).toBeInTheDocument()
    const secondRequest = new URL(String(fetchMock.mock.calls[1]?.[0]), 'http://localhost')
    expect(secondRequest.searchParams.get('cursor')).toBe('cursor-from-server')
    await user.selectOptions(screen.getByRole('combobox', { name: '生成方式' }), 'IMAGE_TO_IMAGE')
    await user.selectOptions(screen.getByRole('combobox', { name: '状态' }), 'FAILED')

    await waitFor(() => expect(fetchMock).toHaveBeenCalledTimes(4))
    const filtered = new URL(String(fetchMock.mock.calls[3]?.[0]), 'http://localhost')
    expect(filtered.searchParams.get('generation_type')).toBe('IMAGE_TO_IMAGE')
    expect(filtered.searchParams.get('status')).toBe('FAILED')
    expect(filtered.searchParams.has('cursor')).toBe(false)
  })

  it.each([
    ['QUEUED', true],
    ['RUNNING', true],
    ['RETRY_WAIT', true],
    ['SUCCEEDED', false],
    ['FAILED', false],
  ] satisfies [TaskStatus, boolean][])('polls %s tasks only while non-terminal', async (status, shouldPoll) => {
    fetchMock.mockResolvedValue(response(task({ status })))
    renderRoute(`/tasks/${TASK_ID}`)
    await waitFor(() => expect(fetchMock).toHaveBeenCalled())
    const initialCalls = fetchMock.mock.calls.length

    if (shouldPoll) {
      await waitFor(() => expect(fetchMock.mock.calls.length).toBeGreaterThan(initialCalls))
    } else {
      await new Promise((resolve) => setTimeout(resolve, 100))
      expect(fetchMock).toHaveBeenCalledTimes(initialCalls)
    }
  })

  it('shows image-to-image reference, retry lineage, attempt timeline, and nullable result dimensions', async () => {
    fetchMock.mockResolvedValue(response(task({
      status: 'SUCCEEDED',
      generation_type: 'IMAGE_TO_IMAGE',
      reference_asset_id: ASSET_ID,
      reference_download_url: `/api/v1/assets/${ASSET_ID}/download`,
      retried_from_task_id: NEXT_ID,
      attempts: [{
        id: '44444444-4444-4444-8444-444444444444',
        sequence: 1,
        status: 'SUCCEEDED',
        phase: 'RESULT_PERSISTED',
        provider_name: 'mock',
        started_at: NOW,
        finished_at: NOW,
        result_digest: 'a'.repeat(64),
      }],
      result: {
        id: '55555555-5555-4555-8555-555555555555',
        role: 'RESULT',
        content_type: 'image/png',
        size_bytes: 1024,
        width: null,
        height: null,
        sha256: 'b'.repeat(64),
        download_url: `/api/v1/assets/55555555-5555-4555-8555-555555555555/download`,
      },
    })))
    renderRoute(`/tasks/${TASK_ID}`)

    expect(await screen.findByRole('img', { name: '图生图参考素材' })).toHaveAttribute('src', `/api/v1/assets/${ASSET_ID}/download`)
    expect(screen.getByText(/此任务由/)).toBeInTheDocument()
    expect(screen.getByRole('heading', { name: 'Attempt 与事件时间线' })).toBeInTheDocument()
    expect(screen.getByText('未知 × 未知')).toBeInTheDocument()
    expect(screen.getByText(/mock-deterministic-image/)).toBeInTheDocument()
  })

  it('creates a linear immutable manual retry with a stable idempotency key', async () => {
    fetchMock
      .mockResolvedValueOnce(response(task({ status: 'FAILED', error_code: 'PROVIDER_REJECTED', error_message: 'raw detail' })))
      .mockImplementation(() => Promise.resolve(response(task({ id: NEXT_ID, status: 'QUEUED', retried_from_task_id: TASK_ID }), 201)))
    const user = userEvent.setup()
    renderRoute(`/tasks/${TASK_ID}`)

    expect(await screen.findByText('生成服务永久拒绝了本次请求。')).toBeInTheDocument()
    expect(screen.queryByText('raw detail')).not.toBeInTheDocument()
    await user.click(screen.getByRole('button', { name: '手动重试' }))
    expect(await screen.findByRole('heading', { name: '排队中' })).toBeInTheDocument()
    const retryRequest = fetchMock.mock.calls[1]
    expect(String(retryRequest?.[0])).toContain(`/tasks/${TASK_ID}/retry`)
    expect(new Headers(retryRequest?.[1]?.headers).get('Idempotency-Key')).toBeTruthy()
    expect(screen.getByText('原任务保持不变', { exact: false })).toBeInTheDocument()
  })

  it('uses stable MuseFlow download paths and never renders signed MinIO URLs', async () => {
    fetchMock.mockResolvedValue(response(task({
      status: 'SUCCEEDED',
      result: {
        id: '55555555-5555-4555-8555-555555555555',
        role: 'RESULT',
        content_type: 'image/png',
        size_bytes: 1024,
        width: 512,
        height: 512,
        sha256: 'a'.repeat(64),
        download_url: '/api/v1/assets/55555555-5555-4555-8555-555555555555/download',
      },
    })))
    renderRoute(`/tasks/${TASK_ID}`)

    const download = await screen.findByRole('link', { name: '下载结果' })
    expect(download).toHaveAttribute('href', '/api/v1/assets/55555555-5555-4555-8555-555555555555/download?attachment=true')
    expect(document.body.innerHTML).not.toContain('X-Amz-')
    expect(document.body.innerHTML).not.toContain('minio:9000')
  })

  it('restores a saved uploaded asset after a page refresh without restoring a File or object URL', async () => {
    sessionStorage.setItem('museflow:create-task:v1', JSON.stringify({
      prompt: '恢复草稿',
      generationType: 'IMAGE_TO_IMAGE',
      sizePreset: '1280*1280',
      taskKey: 'stable-task-key',
      assetId: ASSET_ID,
    }))
    fetchMock.mockResolvedValue(response(asset()))
    renderRoute('/tasks/new')
    expect(await screen.findByRole('img', { name: '所选参考图片预览' })).toHaveAttribute('src', `/api/v1/assets/${ASSET_ID}/download`)
    expect(screen.getByLabelText('创作提示词')).toHaveValue('恢复草稿')
    expect(fetchMock.mock.calls[0]?.[0]).toContain(`/assets/${ASSET_ID}`)
  })
})
