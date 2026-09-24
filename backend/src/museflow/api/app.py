from __future__ import annotations

import os
import tempfile
from uuid import UUID, uuid4

from fastapi import FastAPI, Header, Query, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from sqlalchemy import select, text
from sqlalchemy.orm import Session, sessionmaker
from starlette.datastructures import UploadFile as StarletteUploadFile
from starlette.formparsers import MultiPartException
from starlette.middleware.base import RequestResponseEndpoint
from starlette.middleware.cors import CORSMiddleware
from starlette.responses import RedirectResponse, Response
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from museflow.api.dto import (
    CreateTaskBody,
    DemoCreateTaskBody,
    ErrorResponse,
    HealthResponse,
    ReferenceAssetResponse,
    TaskAssetResponse,
    TaskAttemptResponse,
    TaskEventResponse,
    TaskListResponse,
    TaskResponse,
    TaskSummaryResponse,
)
from museflow.assets import MinioResultAssetStore, ResultAssetStore
from museflow.db.models import GenerationTaskModel, ReferenceAssetModel, ResultAssetModel
from museflow.db.session import create_session_factory
from museflow.provider_profiles import ProviderProfileUnavailableError
from museflow.reference_assets.access import (
    AssetAccessSigner,
    ReferenceAssetReader,
    ReferenceObjectContentMismatchError,
)
from museflow.reference_assets.blob_store import BlobStore, BlobStoreUnavailable, MinioBlobStore
from museflow.reference_assets.image_inspector import MAX_REFERENCE_BYTES
from museflow.reference_assets.repository import ReferenceAssetStateError
from museflow.reference_assets.service import (
    ReferenceAssetConflictError,
    ReferenceAssetService,
    ReferenceAssetUnavailableError,
)
from museflow.reference_assets.streaming import UploadTooLargeError, stage_upload
from museflow.tasks.application import (
    CreateTask,
    GetTaskDetail,
    IdempotencyConflictError,
    ListTasks,
    ManualRetryNotAllowedError,
    TaskNotFoundError,
)
from museflow.tasks.domain import (
    CreateTaskRequest,
    DomainValidationError,
    ProviderTaskSnapshot,
    ReferenceAssetStatus,
    TaskPolicy,
    TaskStatus,
)
from museflow.tasks.execution_models import GenerationAttemptModel

EXPECTED_MIGRATION_REVISION = "0007_reference_operation_leases"
DEFAULT_DATABASE_URL = "postgresql+psycopg://postgres:postgres@localhost:5432/museflow"
MAX_MULTIPART_BODY_BYTES = 6_500_000


class RequestBodyLimitMiddleware:
    """Spool and bound raw multipart bodies before Starlette parses their parts."""

    def __init__(self, app: ASGIApp, *, max_bytes: int = MAX_MULTIPART_BODY_BYTES) -> None:
        self.app = app
        self.max_bytes = max_bytes

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if (
            scope["type"] != "http"
            or scope.get("method") != "POST"
            or scope.get("path") != "/api/v1/assets"
        ):
            await self.app(scope, receive, send)
            return

        headers = {key.lower(): value for key, value in scope.get("headers", [])}
        raw_length = headers.get(b"content-length")
        if raw_length is not None:
            try:
                declared_length = int(raw_length)
            except ValueError:
                declared_length = -1
            if declared_length > self.max_bytes:
                await self._reject(scope, receive, send)
                return

        spool = tempfile.SpooledTemporaryFile(max_size=64 * 1024, mode="w+b")
        total = 0
        try:
            while True:
                message = await receive()
                if message["type"] == "http.disconnect":
                    return
                if message["type"] != "http.request":
                    continue
                body = message.get("body", b"")
                total += len(body)
                if total > self.max_bytes:
                    await self._reject(scope, receive, send)
                    return
                spool.write(body)
                if not message.get("more_body", False):
                    break

            spool.seek(0)
            remaining = total
            exhausted = False

            async def replay_body() -> Message:
                nonlocal remaining, exhausted
                if exhausted:
                    return {"type": "http.disconnect"}
                chunk = spool.read(min(64 * 1024, remaining)) if remaining else b""
                remaining -= len(chunk)
                more_body = remaining > 0
                exhausted = not more_body
                return {"type": "http.request", "body": chunk, "more_body": more_body}

            await self.app(scope, replay_body, send)
        finally:
            spool.close()

    async def _reject(self, scope: Scope, receive: Receive, send: Send) -> None:
        request_id = uuid4()
        response = JSONResponse(
            status_code=413,
            headers={"X-Request-ID": str(request_id)},
            content={
                "error": {
                    "code": "MULTIPART_BODY_TOO_LARGE",
                    "message": "multipart request body exceeds the maximum size",
                    "request_id": str(request_id),
                }
            },
        )
        await response(scope, receive, send)


