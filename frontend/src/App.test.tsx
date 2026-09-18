import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { act, render, screen, waitFor } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { MemoryRouter } from 'react-router-dom'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import App from './App'
import type { Task, TaskList, TaskStatus } from './api/types'

const TASK_ID = '11111111-1111-4111-8111-111111111111'
const NEXT_ID = '22222222-2222-4222-8222-222222222222'
const NOW = '2026-09-18T12:00:00Z'

function task(overrides: Partial<Task> = {}): Task {
  return {
    id: TASK_ID,
    prompt: '雾中的木屋',
    size_preset: '1280*1280',
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

describe('MuseFlow task experience', () => {
  const fetchMock = vi.fn<typeof fetch>()

  beforeEach(() => {
    vi.stubGlobal('fetch', fetchMock)
    fetchMock.mockReset()
  })

  afterEach(() => {
    vi.useRealTimers()
    vi.unstubAllGlobals()
  })

  it('validates the create form and prevents duplicate submission while pending', async () => {
    const user = userEvent.setup()
    let resolveCreate: ((value: Response) => void) | undefined
    fetchMock.mockImplementation(() => new Promise((resolve) => { resolveCreate = resolve }))
    renderRoute('/tasks/new')

    await user.click(screen.getByRole('button', { name: '开始创作' }))
    expect(await screen.findByText('请输入提示词。')).toBeInTheDocument()
    await user.type(screen.getByLabelText('创作提示词'), '星光下的森林')
    await user.click(screen.getByRole('button', { name: '开始创作' }))

    const pending = screen.getByRole('button', { name: '正在创建…' })
    expect(pending).toBeDisabled()
    await user.click(pending)
    expect(fetchMock).toHaveBeenCalledTimes(1)
    await act(async () => resolveCreate?.(response(task({ prompt: '星光下的森林' }), 201)))
    expect(await screen.findByRole('heading', { name: '排队中' })).toBeInTheDocument()
  })

  it.each([201, 200])('accepts a %s create response and opens task detail', async (status) => {
    fetchMock.mockResolvedValue(response(task({ prompt: '一座未来城市', idempotency_replayed: status === 200 }), status))
    const user = userEvent.setup()
    renderRoute('/tasks/new')

    await user.type(screen.getByLabelText('创作提示词'), '一座未来城市')
    await user.click(screen.getByRole('button', { name: '开始创作' }))

    expect(await screen.findByText('一座未来城市')).toBeInTheDocument()
    if (status === 200) {
      expect(screen.getByText('已恢复此前相同的提交，没有创建重复任务。')).toBeInTheDocument()
    }
    const init = fetchMock.mock.calls[0]?.[1]
    expect(new Headers(init?.headers).get('Idempotency-Key')).toBeTruthy()
  })

  it.each([
    [409, 'IDEMPOTENCY_KEY_CONFLICT', '这次提交与之前的内容不一致'],
    [422, 'INVALID_PROMPT', '提示词需要包含有效内容'],
    [503, 'SERVICE_NOT_READY', '创作服务仍在启动'],
  ])('renders safe feedback for %s create failures', async (status, code, message) => {
    fetchMock.mockResolvedValue(errorResponse(status as number, code as string))
    const user = userEvent.setup()
    renderRoute('/tasks/new')

    await user.type(screen.getByLabelText('创作提示词'), '失败场景')
    await user.click(screen.getByRole('button', { name: '开始创作' }))

    expect(await screen.findByText(new RegExp(message as string))).toBeInTheDocument()
    expect(screen.queryByText('internal raw detail')).not.toBeInTheDocument()
  })

  it('shows detail loading, errors, and an empty timeline', async () => {
    let resolveDetail: ((value: Response) => void) | undefined
    fetchMock.mockImplementation(() => new Promise((resolve) => { resolveDetail = resolve }))
    renderRoute(`/tasks/${TASK_ID}`)
    expect(screen.getByText('正在恢复任务状态…')).toBeInTheDocument()

    await act(async () => resolveDetail?.(response(task())))
    expect(await screen.findByText('任务尚未产生执行事件。')).toBeInTheDocument()
  })

  it('shows a recoverable detail error', async () => {
    fetchMock.mockResolvedValue(errorResponse(503, 'SERVICE_NOT_READY'))
    renderRoute(`/tasks/${TASK_ID}`)
    expect(await screen.findByRole('alert')).toHaveTextContent('暂时无法取得任务')
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

  it('loads cursor pages and applies the status filter', async () => {
    const first: TaskList = { items: [task()], next_cursor: 'cursor-2' }
    const second: TaskList = { items: [task({ id: NEXT_ID, prompt: '第二个任务' })], next_cursor: null }
    fetchMock.mockResolvedValueOnce(response(first)).mockResolvedValueOnce(response(second)).mockResolvedValueOnce(response({ items: [], next_cursor: null }))
    const user = userEvent.setup()
    renderRoute('/tasks')

    expect(await screen.findByText('雾中的木屋')).toBeInTheDocument()
    await user.click(screen.getByRole('button', { name: '载入更多' }))
    expect(await screen.findByText('第二个任务')).toBeInTheDocument()
    expect(String(fetchMock.mock.calls[1]?.[0])).toContain('cursor=cursor-2')

    await user.selectOptions(screen.getByLabelText('状态'), 'FAILED')
    await waitFor(() => expect(String(fetchMock.mock.calls[2]?.[0])).toContain('status=FAILED'))
  })

  it('creates a linear retry from a failed task', async () => {
    fetchMock
      .mockResolvedValueOnce(response(task({ status: 'FAILED', error_code: 'RETRY_EXHAUSTED', error_message: '已用尽自动重试' })))
      .mockResolvedValueOnce(response(task({ id: NEXT_ID, prompt: '重试任务' }), 201))
      .mockImplementation(() => Promise.resolve(response(task({ id: NEXT_ID, prompt: '重试任务' }))))
    const user = userEvent.setup()
    renderRoute(`/tasks/${TASK_ID}`)

    await user.click(await screen.findByRole('button', { name: '手动重试' }))
    expect(await screen.findByText('重试任务')).toBeInTheDocument()
    expect(String(fetchMock.mock.calls[1]?.[0])).toContain(`/tasks/${TASK_ID}/retry`)
  })

  it('uses the stable backend download path so expired signed URLs are never cached', async () => {
    fetchMock.mockResolvedValue(response(task({
      status: 'SUCCEEDED',
      result: {
        id: '33333333-3333-4333-8333-333333333333',
        role: 'RESULT',
        content_type: 'image/png',
        size_bytes: 1024,
        sha256: 'a'.repeat(64),
        download_url: '/api/v1/assets/33333333-3333-4333-8333-333333333333/download',
      },
    })))
    renderRoute(`/tasks/${TASK_ID}`)

    const download = await screen.findByRole('link', { name: '下载结果' })
    expect(download).toHaveAttribute('href', 'http://127.0.0.1:8000/api/v1/assets/33333333-3333-4333-8333-333333333333/download')
    expect(download.getAttribute('href')).not.toContain('X-Amz-')
  })

  it('renders user and error text without interpreting HTML', async () => {
    const unsafe = '<img src=x onerror=alert(1)>'
    fetchMock.mockResolvedValue(response(task({ status: 'FAILED', prompt: unsafe, error_code: 'PROVIDER_REJECTED', error_message: unsafe })))
    renderRoute(`/tasks/${TASK_ID}`)

    expect((await screen.findAllByText(unsafe)).length).toBeGreaterThan(0)
    expect(document.querySelector('img')).toBeNull()
  })
})
