import { useMemo, useState } from 'react'
import { Link, useSearchParams } from 'react-router-dom'
import { resultUrl } from '../api/client'
import type { GenerationType, TaskStatus } from '../api/types'
import { ErrorState, LoadingState } from '../components/QueryState'
import { StatusBadge } from '../components/StatusBadge'
import { formatDate, generationTypeLabels, statusLabels } from '../features/tasks/presentation'
import { useTaskHistory } from '../features/tasks/taskQueries'

const generationTypes: readonly GenerationType[] = ['TEXT_TO_IMAGE', 'IMAGE_TO_IMAGE']
const taskStatuses = Object.keys(statusLabels) as TaskStatus[]

function selectedStatus(value: string | null): TaskStatus | undefined {
  return taskStatuses.find((status) => status === value)
}

function selectedGenerationType(value: string | null): GenerationType | undefined {
  return generationTypes.find((generationType) => generationType === value)
}

export function TaskHistoryPage() {
  const [searchParams, setSearchParams] = useSearchParams()
  const [search, setSearch] = useState('')
  const status = selectedStatus(searchParams.get('status'))
  const generationType = selectedGenerationType(searchParams.get('generation_type'))
  const query = useTaskHistory(status, generationType)
  const items = useMemo(
    () => query.data?.pages.flatMap((page) => page.items) ?? [],
    [query.data],
  )
  const visibleItems = items.filter((task) => task.prompt.toLocaleLowerCase().includes(search.trim().toLocaleLowerCase()))

  const updateFilter = (key: 'status' | 'generation_type', value: string) => {
    const next = new URLSearchParams(searchParams)
    if (value) next.set(key, value)
    else next.delete(key)
    setSearchParams(next)
  }

  return (
    <section className="history-page page-enter">
      <header className="history-header">
        <div><p className="eyebrow">CREATIVE ARCHIVE</p><h1>任务历史</h1><p>回看每一次创作，以及它如何抵达结果。</p></div>
        <Link className="button primary" to="/tasks/new">＋ 新建任务</Link>
      </header>
      <div className="history-tools">
        <label className="search-control"><span className="sr-only">搜索已加载任务</span><input value={search} onChange={(event) => setSearch(event.target.value)} placeholder="搜索已加载的提示词" /></label>
        <label className="filter-control"><span>生成方式</span><select aria-label="生成方式" value={generationType ?? ''} onChange={(event) => updateFilter('generation_type', event.target.value)}>
          <option value="">全部</option>
          {generationTypes.map((value) => <option key={value} value={value}>{generationTypeLabels[value]}</option>)}
        </select></label>
        <label className="filter-control"><span>状态</span><select aria-label="状态" value={status ?? ''} onChange={(event) => updateFilter('status', event.target.value)}>
          <option value="">全部</option>
          {taskStatuses.map((value) => <option key={value} value={value}>{statusLabels[value]}</option>)}
        </select></label>
      </div>

      {query.isPending ? <LoadingState /> : query.isError ? <ErrorState onRetry={() => void query.refetch()} /> : visibleItems.length === 0 ? (
        <div className="empty-state">
          <span className="empty-orb" aria-hidden="true" />
          <p className="eyebrow">YOUR FIRST FRAME</p>
          <h2>{items.length ? '没有匹配的任务' : '从第一幅画面开始'}</h2>
          <p>{items.length ? '换一个关键词或筛选条件看看。' : '创建任务后，执行状态与结果都会保存在这里。'}</p>
          {!items.length && <Link className="button primary" to="/tasks/new">开始创作</Link>}
        </div>
      ) : (
        <>
          <div className="task-grid">
            <Link className="task-card new-task-card" to="/tasks/new"><span>＋</span><strong>开始新的创作</strong><small>单图 · 1280 × 1280</small></Link>
            {visibleItems.map((task) => {
              const thumbnail = resultUrl(task.thumbnail_url)
              const generationLabel = task.generation_type
                ? generationTypeLabels[task.generation_type]
                : '生成方式未知'
              return (
                <Link className="task-card" to={`/tasks/${task.id}`} key={task.id}>
                  <div className="thumbnail">{thumbnail ? <img src={thumbnail} alt="生成结果缩略图" loading="lazy" /> : <span className={`thumbnail-state state-${task.status.toLowerCase()}`} />}</div>
                  <div className="card-body">
                    <div className="card-badges"><StatusBadge status={task.status} /><span className="generation-badge">{generationLabel}</span>{task.generation_type === 'IMAGE_TO_IMAGE' && <span className="generation-badge">含参考素材</span>}</div>
                    <h2>{task.prompt}</h2>
                    <time dateTime={task.created_at}>{formatDate(task.created_at)}</time>
                  </div>
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
