import { useRef, type ChangeEvent } from 'react'
import { referenceAssetUrl } from '../../api/client'
import { MAX_REFERENCE_BYTES, type ReferenceUploadController } from './referenceUpload'

export function ReferenceAssetPicker({ upload }: { upload: ReferenceUploadController }) {
  const inputRef = useRef<HTMLInputElement>(null)
  const state = upload.state
  const remoteUrl = state.status === 'UPLOADED' ? referenceAssetUrl(state.asset.asset_id) : undefined
  const preview = upload.localPreviewUrl ?? remoteUrl
  const isUploading = state.status === 'UPLOADING'
  const hasLocalFile = state.status === 'LOCAL_SELECTED' || state.status === 'UPLOADING' || state.status === 'UPLOAD_FAILED'
  const displayedFile = hasLocalFile ? state.file : state.status === 'UPLOADED' ? state.file : null
  const error = state.status === 'UPLOAD_FAILED' || state.status === 'RESTORE_FAILED'
    ? state.error
    : state.status === 'LOCAL_SELECTED' ? state.error : undefined

  const onFileChange = (event: ChangeEvent<HTMLInputElement>) => {
    upload.chooseFile(event.currentTarget.files?.item(0) ?? undefined)
    event.currentTarget.value = ''
  }

  return (
    <fieldset className="reference-picker" disabled={isUploading}>
      <legend>参考图片</legend>
      <p className="upload-guidance" id="reference-help">
        单张 PNG、JPEG 或 WebP，最大 6 MB；建议宽高各 240–2048 像素、RGB、不透明静态图片。浏览器检查仅作提示，服务端会完整校验。
      </p>
      <input
        ref={inputRef}
        className="sr-only"
        id="reference-file"
        type="file"
        accept="image/png,image/jpeg,image/webp"
        aria-describedby="reference-help reference-error"
        onChange={onFileChange}
      />
      <label className="sr-only" htmlFor="reference-file">参考图片文件</label>

      {preview ? (
        <div className="reference-preview">
          <img src={preview} alt="所选参考图片预览" />
          <div className="reference-preview-meta">
            {state.status === 'UPLOADED' && state.asset.width != null && state.asset.height != null
              ? <span>{state.asset.width} × {state.asset.height} · {state.asset.content_type ?? '图片'}</span>
              : displayedFile ? <span>{displayedFile.name} · {formatFileSize(displayedFile.size)}</span> : <span>已恢复上传素材</span>}
            {state.status === 'UPLOADED' && <span className="upload-state-good">服务端校验完成</span>}
          </div>
        </div>
      ) : state.status === 'RESTORING' ? (
        <p className="upload-state" role="status">正在恢复已上传的参考素材…</p>
      ) : state.status === 'NEEDS_FILE' ? (
        <p className="upload-state" role="status">刷新后无法恢复浏览器中的原始文件。重新选择同一文件可继续使用原上传标识；选择其他文件会开始新的上传。</p>
      ) : state.status === 'RESTORE_FAILED' ? (
        <p className="upload-state" role="status">已保存素材 ID，但服务端暂时无法确认素材状态。可以重新上传图片。</p>
      ) : (
        <button className="button upload-select" type="button" onClick={() => inputRef.current?.click()}>
          选择一张参考图片
        </button>
      )}
      {state.status === 'UPLOADING' && <p className="upload-state" role="status">正在上传参考图片…</p>}

      {displayedFile && displayedFile.size > MAX_REFERENCE_BYTES && (
        <p className="upload-warning" role="status">文件超过 6 MB 提示限制；服务端将拒绝该文件。</p>
      )}
      {error && <p className="field-error" id="reference-error" role="alert">{error}</p>}
      {state.status === 'UPLOAD_FAILED' && state.retryable && (
        <p className="upload-state" role="status">重试会继续使用同一上传标识，避免重复创建素材。</p>
      )}
      <div className="upload-actions">
        {(displayedFile || state.status === 'NEEDS_FILE' || state.status === 'RESTORE_FAILED') && (
          <button className="button secondary" type="button" disabled={isUploading} onClick={() => inputRef.current?.click()}>
            {displayedFile ? '替换图片' : '选择图片'}
          </button>
        )}
        {(displayedFile || state.status === 'NEEDS_FILE' || state.status === 'RESTORE_FAILED') && (
          <button className="button secondary" type="button" disabled={isUploading} onClick={upload.clear}>
            清除
          </button>
        )}
        {(state.status === 'LOCAL_SELECTED' && !state.error || state.status === 'UPLOAD_FAILED' && state.retryable) && (
          <button className="button secondary" type="button" disabled={isUploading} onClick={() => void upload.upload()}>
            {isUploading ? '正在上传…' : state.status === 'UPLOAD_FAILED' ? '重试上传' : '上传参考图片'}
          </button>
        )}
        {state.status === 'UPLOADED' && <span className="upload-state-good" role="status">上传完成</span>}
      </div>
    </fieldset>
  )
}

function formatFileSize(size: number): string {
  return size < 1_000_000 ? `${(size / 1_000).toFixed(0)} KB` : `${(size / 1_000_000).toFixed(2)} MB`
}
