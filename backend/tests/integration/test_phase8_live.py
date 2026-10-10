"""Opt-in paid-provider Phase-8 acceptance with a real MCP HTTP boundary.

Run only with ``EM_TEST_PHASE8_LIVE=1`` and ``EM_TEST_LIVE_MODELS=1`` after
loading the local synthetic environment.  The test never reads or prints a
provider secret and writes only a sanitized Markdown result report.
"""

from __future__ import annotations

import asyncio
import json
import os
import re
import secrets
import subprocess
import sys
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from uuid import uuid4

import httpx
import pytest
from conftest import BACKEND, ROOT
from phase8_helpers import (
    Phase8BusinessFixture,
    Phase8MCPFixture,
    free_port,
    refresh_schema,
    stop_process,
    write_mcp_client_config,
)
from psycopg import sql
from sqlalchemy import text

from enterprise_mind.agent.checkpoints import agent_event_loop, initialize_checkpoints
from enterprise_mind.database.contracts import SchemaCatalog
from enterprise_mind.database.guard import SQLGuard
from enterprise_mind.database.mcp_client import DatabaseMCPClient
from enterprise_mind.infrastructure.migrate import migrate
from enterprise_mind.infrastructure.provision import (
    APP,
    BACKUP,
    CHECKPOINT,
    MIGRATOR,
    OWNER,
    provision,
)
from enterprise_mind.infrastructure.settings import ApplicationConfig
from enterprise_mind.rag.config import RAGSettings, load_rag
from enterprise_mind.rag.vector_index import build_vector_index

pytest_plugins = ("phase8_helpers",)

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(
        os.getenv("EM_TEST_PHASE8_LIVE") != "1",
        reason="Phase-8 live acceptance requires explicit EM_TEST_PHASE8_LIVE=1",
    ),
    pytest.mark.skipif(
        os.getenv("EM_TEST_LIVE_MODELS") != "1",
        reason="Real Qwen calls require explicit EM_TEST_LIVE_MODELS=1",
    ),
]

REPORT_PATH = Path(
    os.getenv("EM_PHASE8_LIVE_REPORT_PATH", str(ROOT / ".local/phase8-live-result.md"))
)


def _write_report(report: dict[str, Any]) -> None:
    REPORT_PATH.parent.mkdir(parents=True, exist_ok=True)
    lines = [
        "# Phase 8 live acceptance result",
        "",
        f"- scope: `{report['scope']}`",
        f"- started_at: `{report['started_at']}`",
        f"- finished_at: `{report.get('finished_at', 'running')}`",
        f"- complete: `{str(bool(report.get('complete', False))).lower()}`",
        f"- provider_model_key: `{report.get('provider_model_key', 'not-loaded')}`",
        "",
        "## Sanitized checks",
        "",
    ]
    for name, result in report.get("checks", {}).items():
        status = result.get("status", "recorded")
        details = []
        for key, value in result.items():
            if key == "status":
                continue
            if isinstance(value, bool):
                rendered = str(value).lower()
            elif isinstance(value, (int, float, str)):
                rendered = str(value).replace("\n", " ")[:160]
            else:
                continue
            details.append(f"{key}={rendered}")
        suffix = f" ({', '.join(details)})" if details else ""
        lines.append(f"- `{name}`: `{status}`{suffix}")
    REPORT_PATH.write_text("\n".join(lines) + "\n", encoding="utf-8")


def _record(report: dict[str, Any], name: str, status: str, **values: Any) -> None:
    safe: dict[str, Any] = {"status": status}
    for key, value in values.items():
        if isinstance(value, (bool, int, float)):
            safe[key] = value
        elif isinstance(value, str) and len(value) <= 160:
            safe[key] = value.replace("\n", " ")
    report.setdefault("checks", {})[name] = safe
    _write_report(report)


def _post(
    client: httpx.Client,
    headers: dict[str, str],
    path: str,
    body: dict[str, Any],
    *,
    status: int = 201,
) -> dict[str, Any]:
    response = client.post(
        path,
        headers={**headers, "Idempotency-Key": uuid4().hex},
        json=body,
    )
    assert response.status_code == status, response.text
    return response.json()["data"]


def _login(client: httpx.Client, username: str, password: str) -> dict[str, str]:
    response = client.post("/api/v1/auth/login", json={"username": username, "password": password})
    assert response.status_code == 200, response.text
    return {"Authorization": "Bearer " + response.json()["data"]["access_token"]}


def _wait_api(client: httpx.Client, process: subprocess.Popen[bytes], port: int) -> None:
    deadline = time.monotonic() + 90
    while time.monotonic() < deadline:
        assert process.poll() is None, "Phase-8 API process exited during startup"
        try:
            response = client.get("/health/ready")
            if response.status_code == 200:
                return
        except httpx.TransportError:
            pass
        time.sleep(0.2)
    raise AssertionError(f"Phase-8 API on port {port} did not become ready")


