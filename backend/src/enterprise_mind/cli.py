import argparse
import asyncio
import json
import sys
from pathlib import Path

from pydantic import ValidationError

from enterprise_mind.infrastructure.settings import (
    ConfigurationError,
    DatabaseSettings,
    load_application_config,
)


def _report_failure(command: str, error: BaseException) -> None:
    from enterprise_mind.api.diagnostics import stack_location

    errors: list[dict[str, str]] = []
    seen: set[int] = set()
    current: BaseException | None = error
    while current is not None and id(current) not in seen and len(errors) < 3:
        seen.add(id(current))
        errors.append(
            {"exception_type": type(current).__name__, "stack_location": stack_location(current)}
        )
        # Configuration wrappers suppress public tracebacks. Keep only bounded
        # code locations, never exception text, validation inputs, or locals.
        current = current.__cause__ if current.__cause__ is not None else current.__context__
    print("cli_failure " + json.dumps({"command": command, "errors": errors}), file=sys.stderr)


def main() -> None:
    parser = argparse.ArgumentParser(prog="enterprise-mind")
    commands = parser.add_subparsers(dest="command", required=True)
    provision_parser = commands.add_parser(
        "provision-database",
        help="Provision database roles and ownership using the initialization credential",
    )
    provision_parser.add_argument("--migrator-password-file", type=Path)
    provision_parser.add_argument("--app-password-file", type=Path)
    provision_parser.add_argument("--backup-password-file", type=Path)
    provision_parser.add_argument("--checkpoint-password-file", type=Path)
    commands.add_parser(
        "checkpoint-init", help="Initialize pinned checkpoint tables using migrator"
    )
    migration = commands.add_parser(
        "migrate", help="Apply migrations and initialize an empty system"
    )
    migration.add_argument("--backend-root", type=Path, default=Path("backend"))
    vector_parser = commands.add_parser(
        "build-vector-index", help="Build the configured model index with the migrator credential"
    )
    vector_parser.add_argument("--target-providers", type=Path)
    for name in ("rebuild-embeddings", "activate-embeddings"):
        command = commands.add_parser(name, help="Prepare or activate an embedding model change")
        command.add_argument("--target-providers", type=Path, required=True)
        if name == "activate-embeddings":
            command.add_argument("--services-stopped", action="store_true", required=True)
    serve = commands.add_parser("serve", help="Start the API after checking the schema")
    serve.add_argument("--host", default="127.0.0.1")
    serve.add_argument("--port", type=int, default=8000)
    storage_init = commands.add_parser("storage-init", help="Initialize private storage identity")
    storage_init.add_argument("--root-access-key-file", type=Path, required=True)
    storage_init.add_argument("--root-secret-key-file", type=Path, required=True)
    for name in ("dispatch", "worker"):
        command = commands.add_parser(name, help="Run document processing " + name)
        command.add_argument("--once", action="store_true")
    args = parser.parse_args()
    try:
        settings = DatabaseSettings()
        config = load_application_config(settings.application_config)
        if args.command == "provision-database":
            from enterprise_mind.infrastructure.provision import (
                APP,
                BACKUP,
                CHECKPOINT,
                MIGRATOR,
                provision,
            )

            provision(
                settings.database_url(),
                {
                    MIGRATOR: args.migrator_password_file,
                    APP: args.app_password_file,
                    BACKUP: args.backup_password_file,
                    CHECKPOINT: args.checkpoint_password_file,
                },
            )
            print("数据库角色与归属预配置完成；请切换迁移账号执行 migrate。")
        elif args.command == "migrate":
            from enterprise_mind.infrastructure.migrate import migrate
            from enterprise_mind.infrastructure.provision import MIGRATOR

            if settings.env == "production" and settings.postgres_user != MIGRATOR:
                raise ConfigurationError("生产迁移必须使用专用迁移账号。")
            created = migrate(settings.database_url(), config, args.backend_root.resolve())
            print("迁移与初始化完成。" if created else "迁移完成，已有账号保持不变。")
        elif args.command == "checkpoint-init":
            from enterprise_mind.agent.checkpoints import agent_event_loop, initialize_checkpoints
            from enterprise_mind.infrastructure.provision import MIGRATOR

            if settings.env == "production" and settings.postgres_user != MIGRATOR:
                raise ConfigurationError("生产检查点初始化必须使用迁移账号。")
            with asyncio.Runner(loop_factory=agent_event_loop) as runner:
                runner.run(initialize_checkpoints(settings.database_url()))
            print("检查点框架表已初始化；运行时不执行建表。")
        elif args.command == "build-vector-index":
            from enterprise_mind.infrastructure.provision import MIGRATOR
            from enterprise_mind.rag.config import RAGSettings, load_rag
            from enterprise_mind.rag.vector_index import build_vector_index

            if settings.env == "production" and settings.postgres_user != MIGRATOR:
                raise ConfigurationError("生产索引构建必须使用专用迁移账号。")
            rag_settings = RAGSettings()
            if args.target_providers:
                rag_settings.providers_config = args.target_providers
            rag, providers = load_rag(rag_settings)
            if providers.embedding is None:
                raise ConfigurationError("尚未配置 Embedding 模型，不创建假定维度的索引。")
            build_vector_index(settings.database_url(), providers.embedding, rag.hnsw)
            print("已配置模型的向量索引已建立；效果与召回率仍需离线验收。")
        elif args.command in {"rebuild-embeddings", "activate-embeddings"}:
            from enterprise_mind.infrastructure.provision import MIGRATOR
            from enterprise_mind.rag.config import RAGSettings
            from enterprise_mind.rag.providers import HTTPEmbeddings
            from enterprise_mind.rag.rebuild import (
                activate_embeddings,
                change_profiles,
                rebuild_embeddings,
            )

            if settings.env == "production" and settings.postgres_user != MIGRATOR:
                raise ConfigurationError("生产 Embedding 运维必须使用专用迁移账号。")
            rag_settings = RAGSettings()
            if args.command == "activate-embeddings":
                activate_embeddings(settings.database_url(), rag_settings, args.target_providers)
                print(
                    "Embedding 配置已原子切换，旧配置已备份；请统一启动 API、Worker 和 Dispatcher。"
                )
            else:
                _, target, _ = change_profiles(rag_settings, args.target_providers)
                assert target.embedding
                report = asyncio.run(
                    rebuild_embeddings(
                        settings.database_url(), target.embedding, HTTPEmbeddings(target.embedding)
                    )
                )
                print(
                    f"新写入 {report['written']} 个向量，尚缺 {report['remaining']} 个；"
                    "当前模型配置保持不变。"
                )
        elif args.command == "storage-init":
            from enterprise_mind.infrastructure.settings import IngestionSettings
            from enterprise_mind.ingestion.storage import initialize_storage

            initialize_storage(
                IngestionSettings(),
                config.storage,
                args.root_access_key_file,
                args.root_secret_key_file,
            )
            print("私有 Bucket 和独立应用身份已就绪。")
        elif args.command in {"dispatch", "worker"}:
            from enterprise_mind.ingestion.runtime import run_processing

            asyncio.run(run_processing(args.command, settings, config, once=args.once))
        else:
            import uvicorn

            from enterprise_mind.agent.checkpoints import agent_event_loop
            from enterprise_mind.app import create_app

            # Fail once in the parent instead of continuously restarting unready worker processes.
            app = create_app()

            async def preflight() -> None:
                async with app.router.lifespan_context(app):
                    pass

            with asyncio.Runner(loop_factory=agent_event_loop) as runner:
                runner.run(preflight())
            uvicorn.run(
                "enterprise_mind.app:create_app",
                factory=True,
                host=args.host,
                port=args.port,
                workers=config.api.workers,
                access_log=False,
                ws_max_size=65536,
                loop="enterprise_mind.agent.checkpoints:agent_event_loop",
            )
    except (ConfigurationError, ValidationError) as error:
        _report_failure(args.command, error)
        print("配置或初始化校验失败；请检查配置字段与 Secret，未输出敏感值。", file=sys.stderr)
        raise SystemExit(1) from None
    except Exception as error:
        _report_failure(args.command, error)
        print("命令执行失败；请检查数据库、迁移和运行环境。", file=sys.stderr)
        raise SystemExit(1) from None


if __name__ == "__main__":
    main()
