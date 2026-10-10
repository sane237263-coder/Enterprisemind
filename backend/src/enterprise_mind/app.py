import asyncio
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager
from uuid import uuid4

from fastapi import FastAPI, Request, Response
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from starlette.exceptions import HTTPException

from enterprise_mind import __version__
from enterprise_mind.agent.config import AgentSettings, load_agent
from enterprise_mind.agent.events import EventBus
from enterprise_mind.agent.provider import ChatModel
from enterprise_mind.agent.runner import AgentManager
from enterprise_mind.api.attachments import router as attachments_router
from enterprise_mind.api.auth import router
from enterprise_mind.api.conversations import router as conversations_router
from enterprise_mind.api.database import router as database_router
from enterprise_mind.api.diagnostics import capture_unexpected_api_error
from enterprise_mind.api.documents import router as documents_router
from enterprise_mind.api.errors import (
    APIError,
    ErrorCode,
    api_error_handler,
    error_response,
    http_error_handler,
    validation_error_handler,
)
from enterprise_mind.api.generation import router as generation_router
from enterprise_mind.api.lifecycle import router as lifecycle_router
from enterprise_mind.api.management import router as management_router
from enterprise_mind.api.memory import router as memory_router
from enterprise_mind.api.previews import router as previews_router
from enterprise_mind.database.config import load_database
from enterprise_mind.database.export_runtime import ExportRuntimePlanStore
from enterprise_mind.database.mcp_client import create_client
from enterprise_mind.database.pages import DatabasePageStore
from enterprise_mind.database.query import DatabaseQueryService
from enterprise_mind.database.query_context import DatabaseQueryContextStore
from enterprise_mind.database.sql_provider import ChatDatabaseModel
from enterprise_mind.generation.worker import GenerationWorker
from enterprise_mind.identity.security import Passwords, Tokens
from enterprise_mind.infrastructure.database import (
    DatabaseNotReady,
    check_database,
    create_engine,
    session_factory,
)
from enterprise_mind.infrastructure.settings import (
    APISettings,
    ConfigurationError,
    IngestionSettings,
    load_application_config,
    read_secret,
)
from enterprise_mind.ingestion.queue import TaskQueue
from enterprise_mind.ingestion.storage import ObjectStorage
from enterprise_mind.rag.config import RAGSettings, load_rag
from enterprise_mind.rag.providers import HTTPEmbeddings, HTTPReranker
from enterprise_mind.rag.repository import RetrievalRepository
from enterprise_mind.rag.retrieval import RetrievalService


