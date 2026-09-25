import { readFile } from 'node:fs/promises'
import { deflateSync } from 'node:zlib'
import { expect, test, type APIRequestContext, type Page, type TestInfo } from '@playwright/test'

const baseURL = process.env.PLAYWRIGHT_BASE_URL ?? 'http://127.0.0.1:5173'
const runId = process.env.MUSEFLOW_E2E_RUN_ID ?? `local-${Date.now()}`
const minioURL = `http://127.0.0.1:${process.env.MUSEFLOW_MINIO_PORT ?? '9000'}`

type Scenario = 'success' | 'transient_then_success' | 'rate_limited' | 'permanent_failure'
type TaskBody = {
  id: string
  prompt: string
  status: 'QUEUED' | 'RUNNING' | 'RETRY_WAIT' | 'SUCCEEDED' | 'FAILED'
  error_code: string | null
  retried_from_task_id: string | null
  retry_task_id: string | null
  generation_type: 'TEXT_TO_IMAGE' | 'IMAGE_TO_IMAGE' | null
  reference_asset_id: string | null
  result: { download_url: string; width: number | null; height: number | null; content_type: string } | null
  attempts: { sequence: number; status: string; phase: string }[]
  events: { type: string }[]
}

function crc32(bytes: Buffer): number {
  let crc = 0xffffffff
  for (const byte of bytes) {
    crc ^= byte
    for (let bit = 0; bit < 8; bit += 1) crc = (crc >>> 1) ^ (crc & 1 ? 0xedb88320 : 0)
  }
  return (crc ^ 0xffffffff) >>> 0
}

function pngChunk(name: string, data: Buffer): Buffer {
  const type = Buffer.from(name)
  const length = Buffer.alloc(4)
  length.writeUInt32BE(data.length)
  const checksum = Buffer.alloc(4)
  checksum.writeUInt32BE(crc32(Buffer.concat([type, data])))
  return Buffer.concat([length, type, data, checksum])
}

function referencePng(color: readonly [number, number, number] = [52, 112, 166]): Buffer {
  const width = 512
  const height = 512
  const pixels = Buffer.alloc((width * 3 + 1) * height)
  for (let y = 0; y < height; y += 1) {
    const row = y * (width * 3 + 1)
    for (let x = 0; x < width; x += 1) {
      const pixel = row + 1 + x * 3
      pixels[pixel] = color[0]
      pixels[pixel + 1] = color[1]
      pixels[pixel + 2] = color[2]
    }
  }
  const header = Buffer.alloc(13)
  header.writeUInt32BE(width, 0)
  header.writeUInt32BE(height, 4)
  header[8] = 8
  header[9] = 2
  return Buffer.concat([
    Buffer.from([137, 80, 78, 71, 13, 10, 26, 10]),
    pngChunk('IHDR', header),
    pngChunk('IDAT', deflateSync(pixels)),
    pngChunk('IEND', Buffer.alloc(0)),
  ])
}

function noteBrowserHealth(page: Page) {
  const errors: string[] = []
  const urls: string[] = []
  const httpFailures: { status: number; path: string }[] = []
  page.on('pageerror', (error) => errors.push(error.message))
  page.on('console', (message) => {
    if (message.type() === 'error' && !/^Failed to load resource: the server responded with a status of \d+/.test(message.text())) {
      errors.push(message.text())
    }
  })
  page.on('request', (request) => urls.push(request.url()))
  page.on('response', (response) => {
    if (response.status() >= 400) {
      const url = new URL(response.url())
      httpFailures.push({ status: response.status(), path: url.pathname })
    }
  })
  return { errors, urls, httpFailures }
}

async function captureScreenshot(page: Page, testInfo: TestInfo, name: string) {
  await page.screenshot({ path: testInfo.outputPath(name), fullPage: true, animations: 'disabled' })
}

async function createDemoTask(
  request: APIRequestContext,
  scenario: Scenario,
  prompt: string,
  options: { generationType?: 'TEXT_TO_IMAGE' | 'IMAGE_TO_IMAGE'; referenceAssetId?: string } = {},
): Promise<TaskBody> {
  const response = await request.post('/api/v1/demo/tasks', {
    headers: { 'Idempotency-Key': crypto.randomUUID() },
    data: {
      prompt,
      scenario,
      generation_type: options.generationType ?? 'TEXT_TO_IMAGE',
      ...(options.referenceAssetId ? { reference_asset_id: options.referenceAssetId } : {}),
    },
  })
  expect(response.status()).toBe(201)
  return response.json() as Promise<TaskBody>
}

