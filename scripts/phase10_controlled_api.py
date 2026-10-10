"""Run the Phase-10 acceptance API with a bounded controlled model provider.

This process is only for the synthetic Phase-10 acceptance environment.  The
controlled provider supplies a deterministic, authorization-checked database
selection and SQL draft so the real API can create a query context when no
live model/checkpoint credentials are configured.  Query execution still goes
through the configured independent Database MCP client; generation is handled
by the real external Dispatcher/Worker processes.

The evidence must label this provider as ``controlled``.  It is not a live
Qwen or enterprise-model integration result.
"""

from __future__ import annotations

import argparse

import uvicorn

from enterprise_mind.app import create_app
from enterprise_mind.database.query import DatabaseQueryService
from enterprise_mind.database.sql_provider import DatabaseSelection, SQLDraft
from enterprise_mind.infrastructure.settings import APISettings, IngestionSettings
from enterprise_mind.agent.schemas import Plan


class _Counter:
    @staticmethod
    def count(value: object) -> int:
        return len(str(value))


class ControlledDatabaseModel:
    """Deterministic query planner used only with the synthetic fixture."""

    async def select_relations(self, question: str, semantics: list[str], catalog: object) -> DatabaseSelection:
        del question, semantics, catalog
        return DatabaseSelection(
            relations=[{"schema_name": "business", "relation_name": "phase10_orders"}],
            clarification="none",
        )

    async def generate_sql(self, question: str, semantics: list[str], schema: object) -> SQLDraft:
        del question, semantics, schema
        return SQLDraft(
            query_kind="detail",
            sql=(
                'SELECT o.id, o.order_code, o.category, o.occurred_on, o.amount, '
                'o.nullable_note, o.duplicate_group '
                'FROM "business"."phase10_orders" AS o ORDER BY o.id'
            ),
            parameters=[],
            conditions_summary="Phase10 synthetic fixture: all 235 rows ordered by id",
        )


class ControlledAnswerModel:
    """Minimal Agent model; it never receives or serializes query rows."""

    counter = _Counter()
    max_input_tokens = 32_000
    max_output_tokens = 2_000

    async def plan(self, messages: object, tools: object, feedback: object) -> Plan:
        del messages, tools, feedback
        return Plan(
            intent_kind="database",
            requires_enterprise_facts=True,
            tools=[
                {
                    "key": "database",
                    "tool_name": "database_query",
                    "arguments": {
                        "question": "查询 business.phase10_orders 全部订单",
                        "page_index": 0,
                    },
                }
            ],
        )

    async def answer(self, messages: object):
        del messages
        yield "已基于本轮合成业务源完成查询。[[source_1]]"

    async def summarize(self, messages: object) -> str:
        del messages
        return "Phase10 controlled-provider synthetic query."

    async def close(self) -> None:
        return None


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=28080)
    args = parser.parse_args()

    settings = APISettings()
    ingestion_settings = IngestionSettings()
    app = create_app(settings, ingestion_settings)
    app.state.agent.model = ControlledAnswerModel()
    if app.state.database_client is None:
        raise RuntimeError("Phase-10 controlled API requires the configured Database MCP client")
    app.state.agent.database = DatabaseQueryService(
        app.state.sessions,
        ControlledDatabaseModel(),
        app.state.database_client,
    )
    uvicorn.run(
        app,
        host=args.host,
        port=args.port,
        access_log=False,
        ws_max_size=65536,
        loop="enterprise_mind.agent.checkpoints:agent_event_loop",
    )


if __name__ == "__main__":
    main()
