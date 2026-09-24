import { useState } from 'react'
import { Link, useLocation, useNavigate, useParams } from 'react-router-dom'
import { describeTaskError, messageForError } from '../api/errorMessages'
import { referenceAssetUrl, resultUrl } from '../api/client'
import { ErrorState, LoadingState } from '../components/QueryState'
import { StatusBadge } from '../components/StatusBadge'
import {
  eventLabels,
  formatBytes,
  formatDate,
  generationTypeLabels,
  phaseLabels,
  statusLabels,
} from '../features/tasks/presentation'
import { useRetryTask, useTask } from '../features/tasks/taskQueries'

function retryKeyFor(taskId: string): string {
  const storageKey = `museflow:retry:${taskId}`
  try {
    const saved = sessionStorage.getItem(storageKey)
    if (saved) return saved
    const key = crypto.randomUUID()
    sessionStorage.setItem(storageKey, key)
    return key
  } catch {
    return crypto.randomUUID()
  }
}

function StableAssetImage({ src, alt, className }: { src: string; alt: string; className?: string }) {
  const [failedSrc, setFailedSrc] = useState<string>()
  if (failedSrc === src) return <div className="asset-unavailable" role="status">图片暂时无法访问，任务信息仍可查看。</div>
  return <img className={className} src={src} alt={alt} onError={() => setFailedSrc(src)} />
}

