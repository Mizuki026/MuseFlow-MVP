import type { TaskStatus } from '../api/types'
import { statusLabels } from '../features/tasks/presentation'

export function StatusBadge({ status }: { status: TaskStatus }) {
  return <span className={`status-badge status-${status.toLowerCase()}`}>{statusLabels[status]}</span>
}