def _runtime_env(
    services: Any | None, settings: Any, client_config: Path, checkpoint_file: Path
) -> dict[str, str]:
    env = os.environ.copy()
    for model in (settings, services):
        if model is None:
            continue
        for field, value in model.model_dump().items():
            if value is not None:
                env["EM_" + field.upper()] = str(value)
    env["EM_DATABASE_CONFIG"] = str(client_config)
    env["EM_CHECKPOINT_POSTGRES_USER"] = CHECKPOINT
    env["EM_CHECKPOINT_POSTGRES_PASSWORD_FILE"] = str(checkpoint_file)
    env["EM_PUBLIC_ORIGIN"] = "https://enterprise-mind.test"
    env["PYTHONUTF8"] = "1"
    env["PYTHONPATH"] = str(BACKEND / "src") + os.pathsep + env.get("PYTHONPATH", "")
    env.pop("EM_BOOTSTRAP_ADMIN_USERNAME", None)
    env.pop("EM_BOOTSTRAP_ADMIN_PASSWORD_FILE", None)
    return env


def _run_sse(
    client: httpx.Client,
    headers: dict[str, str],
    run_id: str,
    *,
    timeout: float = 700,
) -> tuple[list[str], list[str]]:
    events: list[str] = []
    deltas: list[str] = []
    with client.stream(
        "GET", f"/api/v1/agent-runs/{run_id}/events", headers=headers, timeout=timeout
    ) as response:
        assert response.status_code == 200, response.text
        event_name = ""
        for line in response.iter_lines():
            if line.startswith("event: "):
                event_name = line[7:]
                events.append(event_name)
            elif line.startswith("data: "):
                payload = json.loads(line[6:])
                if event_name == "answer.delta":
                    deltas.append(str(payload.get("text", "")))
                if event_name == "run.status" and payload.get("status") == "waiting_confirmation":
                    break
    return events, deltas


def _run_agent(
    client: httpx.Client,
    headers: dict[str, str],
    question: str,
    *,
    continuation: tuple[str, str, str] | None = None,
    conversation_id: str | None = None,
) -> dict[str, Any]:
    if continuation is None:
        conversation = (
            {"id": conversation_id}
            if conversation_id is not None
            else _post(client, headers, "/api/v1/conversations", {"title": "phase8-live"})
        )
        path = f"/api/v1/conversations/{conversation['id']}/messages"
        body = {"content_text": question}
    else:
        conversation_id, query_id, result_id = continuation
        conversation = {"id": conversation_id}
        path = f"/api/v1/conversations/{conversation_id}/database-queries/{query_id}/pages"
        body = {"query_id": query_id, "result_id": result_id}
    run = _post(client, headers, path, body, status=202)
    events, deltas = _run_sse(client, headers, run["id"])
    state = client.get(f"/api/v1/agent-runs/{run['id']}", headers=headers)
    assert state.status_code == 200, state.text
    run_state = state.json()["data"]
    messages = client.get(f"/api/v1/conversations/{conversation['id']}/messages", headers=headers)
    assert messages.status_code == 200, messages.text
    history = messages.json()["data"]
    assistant = next(
        (item for item in history if item["id"] == run_state.get("assistant_message_id")),
        None,
    )
    return {
        "conversation_id": conversation["id"],
        "run": run_state,
        "assistant": assistant,
        "history": history,
        "events": events,
        "deltas": deltas,
    }


def _database_sources(result: dict[str, Any]) -> list[dict[str, Any]]:
    assistant = result.get("assistant") or {}
    return [item for item in assistant.get("sources", []) if item.get("source_type") == "database"]


def _assert_order_facts(text: str) -> None:
    normalized = text.replace(",", "").replace("，", "")
    assert re.search(r"(?<!\d)235(?!\d)", normalized)
    assert re.search(r"(?<!\d)2350(?!\d)", normalized)


def _require_completed(runtime: dict[str, Any], result: dict[str, Any], check: str) -> None:
    run = result["run"]
    if run.get("status") != "completed":
        tool_summary = "unavailable"
        try:
            with runtime["database"].engine.connect() as connection:
                calls = connection.execute(
                    text(
                        "SELECT tool_name, status, error_code FROM agent.tool_calls "
                        "WHERE agent_run_id = :run_id ORDER BY id"
                    ),
                    {"run_id": int(run["id"])},
                ).all()
            tool_summary = ";".join(
                f"{tool}:{status}:{error or 'none'}" for tool, status, error in calls
            )[:160]
        except Exception:
            pass
        runtime["record"](
            check,
            "failed",
            run_status=str(run.get("status")),
            failure_code=str(run.get("failure_code") or "missing"),
            current_step=str(run.get("current_step") or "unknown"),
            tool_summary=tool_summary,
        )
        raise AssertionError(
            f"{check} did not complete: {run.get('status')}/{run.get('failure_code')}"
        )


def _source_preview(
    client: httpx.Client, headers: dict[str, str], source: dict[str, Any]
) -> dict[str, Any]:
    response = client.get(f"/api/v1/answer-sources/{source['id']}/preview", headers=headers)
    assert response.status_code == 200, response.text
    return response.json()["data"]


