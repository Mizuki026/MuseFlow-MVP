import type { TaskStatus } from '../../api/types'

export const statusLabels: Record<TaskStatus, string> = {
  QUEUED: '排队中',
  RUNNING: '创作中',
  RETRY_WAIT: '等待重试',
  SUCCEEDED: '已完成',
  FAILED: '失败',
}

export const eventLabels: Record<string, string> = {
  RESULT_STORAGE_FAILED: "\u7ed3\u679c\u4fdd\u5b58\u5931\u8d25\uff0c\u7b49\u5f85\u6062\u590d",
  TASK_QUEUED: '任务进入队列',
  ATTEMPT_STARTED: '开始生成',
  ATTEMPT_RECLAIMED: '恢复执行',
  TASK_RETRY_WAIT: '等待自动重试',
  TASK_SUCCEEDED: '结果已保存',
  TASK_FAILED: '任务失败',
}

export const phaseLabels: Record<string, string> = {
  RESULT_PERSISTING: "\u6b63\u5728\u4fdd\u5b58\u7ed3\u679c",
  RESULT_STORAGE_FAILED: "\u7ed3\u679c\u4fdd\u5b58\u5931\u8d25",
  PROVIDER_RUNNING: '正在调用生成服务',
  PROVIDER_FAILED: '生成服务返回错误',
  RESULT_PERSISTED: '结果已安全保存',
}

export function formatDate(value: string | null | undefined): string {
  if (!value) return '—'
  return new Intl.DateTimeFormat('zh-CN', {
    month: 'short',
    day: 'numeric',
    hour: '2-digit',
    minute: '2-digit',
    second: '2-digit',
  }).format(new Date(value))
}

export function formatBytes(bytes: number): string {
  return bytes < 1024 ? `${bytes} B` : `${(bytes / 1024).toFixed(1)} KiB`
}
