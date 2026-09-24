import { useInfiniteQuery, useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { tasksApi } from '../../api/client'
import type { CreateTaskInput, GenerationType, Task, TaskStatus } from '../../api/types'

const activeStatuses = new Set<TaskStatus>(['QUEUED', 'RUNNING', 'RETRY_WAIT'])
export const taskPollInterval = import.meta.env.MODE === 'test' ? 50 : 1_500

export function isActiveTask(task: Task | undefined): boolean {
  return Boolean(task && activeStatuses.has(task.status))
}

export function useTask(taskId: string) {
  return useQuery({
    queryKey: ['task', taskId],
    queryFn: ({ signal }) => tasksApi.get(taskId, signal),
    staleTime: 1_000,
    retry: (failureCount) => failureCount < 2,
    retryDelay: (attempt) => Math.min(500 * 2 ** attempt, 2_000),
    refetchInterval: (query) =>
      isActiveTask(query.state.data) ? taskPollInterval : false,
    refetchOnWindowFocus: (query) => isActiveTask(query.state.data),
  })
}

export function useTaskHistory(status?: TaskStatus, generationType?: GenerationType) {
  return useInfiniteQuery({
    queryKey: ['tasks', status ?? 'ALL', generationType ?? 'ALL'],
    queryFn: ({ pageParam, signal }) => tasksApi.list(
      { cursor: pageParam, status, generationType },
      signal,
    ),
    initialPageParam: undefined as string | undefined,
    getNextPageParam: (page) => page.next_cursor ?? undefined,
  })
}

export function useCreateTask() {
  const queryClient = useQueryClient()
  return useMutation({
    mutationFn: ({ body, key }: { body: CreateTaskInput; key: string }) =>
      tasksApi.create(body, key),
    onSuccess: (task) => {
      queryClient.setQueryData(['task', task.id], task)
      void queryClient.invalidateQueries({ queryKey: ['tasks'] })
    },
  })
}

export function useRetryTask(taskId: string) {
  const queryClient = useQueryClient()
  return useMutation({
    mutationFn: (key: string) => tasksApi.retry(taskId, key),
    onSuccess: (task) => {
      queryClient.setQueryData(['task', task.id], task)
      void queryClient.invalidateQueries({ queryKey: ['task', taskId] })
      void queryClient.invalidateQueries({ queryKey: ['tasks'] })
    },
  })
}
