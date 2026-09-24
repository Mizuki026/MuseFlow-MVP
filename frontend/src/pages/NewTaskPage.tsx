import { useEffect, useRef, useState } from 'react'
import { useForm } from 'react-hook-form'
import { useNavigate } from 'react-router-dom'
import { messageForError } from '../api/errorMessages'
import type { CreateTaskInput, GenerationType } from '../api/types'
import {
  pendingUploadIntent,
  referenceAssetId,
  useReferenceUpload,
  type PendingUploadIntent,
} from '../features/tasks/referenceUpload'
import { ReferenceAssetPicker } from '../features/tasks/ReferenceAssetPicker'
import { useCreateTask } from '../features/tasks/taskQueries'

const TEXT_TO_IMAGE = 'TEXT_TO_IMAGE' satisfies GenerationType
const IMAGE_TO_IMAGE = 'IMAGE_TO_IMAGE' satisfies GenerationType
const SIZE_PRESET = '1280*1280'
const CREATE_DRAFT_KEY = 'museflow:create-task:v1'

type FormValues = { prompt: string }
type CreateDraft = {
  prompt: string
  generationType: GenerationType
  sizePreset: string
  taskKey: string
  assetId?: string
  pendingUpload?: PendingUploadIntent
}

function isGenerationType(value: unknown): value is GenerationType {
  return value === TEXT_TO_IMAGE || value === IMAGE_TO_IMAGE
}

function readDraft(): CreateDraft {
  try {
    const raw = sessionStorage.getItem(CREATE_DRAFT_KEY)
    if (raw) {
      const value: unknown = JSON.parse(raw)
      if (value && typeof value === 'object') {
        const draft = value as Partial<CreateDraft>
        if (
          typeof draft.prompt === 'string'
          && isGenerationType(draft.generationType)
          && typeof draft.taskKey === 'string'
          && draft.taskKey.length > 0
        ) {
          return {
            prompt: draft.prompt,
            generationType: draft.generationType,
            sizePreset: typeof draft.sizePreset === 'string' ? draft.sizePreset : SIZE_PRESET,
            taskKey: draft.taskKey,
            ...(typeof draft.assetId === 'string' ? { assetId: draft.assetId } : {}),
            ...(isPendingUploadIntent(draft.pendingUpload) ? { pendingUpload: draft.pendingUpload } : {}),
          }
        }
      }
    }
  } catch {
    // An unavailable or malformed session draft starts a clean local form.
  }
  return {
    prompt: '',
    generationType: TEXT_TO_IMAGE,
    sizePreset: SIZE_PRESET,
    taskKey: crypto.randomUUID(),
  }
}

function isPendingUploadIntent(value: unknown): value is PendingUploadIntent {
  if (!value || typeof value !== 'object') return false
  const intent = value as Partial<PendingUploadIntent>
  return typeof intent.key === 'string'
    && typeof intent.name === 'string'
    && typeof intent.size === 'number'
    && typeof intent.lastModified === 'number'
    && typeof intent.contentType === 'string'
}

function fingerprintIntent(values: {
  prompt: string
  generationType: GenerationType
  sizePreset: string
  assetId?: string
  uploadKey?: string
}): string {
  return JSON.stringify(values)
}

