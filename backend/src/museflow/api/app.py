from __future__ import annotations

import os
from uuid import UUID, uuid4

from fastapi import FastAPI, Header, Query, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from sqlalchemy import select, text
from sqlalchemy.orm import Session, sessionmaker
from starlette.middleware.base import RequestResponseEndpoint
from starlette.responses import RedirectResponse, Response

from museflow.api.dto import (
    CreateTaskBody,
    HealthResponse,
    TaskAssetResponse,
    TaskAttemptResponse,
    TaskEventResponse,
    TaskListResponse,
    TaskResponse,
    TaskSummaryResponse,
)
from museflow.assets import MinioResultAssetStore, ResultAssetStore
from museflow.db.models import GenerationTaskModel, ResultAssetModel
from museflow.db.session import create_session_factory
from museflow.tasks.application import (
    CreateTask,
    GetTaskDetail,
    IdempotencyConflictError,
    ListTasks,
    ManualRetryNotAllowedError,
    TaskNotFoundError,
)
from museflow.tasks.domain import CreateTaskRequest, DomainValidationError, TaskStatus
from museflow.tasks.execution_models import GenerationAttemptModel

EXPECTED_MIGRATION_REVISION = "0003_retries_and_result_assets"
DEFAULT_DATABASE_URL = "postgresql+psycopg://postgres:postgres@localhost:5432/museflow"


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
) -> FastAPI:
    factory = session_factory or create_session_factory(
        database_url or os.environ.get("MUSEFLOW_DATABASE_URL", DEFAULT_DATABASE_URL)
    )
    create_task = CreateTask(factory)
    get_task_detail = GetTaskDetail(factory)
    list_tasks = ListTasks(factory)
    result_assets = asset_store or MinioResultAssetStore()

    app = FastAPI(title="MuseFlow API", version="0.1.0")

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

    @app.post("/api/v1/tasks", response_model=TaskResponse, status_code=201)
    def create_task_route(
        body: CreateTaskBody,
        idempotency_key: str | None = Header(default=None, alias="Idempotency-Key"),
    ) -> JSONResponse:
        if not idempotency_key:
            raise ApiError(400, "IDEMPOTENCY_KEY_REQUIRED", "Idempotency-Key is required")
        result = create_task.execute(
            CreateTaskRequest(prompt=body.prompt), idempotency_key=idempotency_key
        )
        response = TaskSummaryResponse.from_record(result.task)
        return JSONResponse(
            status_code=200 if result.idempotency_replayed else 201,
            content=TaskResponse(
                **response.model_dump(mode="json"),
                idempotency_replayed=result.idempotency_replayed,
            ).model_dump(mode="json"),
        )

    @app.get("/api/v1/tasks/{task_id}", response_model=TaskResponse)
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
            CreateTaskRequest(prompt=source.task.prompt, size_preset=source.task.size_preset),
            idempotency_key,
            retried_from_task_id=task_id,
        )
        summary = TaskSummaryResponse.from_record(result.task)
        return JSONResponse(
            status_code=200 if result.idempotency_replayed else 201,
            content=TaskResponse(
                **summary.model_dump(mode="json"),
                idempotency_replayed=result.idempotency_replayed,
            ).model_dump(mode="json"),
        )

    @app.get("/api/v1/assets/{asset_id}/download")
    def download_asset(asset_id: UUID) -> RedirectResponse:
        with factory() as session:
            asset = session.get(ResultAssetModel, asset_id)
            if asset is None:
                raise ApiError(404, "ASSET_NOT_FOUND", "result asset was not found")
            task = session.get(GenerationTaskModel, asset.task_id)
            if task is None or task.status != TaskStatus.SUCCEEDED.value:
                raise ApiError(409, "RESULT_NOT_READY", "result is not ready")
            object_key = asset.object_key
        try:
            url = result_assets.presigned_download(object_key, expires_seconds=300)
        except Exception as error:
            raise ApiError(503, "RESULT_STORE_UNAVAILABLE", "result store is not ready") from error
        return RedirectResponse(url, status_code=307)

    @app.get("/api/v1/tasks", response_model=TaskListResponse)
    def list_tasks_route(
        limit: int = Query(default=20, ge=1, le=ListTasks.MAX_LIMIT),
        cursor: str | None = None,
        status: TaskStatus | None = None,
    ) -> TaskListResponse:
        page = list_tasks.execute(limit=limit, cursor=cursor, status=status)
        return TaskListResponse(
            items=[TaskSummaryResponse.from_record(task) for task in page.items],
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
