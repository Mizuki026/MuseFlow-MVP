import { useRef } from 'react'
import { Link, useLocation, useNavigate, useParams } from 'react-router-dom'
import { resultUrl } from '../api/client'
import { ErrorState, LoadingState } from '../components/QueryState'
import { StatusBadge } from '../components/StatusBadge'
import {
  eventLabels,
  formatBytes,
  formatDate,
  phaseLabels,
  statusLabels,
} from '../features/tasks/presentation'
import { useRetryTask, useTask } from '../features/tasks/taskQueries'

export function TaskDetailPage() {
  const { taskId = '' } = useParams()
  const navigate = useNavigate()
  const location = useLocation()
  const query = useTask(taskId)
  const retry = useRetryTask(taskId)
  const retryKey = useRef(crypto.randomUUID())

  if (query.isPending) return <LoadingState label="正在恢复任务状态…" />
  if (query.isError || !query.data) return <ErrorState onRetry={() => void query.refetch()} />

  const task = query.data
  const latestAttempt = task.attempts.at(-1)
  const previewUrl = resultUrl(task.result?.download_url)
  const replayed = Boolean((location.state as { replayed?: boolean } | null)?.replayed)

  const retryTask = async () => {
    try {
      const next = await retry.mutateAsync(retryKey.current)
      navigate(`/tasks/${next.id}`)
    } catch {
      // The mutation state renders a retry eligibility message.
    }
  }

  return (
    <article className="detail-page page-enter">
      <header className="detail-header">
        <div>
          <Link className="back-link" to="/tasks">← 返回任务历史</Link>
          <div className="title-row">
            <StatusBadge status={task.status} />
            <span className="task-id">#{task.id.slice(0, 8)}</span>
          </div>
          <h1>{task.status === 'SUCCEEDED' ? '创作已完成' : statusLabels[task.status]}</h1>
          <p>{task.prompt}</p>
        </div>
        <Link className="button secondary" to="/tasks/new">创建新任务</Link>
      </header>

      {replayed && <p className="notice" role="status">已恢复此前相同的提交，没有创建重复任务。</p>}

      <div className="detail-grid">
        <section className={`result-stage result-${task.status.toLowerCase()}`} aria-label="任务结果">
          {previewUrl ? (
            <img src={previewUrl} alt="生成结果预览" />
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
          <p className="eyebrow">CURRENT RUN</p>
          <dl className="fact-list">
            <div><dt>当前阶段</dt><dd>{latestAttempt ? phaseLabels[latestAttempt.phase] ?? latestAttempt.phase : statusLabels[task.status]}</dd></div>
            <div><dt>Attempt</dt><dd>{latestAttempt?.sequence ?? 0} / {task.max_attempts}</dd></div>
            <div><dt>下次重试</dt><dd>{formatDate(task.next_attempt_at)}</dd></div>
            <div><dt>任务截止</dt><dd>{formatDate(task.deadline_at)}</dd></div>
          </dl>

          {task.error_code && (
            <div className="error-card" role="alert">
              <strong>{task.error_code}</strong>
              <p>{task.error_message ?? '任务执行失败。'}</p>
            </div>
          )}

          {task.result && (
            <div className="result-meta">
              <p className="eyebrow">RESULT ASSET</p>
              <dl>
                <div><dt>格式</dt><dd>{task.result.content_type.replace('image/', '').toUpperCase()}</dd></div>
                <div><dt>大小</dt><dd>{formatBytes(task.result.size_bytes)}</dd></div>
                <div><dt>SHA-256</dt><dd title={task.result.sha256}>{task.result.sha256.slice(0, 12)}…</dd></div>
              </dl>
            </div>
          )}

          <div className="action-stack">
            {task.status === 'SUCCEEDED' && previewUrl && (
              <a className="button primary" href={previewUrl} download="museflow-result" target="_blank" rel="noreferrer">下载结果</a>
            )}
            {task.status === 'FAILED' && !task.retry_task_id && (
              <button className="button primary" type="button" disabled={retry.isPending} onClick={() => void retryTask()}>
                {retry.isPending ? '正在重试…' : '手动重试'}
              </button>
            )}
            {task.retry_task_id && <Link className="button secondary" to={`/tasks/${task.retry_task_id}`}>查看重试任务</Link>}
            {retry.isError && <p className="field-error" role="alert">无法创建重试任务，请检查该任务是否仍符合重试条件。</p>}
          </div>
        </aside>
      </div>

      <section className="timeline-section">
        <div className="section-heading"><div><p className="eyebrow">EXECUTION LOG</p><h2>事件时间线</h2></div><span>{task.events.length} 个事件</span></div>
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