async function getTask(request: APIRequestContext, id: string): Promise<TaskBody> {
  const response = await request.get(`/api/v1/tasks/${id}`)
  expect(response.ok()).toBeTruthy()
  return response.json() as Promise<TaskBody>
}

async function waitForStatus(
  request: APIRequestContext,
  taskId: string,
  status: TaskBody['status'],
): Promise<TaskBody> {
  try {
    await expect.poll(async () => (await getTask(request, taskId)).status, { timeout: 75_000 }).toBe(status)
  } catch (error) {
    const latest = await getTask(request, taskId)
    console.error('[E2E task state]', JSON.stringify({
      id: latest.id,
      status: latest.status,
      error_code: latest.error_code,
      attempts: latest.attempts.map(({ sequence, status: attemptStatus, phase }) => ({ sequence, status: attemptStatus, phase })),
      events: latest.events.map(({ type }) => type),
    }))
    throw error
  }
  return getTask(request, taskId)
}

async function expectBrowserHealthy(
  page: Page,
  health: ReturnType<typeof noteBrowserHealth>,
  expectedHttpFailures: { status: number; path: string }[] = [],
) {
  expect(health.errors, 'browser console and uncaught page errors').toEqual([])
  expect(health.httpFailures, 'HTTP failures must be expected and surfaced in the UI').toEqual(expectedHttpFailures)
  const hosts = health.urls.map((value) => new URL(value).hostname)
  expect(hosts.filter((host) => /^(api|postgres|redis|minio|worker|scheduler)$/.test(host)), 'browser must use public Web/API routes rather than Compose DNS').toEqual([])
}

