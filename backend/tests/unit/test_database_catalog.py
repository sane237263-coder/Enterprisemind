"""Deterministic catalog identity tests for the stage-8 database boundary."""

import json

import pytest

from enterprise_mind.database.catalog import catalog_to_wire, fetch_catalog


class _CatalogConnection:
    def __init__(self, relation_rows, column_rows, constraint_rows):
        self._responses = [relation_rows, column_rows, constraint_rows]

    async def fetch(self, _query, *_parameters):
        return self._responses.pop(0)


def _rows(*, oid: int, view_definition: str):
    return _CatalogConnection(
        [
            {
                "relation_oid": oid,
                "nspname": "sales",
                "relname": "v_orders",
                "relkind": "v",
                "comment": "safe view",
                "view_definition": view_definition,
            }
        ],
        [
            {
                "attrelid": oid,
                "attnum": 1,
                "attname": "order_id",
                "is_nullable": False,
                "data_type": "bigint",
                "comment": None,
            },
            {
                "attrelid": oid,
                "attnum": 2,
                "attname": "region",
                "is_nullable": True,
                "data_type": "text",
                "comment": None,
            },
        ],
        [
            {
                "conrelid": oid,
                "conname": "v_orders_order_id_key",
                "contype": "u",
                "conkey": [1, 2],
                "confkey": None,
                "confrelid": None,
                "referenced_schema": None,
                "referenced_relation": None,
            }
        ],
    )


@pytest.mark.asyncio
async def test_catalog_retains_oid_and_only_projects_view_signature():
    first = await fetch_catalog(_rows(oid=111, view_definition="SELECT order_id, region"))
    rebuilt = await fetch_catalog(_rows(oid=222, view_definition="SELECT order_id, region"))
    changed_view = await fetch_catalog(
        _rows(oid=111, view_definition="SELECT order_id, NULL::text AS region")
    )

    assert first.relations[0].relation_oid == 111
    assert rebuilt.relations[0].relation_oid == 222
    assert first.relations[0] != rebuilt.relations[0]
    assert first.relations[0].relation_signature != rebuilt.relations[0].relation_signature
    assert first.relations[0].relation_signature != changed_view.relations[0].relation_signature
    # A composite unique key is retained as a constraint, not falsely reported
    # as two independent unique columns.
    assert not any(column.is_unique for column in first.relations[0].columns)
    wire = json.dumps(catalog_to_wire(first), ensure_ascii=False)
    assert '"view_definition":' not in wire
    assert first.relations[0].view_definition_signature in wire