export function TaskDetailPage() {
  const { taskId = '' } = useParams()
  const navigate = useNavigate()
  const location = useLocation()
  const query = useTask(taskId)
  const retry = useRetryTask(taskId)

  if (query.isPending) return <LoadingState label="正在恢复任务状态…" />
  if (query.isError || !query.data) return <ErrorState onRetry={() => void query.refetch()} />

  const task = query.data
  const latestAttempt = task.attempts.at(-1)
  const resultImageUrl = resultUrl(task.result?.download_url)
  const referenceImageUrl = referenceAssetUrl(task.reference_asset_id)
  const replayed = Boolean((location.state as { replayed?: boolean } | null)?.replayed)
  const generationType = task.generation_type ?? task.input_summary?.generation_type
  const generationLabel = generationType ? generationTypeLabels[generationType] : '生成方式未知'

  const retryTask = async () => {
    try {
      const next = await retry.mutateAsync(retryKeyFor(taskId))
      navigate(`/tasks/${next.id}`)
    } catch {
      // The stable retry key remains in session storage after unknown outcomes.
    }
  }

  return (
    <article className="detail-page page-enter">
      <header className="detail-header">
        <div>
          <Link className="back-link" to="/tasks">← 返回任务历史</Link>
          <div className="title-row">
            <StatusBadge status={task.status} />
            <span className="generation-badge">{generationLabel}</span>
            <span className="task-id" title={task.id}>#{task.id.slice(0, 8)}</span>
          </div>
          <h1>{task.status === 'SUCCEEDED' ? '创作已完成' : statusLabels[task.status]}</h1>
          <p>{task.prompt}</p>
        </div>
        <Link className="button secondary" to="/tasks/new">创建新任务</Link>
      </header>

      {replayed && <p className="notice" role="status">已恢复此前相同的提交，没有创建重复任务。</p>}
      {task.retried_from_task_id && <p className="notice retry-lineage">此任务由 <Link to={`/tasks/${task.retried_from_task_id}`}>#{task.retried_from_task_id}</Link> 手动重试创建；原任务保持不变。</p>}

      <div className="detail-grid">
        <section className={`result-stage result-${task.status.toLowerCase()}`} aria-label="任务结果">
          {resultImageUrl ? (
            <StableAssetImage src={resultImageUrl} alt="生成结果预览" />
          ) : (
            <div className="result-placeholder">
              <span className="orb" aria-hidden="true" />
              <p>{task.status === 'FAILED' ? '本次创作没有生成结果' : '结果准备中'}</p>
              <small>{latestAttempt ? phaseLabels[latestAttempt.phase] ?? latestAttempt.phase : '等待 Worker 领取任务'}</small>
            </div>
          )}
          {task.status === 'RETRY_WAIT' && <div className="retry-ribbon">系统将在 {formatDate(task.next_attempt_at)} 自动重试</div>}
        </section>

        <aside className="task-inspector" aria-label="任务信息">
          <p className="eyebrow">TASK SNAPSHOT</p>
          <dl className="fact-list">
            <div><dt>生成方式</dt><dd>{generationLabel}</dd></div>
            <div><dt>尺寸</dt><dd>{task.size_preset}</dd></div>
            <div><dt>当前阶段</dt><dd>{latestAttempt ? phaseLabels[latestAttempt.phase] ?? latestAttempt.phase : statusLabels[task.status]}</dd></div>
            <div><dt>Attempt</dt><dd>{latestAttempt?.sequence ?? 0} / {task.max_attempts}</dd></div>
            <div><dt>下次自动重试</dt><dd>{formatDate(task.next_attempt_at)}</dd></div>
            <div><dt>任务截止</dt><dd>{formatDate(task.deadline_at)}</dd></div>
          </dl>

          {referenceImageUrl && (
            <section className="reference-detail" aria-label="参考素材">
              <p className="eyebrow">REFERENCE ASSET</p>
              <StableAssetImage src={referenceImageUrl} alt="图生图参考素材" />
              <p className="asset-id" title={task.reference_asset_id ?? undefined}>素材 ID：{task.reference_asset_id}</p>
            </section>
          )}

          <section className="provider-detail" aria-label="Provider 快照">
            <p className="eyebrow">PROVIDER SNAPSHOT</p>
            <dl className="fact-list">
              <div><dt>Profile</dt><dd>{task.provider_profile ?? '未知'}</dd></div>
              <div><dt>Provider / Model</dt><dd>{[task.provider_name, task.model_name].filter(Boolean).join(' / ') || '未知'}</dd></div>
              <div><dt>Capability</dt><dd>{task.capability_version ?? '未知'}</dd></div>
            </dl>
          </section>

          {task.error_code && (
            <div className="error-card" role="alert">
              <strong>{task.error_code}</strong>
              <p>{describeTaskError(task.error_code)}</p>
            </div>
          )}

          {task.result && (
            <div className="result-meta">
              <p className="eyebrow">RESULT ASSET</p>
              <dl>
                <div><dt>实际宽高</dt><dd>{task.result.width ?? '未知'} × {task.result.height ?? '未知'}</dd></div>
                <div><dt>媒体类型</dt><dd>{task.result.content_type}</dd></div>
                <div><dt>大小</dt><dd>{formatBytes(task.result.size_bytes)}</dd></div>
                <div><dt>SHA-256</dt><dd title={task.result.sha256}>{task.result.sha256.slice(0, 12)}…</dd></div>
              </dl>
            </div>
          )}

          <div className="action-stack">
            {task.status === 'SUCCEEDED' && resultImageUrl && (
              <a className="button primary" href={resultImageUrl} download="museflow-result">下载结果</a>
            )}
            {task.status === 'FAILED' && !task.retry_task_id && (
              <>
                <button className="button primary" type="button" disabled={retry.isPending} onClick={() => void retryTask()}>
                  {retry.isPending ? '正在创建新任务…' : '手动重试'}
                </button>
                <p className="retry-help">使用此任务的固定输入创建一条新任务，不会修改原任务。</p>
              </>
            )}
            {task.retry_task_id && <Link className="button secondary" to={`/tasks/${task.retry_task_id}`}>查看后续手动重试任务</Link>}
            {retry.isError && <p className="field-error" role="alert">{messageForError(retry.error, '无法创建重试任务，请检查该任务是否仍符合重试条件。')}</p>}
          </div>
        </aside>
      </div>

      <section className="timeline-section">
        <div className="section-heading"><div><p className="eyebrow">EXECUTION LOG</p><h2>Attempt 与事件时间线</h2></div><span>{task.events.length} 个事件 · {task.attempts.length} 次执行</span></div>
        {task.attempts.length > 0 && (
          <ol className="attempt-list" aria-label="Attempt 执行记录">
            {task.attempts.map((attempt) => (
              <li key={attempt.id}>
                <strong>Attempt {attempt.sequence}</strong>
                <span>{attempt.status} · {phaseLabels[attempt.phase] ?? attempt.phase}</span>
                <small>{attempt.provider_name} · {formatDate(attempt.started_at)} – {formatDate(attempt.finished_at)}</small>
              </li>
            ))}
          </ol>
        )}
        {task.events.length === 0 ? (
          <div className="empty-inline">任务尚未产生执行事件。</div>
        ) : (
          <ol className="timeline">
            {task.events.map((event) => (
              <li key={event.id}>
                <span className="timeline-dot" aria-hidden="true" />
                <div><strong>{eventLabels[event.type] ?? event.type}</strong><time dateTime={event.created_at}>{formatDate(event.created_at)}</time></div>
              </li>
            ))}
          </ol>
        )}
      </section>
    </article>
  )
}