async def _direct_mcp_checks(
    business: Phase8BusinessFixture, mcp: Phase8MCPFixture
) -> dict[str, Any]:
    client = DatabaseMCPClient(mcp.endpoint, business.token, source=business.source_metadata)
    try:
        catalog = await client.fetch_schema()
        names = {f"{item.schema_name}.{item.relation_name}" for item in catalog.relations}
        assert business.orders_relation in names
        assert business.secure_relation in names
        assert business.finance_relation in names
        assert business.readable_relation in names
        assert f"{business.private_schema}.{business.private_table}" not in names
        relation = catalog.get(business.sales_schema, business.orders_table)
        assert relation is not None
        quoted = relation.quoted_qualified_name
        guard = SQLGuard(SchemaCatalog((relation,)))
        aggregate = guard.validate(
            f'SELECT count(*) AS row_count, sum("amount") AS total_amount '
            f'FROM {quoted} WHERE "status" = $1::text',
            ("paid",),
            page_size=1,
        )
        aggregate_result = await client.execute_query(aggregate, SchemaCatalog((relation,)), 1)
        assert aggregate_result.row_count == 1
        aggregate_values = list(aggregate_result.rows[0].values())
        assert "235" in aggregate_values
        assert any(value in {"2350", "2350.00"} for value in aggregate_values)
        page_counts: list[int] = []
        seen_ids: list[int] = []
        page_sql = (
            f'SELECT "id", "order_date", "amount", "status" FROM {quoted} '
            'WHERE "status" = $1::text ORDER BY "id"'
        )
        for page_index in range(3):
            guarded = guard.validate(
                page_sql,
                ("paid",),
                page_size=100,
                page_index=page_index,
            )
            result = await client.execute_query(guarded, SchemaCatalog((relation,)), 1)
            page_counts.append(result.row_count)
            seen_ids.extend(int(row["id"]) for row in result.rows)
            assert result.has_more is (page_index < 2)
        assert page_counts == [100, 100, 35]
        assert seen_ids == list(range(1, 236))
        top = guard.validate(
            f'SELECT "id", "amount" FROM {quoted} WHERE "status" = $1::text '
            'ORDER BY "amount" DESC, "id" DESC LIMIT 10',
            ("paid",),
            page_size=100,
        )
        top_result = await client.execute_query(top, SchemaCatalog((relation,)), 1)
        assert top_result.row_count == 10 and not top_result.has_more
        return {
            "relations_visible": len(names),
            "private_relation_hidden": True,
            "paid_count_verified": True,
            "paid_sum_verified": True,
            "page_counts": ",".join(str(value) for value in page_counts),
            "no_duplicate_or_missing_ids": True,
            "top10_verified": True,
        }
    finally:
        await client.close()


