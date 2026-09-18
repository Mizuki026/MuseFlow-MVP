import { useMemo, useState } from 'react'
import { Link } from 'react-router-dom'
import { resultUrl } from '../api/client'
import type { TaskStatus } from '../api/types'
import { ErrorState, LoadingState } from '../components/QueryState'
import { StatusBadge } from '../components/StatusBadge'
import { formatDate, statusLabels } from '../features/tasks/presentation'
import { useTaskHistory } from '../features/tasks/taskQueries'

export function TaskHistoryPage() {
  const [status, setStatus] = useState<TaskStatus | undefined>()
  const [search, setSearch] = useState('')
  const query = useTaskHistory(status)
  const items = useMemo(
    () => query.data?.pages.flatMap((page) => page.items) ?? [],
    [query.data],
  )
  const visibleItems = items.filter((task) => task.prompt.toLocaleLowerCase().includes(search.trim().toLocaleLowerCase()))

  return (
    <section className="history-page page-enter">
      <header className="history-header">
        <div><p className="eyebrow">CREATIVE ARCHIVE</p><h1>任务历史</h1><p>回看每一次创作，以及它如何抵达结果。</p></div>
        <Link className="button primary" to="/tasks/new">＋ 新建任务</Link>
      </header>
      <div className="history-tools">
        <label className="search-control"><span className="sr-only">搜索已加载任务</span><input value={search} onChange={(event) => setSearch(event.target.value)} placeholder="搜索已加载的提示词" /></label>
        <label className="filter-control"><span>状态</span><select value={status ?? ''} onChange={(event) => setStatus((event.target.value || undefined) as TaskStatus | undefined)}><option value="">全部</option>{(Object.entries(statusLabels) as [TaskStatus, string][]).map(([value, label]) => <option key={value} value={value}>{label}</option>)}</select></label>
      </div>

      {query.isPending ? <LoadingState /> : query.isError ? <ErrorState onRetry={() => void query.refetch()} /> : visibleItems.length === 0 ? (
        <div className="empty-state">
          <span className="empty-orb" aria-hidden="true" />
          <p className="eyebrow">YOUR FIRST FRAME</p>
          <h2>{items.length ? '没有匹配的任务' : '从第一幅画面开始'}</h2>
          <p>{items.length ? '换一个关键词或状态看看。' : '创建任务后，执行状态与结果都会保存在这里。'}</p>
          {!items.length && <Link className="button primary" to="/tasks/new">开始创作</Link>}
        </div>
      ) : (
        <>
          <div className="task-grid">
            <Link className="task-card new-task-card" to="/tasks/new"><span>＋</span><strong>开始新的创作</strong><small>单图 · 1280 × 1280</small></Link>
            {visibleItems.map((task) => {
              const thumbnail = resultUrl(task.thumbnail_url)
              return (
                <Link className="task-card" to={`/tasks/${task.id}`} key={task.id}>
                  <div className="thumbnail">{thumbnail ? <img src={thumbnail} alt="" loading="lazy" /> : <span className={`thumbnail-state state-${task.status.toLowerCase()}`} />}</div>
                  <div className="card-body"><StatusBadge status={task.status} /><h2>{task.prompt}</h2><time dateTime={task.created_at}>{formatDate(task.created_at)}</time></div>
                </Link>
              )
            })}
          </div>
          {query.hasNextPage && <div className="load-more"><button className="button secondary" type="button" disabled={query.isFetchingNextPage} onClick={() => void query.fetchNextPage()}>{query.isFetchingNextPage ? '正在载入…' : '载入更多'}</button></div>}
        </>
      )}
    </section>
  )
}