export function NewTaskPage() {
  const navigate = useNavigate()
  const mutation = useCreateTask()
  const [draft] = useState(readDraft)
  const [generationType, setGenerationType] = useState(draft.generationType)
  const [pageError, setPageError] = useState<string>()
  const upload = useReferenceUpload(draft.pendingUpload, draft.assetId)
  // React Hook Form is the project's required form library; this page is not compiler-memoized.
  const { register, handleSubmit, watch, formState: { errors, isSubmitting } } = useForm<FormValues>({
    defaultValues: { prompt: draft.prompt },
  })
  // The React Compiler cannot memoize React Hook Form's watch function, which is used only for draft persistence.
  // oxlint-disable-next-line react/incompatible-library
  const prompt = watch('prompt') ?? ''
  const assetId = referenceAssetId(upload.state)
  const pending = pendingUploadIntent(upload.state)
  const uploadKey = generationType === IMAGE_TO_IMAGE ? pending?.key : undefined
  const currentFingerprint = fingerprintIntent({
    prompt,
    generationType,
    sizePreset: draft.sizePreset,
    ...(generationType === IMAGE_TO_IMAGE && assetId ? { assetId } : {}),
    ...(generationType === IMAGE_TO_IMAGE && !assetId && uploadKey ? { uploadKey } : {}),
  })
  const previousFingerprint = useRef(currentFingerprint)
  const createKey = useRef(draft.taskKey)
  const submitLock = useRef(false)

  useEffect(() => {
    if (previousFingerprint.current !== currentFingerprint) {
      previousFingerprint.current = currentFingerprint
      createKey.current = crypto.randomUUID()
      mutation.reset()
      setPageError(undefined)
    }
    const nextDraft: CreateDraft = {
      prompt,
      generationType,
      sizePreset: draft.sizePreset,
      taskKey: createKey.current,
      ...(assetId ? { assetId } : {}),
      ...(pending ? { pendingUpload: pending } : {}),
    }
    try {
      sessionStorage.setItem(CREATE_DRAFT_KEY, JSON.stringify(nextDraft))
    } catch {
      // The form remains usable if browser storage is unavailable.
    }
  }, [assetId, currentFingerprint, draft.sizePreset, generationType, mutation, pending, prompt])

  const submit = handleSubmit(async ({ prompt: submittedPrompt }) => {
    if (submitLock.current) return
    setPageError(undefined)

    let body: CreateTaskInput
    if (generationType === IMAGE_TO_IMAGE) {
      const uploadedAssetId = upload.state.status === 'UPLOADED' ? upload.state.asset.asset_id : undefined
      if (!uploadedAssetId) {
        setPageError('请先上传并完成校验参考图片，再创建图生图任务。')
        return
      }
      body = {
        prompt: submittedPrompt,
        size_preset: draft.sizePreset,
        generation_type: IMAGE_TO_IMAGE,
        reference_asset_id: uploadedAssetId,
      }
    } else {
      body = {
        prompt: submittedPrompt,
        size_preset: draft.sizePreset,
        generation_type: TEXT_TO_IMAGE,
      }
    }

    submitLock.current = true
    try {
      const task = await mutation.mutateAsync({ body, key: createKey.current })
      try { sessionStorage.removeItem(CREATE_DRAFT_KEY) } catch { /* ignore unavailable session storage */ }
      navigate(`/tasks/${task.id}`, { state: { replayed: task.idempotency_replayed } })
    } catch {
      // Stable, user-safe feedback is shown from the mutation state below.
    } finally {
      submitLock.current = false
    }
  })

  const isBusy = mutation.isPending || isSubmitting || upload.state.status === 'UPLOADING'
  const isRestoring = upload.state.status === 'RESTORING'

  return (
    <section className="create-page page-enter">
      <div className="create-heading">
        <p className="eyebrow">ONE IMAGE · RELIABLE GENERATION</p>
        <h1>把脑海里的画面，<br />交给可靠的创作流程。</h1>
        <p>选择生成方式，描述画面。MuseFlow 会保存任务、持续执行，并让结果可追溯。</p>
      </div>
      <form className="prompt-composer" onSubmit={(event) => void submit(event)} noValidate>
        <fieldset className="generation-choice" disabled={isBusy}>
          <legend>生成方式</legend>
          <label>
            <input
              type="radio"
              name="generationType"
              value={TEXT_TO_IMAGE}
              checked={generationType === TEXT_TO_IMAGE}
              onChange={() => { setGenerationType(TEXT_TO_IMAGE); setPageError(undefined) }}
            />
            <span><strong>文生图</strong><small>根据提示词生成一张图片</small></span>
          </label>
          <label>
            <input
              type="radio"
              name="generationType"
              value={IMAGE_TO_IMAGE}
              checked={generationType === IMAGE_TO_IMAGE}
              onChange={() => { setGenerationType(IMAGE_TO_IMAGE); setPageError(undefined) }}
            />
            <span><strong>图生图</strong><small>使用一张已校验的参考图片</small></span>
          </label>
        </fieldset>

        <label htmlFor="prompt">创作提示词</label>
        <textarea
          id="prompt"
          placeholder="例如：雨后清晨的山谷，一座极简木屋映在薄雾中…"
          aria-describedby="prompt-help prompt-error"
          disabled={isBusy}
          {...register('prompt', {
            required: '请输入提示词。',
            validate: (value) => value.trim().length > 0 || '提示词不能只有空格。',
            maxLength: { value: 2000, message: '提示词不能超过 2000 个字符。' },
          })}
        />

        {generationType === IMAGE_TO_IMAGE && <ReferenceAssetPicker upload={upload} />}

        <div className="composer-footer">
          <div className="strategy" id="prompt-help">
            <span><i aria-hidden="true" />单图</span>
            <span><i aria-hidden="true" />1280 × 1280</span>
            <span><i aria-hidden="true" />{generationType === IMAGE_TO_IMAGE ? '单张参考图' : '纯提示词'}</span>
          </div>
          <button className="button primary" type="submit" disabled={isBusy || isRestoring}>
            {isBusy ? '正在提交…' : isRestoring ? '正在恢复素材…' : mutation.isError ? '重试创建任务' : '开始创作'}
          </button>
        </div>
        {errors.prompt && <p className="field-error" id="prompt-error" role="alert">{errors.prompt.message}</p>}
        {pageError && <p className="form-error" role="alert">{pageError}</p>}
        {mutation.isError && <p className="form-error" role="alert">{messageForError(mutation.error, '创建任务失败，请检查输入或服务状态后重试。')}</p>}
        {mutation.isError && <p className="idempotency-note" role="status">输入没有变化，再次提交会沿用本次任务幂等标识。</p>}
      </form>
      <p className="trust-note"><span aria-hidden="true">✦</span> 刷新会恢复提示词、幂等标识和已上传素材 ID；浏览器不会保存原始图片文件。</p>
    </section>
  )
}