def _build_live_runtime(
    database: Any,
    phase8_mcp: Phase8MCPFixture,
    tmp_path: Path,
    *,
    services: Any | None,
    require_rag: bool,
):
    if not os.getenv("EM_PROVIDERS_CONFIG") or not os.getenv("EM_RAG_CONFIG"):
        pytest.skip("Load .local/model-env.ps1 with local provider and RAG configuration")
    if not os.getenv("EM_LLM_API_KEY_FILE"):
        pytest.skip("Load the capability-named llm_api_key.txt through model-env.ps1")
    rag, providers = load_rag(RAGSettings())
    if not providers.llm:
        pytest.skip("Phase-8 live acceptance requires the configured Qwen LLM provider")
    if require_rag and (not providers.embedding or not providers.rerank):
        pytest.skip("Phase-8 live acceptance requires LLM, embedding and rerank providers")

    report: dict[str, Any] = {
        "scope": (
            "synthetic_qwen_agent_database_mcp_and_rag"
            if require_rag
            else "synthetic_qwen_agent_database_mcp"
        ),
        "started_at": datetime.now(UTC).isoformat(),
        "complete": False,
        "provider_model_key": providers.llm.model_key,
        "checks": {},
    }
    _write_report(report)
    database.initialize()
    process: subprocess.Popen[bytes] | None = None
    process_log: Any | None = None
    files: dict[str, Path] = {}
    provisioned = False
    api_port = free_port()
    client_config = write_mcp_client_config(
        phase8_mcp.business,
        phase8_mcp.endpoint,
        tmp_path / "database.yaml",
    )
    try:
        for role in (MIGRATOR, APP, BACKUP, CHECKPOINT):
            files[role] = tmp_path / role
            files[role].write_text(secrets.token_urlsafe(32), encoding="utf-8")
        provision(database.url, files)
        provisioned = True
        migrator_url = database.url.set(
            username=MIGRATOR,
            password=files[MIGRATOR].read_text(encoding="utf-8"),
        )
        migrate(migrator_url, ApplicationConfig(), BACKEND)
        if services is not None:
            assert providers.embedding is not None
            build_vector_index(migrator_url, providers.embedding, rag.hnsw)
        with asyncio.Runner(loop_factory=agent_event_loop) as runner:
            runner.run(initialize_checkpoints(migrator_url))
        settings = database.settings.model_copy(
            update={
                "env": "production" if services is not None else "test",
                "public_origin": "https://enterprise-mind.test",
                "postgres_user": APP,
                "postgres_password_file": files[APP],
            }
        )
        env = _runtime_env(services, settings, client_config, files[CHECKPOINT])
        if os.getenv("EM_TEST_PHASE8_GUARD_TRACE") == "1":
            trace_path = tmp_path / "guard-reasons.jsonl"
            trace_module_dir = Path(os.environ["EM_TEST_PHASE8_TRACE_MODULE_DIR"])
            assert (trace_module_dir / "sitecustomize.py").is_file()
            env["PYTHONPATH"] = str(trace_module_dir) + os.pathsep + env["PYTHONPATH"]
            env["EM_TEST_GUARD_REASON_FILE"] = str(trace_path)
            runtime_trace = os.getenv("EM_TEST_PHASE8_GUARD_TRACE_OUTPUT")
            if runtime_trace:
                env["EM_TEST_GUARD_REASON_FILE"] = runtime_trace
        process_log = (tmp_path / "phase8-api.log").open("wb")
        process = subprocess.Popen(
            [
                sys.executable,
                "-c",
                "import uvicorn; uvicorn.run('enterprise_mind.app:create_app', "
                f"factory=True, host='127.0.0.1', port={api_port}, access_log=False, "
                "log_config=None, loop='enterprise_mind.agent.checkpoints:agent_event_loop')",
            ],
            cwd=BACKEND,
            env=env,
            stdout=process_log,
            stderr=subprocess.STDOUT,
            creationflags=subprocess.CREATE_NO_WINDOW if sys.platform == "win32" else 0,
        )
        with httpx.Client(
            base_url=f"http://127.0.0.1:{api_port}", timeout=45, trust_env=False
        ) as client:
            _wait_api(client, process, api_port)
            admin = _login(client, "初始管理员", database.admin_password)
            _record(report, "api_and_checkpoint", "passed", api_ready=True)
            yield {
                "client": client,
                "admin": admin,
                "database": database,
                "business": phase8_mcp.business,
                "mcp": phase8_mcp,
                "settings": database.settings,
                "env": env,
                "rag": rag,
                "providers": providers,
                "report": report,
                "record": lambda name, status, **values: _record(report, name, status, **values),
            }
    finally:
        stop_process(process)
        if process_log is not None:
            process_log.close()
        if provisioned:
            try:
                with database.engine.begin() as connection:
                    if connection.scalar(
                        text("SELECT 1 FROM pg_roles WHERE rolname=:role"), {"role": OWNER}
                    ):
                        connection.execute(text(f'REASSIGN OWNED BY {OWNER} TO "em_test_owner"'))
                    for role in (CHECKPOINT, APP, BACKUP, MIGRATOR, OWNER):
                        if connection.scalar(
                            text("SELECT 1 FROM pg_roles WHERE rolname=:role"), {"role": role}
                        ):
                            connection.execute(text(f"DROP OWNED BY {role}"))
                            connection.execute(text(f"DROP ROLE {role}"))
            except Exception:
                report["cleanup_error"] = "application role cleanup failed"
        report["finished_at"] = datetime.now(UTC).isoformat()
        _write_report(report)


@pytest.fixture
def phase8_live_database_runtime(database, phase8_mcp, tmp_path):
    yield from _build_live_runtime(
        database,
        phase8_mcp,
        tmp_path,
        services=None,
        require_rag=False,
    )


@pytest.fixture
def phase8_live_runtime(database, ingestion_settings, phase8_mcp, tmp_path):
    services, _, _ = ingestion_settings
    yield from _build_live_runtime(
        database,
        phase8_mcp,
        tmp_path,
        services=services,
        require_rag=True,
    )