def create_app(
    settings: APISettings | None = None, ingestion_settings: IngestionSettings | None = None
) -> FastAPI:
    settings = settings or APISettings()
    if ingestion_settings is None and settings.env != "test":
        ingestion_settings = IngestionSettings()
    if settings.env == "production":
        from enterprise_mind.infrastructure.provision import APP

        if settings.postgres_user != APP:
            raise ConfigurationError("生产 API 必须使用专用运行账号。")
    config = load_application_config(settings.application_config)
    rag_config, providers = load_rag(RAGSettings())
    agent_settings = AgentSettings()
    agent_config = load_agent(agent_settings)
    language_model = ChatModel(providers.llm) if providers.llm else None
    checkpoint_url = settings.database_url() if settings.env == "test" else None
    if agent_settings.checkpoint_postgres_password_file is not None:
        from enterprise_mind.infrastructure.provision import CHECKPOINT

        if agent_settings.checkpoint_postgres_user != CHECKPOINT:
            raise ConfigurationError("检查点必须使用专用账号。")
        checkpoint_url = settings.database_url().set(
            username=CHECKPOINT,
            password=read_secret(
                agent_settings.checkpoint_postgres_password_file
            ).get_secret_value(),
        )
    if language_model and checkpoint_url is None:
        raise ConfigurationError("启用语言模型前必须配置检查点专用账号。")
    embedding = HTTPEmbeddings(providers.embedding) if providers.embedding else None
    reranker = HTTPReranker(providers.rerank) if providers.rerank else None
    tokens = Tokens(read_secret(settings.token_signing_key_file), config.auth)
    passwords = Passwords(config.auth)
    engine = create_engine(settings, config)
    sessions = session_factory(engine)
    database_config = load_database()
    database_client = create_client(database_config, sessions) if database_config else None
    database_pages = DatabasePageStore()
    database_queries = DatabaseQueryContextStore()
    export_plans = ExportRuntimePlanStore()
    storage = ObjectStorage(ingestion_settings, config.storage) if ingestion_settings else None
    queue = TaskQueue(ingestion_settings) if ingestion_settings else None
    generation_worker = GenerationWorker(
        sessions,
        storage,
        config.phase9,
        worker_config=config.worker,
        database=database_client,
        export_plans=export_plans,
    )
    stage = "ingestion_foundation" if ingestion_settings else "organization_foundation"

    async def check_services() -> None:
        await check_database(engine)
        if storage is not None and queue is not None:
            await asyncio.to_thread(storage.check)
            await queue.check()

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        try:
            try:
                await check_services()
            except Exception:
                raise DatabaseNotReady("数据库、对象存储或任务队列尚未就绪。") from None
            if storage is not None and queue is not None:
                await generation_worker.recover_database_exports()
            if language_model:
                from enterprise_mind.agent.checkpoints import open_saver

                assert checkpoint_url is not None
                async with open_saver(checkpoint_url) as saver:
                    await saver.aget_tuple({"configurable": {"thread_id": "readiness"}})
            app.state.agent.start()
            yield
        finally:
            jobs = list(app.state.phase9_generation_jobs.values())
            for job in jobs:
                job.cancel()
            if jobs:
                await asyncio.gather(*jobs, return_exceptions=True)
            await app.state.agent.close()
            if database_client is not None:
                await database_client.close()
            if queue is not None:
                await queue.close()
            await engine.dispose()

    app = FastAPI(
        title="EnterpriseMind",
        version=__version__,
        lifespan=lifespan,
        docs_url="/docs" if settings.env != "production" else None,
        redoc_url=None,
        openapi_url="/openapi.json" if settings.env != "production" else None,
    )
    app.state.sessions = sessions
    app.state.retrieval = (
        RetrievalService(
            RetrievalRepository(
                app.state.sessions, rag_config, embedding.model_key, embedding.dimensions
            ),
            embedding,
            reranker,
            rag_config,
        )
        if embedding
        else None
    )
    app.state.tokens = tokens
    app.state.passwords = passwords
    app.state.config = config
    app.state.rag_config = rag_config
    app.state.storage = storage
    app.state.queue = queue
    app.state.public_origin = settings.public_origin
    app.state.database_client = database_client
    app.state.database_pages = database_pages
    app.state.database_queries = database_queries
    app.state.export_plans = export_plans
    app.state.generation_worker = generation_worker
    app.state.phase9_generation_jobs = {}
    app.state.agent = AgentManager(
        app.state.sessions,
        checkpoint_url,
        language_model,
        app.state.retrieval,
        agent_config,
        EventBus(
            queue.redis if queue else None,
            (queue.stream + ":agent") if queue else "enterprise-mind:agent",
        ),
        database=(
            DatabaseQueryService(sessions, ChatDatabaseModel(language_model), database_client)
            if database_client is not None and language_model is not None
            else None
        ),
        database_pages=database_pages,
        database_queries=database_queries,
        generation_worker=generation_worker,
        export_plans=export_plans,
        export_plan_owner=database_client,
        phase9=config.phase9,
    )
    app.add_exception_handler(APIError, api_error_handler)
    app.add_exception_handler(RequestValidationError, validation_error_handler)
    app.add_exception_handler(HTTPException, http_error_handler)

    @app.middleware("http")
    async def request_context(
        request: Request, call_next: Callable[[Request], Awaitable[Response]]
    ) -> Response:
        # Generate our own ID; arbitrary incoming headers never reach logs.
        request.state.request_id = str(uuid4())
        try:
            timeout = (
                config.upload.request_timeout_seconds
                if request.url.path.endswith(("/document-uploads", "/overwrite-requests"))
                else config.api.regular_request_timeout_seconds
            )
            async with asyncio.timeout(timeout):
                response = await call_next(request)
        except Exception as error:
            capture_unexpected_api_error(request, error)
            response = error_response(request, APIError(ErrorCode.INTERNAL_ERROR))
        response.headers["X-Request-ID"] = request.state.request_id
        if "Cache-Control" not in response.headers:
            response.headers["Cache-Control"] = "no-store"
        response.headers["X-Content-Type-Options"] = "nosniff"
        return response

    @app.get("/health/live", include_in_schema=False)
    async def live() -> dict[str, str]:
        return {"status": "alive"}

    @app.get("/health/ready", include_in_schema=False)
    async def ready() -> JSONResponse:
        try:
            await check_services()
        except Exception:
            return JSONResponse({"status": "not_ready", "stage": stage}, 503)
        return JSONResponse({"status": "ready", "stage": stage})

    app.include_router(router)
    app.include_router(management_router)
    app.include_router(documents_router)
    app.include_router(lifecycle_router)
    app.include_router(conversations_router)
    app.include_router(attachments_router)
    app.include_router(previews_router)
    app.include_router(database_router)
    app.include_router(memory_router)
    app.include_router(generation_router)
    return app
