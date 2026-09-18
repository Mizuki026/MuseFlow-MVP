import { useState } from 'react'
import { useForm } from 'react-hook-form'
import { useNavigate } from 'react-router-dom'
import { ApiError } from '../api/client'
import { useCreateTask } from '../features/tasks/taskQueries'

type FormValues = { prompt: string }

function errorMessage(error: unknown): string {
  if (!(error instanceof ApiError)) return '创建失败，请稍后重试。'
  const messages: Record<string, string> = {
    IDEMPOTENCY_KEY_CONFLICT: '这次提交与之前的内容不一致，请修改提示词后再试。',
    INVALID_PROMPT: '提示词需要包含有效内容，且不能超过 2000 个字符。',
    INVALID_REQUEST: '提交内容无法识别，请检查后重试。',
    SERVICE_NOT_READY: '创作服务仍在启动，请稍后重试。',
  }
  return messages[error.code] ?? (error.status === 503 ? '创作服务暂时不可用，请稍后重试。' : '创建失败，请稍后重试。')
}

export function NewTaskPage() {
  const navigate = useNavigate()
  const mutation = useCreateTask()
  const [intent, setIntent] = useState<{ prompt: string; key: string }>()
  const { register, handleSubmit, formState: { errors } } = useForm<FormValues>({
    defaultValues: { prompt: '' },
  })

  const submit = handleSubmit(async ({ prompt }) => {
    const currentIntent = intent?.prompt === prompt
      ? intent
      : { prompt, key: crypto.randomUUID() }
    setIntent(currentIntent)
    try {
      const task = await mutation.mutateAsync(currentIntent)
      navigate(`/tasks/${task.id}`, { state: { replayed: task.idempotency_replayed } })
    } catch {
      // The mutation state renders the normalized, user-safe error message.
    }
  })

  return (
    <section className="create-page page-enter">
      <div className="create-heading">
        <p className="eyebrow">TEXT TO IMAGE · ONE RESULT</p>
        <h1>把脑海里的画面，<br />交给可靠的创作流程。</h1>
        <p>描述你想看到的场景。MuseFlow 会持续执行、自动恢复，并保存一张可验证的结果。</p>
      </div>
      <form className="prompt-composer" onSubmit={(event) => void submit(event)} noValidate>
        <label htmlFor="prompt">创作提示词</label>
        <textarea
          id="prompt"
          placeholder="例如：雨后清晨的山谷，一座极简木屋映在薄雾中…"
          aria-describedby="prompt-help prompt-error"
          {...register('prompt', {
            required: '请输入提示词。',
            validate: (value) => value.trim().length > 0 || '提示词不能只有空格。',
            maxLength: { value: 2000, message: '提示词不能超过 2000 个字符。' },
          })}
        />
        <div className="composer-footer">
          <div className="strategy" id="prompt-help">
            <span><i aria-hidden="true" />单图</span>
            <span><i aria-hidden="true" />1280 × 1280</span>
          </div>
          <button className="button primary" type="submit" disabled={mutation.isPending}>
            {mutation.isPending ? '正在创建…' : '开始创作'}
          </button>
        </div>
        {errors.prompt && <p className="field-error" id="prompt-error" role="alert">{errors.prompt.message}</p>}
        {mutation.isError && <p className="form-error" role="alert">{errorMessage(mutation.error)}</p>}
      </form>
      <p className="trust-note"><span aria-hidden="true">✦</span> 提交后可以安全离开页面，任务状态会被持久保存。</p>
    </section>
  )
}
