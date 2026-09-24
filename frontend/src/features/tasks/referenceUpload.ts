import { useEffect, useRef, useState } from 'react'
import { ApiError, referenceAssetsApi } from '../../api/client'
import { messageForError } from '../../api/errorMessages'
import type { ReferenceAsset } from '../../api/types'

export const MAX_REFERENCE_BYTES = 6_000_000
const SUPPORTED_CONTENT_TYPES = new Set(['image/png', 'image/jpeg', 'image/webp'])

export type PendingUploadIntent = {
  key: string
  name: string
  size: number
  lastModified: number
  contentType: string
}

export type ReferenceUploadState =
  | { status: 'IDLE' }
  | { status: 'NEEDS_FILE'; intent: PendingUploadIntent }
  | { status: 'LOCAL_SELECTED'; file: File; key: string; error?: string }
  | { status: 'UPLOADING'; file: File; key: string }
  | { status: 'UPLOAD_FAILED'; file: File; key: string; error: string; retryable: boolean }
  | { status: 'RESTORING'; assetId: string }
  | { status: 'RESTORE_FAILED'; assetId: string; error: string }
  | { status: 'UPLOADED'; asset: ReferenceAsset; file: File | null }

export function pendingUploadIntent(state: ReferenceUploadState): PendingUploadIntent | undefined {
  if (state.status === 'NEEDS_FILE') return state.intent
  if (state.status === 'LOCAL_SELECTED' || state.status === 'UPLOADING' || state.status === 'UPLOAD_FAILED') {
    return {
      key: state.key,
      name: state.file.name,
      size: state.file.size,
      lastModified: state.file.lastModified,
      contentType: state.file.type,
    }
  }
  return undefined
}

export function referenceAssetId(state: ReferenceUploadState): string | undefined {
  if (state.status === 'UPLOADED') return state.asset.asset_id
  if (state.status === 'RESTORING' || state.status === 'RESTORE_FAILED') return state.assetId
  return undefined
}

function matchesIntent(file: File, intent: PendingUploadIntent): boolean {
  return file.name === intent.name
    && file.size === intent.size
    && file.lastModified === intent.lastModified
    && file.type === intent.contentType
}

export function useReferenceUpload(pendingIntent?: PendingUploadIntent, initialAssetId?: string) {
  const [state, setState] = useState<ReferenceUploadState>(
    () => initialAssetId
      ? { status: 'RESTORING', assetId: initialAssetId }
      : pendingIntent ? { status: 'NEEDS_FILE', intent: pendingIntent } : { status: 'IDLE' },
  )
  const stateRef = useRef(state)
  const uploadLock = useRef(false)
  const file = state.status === 'LOCAL_SELECTED' || state.status === 'UPLOADING' || state.status === 'UPLOAD_FAILED'
    ? state.file
    : state.status === 'UPLOADED' ? state.file : null
  const [localPreviewUrl, setLocalPreviewUrl] = useState<string>()

  useEffect(() => {
    if (!file) {
      setLocalPreviewUrl(undefined)
      return
    }
    const objectUrl = URL.createObjectURL(file)
    setLocalPreviewUrl(objectUrl)
    return () => URL.revokeObjectURL(objectUrl)
  }, [file])

  const transition = (next: ReferenceUploadState) => {
    stateRef.current = next
    setState(next)
  }

  const chooseFile = (selected: File | undefined) => {
    if (!selected) return
    const current = stateRef.current
    const intent = current.status === 'NEEDS_FILE' ? current.intent : undefined
    const key = intent && matchesIntent(selected, intent) ? intent.key : crypto.randomUUID()
    const error = !SUPPORTED_CONTENT_TYPES.has(selected.type)
      ? '只支持 PNG、JPEG 和 WebP 图片。浏览器的文件类型信息仅用于提示，服务端会再次校验。'
      : undefined
    transition({ status: 'LOCAL_SELECTED', file: selected, key, error })
  }

  const clear = () => {
    if (uploadLock.current) return
    transition({ status: 'IDLE' })
  }

  const restore = async (assetId: string) => {
    transition({ status: 'RESTORING', assetId })
    try {
      const asset = await referenceAssetsApi.get(assetId)
      if (asset.status !== 'READY') throw new Error('REFERENCE_ASSET_NOT_READY')
      transition({ status: 'UPLOADED', asset, file: null })
    } catch (error) {
      transition({
        status: 'RESTORE_FAILED',
        assetId,
        error: messageForError(error, '无法恢复已上传的参考素材，请检查状态或重新上传。'),
      })
    }
  }

  useEffect(() => {
    if (initialAssetId) void restore(initialAssetId)
    // The first page draft is the only asset restored on mount.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [])

  const upload = async (): Promise<ReferenceAsset | undefined> => {
    const current = stateRef.current
    if (current.status === 'UPLOADED') return current.asset
    if (current.status !== 'LOCAL_SELECTED' && current.status !== 'UPLOAD_FAILED') return undefined
    if (
      current.status === 'LOCAL_SELECTED' && current.error
      || current.status === 'UPLOAD_FAILED' && !current.retryable
      || uploadLock.current
    ) return undefined

    uploadLock.current = true
    transition({ status: 'UPLOADING', file: current.file, key: current.key })
    try {
      const asset = await referenceAssetsApi.upload(current.file, current.key)
      if (asset.status !== 'READY') throw new Error('REFERENCE_ASSET_NOT_READY')
      transition({ status: 'UPLOADED', asset, file: current.file })
      return asset
    } catch (error) {
      const retryable = error instanceof ApiError
        ? error.status === 0 || error.status === 408 || error.status === 429 || error.status >= 500
        : !(error instanceof Error && error.message === 'REFERENCE_ASSET_NOT_READY')
      const message = error instanceof Error && error.message === 'REFERENCE_ASSET_NOT_READY'
        ? '参考素材尚未就绪，请稍后重试。'
        : messageForError(error, '上传结果暂时无法确认；可以复用同一上传标识重试。')
      transition({ status: 'UPLOAD_FAILED', file: current.file, key: current.key, error: message, retryable })
      return undefined
    } finally {
      uploadLock.current = false
    }
  }

  return { state, file, localPreviewUrl, chooseFile, clear, restore, upload }
}

export type ReferenceUploadController = ReturnType<typeof useReferenceUpload>
