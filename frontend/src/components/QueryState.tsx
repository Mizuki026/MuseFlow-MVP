export function LoadingState({ label = '正在载入创作记录…' }: { label?: string }) {
  return <div className="state-panel loading-state" role="status"><span className="spinner" />{label}</div>
}

export function ErrorState({ onRetry }: { onRetry: () => void }) {
  return (
    <div className="state-panel" role="alert">
      <p className="eyebrow">连接未完成</p>
      <h2>暂时无法取得任务</h2>
      <p>请检查本地服务后重试。</p>
      <button className="button secondary" type="button" onClick={onRetry}>重新加载</button>
    </div>
  )
}
