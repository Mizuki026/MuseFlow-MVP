import { expect, test, type APIRequestContext } from '@playwright/test'

const API = 'http://127.0.0.1:8000/api/v1'

type Scenario = 'success' | 'transient_then_success' | 'rate_limited' | 'timeout' | 'permanent_failure'
type TaskResponse = {
  id: string
  status: 'QUEUED' | 'RUNNING' | 'RETRY_WAIT' | 'SUCCEEDED' | 'FAILED'
  events: { type: string }[]
  result?: { download_url?: string | null } | null
}

async function createDemoTask(
  request: APIRequestContext,
  scenario: Scenario,
  prompt: string,
): Promise<TaskResponse> {
  const response = await request.post(`${API}/demo/tasks`, {
    headers: { 'Idempotency-Key': crypto.randomUUID() },
    data: { prompt, scenario },
  })
  expect(response.status()).toBe(201)
  return response.json() as Promise<TaskResponse>
}

async function waitForStatus(
  request: APIRequestContext,
  taskId: string,
  expected: TaskResponse['status'],
): Promise<TaskResponse> {
  await expect.poll(async () => {
    const response = await request.get(`${API}/tasks/${taskId}`)
    return (await response.json() as TaskResponse).status
  }, { timeout: 40_000 }).toBe(expected)
  return (await (await request.get(`${API}/tasks/${taskId}`)).json()) as TaskResponse
}

test.describe.serial('real Compose task flow', () => {
  test('creates through the UI, converges to success, shows timeline, downloads, and survives refresh', async ({ page, request }) => {
    const prompt = `Playwright 成功场景 ${Date.now()}`
    await page.goto('/tasks/new')
    await page.getByLabel('创作提示词').fill(prompt)
    await page.getByRole('button', { name: '开始创作' }).click()

    await expect(page).toHaveURL(/\/tasks\/[0-9a-f-]+$/)
    await expect(page.getByRole('heading', { name: '创作已完成' })).toBeVisible({ timeout: 40_000 })
    await expect(page.getByRole('heading', { name: '事件时间线' })).toBeVisible()
    await expect(page.getByText('结果已保存')).toBeVisible()

    const taskId = page.url().split('/').at(-1)!
    const detail = (await (await request.get(`${API}/tasks/${taskId}`)).json()) as TaskResponse
    const stableDownload = detail.result?.download_url
    expect(stableDownload).toMatch(/^\/api\/v1\/assets\/.+\/download$/)
    const download = await request.get(`http://127.0.0.1:8000${stableDownload}`)
    expect(download.status()).toBe(200)
    expect(download.headers()['content-type']).toContain('image/png')

    await page.reload()
    await expect(page.getByRole('heading', { name: '创作已完成' })).toBeVisible()
    await expect(page.getByText(prompt)).toBeVisible()
  })

  test('shows cursor history and status filtering', async ({ page, request }) => {
    const prefix = `分页样本 ${Date.now()}`
    for (let index = 0; index < 11; index += 1) {
      await createDemoTask(request, 'success', `${prefix} ${index}`)
    }
    const failedPrompt = `永久失败筛选 ${Date.now()}`
    const failed = await createDemoTask(request, 'permanent_failure', failedPrompt)
    await waitForStatus(request, failed.id, 'FAILED')

    await page.goto('/tasks')
    await expect(page.getByRole('button', { name: '载入更多' })).toBeVisible()
    await page.getByRole('button', { name: '载入更多' }).click()
    await expect(page.getByText(`${prefix} 0`)).toBeVisible()

    await page.getByLabel('状态').selectOption('FAILED')
    await expect(page.getByText(failedPrompt)).toBeVisible()
    await page.getByText(failedPrompt).click()
    await expect(page).toHaveURL(new RegExp(`/tasks/${failed.id}$`))
  })

  test('recovers a transient failure and records the retry timeline', async ({ page, request }) => {
    const prompt = `临时错误恢复 ${Date.now()}`
    const created = await createDemoTask(request, 'transient_then_success', prompt)
    const completed = await waitForStatus(request, created.id, 'SUCCEEDED')
    expect(completed.events.map((event) => event.type)).toContain('TASK_RETRY_WAIT')

    await page.goto(`/tasks/${created.id}`)
    await expect(page.getByRole('heading', { name: '创作已完成' })).toBeVisible()
    await expect(page.getByText('等待自动重试')).toBeVisible()
  })

  test('shows permanent failure and creates a manual retry after retry exhaustion', async ({ page, request }) => {
    const permanent = await createDemoTask(request, 'permanent_failure', `永久错误 ${Date.now()}`)
    await waitForStatus(request, permanent.id, 'FAILED')
    await page.goto(`/tasks/${permanent.id}`)
    await expect(page.getByText('PROVIDER_REJECTED')).toBeVisible()
    await expect(page.getByText('mock provider rejected request')).toBeVisible()

    const exhausted = await createDemoTask(request, 'rate_limited', `手动重试 ${Date.now()}`)
    await waitForStatus(request, exhausted.id, 'FAILED')
    await page.goto(`/tasks/${exhausted.id}`)
    await page.getByRole('button', { name: '手动重试' }).click()
    await expect(page).not.toHaveURL(new RegExp(`/tasks/${exhausted.id}$`))
    await expect(page.getByText(`手动重试`, { exact: false })).toBeVisible()
  })
})