class ApiError(Exception):
    def __init__(self, status_code: int, code: str, message: str) -> None:
        super().__init__(message)
        self.status_code = status_code
        self.code = code
        self.message = message


def _request_id(request: Request) -> str:
    return getattr(request.state, "request_id", str(uuid4()))


def _error_response(request: Request, error: ApiError) -> JSONResponse:
    return JSONResponse(
        status_code=error.status_code,
        content={
            "error": {
                "code": error.code,
                "message": error.message,
                "request_id": _request_id(request),
            }
        },
    )


def _task_response(
    task: TaskResponse, events: list[TaskEventResponse] | None = None
) -> TaskResponse:
    if events is not None:
        task.events = events
    return task


def create_app(
    *,
    session_factory: sessionmaker[Session] | None = None,
    database_url: str | None = None,
    ready_revision: str = EXPECTED_MIGRATION_REVISION,
    asset_store: ResultAssetStore | None = None,
    reference_blob_store: BlobStore | None = None,
    demo_mode: bool | None = None,
    raw_body_limit: int = MAX_MULTIPART_BODY_BYTES,
) -> FastAPI:
    factory = session_factory or create_session_factory(
        database_url or os.environ.get("MUSEFLOW_DATABASE_URL", DEFAULT_DATABASE_URL)
    )
    create_task = CreateTask(factory)
    get_task_detail = GetTaskDetail(factory)
    list_tasks = ListTasks(factory)
    result_assets = asset_store or MinioResultAssetStore()
    reference_blobs = reference_blob_store or MinioBlobStore()
    reference_service = ReferenceAssetService(factory, reference_blobs)
    reference_reader = ReferenceAssetReader(factory, reference_blobs)
    access_signer = AssetAccessSigner(reference_reader, reference_blobs)

    app = FastAPI(title="MuseFlow API", version="0.1.0")
    allowed_origins = os.environ.get(
        "MUSEFLOW_CORS_ORIGINS",
        (
            "http://127.0.0.1:5173,http://localhost:5173,"
            "http://127.0.0.1:4173,http://localhost:4173"
        ),
    )
    app.add_middleware(
        CORSMiddleware,
        allow_origins=[origin.strip() for origin in allowed_origins.split(",") if origin.strip()],
        allow_methods=["GET", "POST", "OPTIONS"],
        allow_headers=["Content-Type", "Idempotency-Key", "X-Request-ID"],
    )
    app.add_middleware(RequestBodyLimitMiddleware, max_bytes=raw_body_limit)

    @app.middleware("http")
    async def add_request_id(request: Request, call_next: RequestResponseEndpoint) -> Response:
        request.state.request_id = request.headers.get("X-Request-ID") or str(uuid4())
        response = await call_next(request)
        response.headers["X-Request-ID"] = request.state.request_id
        return response

    @app.exception_handler(ApiError)
    async def handle_api_error(request: Request, error: ApiError) -> JSONResponse:
        return _error_response(request, error)

    @app.exception_handler(DomainValidationError)
    async def handle_domain_error(request: Request, error: DomainValidationError) -> JSONResponse:
        return _error_response(request, ApiError(422, error.code.value, str(error)))

    @app.exception_handler(IdempotencyConflictError)
    async def handle_idempotency_conflict(
        request: Request, error: IdempotencyConflictError
    ) -> JSONResponse:
        return _error_response(request, ApiError(409, "IDEMPOTENCY_KEY_CONFLICT", str(error)))

    @app.exception_handler(TaskNotFoundError)
    async def handle_not_found(request: Request, error: TaskNotFoundError) -> JSONResponse:
        return _error_response(request, ApiError(404, "TASK_NOT_FOUND", "task was not found"))

    @app.exception_handler(ManualRetryNotAllowedError)
    async def handle_manual_retry_error(
        request: Request, error: ManualRetryNotAllowedError
    ) -> JSONResponse:
        return _error_response(request, ApiError(409, "RETRY_NOT_ALLOWED", str(error)))

    @app.exception_handler(ProviderProfileUnavailableError)
    async def handle_provider_profile_unavailable(
        request: Request, error: ProviderProfileUnavailableError
    ) -> JSONResponse:
        return _error_response(
            request,
            ApiError(503, "PROVIDER_PROFILE_UNAVAILABLE", str(error)),
        )

    @app.exception_handler(RequestValidationError)
    async def handle_request_validation(
        request: Request, error: RequestValidationError
    ) -> JSONResponse:
        return _error_response(
            request,
            ApiError(422, "INVALID_REQUEST", "request parameters are invalid"),
        )

    @app.exception_handler(ValueError)
    async def handle_value_error(request: Request, error: ValueError) -> JSONResponse:
        return _error_response(request, ApiError(400, "INVALID_REQUEST", str(error)))

    @app.exception_handler(ReferenceAssetConflictError)
    async def handle_reference_conflict(
        request: Request, error: ReferenceAssetConflictError
    ) -> JSONResponse:
        return _error_response(request, ApiError(409, error.code, str(error)))

    @app.exception_handler(ReferenceAssetUnavailableError)
    async def handle_reference_unavailable(
        request: Request, error: ReferenceAssetUnavailableError
    ) -> JSONResponse:
        if error.code == "FILE_TOO_LARGE":
            status_code = 413
        elif error.code == "IDEMPOTENCY_KEY_INVALID":
            status_code = 400
        elif error.code == "REFERENCE_ASSET_PROCESSING":
            status_code = 409
        elif error.code.startswith("IMAGE_"):
            status_code = 422
        else:
            status_code = 503
        return _error_response(request, ApiError(status_code, error.code, str(error)))

    def _reference_asset_response(
        asset: ReferenceAssetModel, *, idempotency_replayed: bool = False
    ) -> ReferenceAssetResponse:
        return ReferenceAssetResponse(
            asset_id=asset.id,
            status=ReferenceAssetStatus(asset.status),
            content_type=asset.content_type,
            width=asset.width,
            height=asset.height,
            size_bytes=asset.size_bytes,
            sha256=asset.sha256,
            download_url=f"/api/v1/assets/{asset.id}/download",
            idempotency_replayed=idempotency_replayed,
        )

    @app.post("/api/v1/assets", response_model=ReferenceAssetResponse, status_code=201)
    async def upload_reference_asset(
        request: Request,
        idempotency_key: str | None = Header(default=None, alias="Idempotency-Key"),
    ) -> JSONResponse:
        if not idempotency_key:
            raise ApiError(400, "IDEMPOTENCY_KEY_REQUIRED", "Idempotency-Key is required")
        temporary_path = None
        try:
            async with request.form(
                max_files=1, max_fields=0, max_part_size=MAX_REFERENCE_BYTES
            ) as form:
                parts = list(form.multi_items())
                if (
                    len(parts) != 1
                    or parts[0][0] != "file"
                    or not isinstance(parts[0][1], StarletteUploadFile)
                ):
                    raise ApiError(400, "INVALID_MULTIPART", "upload one image in the file field")
                upload = parts[0][1]
                staged = await stage_upload(upload.read)
                temporary_path = staged.path
                try:
                    from starlette.concurrency import run_in_threadpool

                    asset, replayed = await run_in_threadpool(
                        lambda: reference_service.upload(
                            idempotency_key=idempotency_key,
                            path=staged.path,
                            declared_content_type=upload.content_type or "",
                            upload_size_bytes=staged.size_bytes,
                            upload_sha256=staged.sha256,
                        )
                    )
                finally:
                    staged.path.unlink(missing_ok=True)
                    temporary_path = None
        except UploadTooLargeError as error:
            raise ReferenceAssetUnavailableError("FILE_TOO_LARGE", str(error)) from error
        except MultiPartException as error:
            raise ApiError(400, "INVALID_MULTIPART", "multipart request is invalid") from error
        finally:
            if temporary_path is not None:
                temporary_path.unlink(missing_ok=True)

        with factory() as session:
            model = session.get(ReferenceAssetModel, asset.id)
            if model is None:
                raise ApiError(503, "REFERENCE_ASSET_UNAVAILABLE", "reference asset is unavailable")
            body = _reference_asset_response(model, idempotency_replayed=replayed).model_dump(
                mode="json"
            )
        return JSONResponse(status_code=200 if replayed else 201, content=body)

    @app.get("/api/v1/assets/{asset_id}", response_model=ReferenceAssetResponse)
    def get_reference_asset(asset_id: UUID) -> ReferenceAssetResponse:
        with factory() as session:
            model = session.get(ReferenceAssetModel, asset_id)
            if model is None:
                raise ApiError(404, "REFERENCE_ASSET_NOT_FOUND", "reference asset was not found")
            return _reference_asset_response(model)

    @app.get("/api/v1/assets/{asset_id}/download")
    def download_asset(asset_id: UUID) -> Response:
        with factory() as session:
            reference_asset = session.get(ReferenceAssetModel, asset_id)
            if reference_asset is None:
                result_asset = session.get(ResultAssetModel, asset_id)
                if result_asset is None:
                    raise ApiError(404, "ASSET_NOT_FOUND", "asset was not found")
                task = session.get(GenerationTaskModel, result_asset.task_id)
                if task is None or task.status != TaskStatus.SUCCEEDED.value:
                    raise ApiError(409, "RESULT_NOT_READY", "result is not ready")
                result_object_key = result_asset.object_key
            else:
                result_object_key = None

        if result_object_key is not None:
            try:
                url = result_assets.presigned_download(result_object_key, expires_seconds=300)
            except Exception as error:
                raise ApiError(
                    503, "RESULT_STORE_UNAVAILABLE", "result store is not ready"
                ) from error
            return RedirectResponse(url, status_code=307)

        try:
            verified = reference_reader.read(asset_id)
        except LookupError as error:
            raise ApiError(
                404, "REFERENCE_ASSET_NOT_FOUND", "reference asset was not found"
            ) from error
        except ReferenceAssetStateError as error:
            raise ApiError(
                409, "REFERENCE_ASSET_NOT_READY", "reference asset is not ready"
            ) from error
        except FileNotFoundError as error:
            raise ApiError(
                503, "REFERENCE_OBJECT_MISSING", "reference object is missing"
            ) from error
        except ReferenceObjectContentMismatchError as error:
            raise ApiError(
                503, "REFERENCE_OBJECT_MISMATCH", "reference object failed integrity checks"
            ) from error
        except BlobStoreUnavailable as error:
            raise ApiError(
                503, "OBJECT_STORE_UNAVAILABLE", "reference object storage is unavailable"
            ) from error
        return Response(
            content=verified.content,
            media_type=verified.content_type,
            headers={
                "Cache-Control": "private, no-store",
                "Content-Length": str(verified.size_bytes),
            },
        )

    @app.get("/api/v1/assets/{asset_id}/access", include_in_schema=False)
    def access_reference_asset(asset_id: UUID) -> RedirectResponse:
        try:
            url = access_signer.sign(asset_id, expires_seconds=300)
        except LookupError as error:
            raise ApiError(
                404, "REFERENCE_ASSET_NOT_FOUND", "reference asset was not found"
            ) from error
        except ReferenceAssetStateError as error:
            raise ApiError(
                409, "REFERENCE_ASSET_NOT_READY", "reference asset is not ready"
            ) from error
        except FileNotFoundError as error:
            raise ApiError(
                503, "REFERENCE_OBJECT_MISSING", "reference object is missing"
            ) from error
        except ReferenceObjectContentMismatchError as error:
            raise ApiError(
                503, "REFERENCE_OBJECT_MISMATCH", "reference object failed integrity checks"
            ) from error
        except BlobStoreUnavailable as error:
            raise ApiError(
                503, "OBJECT_STORE_UNAVAILABLE", "reference object storage is unavailable"
            ) from error
        return RedirectResponse(url, status_code=307)

    @app.post(
        "/api/v1/tasks",
        response_model=TaskResponse,
        status_code=201,
        responses={409: {"model": ErrorResponse}, 422: {"model": ErrorResponse}},
    )
    def create_task_route(
        body: CreateTaskBody,
        idempotency_key: str | None = Header(default=None, alias="Idempotency-Key"),
    ) -> JSONResponse:
        if not idempotency_key:
            raise ApiError(400, "IDEMPOTENCY_KEY_REQUIRED", "Idempotency-Key is required")
        result = create_task.execute(
            CreateTaskRequest(
                prompt=body.prompt,
                generation_type=body.generation_type,
                reference_asset_id=body.reference_asset_id,
                reference_sha256=body.reference_sha256,
            ),
            idempotency_key=idempotency_key,
        )
        response = TaskSummaryResponse.from_record(result.task)
        return JSONResponse(
            status_code=200 if result.idempotency_replayed else 201,
            content=TaskResponse(
                **response.model_dump(mode="json"),
                idempotency_replayed=result.idempotency_replayed,
            ).model_dump(mode="json"),
        )

    enabled_demo_mode = demo_mode
    if enabled_demo_mode is None:
        enabled_demo_mode = os.environ.get("MUSEFLOW_DEMO_MODE", "false").lower() == "true"
    if enabled_demo_mode:

        @app.post(
            "/api/v1/demo/tasks",
            response_model=TaskResponse,
            status_code=201,
            responses={409: {"model": ErrorResponse}, 422: {"model": ErrorResponse}},
        )
        def create_demo_task_route(
            body: DemoCreateTaskBody,
            idempotency_key: str | None = Header(default=None, alias="Idempotency-Key"),
        ) -> JSONResponse:
            if not idempotency_key:
                raise ApiError(400, "IDEMPOTENCY_KEY_REQUIRED", "Idempotency-Key is required")
            result = create_task.execute(
                CreateTaskRequest(
                    prompt=body.prompt,
                    execution_profile=body.scenario.value,
                    generation_type=body.generation_type,
                    reference_asset_id=body.reference_asset_id,
                    reference_sha256=body.reference_sha256,
                ),
                idempotency_key=idempotency_key,
                provider_snapshot=ProviderTaskSnapshot(
                    profile="mock-text-to-image-v1",
                    provider_name="mock",
                    model_name="mock-deterministic-image",
                    capability_version="text-to-image-v1",
                ),
            )
            response = TaskSummaryResponse.from_record(result.task)
            return JSONResponse(
                status_code=200 if result.idempotency_replayed else 201,
                content=TaskResponse(
                    **response.model_dump(mode="json"),
                    idempotency_replayed=result.idempotency_replayed,
                ).model_dump(mode="json"),
            )

    @app.get(
        "/api/v1/tasks/{task_id}",
        response_model=TaskResponse,
        responses={404: {"model": ErrorResponse}},
    )
    def get_task_route(task_id: UUID) -> TaskResponse:
        detail = get_task_detail.execute(task_id)
        response = TaskSummaryResponse.from_record(detail.task)
        with factory() as session:
            attempts = list(
                session.scalars(
                    select(GenerationAttemptModel)
                    .where(GenerationAttemptModel.task_id == task_id)
                    .order_by(GenerationAttemptModel.sequence.asc())
                )
            )
            asset = session.scalar(
                select(ResultAssetModel).where(
                    ResultAssetModel.task_id == task_id,
                    ResultAssetModel.role == "RESULT",
                )
            )
            retry_task_id = session.scalar(
                select(GenerationTaskModel.id).where(
                    GenerationTaskModel.retried_from_task_id == task_id
                )
            )
        result = None
        if asset is not None and detail.task.status is TaskStatus.SUCCEEDED:
            result = TaskAssetResponse(
                id=asset.id,
                role=asset.role,
                content_type=asset.content_type,
                size_bytes=asset.size_bytes,
                width=asset.width,
                height=asset.height,
                sha256=asset.sha256,
                download_url=f"/api/v1/assets/{asset.id}/download",
            )
        return _task_response(
            TaskResponse(
                **response.model_dump(),
                attempts=[TaskAttemptResponse.from_model(attempt) for attempt in attempts],
                result=result,
                retry_task_id=retry_task_id,
            ),
            [TaskEventResponse.from_record(event) for event in detail.events],
        )

    @app.post("/api/v1/tasks/{task_id}/retry", response_model=TaskResponse, status_code=201)
    def retry_task_route(
        task_id: UUID,
        idempotency_key: str | None = Header(default=None, alias="Idempotency-Key"),
    ) -> JSONResponse:
        if not idempotency_key:
            raise ApiError(400, "IDEMPOTENCY_KEY_REQUIRED", "Idempotency-Key is required")
        source = get_task_detail.execute(task_id)
        result = create_task.execute(
            CreateTaskRequest(
                prompt=source.task.prompt,
                size_preset=source.task.size_preset,
                execution_profile=source.task.execution_profile,
                generation_type=source.task.generation_type,
                reference_asset_id=source.task.reference_asset_id,
                reference_sha256=source.task.reference_sha256,
            ),
            idempotency_key,
            retried_from_task_id=task_id,
            provider_snapshot=ProviderTaskSnapshot(
                profile=source.task.provider_profile,
                provider_name=source.task.provider_name,
                model_name=source.task.model_name,
                capability_version=source.task.capability_version,
            ),
            policy=TaskPolicy(
                max_attempts=source.task.max_attempts,
                policy_version=source.task.policy_version,
                deadline_seconds=int(
                    source.task.policy_snapshot.get(
                        "deadline_seconds",
                        (source.task.deadline_at - source.task.created_at).total_seconds(),
                    )
                ),
            ),
            policy_snapshot=source.task.policy_snapshot,
        )
        summary = TaskSummaryResponse.from_record(result.task)
        return JSONResponse(
            status_code=200 if result.idempotency_replayed else 201,
            content=TaskResponse(
                **summary.model_dump(mode="json"),
                idempotency_replayed=result.idempotency_replayed,
            ).model_dump(mode="json"),
        )

    @app.get("/api/v1/tasks", response_model=TaskListResponse)
    def list_tasks_route(
        limit: int = Query(default=20, ge=1, le=ListTasks.MAX_LIMIT),
        cursor: str | None = None,
        status: TaskStatus | None = None,
    ) -> TaskListResponse:
        page = list_tasks.execute(limit=limit, cursor=cursor, status=status)
        task_ids = [task.id for task in page.items]
        with factory() as session:
            assets = list(
                session.scalars(
                    select(ResultAssetModel).where(
                        ResultAssetModel.task_id.in_(task_ids),
                        ResultAssetModel.role == "RESULT",
                    )
                )
            )
        thumbnails = {
            asset.task_id: f"/api/v1/assets/{asset.id}/download" for asset in assets
        }
        return TaskListResponse(
            items=[
                TaskSummaryResponse(
                    **TaskSummaryResponse.from_record(task).model_dump(
                        exclude={"thumbnail_url"}
                    ),
                    thumbnail_url=thumbnails.get(task.id),
                )
                for task in page.items
            ],
            next_cursor=page.next_cursor,
        )

    @app.get("/api/v1/health/live", response_model=HealthResponse)
    def live() -> HealthResponse:
        return HealthResponse(status="ok")

    @app.get("/api/v1/health/ready", response_model=HealthResponse)
    def ready() -> HealthResponse:
        try:
            with factory() as session:
                session.execute(text("SELECT 1"))
                revision = session.execute(
                    text("SELECT version_num FROM alembic_version")
                ).scalar_one_or_none()
        except Exception as error:
            raise ApiError(503, "SERVICE_NOT_READY", "database is not ready") from error
        if revision != ready_revision:
            raise ApiError(503, "SERVICE_NOT_READY", "database migration is not current")
        return HealthResponse(status="ready")

    return app