def _run_database_acceptance(
    runtime: dict[str, Any], *, setup_only: bool = False
) -> dict[str, Any]:
    client: httpx.Client = runtime["client"]
    admin: dict[str, str] = runtime["admin"]
    business: Phase8BusinessFixture = runtime["business"]
    mcp: Phase8MCPFixture = runtime["mcp"]
    record = runtime["record"]
    database = runtime["database"]

    with asyncio.Runner(loop_factory=agent_event_loop) as runner:
        direct = runner.run(_direct_mcp_checks(business, mcp))
    record("real_mcp_http", "passed", **direct)

    source_response = client.put(
        "/api/v1/admin/database-mcp/source",
        headers={**admin, "Idempotency-Key": "phase8-source"},
        json=business.source_body,
    )
    assert source_response.status_code == 201, source_response.text
    assert "secret_ref" not in source_response.text
    record("source_configured_without_secret_echo", "passed")

    connection = client.post("/api/v1/admin/database-mcp/test-connection", headers=admin)
    assert connection.status_code == 200, connection.text
    assert connection.json()["data"]["reachable"] is True
    record(
        "real_mcp_connection_and_readonly_preflight",
        "passed",
        relation_count=connection.json()["data"]["relation_count"],
    )

    refresh = _post(
        client,
        admin,
        "/api/v1/admin/database-mcp/schema-refresh",
        {},
        status=202,
    )
    assert refresh["status"] == "building"
    processed = refresh_schema(database.settings, mcp.endpoint, business.token, limit=1)
    assert processed == 1
    schema = client.get("/api/v1/admin/database-mcp/schema", headers=admin)
    assert schema.status_code == 200, schema.text
    assert schema.json()["data"]["schema_snapshot_id"] == refresh["id"]
    record("schema_refresh_and_identity_catalog", "passed", processed=processed)

    sales_department = _post(client, admin, "/api/v1/admin/departments", {"name": "Phase 8 Sales"})
    finance_department = _post(
        client, admin, "/api/v1/admin/departments", {"name": "Phase 8 Finance"}
    )
    sales_password = secrets.token_urlsafe(24)
    finance_password = secrets.token_urlsafe(24)
    _post(
        client,
        admin,
        "/api/v1/admin/users",
        {
            "username": "phase8-sales-user",
            "password": sales_password,
            "role": "user",
            "department_id": sales_department["id"],
        },
    )
    _post(
        client,
        admin,
        "/api/v1/admin/users",
        {
            "username": "phase8-finance-user",
            "password": finance_password,
            "role": "user",
            "department_id": finance_department["id"],
        },
    )
    sales = _login(client, "phase8-sales-user", sales_password)
    finance = _login(client, "phase8-finance-user", finance_password)

    for _key, schema_name, relation_name, departments in (
        ("sales-orders", business.sales_schema, business.orders_table, [sales_department["id"]]),
        ("sales-secure", business.sales_schema, business.secure_view, [sales_department["id"]]),
        (
            "archive-orders",
            business.archive_schema,
            business.orders_table,
            [sales_department["id"]],
        ),
        (
            "finance-budget",
            business.finance_schema,
            business.finance_table,
            [finance_department["id"]],
        ),
    ):
        policy = client.put(
            "/api/v1/admin/database-mcp/relation-policies",
            headers=admin,
            json={
                "schema_name": schema_name,
                "relation_name": relation_name,
                "enabled": True,
                "department_ids": departments,
            },
        )
        assert policy.status_code == 200, policy.text
    record("department_relation_policies", "passed", policy_count=4)
    if setup_only:
        return {"sales": sales}
    orders_relation = business.orders_relation
    finance_relation = business.finance_relation

    count_sum = _run_agent(
        client,
        sales,
        (
            f"请查询 {orders_relation} 中 status 为 paid 的订单数量和 amount 总和，"
            "必须使用数据库聚合，给出数据库来源。"
        ),
    )
    _require_completed(runtime, count_sum, "qwen_database_count_sum_with_source")
    count_sources = _database_sources(count_sum)
    assert count_sources
    count_preview = _source_preview(client, sales, count_sources[0])
    count_values = list(count_preview.get("rows", [{}])[0].values())
    assert "235" in count_values
    assert any(value in {"2350", "2350.00"} for value in count_values)
    assert count_sources[0]["referenced_relations"]
    count_text = str((count_sum.get("assistant") or {}).get("content_text") or "")
    _assert_order_facts(count_text)
    record(
        "qwen_database_count_sum_with_source",
        "passed",
        database_sources=len(count_sources),
        aggregate_row_count=count_preview.get("returned_rows", 0),
    )

    top10 = _run_agent(
        client,
        sales,
        (
            f"请查询 {orders_relation} 中 paid 订单，按 amount 从高到低返回前 10 条明细，"
            "并附数据库来源。"
        ),
    )
    _require_completed(runtime, top10, "qwen_database_top10")
    top_sources = _database_sources(top10)
    assert top_sources
    top_preview = _source_preview(client, sales, top_sources[0])
    assert top_preview.get("returned_rows") == 10
    record("qwen_database_top10", "passed", returned_rows=top_preview["returned_rows"])

    detail = _run_agent(
        client,
        sales,
        (
            f"请查询 {orders_relation} 的全部 paid 订单明细，按 id 升序，每页返回 100 行，"
            "并附数据库来源。"
        ),
    )
    _require_completed(runtime, detail, "qwen_database_continuation_pages")
    page_sources = _database_sources(detail)
    assert page_sources
    page_rows: list[int] = []
    current_source = page_sources[0]
    first_preview = _source_preview(client, sales, current_source)
    assert first_preview["returned_rows"] == 100 and first_preview["has_more"] is True
    page_rows.extend(int(row["id"]) for row in first_preview["rows"])
    continuation: tuple[str, str, str] = (
        detail["conversation_id"],
        current_source["query_id"],
        current_source["result_id"],
    )
    continuation_results = []
    for expected_count, expected_more in ((100, True), (35, False)):
        next_page = _run_agent(
            client,
            sales,
            continuation[2],
            continuation=continuation,
        )
        assert next_page["run"]["status"] == "completed"
        sources = _database_sources(next_page)
        assert sources
        preview = _source_preview(client, sales, sources[0])
        assert preview["returned_rows"] == expected_count
        assert preview["has_more"] is expected_more
        page_rows.extend(int(row["id"]) for row in preview["rows"])
        continuation_results.append(preview["returned_rows"])
        continuation = (
            next_page["conversation_id"],
            sources[0]["query_id"],
            sources[0]["result_id"],
        )
    assert page_rows == list(range(1, 236))
    assert continuation_results == [100, 35]
    record(
        "qwen_database_continuation_pages",
        "passed",
        page_counts="100,100,35",
        no_duplicate_or_missing_ids=True,
    )

    ambiguous = _run_agent(client, sales, "请查询 orders 的数量。")
    assert ambiguous["run"]["status"] == "waiting_confirmation"
    pending_id = ambiguous["run"].get("pending_action_id")
    assert pending_id
    pending = client.get(f"/api/v1/pending-actions/{pending_id}", headers=sales)
    assert pending.status_code == 200, pending.text
    pending_data = pending.json()["data"]
    assert pending_data["action_type"] == "disambiguation"
    targets = pending_data["targets"]
    assert len(targets) >= 2
    selected_target = targets[0]["id"]
    accepted = _post(
        client,
        sales,
        f"/api/v1/pending-actions/{pending_id}/responses",
        {"decision": "accept", "selected_target_id": selected_target},
        status=200,
    )
    assert accepted["status"] == "accepted"
    _run_sse(client, sales, ambiguous["run"]["id"])
    final_ambiguous = client.get(f"/api/v1/agent-runs/{ambiguous['run']['id']}", headers=sales)
    assert final_ambiguous.status_code == 200
    assert final_ambiguous.json()["data"]["status"] == "completed"
    record("qwen_needed_clarification_then_explicit_choice", "passed", target_count=len(targets))

    forbidden = _run_agent(
        client,
        sales,
        f"请查询 {finance_relation} 的预算总额，并给出数据库依据。",
    )
    assert forbidden["run"]["status"] == "completed", forbidden["run"]
    forbidden_text = str((forbidden.get("assistant") or {}).get("content_text") or "")
    assert "PRIVATE_DB_SENTINEL" not in forbidden_text
    assert forbidden_text.strip()
    assert any(
        marker in forbidden_text
        for marker in ("权限", "无权", "无法", "不可访问", "未授权", "拒绝")
    )
    assert all(
        business.finance_relation not in item.get("referenced_relations", [])
        for item in _database_sources(forbidden)
    )
    finance_cross = _run_agent(
        client,
        finance,
        f"请查询 {orders_relation} 的 paid 订单数量。",
    )
    assert finance_cross["run"]["status"] == "completed", finance_cross["run"]
    finance_cross_text = str((finance_cross.get("assistant") or {}).get("content_text") or "")
    assert finance_cross_text.strip()
    assert any(
        marker in finance_cross_text
        for marker in ("权限", "无权", "无法", "不可访问", "未授权", "拒绝")
    )
    assert not _database_sources(finance_cross)
    record("department_cross_scope_refusal_without_private_data", "passed")

    outsider_department = _post(
        client, admin, "/api/v1/admin/departments", {"name": "Phase 8 No Database Access"}
    )
    outsider_password = secrets.token_urlsafe(24)
    _post(
        client,
        admin,
        "/api/v1/admin/users",
        {
            "username": "phase8-no-database-user",
            "password": outsider_password,
            "role": "user",
            "department_id": outsider_department["id"],
        },
    )
    outsider = _login(client, "phase8-no-database-user", outsider_password)
    outsider_result = _run_agent(
        client,
        outsider,
        f"请查询 {orders_relation} 的 paid 订单数量。",
    )
    assert outsider_result["run"]["status"] == "completed", outsider_result["run"]
    outsider_text = str((outsider_result.get("assistant") or {}).get("content_text") or "")
    assert outsider_text.strip()
    assert any(
        marker in outsider_text for marker in ("权限", "无权", "无法", "不可访问", "未授权", "拒绝")
    )
    assert not _database_sources(outsider_result)
    record("authenticated_user_without_relation_grant", "passed")

    no_auth = client.get("/api/v1/admin/database-mcp/schema")
    assert no_auth.status_code == 401
    record("no_auth_rejected", "passed", http_status=no_auth.status_code)
    return {"sales": sales}


