import type { components } from './schema.generated'

export type ApiErrorEnvelope = components['schemas']['ErrorResponse']
export type CreateTaskBody = components['schemas']['CreateTaskBody']
export type GenerationType = components['schemas']['GenerationType']
export type ReferenceAsset = components['schemas']['ReferenceAssetResponse']
export type CreateTaskInput = Omit<
  CreateTaskBody,
  'generation_type' | 'reference_asset_id' | 'size_preset'
> & {
  size_preset: string
} & (
    | { generation_type: Extract<GenerationType, 'TEXT_TO_IMAGE'>; reference_asset_id?: never }
    | { generation_type: Extract<GenerationType, 'IMAGE_TO_IMAGE'>; reference_asset_id: string }
  )
export type Task = components['schemas']['TaskResponse']
export type TaskList = components['schemas']['TaskListResponse']
export type TaskStatus = components['schemas']['TaskStatus']
export type TaskSummary = components['schemas']['TaskSummaryResponse']