test.describe.serial('production Web UI over the real Compose stack', () => {
  test.beforeEach(async ({ page }) => {
    page.setDefaultTimeout(20_000)
  })

  test('creates text-to-image from the static frontend, shows the result and history, and survives refresh', async ({ page, request }, testInfo) => {
    const health = noteBrowserHealth(page)
    const prompt = `${runId}:persist:text:雨后山谷木屋`
    await page.goto('/tasks/new')
    await expect(page.getByRole('radio', { name: /文生图/ })).toBeChecked()
    await page.getByLabel('创作提示词').fill(prompt)
    await captureScreenshot(page, testInfo, 'create-text-desktop.png')
    await page.getByRole('button', { name: '开始创作' }).click()

    await expect(page).toHaveURL(/\/tasks\/[0-9a-f-]+$/)
    await expect(page.getByRole('heading', { name: '创作已完成' })).toBeVisible({ timeout: 75_000 })
    await expect(page.getByRole('heading', { name: 'Attempt 与事件时间线' })).toBeVisible()
    await expect(page.getByText('结果已保存')).toBeVisible()
    const resultImage = page.getByRole('img', { name: '生成结果预览' })
    await expect(resultImage).toBeVisible()
    await expect.poll(() => resultImage.evaluate((element) => (element as HTMLImageElement).naturalWidth)).toBeGreaterThan(0)

    const taskId = page.url().split('/').at(-1)!
    const detail = await getTask(request, taskId)
    expect(detail.generation_type).toBe('TEXT_TO_IMAGE')
    expect(detail.result?.download_url).toMatch(/^\/api\/v1\/assets\/.+\/download$/)
    expect(detail.result?.width).toBeGreaterThan(0)
    await expect(resultImage).toHaveAttribute('src', detail.result!.download_url)
    const download = await request.get(new URL(detail.result!.download_url, baseURL).toString())
    expect(download.status()).toBe(200)
    expect(download.headers()['content-type']).toContain('image/png')
    await captureScreenshot(page, testInfo, 'text-detail-complete.png')

    const browserDownload = page.waitForEvent('download')
    await page.getByRole('link', { name: '下载结果' }).click()
    const savedResult = await browserDownload
    expect(savedResult.suggestedFilename()).toBe('museflow-result.png')
    expect(await savedResult.failure()).toBeNull()
    const savedPath = await savedResult.path()
    expect(savedPath).toBeTruthy()
    const savedBytes = await readFile(savedPath!)
    expect(savedBytes.subarray(0, 8)).toEqual(Buffer.from([137, 80, 78, 71, 13, 10, 26, 10]))
    await expect(page.getByRole('img', { name: '生成结果预览' })).toBeVisible()

    await page.reload()
    await expect(page.getByRole('heading', { name: '创作已完成' })).toBeVisible()
    await page.getByRole('link', { name: '返回任务历史' }).click()
    await expect(page).toHaveURL(/\/tasks$/)
    await page.getByPlaceholder('搜索已加载的提示词').fill(prompt)
    await expect(page.getByRole('heading', { name: prompt })).toBeVisible()
    await page.getByPlaceholder('搜索已加载的提示词').fill('')
    await page.setViewportSize({ width: 390, height: 844 })
    await captureScreenshot(page, testInfo, 'history-mobile.png')
    await expectBrowserHealthy(page, health)
  })

  test('uploads one real image, creates image-to-image, filters it in history, and keeps MinIO private', async ({ page, request }, testInfo) => {
    const health = noteBrowserHealth(page)
    const prompt = `${runId}:图生图:清晨的蓝色木屋`
    const image = referencePng()
    await page.goto('/tasks/new')
    await page.getByRole('radio', { name: /图生图/ }).check()
    await page.getByLabel('参考图片文件').setInputFiles({ name: 'reference.png', mimeType: 'image/png', buffer: image })
    const localPreview = page.getByRole('img', { name: '所选参考图片预览' })
    await expect(localPreview).toHaveAttribute('src', /^blob:/)
    await expect(localPreview).toBeVisible()
    await captureScreenshot(page, testInfo, 'image-local-preview.png')
    await page.getByLabel('创作提示词').fill(prompt)

    await page.route('**/api/v1/assets', async (route) => {
      await new Promise((resolveDelay) => setTimeout(resolveDelay, 700))
      await route.continue()
    })
    const uploadResponse = page.waitForResponse((response) => response.url().endsWith('/api/v1/assets') && response.request().method() === 'POST')
    await page.getByRole('button', { name: '上传参考图片' }).click()
    await expect(page.getByText('正在上传参考图片…')).toBeVisible()
    await captureScreenshot(page, testInfo, 'image-uploading.png')
    const uploaded = await uploadResponse
    await page.unroute('**/api/v1/assets')
    expect(uploaded.status()).toBe(201)
    const asset = await uploaded.json() as { asset_id: string; status: string; download_url: string }
    expect(asset.status).toBe('READY')
    expect(asset.download_url).toMatch(new RegExp(`^/api/v1/assets/${asset.asset_id}/download$`))
    const uploadKey = uploaded.request().headers()['idempotency-key']
    expect(uploadKey).toBeTruthy()
    await expect(page.getByText('上传完成')).toBeVisible()
    await expect(page.getByRole('img', { name: '所选参考图片预览' })).toHaveAttribute('src', /^blob:/)

    const replay = await request.post('/api/v1/assets', {
      headers: { 'Idempotency-Key': uploadKey! },
      multipart: { file: { name: 'reference.png', mimeType: 'image/png', buffer: image } },
    })
    expect(replay.status()).toBe(200)
    expect((await replay.json() as typeof asset).asset_id).toBe(asset.asset_id)
    const conflict = await request.post('/api/v1/assets', {
      headers: { 'Idempotency-Key': uploadKey! },
      multipart: { file: { name: 'different.png', mimeType: 'image/png', buffer: referencePng([180, 70, 30]) } },
    })
    expect(conflict.status()).toBe(409)
    expect((await conflict.json()).error.code).toBe('IDEMPOTENCY_KEY_CONFLICT')

    const createResponsePromise = page.waitForResponse((response) => response.url().endsWith('/api/v1/tasks') && response.request().method() === 'POST')
    await page.getByRole('button', { name: '开始创作' }).click()
    const createdResponse = await createResponsePromise
    expect(createdResponse.status()).toBe(201)
    const createdResponseBody = await createdResponse.json() as TaskBody
    const createRequest = createdResponse.request()
    const createKey = createRequest.headers()['idempotency-key']
    const body = createRequest.postDataJSON() as Record<string, unknown>
    expect(body).toMatchObject({ generation_type: 'IMAGE_TO_IMAGE', reference_asset_id: asset.asset_id })
    expect(body).not.toHaveProperty('reference_sha256')
    expect(createKey).toBeTruthy()
    expect(createKey).not.toBe(uploadKey)
    await expect(page).toHaveURL(new RegExp(`/tasks/${createdResponseBody.id}$`))
    await expect(page.getByRole('heading', { name: '创作已完成' })).toBeVisible({ timeout: 75_000 })
    const created = await getTask(request, createdResponseBody.id)
    expect(created.result?.download_url).toBeTruthy()
    await expect(page.getByRole('img', { name: '图生图参考素材' })).toBeVisible()
    const resultImage = page.getByRole('img', { name: '生成结果预览' })
    await expect(resultImage).toBeVisible()
    await expect.poll(() => resultImage.evaluate((element) => (element as HTMLImageElement).naturalWidth)).toBeGreaterThan(0)
    await expect(resultImage).toHaveAttribute('src', /^\/api\/v1\/assets\/.+\/download$/)
    const displayedImageUrls = await page.locator('img').evaluateAll((images) => images.map((image) => image.getAttribute('src') ?? ''))
    expect(displayedImageUrls.every((url) => url.startsWith('/api/v1/assets/'))).toBe(true)
    await expect(page.locator('body')).not.toContainText('minio')
    await captureScreenshot(page, testInfo, 'image-detail-complete.png')
    await page.reload()
    await expect(page.getByRole('img', { name: '图生图参考素材' })).toBeVisible()
    await expect(page.getByRole('img', { name: '生成结果预览' })).toBeVisible()

    const stableDownload = await request.get(new URL(created.result!.download_url, baseURL).toString(), { maxRedirects: 0 })
    expect(stableDownload.status()).toBe(307)
    const signedDownload = new URL(stableDownload.headers().location!)
    expect(signedDownload.hostname).toBe('127.0.0.1')
    signedDownload.search = ''
    const anonymousObject = await request.get(signedDownload.toString())
    expect(anonymousObject.status()).toBe(403)
    const stableReference = await request.get(new URL(asset.download_url, baseURL).toString())
    expect(stableReference.status()).toBe(200)
    expect(stableReference.headers()['content-type']).toContain('image/png')
    const anonymousBucket = await request.get(`${minioURL}/museflow-results/`)
    expect([403, 404]).toContain(anonymousBucket.status())

    const browserDownload = page.waitForEvent('download')
    await page.getByRole('link', { name: '下载结果' }).click()
    const savedResult = await browserDownload
    expect(savedResult.suggestedFilename()).toBe('museflow-result.png')
    expect(await savedResult.failure()).toBeNull()
    const savedPath = await savedResult.path()
    expect(savedPath).toBeTruthy()
    const savedBytes = await readFile(savedPath!)
    expect(savedBytes.subarray(0, 8)).toEqual(Buffer.from([137, 80, 78, 71, 13, 10, 26, 10]))
    await expect(resultImage).toBeVisible()

    await page.getByRole('link', { name: '返回任务历史' }).click()
    await page.getByLabel('生成方式').selectOption('IMAGE_TO_IMAGE')
    await page.getByLabel('状态').selectOption('SUCCEEDED')
    await expect(page.getByRole('heading', { name: prompt })).toBeVisible()
    await expect(page.getByText('含参考素材')).toBeVisible()
    await expect(page.getByRole('img', { name: '生成结果缩略图' }).first()).toHaveAttribute('src', /^\/api\/v1\/assets\/.+\/download$/)
    await page.setViewportSize({ width: 390, height: 844 })
    await captureScreenshot(page, testInfo, 'image-history-mobile.png')
    await expectBrowserHealthy(page, health)
  })

  test('shows unsupported, corrupt, and oversized upload errors from browser checks and the real API', async ({ page }, testInfo) => {
    const health = noteBrowserHealth(page)
    await page.goto('/tasks/new')
    await page.getByRole('radio', { name: /图生图/ }).check()
    const input = page.getByLabel('参考图片文件')

    await input.setInputFiles({ name: 'notes.txt', mimeType: 'text/plain', buffer: Buffer.from('not an image') })
    await expect(page.getByRole('alert')).toContainText('只支持 PNG、JPEG 和 WebP')
    await captureScreenshot(page, testInfo, 'upload-non-image-error.png')
    await page.getByRole('button', { name: '替换图片' }).click()
    await input.setInputFiles({ name: 'vector.svg', mimeType: 'image/svg+xml', buffer: Buffer.from('<svg xmlns="http://www.w3.org/2000/svg"/>') })
    await expect(page.getByRole('alert')).toContainText('只支持 PNG、JPEG 和 WebP')

    await page.getByRole('button', { name: '替换图片' }).click()
    await input.setInputFiles({ name: 'broken.png', mimeType: 'image/png', buffer: Buffer.from([137, 80, 78, 71, 13, 10, 26, 10, 0, 1, 2]) })
    await page.getByRole('button', { name: '上传参考图片' }).click()
    await expect(page.getByRole('alert')).toContainText('图片无法完整读取')
    await captureScreenshot(page, testInfo, 'upload-corrupt-image-error.png')

    await page.getByRole('button', { name: '替换图片' }).click()
    await input.setInputFiles({ name: 'large.png', mimeType: 'image/png', buffer: Buffer.alloc(6_000_001, 1) })
    await expect(page.getByText('文件超过 6 MB 提示限制')).toBeVisible()
    await page.getByRole('button', { name: '上传参考图片' }).click()
    await expect(page.getByRole('alert')).toContainText('图片超过服务端允许的大小限制', { timeout: 30_000 })
    await expectBrowserHealthy(page, health, [
      { status: 422, path: '/api/v1/assets' },
      { status: 413, path: '/api/v1/assets' },
    ])
  })

  test('retains an uploaded asset when task creation fails and retries with the same task key', async ({ page }, testInfo) => {
    const health = noteBrowserHealth(page)
    const prompt = `${runId}:create-retry:雪夜木屋`
    const image = referencePng([140, 80, 180])
    let createAttempts = 0
    const createKeys: string[] = []
    let uploadCount = 0
    page.on('response', (response) => {
      if (response.url().endsWith('/api/v1/assets') && response.request().method() === 'POST') uploadCount += 1
    })
    await page.route('**/api/v1/tasks', async (route) => {
      if (route.request().method() !== 'POST') return route.continue()
      createAttempts += 1
      createKeys.push(route.request().headers()['idempotency-key'] ?? '')
      if (createAttempts === 1) {
        await route.fulfill({
          status: 503,
          contentType: 'application/json',
          body: JSON.stringify({ error: { code: 'SERVICE_NOT_READY', message: 'sensitive backend detail is hidden', request_id: crypto.randomUUID() } }),
        })
      } else {
        await route.continue()
      }
    })

    await page.goto('/tasks/new')
    await page.getByRole('radio', { name: /图生图/ }).check()
    await page.getByLabel('参考图片文件').setInputFiles({ name: 'retry.png', mimeType: 'image/png', buffer: image })
    await page.getByLabel('创作提示词').fill(prompt)
    await page.getByRole('button', { name: '上传参考图片' }).click()
    await expect(page.getByText('上传完成')).toBeVisible()
    await page.getByRole('button', { name: '开始创作' }).click()
    await expect(page.getByRole('alert')).toContainText('创作服务仍在启动')
    await expect(page.getByText('上传完成')).toBeVisible()
    await captureScreenshot(page, testInfo, 'image-create-transient-error.png')
    await page.getByRole('button', { name: '重试创建任务' }).click()
    await expect(page).toHaveURL(/\/tasks\/[0-9a-f-]+$/)
    await expect(page.getByRole('heading', { name: '创作已完成' })).toBeVisible({ timeout: 75_000 })
    expect(createAttempts).toBe(2)
    expect(createKeys[0]).toBeTruthy()
    expect(createKeys[1]).toBe(createKeys[0])
    expect(uploadCount).toBe(1)
    expect(await page.locator('[aria-label="参考素材"] img').getAttribute('src')).toMatch(/^\/api\/v1\/assets\/.+\/download$/)
    await captureScreenshot(page, testInfo, 'image-create-retry-complete.png')
    await expectBrowserHealthy(page, health, [{ status: 503, path: '/api/v1/tasks' }])
  })

  test('recovers transient failures and creates an immutable manual retry chain after permanent failure', async ({ page, request }, testInfo) => {
    test.setTimeout(100_000)
    const health = noteBrowserHealth(page)
    const transient = await createDemoTask(request, 'transient_then_success', `${runId}:transient`)
    const recovered = await waitForStatus(request, transient.id, 'SUCCEEDED')
    expect(recovered.events.map((event) => event.type)).toContain('TASK_RETRY_WAIT')
    expect(recovered.attempts.length).toBeGreaterThanOrEqual(2)
    await page.goto(`/tasks/${transient.id}`)
    await expect(page.getByRole('heading', { name: '创作已完成' })).toBeVisible()
    await expect(page.getByText('等待自动重试')).toBeVisible()
    await captureScreenshot(page, testInfo, 'transient-recovery-timeline.png')

    const permanent = await createDemoTask(request, 'permanent_failure', `${runId}:permanent`)
    await waitForStatus(request, permanent.id, 'FAILED')
    await page.goto(`/tasks/${permanent.id}`)
    await expect(page.getByText('PROVIDER_REJECTED')).toBeVisible()
    await expect(page.getByText('生成服务永久拒绝了本次请求。')).toBeVisible()
    await expect(page.getByRole('button', { name: '手动重试' })).toBeVisible()
    await captureScreenshot(page, testInfo, 'permanent-failure-detail.png')

    const exhausted = await createDemoTask(request, 'rate_limited', `${runId}:manual-retry`)
    const failed = await waitForStatus(request, exhausted.id, 'FAILED')
    expect(failed.error_code).toBe('RETRY_EXHAUSTED')
    await page.goto(`/tasks/${exhausted.id}`)
    await expect(page.getByRole('button', { name: '手动重试' })).toBeVisible()
    const originalPrompt = await page.locator('.detail-header > div > p').textContent()
    await page.getByRole('button', { name: '手动重试' }).click()
    await expect(page).toHaveURL(new RegExp(`/tasks/(?!${exhausted.id})[0-9a-f-]+$`))
    await expect(page.locator('.retry-lineage')).toContainText(exhausted.id)
    await expect(page.getByRole('link', { name: '查看后续手动重试任务' })).toHaveCount(0)
    const childId = page.url().split('/').at(-1)!
    const child = await getTask(request, childId)
    expect(child.retried_from_task_id).toBe(exhausted.id)
    expect(child.prompt).toBe(originalPrompt)
    await captureScreenshot(page, testInfo, 'manual-retry-child.png')
    await page.goto(`/tasks/${exhausted.id}`)
    await expect(page.getByRole('link', { name: '查看后续手动重试任务' })).toBeVisible()
    await expect(page.getByRole('button', { name: '手动重试' })).toHaveCount(0)
    await expectBrowserHealthy(page, health)
  })

  test('uses backend cursors for bounded history pagination and combined filters', async ({ page, request }, testInfo) => {
    const prefix = `${runId}:history`
    const created = await Promise.all(Array.from({ length: 11 }, (_, index) => createDemoTask(request, 'success', `${prefix}:${index}`)))
    await Promise.all(created.map((task) => waitForStatus(request, task.id, 'SUCCEEDED')))
    const health = noteBrowserHealth(page)
    await page.goto('/tasks')
    await page.getByLabel('生成方式').selectOption('TEXT_TO_IMAGE')
    await page.getByLabel('状态').selectOption('SUCCEEDED')
    await expect(page.getByRole('button', { name: '载入更多' })).toBeVisible()
    const firstPageIds = await page.locator('a.task-card[href^="/tasks/"]').evaluateAll((links) => links
      .map((link) => link.getAttribute('href')!)
      .filter((href) => href !== '/tasks/new'))
    expect(firstPageIds.length).toBe(9)
    await page.getByRole('button', { name: '载入更多' }).click()
    await expect(page.getByRole('heading', { name: `${prefix}:0`, exact: true })).toBeVisible()
    await expect(page.getByRole('heading', { name: `${prefix}:1`, exact: true })).toBeVisible()
    const allVisibleIds = await page.locator('a.task-card[href^="/tasks/"]').evaluateAll((links) => links
      .map((link) => link.getAttribute('href')!)
      .filter((href) => href !== '/tasks/new'))
    expect(new Set(allVisibleIds).size).toBe(allVisibleIds.length)
    expect(allVisibleIds.length).toBeGreaterThan(firstPageIds.length)
    await page.getByPlaceholder('搜索已加载的提示词').fill(`${prefix}:0`)
    await expect(page.getByRole('heading', { name: `${prefix}:0`, exact: true })).toBeVisible()
    await expect(page.getByRole('heading', { name: `${prefix}:1`, exact: true })).toHaveCount(0)
    await captureScreenshot(page, testInfo, 'history-filtered-desktop.png')
    await expectBrowserHealthy(page, health)
  })
})