def test_phase8_real_qwen_database_mcp(phase8_live_database_runtime):
    runtime = phase8_live_database_runtime
    _run_database_acceptance(runtime)
    runtime["record"]("qwen_database_chain", "passed", rag_not_run=True)


def test_phase8_real_qwen_three_natural_language_paging_chains(phase8_live_database_runtime):
    """Three consecutive Qwen→Agent→independent MCP chains over 235 matching rows."""

    runtime = phase8_live_database_runtime
    client: httpx.Client = runtime["client"]
    admin: dict[str, str] = runtime["admin"]
    business: Phase8BusinessFixture = runtime["business"]
    mcp: Phase8MCPFixture = runtime["mcp"]
    record = runtime["record"]

    # Out-of-range and unpaid rows make omitted predicates observable in the result.
    with business.admin() as connection:
        with connection.cursor() as cursor:
            cursor.executemany(
                sql.SQL(
                    "INSERT INTO {}.orders (id, order_date, amount, status, department) "
                    "VALUES (%s, %s, %s, %s, %s)"
                ).format(sql.Identifier(business.sales_schema)),
                [
                    (236, "2025-12-31", "10.00", "paid", "sales"),
                    (237, "2026-02-01", "10.00", "paid", "sales"),
                    (238, "2026-01-15", "10.00", "unpaid", "sales"),
                ],
            )

    source = client.put(
        "/api/v1/admin/database-mcp/source",
        headers={**admin, "Idempotency-Key": "p8-r2-source"},
        json=business.source_body,
    )
    assert source.status_code == 201
    reachable = client.post("/api/v1/admin/database-mcp/test-connection", headers=admin)
    assert reachable.status_code == 200 and reachable.json()["data"]["reachable"] is True
    refresh = _post(client, admin, "/api/v1/admin/database-mcp/schema-refresh", {}, status=202)
    assert refresh_schema(runtime["settings"], mcp.endpoint, business.token, limit=1) == 1
    schema = client.get("/api/v1/admin/database-mcp/schema", headers=admin)
    assert schema.status_code == 200
    snapshot_id = schema.json()["data"]["schema_snapshot_id"]
    assert snapshot_id == refresh["id"]
    department = _post(client, admin, "/api/v1/admin/departments", {"name": "P8-R2 reader"})
    password = secrets.token_urlsafe(24)
    _post(
        client,
        admin,
        "/api/v1/admin/users",
        {
            "username": "p8-r2-reader",
            "password": password,
            "role": "user",
            "department_id": department["id"],
        },
    )
    policy = client.put(
        "/api/v1/admin/database-mcp/relation-policies",
        headers=admin,
        json={
            "schema_name": business.sales_schema,
            "relation_name": business.orders_table,
            "enabled": True,
            "department_ids": [department["id"]],
        },
    )
    assert policy.status_code == 200
    reader = _login(client, "p8-r2-reader", password)
    record("p8_r2_preflight", "passed", source_active=True, snapshot_id=snapshot_id)

    for attempt in range(1, 4):
        question = (
            f"请查询当前授权数据库 {business.orders_relation} 中 order_date >= '2026-01-01' "
            "且 order_date < '2026-02-01'、status 为 paid 的全部订单明细，"
            "按 id 升序，每页返回 100 行。请说明本页行数、has_more、来源和查询时间。"
        )
        current = _run_agent(client, reader, question)
        conversation_id = current["conversation_id"]
        ids: list[int] = []
        query_ids: list[str] = []
        snapshot_ids: list[str] = []
        page_counts: list[int] = []
        more_flags: list[bool] = []
        for index in range(3):
            _require_completed(runtime, current, f"p8_r2_attempt_{attempt}_page_{index + 1}")
            sources = _database_sources(current)
            if len(sources) != 1:
                run = current["run"]
                with runtime["database"].engine.connect() as connection:
                    calls = connection.execute(
                        text(
                            "SELECT tool_name, status, error_code FROM agent.tool_calls "
                            "WHERE agent_run_id = :run_id ORDER BY id"
                        ),
                        {"run_id": int(run["id"])},
                    ).all()
                record(
                    f"p8_r2_attempt_{attempt}_page_{index + 1}",
                    "failed",
                    reason="no_database_source",
                    tool_summary=";".join(
                        f"{tool}:{status}:{error or 'none'}" for tool, status, error in calls
                    ),
                    replan_count=run.get("replan_count", 0),
                    assistant_chars=len(
                        str((current.get("assistant") or {}).get("content_text") or "")
                    ),
                )
            assert len(sources) == 1
            preview = _source_preview(client, reader, sources[0])
            page_counts.append(preview["returned_rows"])
            more_flags.append(preview["has_more"])
            ids.extend(int(row["id"]) for row in preview["rows"])
            query_ids.append(preview["query_id"])
            snapshot_ids.append(str(sources[0]["schema_snapshot_id"]))
            assert preview["queried_at"]
            assert preview["referenced_relations"] == [
                f'"{business.sales_schema}"."{business.orders_table}"'
            ]
            receipt = re.sub(
                r"[*`]", "", str((current.get("assistant") or {}).get("content_text") or "")
            )
            assert "[1]" in receipt
            assert re.search(rf"(?<!\d){preview['returned_rows']}(?!\d)", receipt)
            more = re.search(r"has_more\s*[:：=]\s*(true|false)", receipt, re.IGNORECASE)
            assert more is not None and (more.group(1).lower() == "true") is preview["has_more"]
            assert re.search(r"\d{4}-\d{2}-\d{2}[T\s]\d{2}:\d{2}", receipt)
            if index < 2:
                current = _run_agent(
                    client,
                    reader,
                    "继续下一页，沿用刚才的订单查询。",
                    conversation_id=conversation_id,
                )
        assert page_counts == [100, 100, 35]
        assert more_flags == [True, True, False]
        assert ids == list(range(1, 236))
        assert len(set(query_ids)) == len(set(snapshot_ids)) == 1
        assert snapshot_ids[0] == str(snapshot_id)
        terminal = _run_agent(
            client, reader, "继续下一页，沿用刚才的订单查询。", conversation_id=conversation_id
        )
        _require_completed(runtime, terminal, f"p8_r2_attempt_{attempt}_terminal")
        assert not _database_sources(terminal)
        assert "最后一页" in str((terminal.get("assistant") or {}).get("content_text") or "")
        record(
            f"p8_r2_attempt_{attempt}",
            "passed",
            page_counts="100,100,35",
            has_more="true,true,false",
            ordered_ids_once=True,
            query_binding_stable=True,
        )

    record("p8_r2_three_chains", "passed", excluded_control_rows=3)
    runtime["report"]["complete"] = True
    _write_report(runtime["report"])


def test_phase8_real_qwen_database_mcp_and_rag(phase8_live_runtime):
    runtime = phase8_live_runtime
    database_state = _run_database_acceptance(runtime, setup_only=True)
    client: httpx.Client = runtime["client"]
    admin: dict[str, str] = runtime["admin"]
    sales: dict[str, str] = database_state["sales"]
    record = runtime["record"]
    business: Phase8BusinessFixture = runtime["business"]
    orders_relation = business.orders_relation

    public = _post(
        client,
        admin,
        "/api/v1/knowledge-bases",
        {"name": "Phase 8 public facts", "type": "public"},
    )
    policy_text = (
        "# Phase 8 synthetic order policy\n\n"
        "合成订单政策规定：paid 订单的数量按 status 为 paid 的订单行计数，"
        "金额总和按这些订单的 amount 求和。销售部门管理该订单表。"
    )
    upload = client.post(
        f"/api/v1/knowledge-bases/{public['id']}/document-uploads",
        headers={**admin, "Idempotency-Key": uuid4().hex},
        files=[("files", ("phase8-order-policy.md", policy_text.encode(), "text/markdown"))],
    )
    assert upload.status_code == 202, upload.text
    for command in ("dispatch", "worker", "worker"):
        completed = subprocess.run(
            [sys.executable, "-m", "enterprise_mind.cli", command, "--once"],
            cwd=BACKEND,
            env=runtime["env"],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            timeout=300,
        )
        assert completed.returncode == 0
    joint = _run_agent(
        client,
        sales,
        (
            "请先检索文档 phase8-order-policy.md 中 paid 订单数量与金额总和的定义，"
            f"再查询数据库表 {orders_relation} 中 paid 订单的数量和 amount 总和。"
            "回答必须同时给出知识库引用和数据库来源，不要猜测。"
        ),
    )
    assert joint["run"]["status"] == "completed"
    with runtime["database"].engine.connect() as connection:
        joint_calls = connection.execute(
            text(
                "SELECT tool_name, status, error_code, result_summary FROM agent.tool_calls "
                "WHERE agent_run_id = :run_id ORDER BY id"
            ),
            {"run_id": int(joint["run"]["id"])},
        ).all()
    tool_summary = ";".join(
        f"{name}:{status}:{code or 'none'}" for name, status, code, _ in joint_calls
    )
    retrieval_summary = (
        next((summary for name, _, _, summary in joint_calls if name == "knowledge_retrieve"), {})
        or {}
    )
    record(
        "joint_tool_execution",
        "observed",
        tools=tool_summary,
        retrieval_status=retrieval_summary.get("status", "missing"),
        retrieved_chunks=len(retrieval_summary.get("chunk_ids", [])),
    )
    joint_sources = (joint.get("assistant") or {}).get("sources", [])
    assert any(item.get("source_type") == "knowledge" for item in joint_sources)
    assert any(item.get("source_type") == "database" for item in joint_sources), tool_summary
    joint_text = str((joint.get("assistant") or {}).get("content_text") or "")
    assert "PRIVATE_DB_SENTINEL" not in joint_text
    _assert_order_facts(joint_text)
    joint_values = []
    for source in joint_sources:
        if source.get("source_type") == "database":
            preview = _source_preview(client, sales, source)
            if preview.get("rows"):
                joint_values.extend(preview["rows"][0].values())
    assert "235" in joint_values
    assert any(value in {"2350", "2350.00"} for value in joint_values)
    assert "paid" in joint_text.lower() or "已付款" in joint_text
    record(
        "qwen_joint_rag_database_answer",
        "passed",
        knowledge_source=True,
        database_source=True,
        private_sentinel_absent=True,
    )
    runtime["report"]["complete"] = True
    _write_report(runtime["report"])
