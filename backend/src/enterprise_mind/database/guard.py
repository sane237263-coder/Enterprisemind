"""Fail-closed PostgreSQL AST SQL guard for Database MCP queries.

The guard accepts only a single PostgreSQL ``SelectStmt`` and validates every
relation, column, function, operator, parameter, and nested scope before a
server-owned pagination wrapper is produced.  It intentionally does not use
regular expressions: lexical edge cases (comments, dollar quoted strings,
Unicode, and semicolons inside literals) are handled by ``pglast``'s
PostgreSQL parser.
"""

from __future__ import annotations

from collections import OrderedDict
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from math import isfinite
from typing import Any, NoReturn

from .contracts import (
    GeneratedQuery,
    GuardedQuery,
    JSONScalar,
    SchemaCatalog,
    SchemaRelation,
)

MAX_SQL_BYTES = 64 * 1024
DEFAULT_PAGE_SIZE = 100
MAX_PAGE_SIZE = 100
MAX_BUSINESS_LIMIT = 10_000
MAX_PARAMETERS = 100
MAX_OFFSET = (1 << 63) - 1
SYSTEM_SCHEMAS = {"pg_catalog", "information_schema", "pg_toast", "pg_temp", "pg_temp_1"}


class SQLGuardError(ValueError):
    """Safe public error; it never stores or renders submitted SQL."""

    def __init__(
        self, reason: str = "query rejected", *, code: str = "DATABASE_QUERY_REJECTED"
    ) -> None:
        safe_reason = reason if reason in _SAFE_REASONS else "query rejected"
        self.code = code
        self.reason = safe_reason
        super().__init__(safe_reason)

    def __repr__(self) -> str:
        return f"SQLGuardError(code={self.code!r}, reason={self.reason!r})"


DatabaseQueryRejected = SQLGuardError


_SAFE_REASONS = {
    "parser unavailable",
    "parser version unsupported",
    "invalid text",
    "query too long",
    "empty query",
    "multiple statements",
    "statement is not SELECT",
    "unsupported statement structure",
    "data modification is not allowed",
    "pagination clause is server-controlled",
    "business limit is invalid",
    "row locking is not allowed",
    "unqualified relation",
    "system relation is not allowed",
    "relation is not authorized",
    "duplicate relation alias",
    "invalid relation alias",
    "cte must be read-only SELECT",
    "recursive CTE is not allowed",
    "subquery alias is required",
    "natural join is not allowed",
    "field is not authorized",
    "ambiguous field",
    "wildcard is not allowed",
    "unsupported expression",
    "function is not allowed",
    "function argument count is not allowed",
    "operator is not allowed",
    "type cast is not allowed",
    "custom collation is not allowed",
    "parameter is invalid",
    "parameter count mismatch",
    "parameters are not JSON scalars",
    "page size is invalid",
    "page index is invalid",
    "stable ordering is required",
    "renderer unavailable",
    "duplicate result column",
}


def _reject(reason: str, *, code: str = "DATABASE_QUERY_REJECTED") -> NoReturn:
    raise SQLGuardError(reason, code=code)


def _node_name(value: Any) -> str:
    return type(value).__name__ if value is not None else ""


def _as_sequence(value: Any) -> tuple[Any, ...]:
    if value is None:
        return ()
    if isinstance(value, tuple):
        return value
    if isinstance(value, list):
        return tuple(value)
    return (value,)


def _text_node(value: Any) -> str | None:
    if isinstance(value, str):
        return value
    if value is None:
        return None
    if _node_name(value) == "String":
        result = getattr(value, "sval", None)
        if isinstance(result, str):
            return result
        result = getattr(value, "val", None)
        return result if isinstance(result, str) else None
    return None


def _enum_name(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, str):
        return value
    name = getattr(value, "name", None)
    if isinstance(name, str):
        return name
    if isinstance(value, Mapping):
        name = value.get("name")
        if isinstance(name, str):
            return name
    return ""


def _get(node: Any, name: str, default: Any = None) -> Any:
    return getattr(node, name, default)


def _quote_identifier(value: str) -> str:
    return '"' + value.replace('"', '""') + '"'


@dataclass
class _Source:
    columns: tuple[str, ...]
    relation: SchemaRelation | None = None


@dataclass
class _Scope:
    outer: _Scope | None = None
    sources: OrderedDict[str, _Source] = field(default_factory=OrderedDict)
    ctes: dict[str, tuple[str, ...]] = field(default_factory=dict)
    windows: set[str] = field(default_factory=set)


@dataclass
class _GuardState:
    catalog: SchemaCatalog
    parameters: tuple[JSONScalar, ...]
    parameter_numbers: set[int] = field(default_factory=set)
    business_limit_parameters: set[int] = field(default_factory=set)
    business_limit_count: int = 0
    referenced_relations: OrderedDict[str, None] = field(default_factory=OrderedDict)


class _ASTGuard:
    def __init__(self, state: _GuardState) -> None:
        self.state = state

    def validate_root(self, root: Any) -> None:
        if _node_name(root) != "SelectStmt":
            _reject("statement is not SELECT")
        self._validate_select(root, None, {})

    def has_deterministic_order(self, root: Any) -> bool:
        """Prove that page offsets cannot reorder equal rows.

        A syntactically present ORDER BY is not enough: a constant, a
        non-unique column, or a custom sort operator can still make the next
        page unstable.  The proof is intentionally conservative.
        """

        operation = _enum_name(_get(root, "op"))
        if operation and operation != "SETOP_NONE":
            output = self._output_names(root)
            if not output or bool(_get(root, "all", False)):
                return False
            return self._sort_output_names(_get(root, "sortClause"), output) == set(output)
        sort_columns = self._sort_column_names(_get(root, "sortClause"))
        if not sort_columns:
            return False
        from_items = _as_sequence(_get(root, "fromClause"))
        if len(from_items) == 1 and _node_name(from_items[0]) == "RangeVar":
            relation = self._range_relation(from_items[0])
            if relation is not None:
                relation_sort_columns = self._sort_relation_column_names(root)
                if relation_sort_columns is None:
                    return False
                columns = {column.name: column for column in relation.columns}
                for constraint in relation.constraints:
                    if constraint.constraint_type not in {"primary_key", "unique"}:
                        continue
                    # Ordinary UNIQUE constraints do not prove row
                    # uniqueness when a key column can be NULL.  Do not use a
                    # per-column uniqueness hint to infer a composite key.
                    if constraint.constraint_type == "unique" and any(
                        columns.get(name) is None or columns[name].is_nullable is not False
                        for name in constraint.column_names
                    ):
                        continue
                    if set(constraint.column_names).issubset(relation_sort_columns):
                        return True
        # GROUP BY keys identify one output row per key.  We only accept simple
        # column keys; expressions and grouping sets require a stronger proof.
        group_keys = self._simple_column_paths(_get(root, "groupClause"))
        order_keys = self._group_order_keys(root)
        if group_keys and order_keys is not None and group_keys.issubset(order_keys):
            return True
        # SELECT DISTINCT rows are unique by the complete projected output.
        distinct_clause = _get(root, "distinctClause")
        if distinct_clause is not None and all(
            item is None for item in _as_sequence(distinct_clause)
        ):
            output = self._output_names(root)
            if output and self._sort_output_names(_get(root, "sortClause"), output) == set(output):
                return True
        # An aggregate without GROUP BY has one row, provided no window
        # function or ordinary column is projected.
        if not _get(root, "groupClause") and self._is_single_aggregate_target(root):
            return True
        return False

    def _range_relation(self, node: Any) -> SchemaRelation | None:
        schema_name = _text_node(_get(node, "schemaname"))
        relation_name = _text_node(_get(node, "relname"))
        if schema_name is None or relation_name is None or schema_name.lower() in SYSTEM_SCHEMAS:
            return None
        return self.state.catalog.get(schema_name, relation_name)

    def _output_names(self, node: Any) -> tuple[str, ...]:
        operation = _enum_name(_get(node, "op"))
        if operation and operation != "SETOP_NONE":
            left = _get(node, "larg")
            return self._output_names(left) if left is not None else ()
        return tuple(
            _text_node(_get(target, "name")) or self._expression_output_name(_get(target, "val"))
            for target in _as_sequence(_get(node, "targetList"))
            if _node_name(target) == "ResTarget"
        )

    def _simple_column_paths(self, values: Any) -> set[tuple[str, ...]]:
        names: set[tuple[str, ...]] = set()
        items = _as_sequence(values)
        if not items:
            return names
        for item in items:
            node = _get(item, "node") if _node_name(item) == "SortBy" else item
            if _node_name(node) != "ColumnRef":
                return set()
            fields = tuple(_text_node(value) for value in _as_sequence(_get(node, "fields")))
            if len(fields) not in {1, 2, 3} or any(field is None for field in fields):
                return set()
            # Equal column names from different joined relations remain distinct keys.
            names.add(tuple(field for field in fields if field is not None))
        return names

    def _group_order_keys(self, node: Any) -> set[tuple[str, ...]] | None:
        """Resolve GROUP BY ordering against input expressions, not aliases.

        PostgreSQL resolves an unqualified ORDER BY name to a target-list
        alias before an input column.  A grouped query such as ``GROUP BY
        amount ORDER BY amount`` can therefore order by an aggregate alias
        named ``amount`` instead of the input grouping column.  Treat an
        aliased non-column expression as unprovable rather than claiming that
        the groups are stably ordered.
        """

        aliases: dict[str, Any] = {}
        for target in _as_sequence(_get(node, "targetList")):
            if _node_name(target) != "ResTarget":
                return None
            expression = _get(target, "val")
            name = _text_node(_get(target, "name")) or self._expression_output_name(expression)
            if name in aliases:
                return None
            aliases[name] = expression

        keys: set[tuple[str, ...]] = set()
        for item in _as_sequence(_get(node, "sortClause")):
            if _node_name(item) != "SortBy" or _get(item, "useOp"):
                return None
            sort_node = _get(item, "node")
            if _node_name(sort_node) != "ColumnRef":
                return None
            fields = tuple(_text_node(value) for value in _as_sequence(_get(sort_node, "fields")))
            if not fields or any(field is None for field in fields) or len(fields) > 3:
                return None
            if len(fields) == 1 and fields[0] in aliases:
                alias_keys = self._simple_column_paths((aliases[fields[0]],))
                if not alias_keys:
                    return None
                keys.update(alias_keys)
            else:
                keys.add(tuple(field for field in fields if field is not None))
        return keys

    def _sort_column_names(self, values: Any) -> set[str]:
        items = _as_sequence(values)
        if not items:
            return set()
        names: set[str] = set()
        for item in items:
            if _node_name(item) != "SortBy" or _get(item, "useOp"):
                return set()
            node = _get(item, "node")
            if _node_name(node) != "ColumnRef":
                return set()
            fields = tuple(_text_node(value) for value in _as_sequence(_get(node, "fields")))
            if len(fields) not in {1, 2, 3} or fields[-1] is None:
                return set()
            names.add(fields[-1])
        return names

    def _sort_relation_column_names(self, node: Any) -> set[str] | None:
        """Resolve simple ORDER BY aliases back to their source columns."""

        alias_sources: dict[str, str | None] = {}
        for target in _as_sequence(_get(node, "targetList")):
            if _node_name(target) != "ResTarget":
                return None
            expression = _get(target, "val")
            output_name = _text_node(_get(target, "name")) or self._expression_output_name(
                expression
            )
            if _node_name(expression) != "ColumnRef":
                alias_sources[output_name] = None
                continue
            fields = tuple(_text_node(value) for value in _as_sequence(_get(expression, "fields")))
            if not fields or any(value is None for value in fields) or len(fields) > 3:
                alias_sources[output_name] = None
                continue
            alias_sources[output_name] = fields[-1]

        result: set[str] = set()
        for item in _as_sequence(_get(node, "sortClause")):
            if _node_name(item) != "SortBy" or _get(item, "useOp"):
                return None
            sort_node = _get(item, "node")
            if _node_name(sort_node) != "ColumnRef":
                return None
            fields = tuple(_text_node(value) for value in _as_sequence(_get(sort_node, "fields")))
            if not fields or any(value is None for value in fields) or len(fields) > 3:
                return None
            column_name = fields[-1]
            if column_name is None:
                return None
            if len(fields) == 1 and column_name in alias_sources:
                source_column = alias_sources[column_name]
                if source_column is None:
                    return None
                column_name = source_column
            result.add(column_name)
        return result

    def _sort_output_names(self, values: Any, output: tuple[str, ...]) -> set[str]:
        names = self._sort_column_names(values)
        return names.intersection(output)

    def _is_single_aggregate_target(self, node: Any) -> bool:
        targets = _as_sequence(_get(node, "targetList"))
        if not targets:
            return False
        for target in targets:
            expression = _get(target, "val")
            if _node_name(expression) == "FuncCall":
                function_name = _text_node(_as_sequence(_get(expression, "funcname"))[-1])
                if (
                    function_name not in _AGGREGATE_FUNCTIONS
                    or _get(expression, "over") is not None
                ):
                    return False
            elif _node_name(expression) != "A_Const":
                return False
        return True

    def _validate_select(
        self,
        node: Any,
        outer_scope: _Scope | None,
        inherited_ctes: Mapping[str, tuple[str, ...]],
        *,
        output_override: tuple[str, ...] | None = None,
        _allow_duplicate_output: bool = False,
    ) -> tuple[str, ...]:
        if _node_name(node) != "SelectStmt":
            _reject("unsupported statement structure")

        with_clause = _get(node, "withClause")
        available_ctes = dict(inherited_ctes)
        if with_clause is not None:
            if bool(_get(with_clause, "recursive", False)):
                _reject("recursive CTE is not allowed")
            for cte in _as_sequence(_get(with_clause, "ctes")):
                if _node_name(cte) != "CommonTableExpr":
                    _reject("unsupported statement structure")
                if bool(_get(cte, "cterecursive", False)):
                    _reject("recursive CTE is not allowed")
                name = _text_node(_get(cte, "ctename"))
                if not name or name in available_ctes:
                    _reject("unsupported statement structure")
                query = _get(cte, "ctequery")
                if _node_name(query) != "SelectStmt":
                    _reject("cte must be read-only SELECT")
                alias_columns = tuple(
                    item
                    for item in (
                        _text_node(value) for value in _as_sequence(_get(cte, "aliascolnames"))
                    )
                    if item is not None
                )
                output = self._validate_select(
                    query,
                    outer_scope,
                    available_ctes,
                    _allow_duplicate_output=bool(alias_columns),
                )
                if alias_columns:
                    if len(alias_columns) != len(output):
                        _reject("unsupported statement structure")
                    if len(alias_columns) != len(set(alias_columns)):
                        _reject("duplicate result column")
                    output = alias_columns
                available_ctes[name] = output

        operation = _enum_name(_get(node, "op"))
        if operation and operation != "SETOP_NONE":
            # A set-operation's branches carry all actual relations.  A root
            # ORDER BY is checked against branch output names below.
            if _get(node, "intoClause") is not None or _get(node, "valuesLists"):
                _reject("unsupported statement structure")
            self._validate_limit_clause(node)
            if _get(node, "lockingClause"):
                _reject("row locking is not allowed")
            larg = _get(node, "larg")
            rarg = _get(node, "rarg")
            if _node_name(larg) != "SelectStmt" or _node_name(rarg) != "SelectStmt":
                _reject("unsupported statement structure")
            left = self._validate_select(
                larg,
                outer_scope,
                available_ctes,
                _allow_duplicate_output=output_override is not None,
            )
            right = self._validate_select(
                rarg,
                outer_scope,
                available_ctes,
                _allow_duplicate_output=output_override is not None,
            )
            if len(left) != len(right):
                _reject("unsupported statement structure")
            effective_output = output_override or left
            if not _allow_duplicate_output and len(effective_output) != len(set(effective_output)):
                _reject("duplicate result column")
            if _get(node, "sortClause"):
                set_scope = _Scope(outer=outer_scope, ctes=available_ctes)
                set_scope.sources["__enterprise_mind_set__"] = _Source(left)
                self._validate_sort_clause(_get(node, "sortClause"), set_scope, left)
            return effective_output

        if _get(node, "intoClause") is not None or _get(node, "valuesLists"):
            _reject("unsupported statement structure")
        self._validate_limit_clause(node)
        if _get(node, "lockingClause"):
            _reject("row locking is not allowed")

        scope = _Scope(outer=outer_scope, ctes=available_ctes)
        for item in _as_sequence(_get(node, "fromClause")):
            self._validate_from(item, scope)

        # Window declarations are visible to target expressions below.  A
        # declaration may not inherit an unvalidated named window.
        for window in _as_sequence(_get(node, "windowClause")):
            self._validate_window_definition(window, scope, (), declaration=True)
            name = _text_node(_get(window, "name"))
            if name:
                if name in scope.windows:
                    _reject("unsupported statement structure")
                scope.windows.add(name)

        target_list = _as_sequence(_get(node, "targetList"))
        if not target_list:
            _reject("unsupported statement structure")
        output_names: list[str] = []
        for target in target_list:
            if _node_name(target) != "ResTarget" or _get(target, "indirection"):
                _reject("unsupported statement structure")
            expression = _get(target, "val")
            self._validate_expr(expression, scope)
            name = _text_node(_get(target, "name"))
            if not name:
                name = self._expression_output_name(expression)
            output_names.append(name)
        effective_output = output_override or tuple(output_names)
        if not _allow_duplicate_output and len(output_names) != len(set(output_names)):
            _reject("duplicate result column")
        if output_override is not None and len(effective_output) != len(set(effective_output)):
            _reject("duplicate result column")

        self._validate_expr(_get(node, "whereClause"), scope)
        for expression in _as_sequence(_get(node, "groupClause")):
            self._validate_expr(expression, scope, output_names=tuple(output_names))
        self._validate_expr(_get(node, "havingClause"), scope, output_names=tuple(output_names))
        for distinct in _as_sequence(_get(node, "distinctClause")):
            # DISTINCT ON entries are SortBy nodes; plain DISTINCT has no list.
            self._validate_expr(
                _get(distinct, "node") if _node_name(distinct) == "SortBy" else distinct,
                scope,
                output_names=tuple(output_names),
            )
        self._validate_sort_clause(_get(node, "sortClause"), scope, tuple(output_names))
        return effective_output

    def _validate_limit_clause(self, node: Any) -> None:
        """Allow a bounded business LIMIT while keeping transport paging owned by us."""

        if _get(node, "limitOffset") is not None:
            _reject("pagination clause is server-controlled")
        limit = _get(node, "limitCount")
        option = _enum_name(_get(node, "limitOption"))
        if limit is None:
            return
        self.state.business_limit_count += 1
        if option and option not in {"LIMIT_OPTION_COUNT"}:
            _reject("business limit is invalid")
        if _node_name(limit) == "ParamRef":
            number = _get(limit, "number")
            if not isinstance(number, int) or isinstance(number, bool) or number < 1:
                _reject("business limit is invalid")
            self.state.parameter_numbers.add(number)
            self.state.business_limit_parameters.add(number)
            return
        if _node_name(limit) != "A_Const" or bool(_get(limit, "isnull", False)):
            _reject("business limit is invalid")
        literal = _get(limit, "val")
        value = _get(literal, "ival") if _node_name(literal) == "Integer" else None
        if (
            not isinstance(value, int)
            or isinstance(value, bool)
            or not 0 <= value <= MAX_BUSINESS_LIMIT
        ):
            _reject("business limit is invalid")

    def _validate_from(self, node: Any, scope: _Scope) -> None:
        kind = _node_name(node)
        if kind == "RangeVar":
            self._validate_range_var(node, scope)
            return
        if kind == "RangeSubselect":
            alias = _get(node, "alias")
            alias_name = _text_node(_get(alias, "aliasname")) if alias is not None else None
            if not alias_name:
                _reject("subquery alias is required")
            if bool(_get(node, "lateral", False)):
                # LATERAL is read-only but its correlated scope is harder to
                # reason about; the subquery itself remains safely isolated.
                pass
            subquery = _get(node, "subquery")
            output_scope = scope if bool(_get(node, "lateral", False)) else None
            output = self._validate_select(subquery, output_scope, scope.ctes)
            self._add_source(scope, alias_name, _Source(output))
            return
        if kind == "JoinExpr":
            if bool(_get(node, "isNatural", False)):
                _reject("natural join is not allowed")
            self._validate_from(_get(node, "larg"), scope)
            self._validate_from(_get(node, "rarg"), scope)
            using = tuple(
                value
                for value in (_text_node(item) for item in _as_sequence(_get(node, "usingClause")))
                if value is not None
            )
            for column in using:
                if not self._scope_has_column(scope, column):
                    _reject("field is not authorized")
            self._validate_expr(_get(node, "quals"), scope)
            return
        # RangeFunction, RangeTableSample, and table functions are not part of
        # the fixed read-only function boundary.
        _reject("unsupported statement structure")

    def _validate_range_var(self, node: Any, scope: _Scope) -> None:
        schema_name = _text_node(_get(node, "schemaname"))
        relation_name = _text_node(_get(node, "relname"))
        if not relation_name:
            _reject("unsupported statement structure")
        if _get(node, "catalogname") is not None or _get(node, "relpersistence") == "t":
            _reject("system relation is not allowed")
        if schema_name is None:
            cte_columns = scope.ctes.get(relation_name)
            if cte_columns is None:
                _reject("unqualified relation")
            alias = _get(node, "alias")
            alias_name = (
                _text_node(_get(alias, "aliasname")) if alias is not None else relation_name
            )
            if not alias_name:
                _reject("invalid relation alias")
            self._add_source(scope, alias_name, _Source(cte_columns))
            return
        if schema_name.lower() in SYSTEM_SCHEMAS or schema_name.lower().startswith(
            ("pg_temp", "pg_toast")
        ):
            _reject("system relation is not allowed")
        relation = self.state.catalog.get(schema_name, relation_name)
        if relation is None:
            _reject("relation is not authorized", code="DATABASE_RELATION_FORBIDDEN")
        canonical = relation.quoted_qualified_name
        self._validate_relation_columns(relation)
        self.state.referenced_relations.setdefault(canonical, None)
        alias = _get(node, "alias")
        alias_name = _text_node(_get(alias, "aliasname")) if alias is not None else relation_name
        if not alias_name:
            _reject("invalid relation alias")
        self._add_source(scope, alias_name, _Source(relation.column_names, relation))

    def _add_source(self, scope: _Scope, alias: str, source: _Source) -> None:
        if alias in scope.sources:
            _reject("duplicate relation alias")
        scope.sources[alias] = source

    def _validate_relation_columns(self, relation: SchemaRelation) -> None:
        for column in relation.columns:
            if not _schema_type_allowed(column.data_type):
                _reject("type cast is not allowed")

    def _scope_has_column(self, scope: _Scope, column: str) -> bool:
        return any(source.columns.count(column) == 1 for source in scope.sources.values())

    def _resolve_column(self, scope: _Scope, qualifier: str | None, column: str) -> None:
        if qualifier is not None:
            source = scope.sources.get(qualifier)
            if source is not None:
                if source.columns.count(column) != 1:
                    _reject("field is not authorized")
                return
            if scope.outer is not None:
                self._resolve_column(scope.outer, qualifier, column)
                return
            _reject("field is not authorized")

        local_matches = sum(source.columns.count(column) for source in scope.sources.values())
        if local_matches > 1:
            _reject("ambiguous field")
        if local_matches == 1:
            return
        if scope.outer is not None:
            self._resolve_column(scope.outer, None, column)
            return
        _reject("field is not authorized")

    def _validate_column_ref(
        self,
        node: Any,
        scope: _Scope,
        *,
        output_names: tuple[str, ...] = (),
    ) -> None:
        fields = _as_sequence(_get(node, "fields"))
        if not fields or any(_node_name(item) == "A_Star" for item in fields):
            _reject("wildcard is not allowed")
        names = tuple(_text_node(item) for item in fields)
        if any(item is None for item in names):
            _reject("unsupported expression")
        text_names = tuple(item for item in names if item is not None)
        if len(text_names) == 1:
            try:
                self._resolve_column(scope, None, text_names[0])
            except SQLGuardError as error:
                if text_names[0] not in output_names or error.reason not in {
                    "field is not authorized",
                    "ambiguous field",
                }:
                    raise
            return
        if len(text_names) == 2:
            self._resolve_column(scope, text_names[0], text_names[1])
            return
        if len(text_names) == 3:
            schema_name, relation_name, column_name = text_names
            current: _Scope | None = scope
            while current is not None:
                for alias, source in current.sources.items():
                    if (
                        source.relation is not None
                        and source.relation.schema_name == schema_name
                        and source.relation.relation_name == relation_name
                        and alias == relation_name
                    ):
                        if source.columns.count(column_name) != 1:
                            _reject("field is not authorized")
                        return
                current = current.outer
            _reject("field is not authorized")
        if len(text_names) > 3:
            _reject("field is not authorized")

    def _validate_expr(
        self,
        node: Any,
        scope: _Scope,
        *,
        output_names: tuple[str, ...] = (),
    ) -> None:
        if node is None:
            return
        kind = _node_name(node)
        if kind == "ColumnRef":
            self._validate_column_ref(node, scope, output_names=output_names)
            return
        if kind == "ParamRef":
            number = _get(node, "number")
            if not isinstance(number, int) or isinstance(number, bool) or number < 1:
                _reject("parameter is invalid")
            self.state.parameter_numbers.add(number)
            return
        if kind == "A_Const":
            if bool(_get(node, "isnull", False)):
                if _get(node, "val") is not None:
                    _reject("unsupported expression")
                return
            literal = _get(node, "val")
            if _node_name(literal) not in {
                "Integer",
                "Float",
                "String",
                "Boolean",
                "Null",
                "BitString",
            }:
                _reject("unsupported expression")
            return
        if kind == "A_Expr":
            operator = tuple(
                value
                for value in (_text_node(item) for item in _as_sequence(_get(node, "name")))
                if value is not None
            )
            if len(operator) != 1 or operator[0].lower() not in _ALLOWED_OPERATORS:
                _reject("operator is not allowed")
            self._validate_expr(_get(node, "lexpr"), scope, output_names=output_names)
            rhs = _get(node, "rexpr")
            if isinstance(rhs, tuple | list):
                for value in rhs:
                    self._validate_expr(value, scope, output_names=output_names)
            else:
                self._validate_expr(rhs, scope, output_names=output_names)
            return
        if kind == "BoolExpr":
            if _enum_name(_get(node, "boolop")) not in {"AND_EXPR", "OR_EXPR", "NOT_EXPR"}:
                _reject("operator is not allowed")
            for argument in _as_sequence(_get(node, "args")):
                self._validate_expr(argument, scope, output_names=output_names)
            return
        if kind in {"NullTest", "BooleanTest"}:
            self._validate_expr(_get(node, "arg"), scope, output_names=output_names)
            return
        if kind == "CaseExpr":
            self._validate_expr(_get(node, "arg"), scope, output_names=output_names)
            for item in _as_sequence(_get(node, "args")):
                self._validate_expr(item, scope, output_names=output_names)
            self._validate_expr(_get(node, "defresult"), scope, output_names=output_names)
            return
        if kind == "CaseWhen":
            self._validate_expr(_get(node, "expr"), scope, output_names=output_names)
            self._validate_expr(_get(node, "result"), scope, output_names=output_names)
            return
        if kind in {"CoalesceExpr", "MinMaxExpr", "NullIfExpr"}:
            if kind == "MinMaxExpr" and _enum_name(_get(node, "op")) not in {
                "IS_GREATEST",
                "IS_LEAST",
            }:
                _reject("operator is not allowed")
            for argument in _as_sequence(_get(node, "args")):
                self._validate_expr(argument, scope, output_names=output_names)
            return
        if kind == "FuncCall":
            self._validate_function(node, scope, output_names)
            return
        if kind == "TypeCast":
            self._validate_expr(_get(node, "arg"), scope, output_names=output_names)
            self._validate_type_name(_get(node, "typeName"), scope, output_names)
            return
        if kind == "SubLink":
            self._validate_expr(_get(node, "testexpr"), scope, output_names=output_names)
            oper_name = tuple(
                value
                for value in (_text_node(item) for item in _as_sequence(_get(node, "operName")))
                if value is not None
            )
            if oper_name and (
                len(oper_name) != 1 or oper_name[0].lower() not in _ALLOWED_OPERATORS
            ):
                _reject("operator is not allowed")
            subselect = _get(node, "subselect")
            if _node_name(subselect) != "SelectStmt":
                _reject("unsupported expression")
            self._validate_select(subselect, scope, scope.ctes)
            return
        if kind == "SQLValueFunction":
            if _enum_name(_get(node, "op")) not in _ALLOWED_SQL_VALUES:
                _reject("unsupported expression")
            return
        if kind == "CollateExpr":
            # A user-defined collation can invoke implementation-defined
            # behavior and is not part of the fixed function/type boundary.
            _reject("custom collation is not allowed")
        if kind in {
            "RowExpr",
            "ArrayExpr",
            "A_ArrayExpr",
            "XmlExpr",
            "JsonExpr",
            "JsonConstructorExpr",
        }:
            _reject("unsupported expression")
        # SortBy/WindowDef are handled only in their containing clauses; any
        # other AST expression is rejected so new PostgreSQL nodes fail closed.
        _reject("unsupported expression")

    def _validate_function(self, node: Any, scope: _Scope, output_names: tuple[str, ...]) -> None:
        names = tuple(
            value
            for value in (_text_node(item) for item in _as_sequence(_get(node, "funcname")))
            if value is not None
        )
        if not names or len(names) > 2 or (len(names) == 2 and names[0] != "pg_catalog"):
            _reject("function is not allowed")
        function_name = names[-1]
        if function_name != function_name.lower() or function_name not in _FUNCTION_ARITY:
            _reject("function is not allowed")
        if bool(_get(node, "func_variadic", False)) or bool(_get(node, "agg_within_group", False)):
            _reject("function is not allowed")
        args = _as_sequence(_get(node, "args"))
        if bool(_get(node, "agg_star", False)):
            if function_name != "count" or args:
                _reject("wildcard is not allowed")
        else:
            minimum, maximum = _FUNCTION_ARITY[function_name]
            if len(args) < minimum or (maximum is not None and len(args) > maximum):
                _reject("function argument count is not allowed")
        for argument in args:
            self._validate_expr(argument, scope, output_names=output_names)
        for item in _as_sequence(_get(node, "agg_order")):
            self._validate_sort_item(item, scope, output_names)
        self._validate_expr(_get(node, "agg_filter"), scope, output_names=output_names)
        over = _get(node, "over")
        if over is not None:
            if _node_name(over) == "WindowDef":
                self._validate_window_definition(over, scope, output_names)
            else:
                window_name = _text_node(over)
                if not window_name or window_name not in scope.windows:
                    _reject("unsupported expression")

    def _validate_type_name(
        self,
        node: Any,
        scope: _Scope,
        output_names: tuple[str, ...],
    ) -> None:
        if _node_name(node) != "TypeName":
            _reject("type cast is not allowed")
        names = tuple(
            value
            for value in (_text_node(item) for item in _as_sequence(_get(node, "names")))
            if value is not None
        )
        if not names or len(names) > 2 or (len(names) == 2 and names[0] != "pg_catalog"):
            _reject("type cast is not allowed")
        if names[-1].lower() not in _ALLOWED_TYPES or names[-1] != names[-1].lower():
            _reject("type cast is not allowed")
        if _get(node, "arrayBounds") or bool(_get(node, "setof", False)) or _get(node, "pctType"):
            _reject("type cast is not allowed")
        for modifier in _as_sequence(_get(node, "typmods")):
            self._validate_expr(modifier, scope, output_names=output_names)

    def _validate_window_definition(
        self,
        node: Any,
        scope: _Scope,
        output_names: tuple[str, ...],
        *,
        declaration: bool = False,
    ) -> None:
        if _node_name(node) != "WindowDef":
            _reject("unsupported expression")
        if not declaration and (
            _get(node, "name") is not None or _get(node, "refname") is not None
        ):
            _reject("unsupported expression")
        if declaration and _get(node, "refname") is not None:
            _reject("unsupported expression")
        for item in _as_sequence(_get(node, "partitionClause")):
            self._validate_expr(item, scope, output_names=output_names)
        self._validate_sort_clause(_get(node, "orderClause"), scope, output_names)
        # frameOptions is an integer bitmask produced by the parser; offsets
        # are expression nodes and must still pass the same checks.
        self._validate_expr(_get(node, "startOffset"), scope, output_names=output_names)
        self._validate_expr(_get(node, "endOffset"), scope, output_names=output_names)

    def _validate_sort_clause(
        self,
        value: Any,
        scope: _Scope | None,
        output_names: tuple[str, ...],
    ) -> None:
        if not value:
            return
        if scope is None:
            _reject("unsupported statement structure")
        for item in _as_sequence(value):
            self._validate_sort_item(item, scope, output_names)

    def _validate_sort_item(self, item: Any, scope: _Scope, output_names: tuple[str, ...]) -> None:
        if _node_name(item) != "SortBy":
            _reject("unsupported statement structure")
        if _get(item, "useOp"):
            _reject("operator is not allowed")
        self._validate_expr(_get(item, "node"), scope, output_names=output_names)

    @staticmethod
    def _expression_output_name(node: Any) -> str:
        if _node_name(node) == "ColumnRef":
            fields = _as_sequence(_get(node, "fields"))
            if fields:
                name = _text_node(fields[-1])
                if name:
                    return name
        if _node_name(node) == "FuncCall":
            names = tuple(
                value
                for value in (_text_node(item) for item in _as_sequence(_get(node, "funcname")))
                if value is not None
            )
            if names:
                return names[-1]
        if _node_name(node) == "TypeCast":
            return _ASTGuard._expression_output_name(_get(node, "arg"))
        if _node_name(node) == "CoalesceExpr":
            return "coalesce"
        if _node_name(node) == "NullIfExpr":
            return "nullif"
        if _node_name(node) == "MinMaxExpr":
            return {
                "IS_GREATEST": "greatest",
                "IS_LEAST": "least",
            }.get(_enum_name(_get(node, "op")), "?column?")
        return "?column?"


_ALLOWED_OPERATORS = {
    "=",
    "<>",
    "!=",
    "<",
    "<=",
    ">",
    ">=",
    "+",
    "-",
    "*",
    "/",
    "%",
    "^",
    "||",
    "between",
    "~~",
    "!~~",
    "~~*",
    "!~~*",
    "!~",
    "!~*",
    "~",
    "~*",
}

_FUNCTION_ARITY: dict[str, tuple[int, int | None]] = {
    "count": (1, 1),
    "sum": (1, 1),
    "avg": (1, 1),
    "min": (1, 1),
    "max": (1, 1),
    "bool_and": (1, 1),
    "bool_or": (1, 1),
    "coalesce": (1, None),
    "nullif": (2, 2),
    "greatest": (1, None),
    "least": (1, None),
    "lower": (1, 1),
    "upper": (1, 1),
    "trim": (1, 1),
    "btrim": (1, 1),
    "length": (1, 1),
    "date_trunc": (2, 2),
    "extract": (1, 2),
    "abs": (1, 1),
    "round": (1, 2),
    "ceil": (1, 1),
    "floor": (1, 1),
}

_AGGREGATE_FUNCTIONS = {
    "count",
    "sum",
    "avg",
    "min",
    "max",
    "bool_and",
    "bool_or",
}

_ALLOWED_TYPES = {
    "bool",
    "boolean",
    "int2",
    "smallint",
    "int4",
    "integer",
    "int",
    "int8",
    "bigint",
    "numeric",
    "decimal",
    "float4",
    "real",
    "float8",
    "double",
    "text",
    "varchar",
    "character varying",
    "char",
    "bpchar",
    "uuid",
    "date",
    "time",
    "timetz",
    "timestamp",
    "timestamptz",
    "interval",
    "json",
    "jsonb",
}

_ALLOWED_SCHEMA_TYPES = _ALLOWED_TYPES | {"bytea"}

_ALLOWED_SQL_VALUES = {
    "SVFOP_CURRENT_DATE",
    "SVFOP_CURRENT_TIME",
    "SVFOP_CURRENT_TIME_N",
    "SVFOP_CURRENT_TIMESTAMP",
    "SVFOP_CURRENT_TIMESTAMP_N",
    "SVFOP_LOCALTIME",
    "SVFOP_LOCALTIME_N",
    "SVFOP_LOCALTIMESTAMP",
    "SVFOP_LOCALTIMESTAMP_N",
}


def _validate_parameters(
    parameters: Sequence[Any] | None,
    numbers: set[int],
    *,
    max_parameters: int = MAX_PARAMETERS,
) -> tuple[JSONScalar, ...]:
    values = () if parameters is None else tuple(parameters)
    if len(values) > max_parameters:
        _reject("parameter count mismatch")
    result: list[JSONScalar] = []
    for value in values:
        if value is None or isinstance(value, str | bool | int):
            result.append(value)
        elif isinstance(value, float) and isfinite(value):
            result.append(value)
        else:
            _reject("parameters are not JSON scalars")
    expected = set(range(1, len(result) + 1))
    if numbers != expected:
        _reject("parameter count mismatch")
    return tuple(result)


def _load_parser() -> tuple[Any, Any] | None:
    try:
        import pglast
        from pglast import parse_sql
    except Exception:
        return None
    version = str(getattr(pglast, "__version__", ""))
    if version not in {"v8.4", "8.4"}:
        return None
    return pglast, parse_sql


def _schema_type_allowed(data_type: str) -> bool:
    """Parse snapshot type text as a PostgreSQL TypeName and allow built-ins.

    Snapshot metadata is external input.  Parsing it with the same PostgreSQL
    parser avoids treating a domain/enum or a crafted type expression as an
    ordinary scalar merely because its text resembles one.
    """

    parser = _load_parser()
    if parser is None or not isinstance(data_type, str):
        return False
    try:
        encoded = data_type.encode("utf-8")
    except UnicodeEncodeError:
        return False
    if not encoded or len(encoded) > 1024 or "\x00" in data_type:
        return False
    try:
        parsed = tuple(parser[1]("SELECT NULL::" + data_type))
    except Exception:
        return False
    if len(parsed) != 1:
        return False
    statement = _get(parsed[0], "stmt")
    targets = _as_sequence(_get(statement, "targetList"))
    if _node_name(statement) != "SelectStmt" or len(targets) != 1:
        return False
    cast = _get(targets[0], "val")
    if _node_name(cast) != "TypeCast":
        return False
    type_name = _get(cast, "typeName")
    if _node_name(type_name) != "TypeName":
        return False
    names = tuple(
        item
        for item in (_text_node(value) for value in _as_sequence(_get(type_name, "names")))
        if item is not None
    )
    if not names or len(names) > 2:
        return False
    if len(names) == 2 and names[0] != "pg_catalog":
        return False
    if names[-1] != names[-1].lower() or names[-1] not in _ALLOWED_SCHEMA_TYPES:
        return False
    if bool(_get(type_name, "setof", False)) or bool(_get(type_name, "pct_type", False)):
        return False
    for modifier in _as_sequence(_get(type_name, "typmods")):
        if _node_name(modifier) != "A_Const":
            return False
        if _node_name(_get(modifier, "val")) not in {"Integer", "Float", "String"}:
            return False
    return True


def _parse_single_select(sql: str) -> tuple[Any, Any]:
    parser = _load_parser()
    if parser is None:
        _reject("parser unavailable")
    _, parse_sql = parser
    if not isinstance(sql, str):
        _reject("invalid text")
    try:
        encoded = sql.encode("utf-8")
    except UnicodeEncodeError:
        _reject("invalid text")
    if not encoded:
        _reject("empty query")
    if len(encoded) > MAX_SQL_BYTES:
        _reject("query too long")
    if "\x00" in sql or any(ord(char) < 32 and char not in "\t\n\r" for char in sql):
        _reject("invalid text")
    try:
        # PostgreSQL's AST normalizes FETCH into limitCount, so inspect the
        # parser token stream as well.  This remains lexical-parser based and
        # does not mistake quoted/string values for a FETCH clause.
        from pglast import scan

        tokens = tuple(scan(sql))
        if any(getattr(token, "name", None) == "FETCH" for token in tokens):
            _reject("pagination clause is server-controlled")
        for index, token in enumerate(tokens[:-1]):
            if getattr(token, "name", None) == "LIMIT" and getattr(
                tokens[index + 1], "name", None
            ) in {"ALL", "NULL_P"}:
                _reject("business limit is invalid")
        parsed = tuple(parse_sql(sql))
    except SQLGuardError:
        raise
    except Exception:
        _reject("invalid text")
    if len(parsed) != 1:
        _reject("multiple statements")
    raw = parsed[0]
    statement = _get(raw, "stmt")
    if _node_name(statement) != "SelectStmt":
        _reject("statement is not SELECT")
    return parser[0], statement


def _render_statement(pglast_module: Any, statement: Any) -> str:
    try:
        from pglast.stream import RawStream

        rendered = RawStream()(statement)
    except Exception:
        _reject("renderer unavailable")
    if not isinstance(rendered, str) or not rendered.strip():
        _reject("renderer unavailable")
    return rendered.strip().rstrip(";").rstrip()


def _validate_pagination_wrapper(
    sql: str,
    expected_inner: str,
    *,
    limit_position: int,
    offset_position: int,
) -> None:
    """Reparse and structurally verify the server-owned pagination wrapper."""

    pglast_module, root = _parse_single_select(sql)
    if _get(root, "intoClause") is not None or _get(root, "whereClause") is not None:
        _reject("unsupported statement structure")
    if _get(root, "groupClause") or _get(root, "havingClause") or _get(root, "sortClause"):
        _reject("unsupported statement structure")
    targets = _as_sequence(_get(root, "targetList"))
    if len(targets) != 1 or _node_name(targets[0]) != "ResTarget":
        _reject("unsupported statement structure")
    target_value = _get(targets[0], "val")
    if _node_name(target_value) != "ColumnRef":
        _reject("unsupported statement structure")
    fields = _as_sequence(_get(target_value, "fields"))
    if len(fields) != 1 or _node_name(fields[0]) != "A_Star":
        _reject("unsupported statement structure")
    from_clause = _as_sequence(_get(root, "fromClause"))
    if len(from_clause) != 1 or _node_name(from_clause[0]) != "RangeSubselect":
        _reject("unsupported statement structure")
    alias = _get(from_clause[0], "alias")
    if _text_node(_get(alias, "aliasname")) != "enterprise_mind_guarded":
        _reject("unsupported statement structure")
    inner = _get(from_clause[0], "subquery")
    if _node_name(inner) != "SelectStmt":
        _reject("unsupported statement structure")
    rendered_inner = _render_statement(pglast_module, inner)
    if rendered_inner != expected_inner:
        _reject("unsupported statement structure")

    def is_param(node: Any, number: int) -> bool:
        return _node_name(node) == "ParamRef" and _get(node, "number") == number

    if not is_param(_get(root, "limitCount"), limit_position):
        _reject("unsupported statement structure")
    if not is_param(_get(root, "limitOffset"), offset_position):
        _reject("unsupported statement structure")
    if _get(root, "lockingClause") or _get(root, "withClause") or _get(root, "valuesLists"):
        _reject("unsupported statement structure")


def _walk_expression(node: Any, callback: Any, *, seen: set[int] | None = None) -> None:
    """Visit expression nodes without rendering or exposing their values."""

    if node is None:
        return
    if isinstance(node, tuple | list):
        for item in node:
            _walk_expression(item, callback, seen=seen)
        return
    if not hasattr(node, "__class__"):
        return
    visited = seen if seen is not None else set()
    marker = id(node)
    if marker in visited:
        return
    visited.add(marker)
    callback(node)
    for name in (
        "lexpr",
        "rexpr",
        "args",
        "arg",
        "testexpr",
        "defresult",
        "expr",
        "result",
        "funcname",
        "agg_filter",
        "agg_order",
        "over",
        "node",
        "startOffset",
        "endOffset",
        "partitionClause",
        "orderClause",
    ):
        _walk_expression(_get(node, name), callback, seen=visited)


def _collect_condition_expressions(
    node: Any, fields: list[str], operators: list[str], *, seen: set[int] | None = None
) -> None:
    """Collect only fields/operators from actual WHERE/HAVING/JOIN predicates."""

    if node is None:
        return
    visited = seen if seen is not None else set()
    marker = id(node)
    if marker in visited:
        return
    visited.add(marker)
    kind = _node_name(node)
    if kind == "SelectStmt":
        for name in ("whereClause", "havingClause"):
            _walk_expression(
                _get(node, name),
                lambda item: _collect_condition_node(item, fields, operators),
            )
        for item in _as_sequence(_get(node, "fromClause")):
            _collect_join_conditions(item, fields, operators, seen=visited)
        with_clause = _get(node, "withClause")
        for cte in _as_sequence(_get(with_clause, "ctes")) if with_clause else ():
            _collect_condition_expressions(_get(cte, "ctequery"), fields, operators, seen=visited)
        operation = _enum_name(_get(node, "op"))
        if operation and operation != "SETOP_NONE":
            _collect_condition_expressions(_get(node, "larg"), fields, operators, seen=visited)
            _collect_condition_expressions(_get(node, "rarg"), fields, operators, seen=visited)
        return
    _collect_condition_node(node, fields, operators, seen=visited)


def _collect_join_conditions(
    node: Any, fields: list[str], operators: list[str], *, seen: set[int]
) -> None:
    if node is None:
        return
    if _node_name(node) == "JoinExpr":
        _walk_expression(
            _get(node, "quals"),
            lambda item: _collect_condition_node(item, fields, operators),
        )
        _collect_join_conditions(_get(node, "larg"), fields, operators, seen=seen)
        _collect_join_conditions(_get(node, "rarg"), fields, operators, seen=seen)
    elif _node_name(node) == "RangeSubselect":
        _collect_condition_expressions(_get(node, "subquery"), fields, operators, seen=seen)


def _collect_condition_node(
    node: Any,
    fields: list[str],
    operators: list[str],
    *,
    seen: set[int] | None = None,
) -> None:
    if node is None:
        return
    visited = seen if seen is not None else set()
    marker = id(node)
    if marker in visited:
        return
    visited.add(marker)
    kind = _node_name(node)
    if kind == "ColumnRef":
        names = tuple(_text_node(item) for item in _as_sequence(_get(node, "fields")))
        if names and names[-1] is not None and names[-1] not in fields:
            fields.append(names[-1])
    elif kind == "A_Expr":
        name = tuple(
            value
            for value in (_text_node(item) for item in _as_sequence(_get(node, "name")))
            if value is not None
        )
        if name and name[0] not in operators:
            operators.append(name[0])
    elif kind in {"NullTest", "BooleanTest"}:
        operators.append("IS")
    elif kind == "SubLink":
        _collect_condition_expressions(_get(node, "subselect"), fields, operators, seen=visited)


def summarize_conditions(sql: str) -> str:
    """Summarize guarded predicates without retaining SQL, parameters or values."""

    try:
        _, statement = _parse_single_select(sql)
    except SQLGuardError:
        return "已应用筛选条件；具体条件值已隐藏"
    fields: list[str] = []
    operators: list[str] = []
    _collect_condition_expressions(statement, fields, operators)
    if not fields and not operators:
        return "未声明筛选条件（仅查询授权对象）"
    if fields:
        return "筛选字段：" + "、".join(fields[:12]) + "；具体条件值已隐藏"
    return "已应用筛选条件；具体条件值已隐藏"


def _has_aggregate(node: Any) -> bool:
    found = False

    def visit(item: Any) -> None:
        nonlocal found
        if _node_name(item) != "FuncCall" or _get(item, "over") is not None:
            return
        names = tuple(
            value
            for value in (_text_node(part) for part in _as_sequence(_get(item, "funcname")))
            if value is not None
        )
        if names and names[-1] in _AGGREGATE_FUNCTIONS:
            found = True

    _walk_expression(node, visit)
    return found


def _infer_statement_kind(statement: Any) -> str:
    operation = _enum_name(_get(statement, "op"))
    if operation and operation != "SETOP_NONE":
        return (
            "aggregate"
            if _infer_statement_kind(_get(statement, "larg")) == "aggregate"
            or _infer_statement_kind(_get(statement, "rarg")) == "aggregate"
            else "detail"
        )
    if _get(statement, "groupClause"):
        return "aggregate"
    targets = _as_sequence(_get(statement, "targetList"))
    return (
        "aggregate" if any(_has_aggregate(_get(target, "val")) for target in targets) else "detail"
    )


def infer_query_kind(sql: str) -> str:
    """Infer the query kind from the guarded AST rather than model metadata."""

    try:
        _, statement = _parse_single_select(sql)
    except SQLGuardError:
        return "detail"
    return _infer_statement_kind(statement)


class SQLGuard:
    """Validate and paginate SQL against one authorized schema catalog."""

    def __init__(
        self,
        catalog: SchemaCatalog | Iterable[SchemaRelation],
        *,
        max_sql_bytes: int = MAX_SQL_BYTES,
        max_parameters: int = MAX_PARAMETERS,
    ) -> None:
        if isinstance(catalog, SchemaCatalog):
            self.catalog = catalog
        else:
            self.catalog = SchemaCatalog(tuple(catalog))
        if max_sql_bytes != MAX_SQL_BYTES or max_parameters != MAX_PARAMETERS:
            # The V1 boundary is fixed; accepting a looser constructor would
            # make a caller accidentally bypass the documented limits.
            if max_sql_bytes > MAX_SQL_BYTES or max_parameters > MAX_PARAMETERS:
                raise ValueError("SQL guard limits cannot exceed the V1 maximum")
            self.max_sql_bytes = max_sql_bytes
            self.max_parameters = max_parameters
        else:
            self.max_sql_bytes = MAX_SQL_BYTES
            self.max_parameters = MAX_PARAMETERS

    def validate(
        self,
        sql: str | GeneratedQuery,
        parameters: Sequence[Any] | None = None,
        *,
        page_size: int = DEFAULT_PAGE_SIZE,
        page_index: int = 0,
    ) -> GuardedQuery:
        if isinstance(sql, GeneratedQuery):
            if parameters is not None:
                _reject("parameters are not JSON scalars")
            parameters = sql.parameters
            query_text = sql.sql
        else:
            query_text = sql
        if isinstance(query_text, str):
            try:
                if len(query_text.encode("utf-8")) > self.max_sql_bytes:
                    _reject("query too long")
            except UnicodeEncodeError:
                _reject("invalid text")
        if (
            isinstance(page_size, bool)
            or not isinstance(page_size, int)
            or not 1 <= page_size <= MAX_PAGE_SIZE
        ):
            _reject("page size is invalid")
        if isinstance(page_index, bool) or not isinstance(page_index, int) or page_index < 0:
            _reject("page index is invalid")
        offset = page_size * page_index
        if offset > MAX_OFFSET:
            _reject("page index is invalid")

        pglast_module, statement = _parse_single_select(query_text)
        state = _GuardState(self.catalog, ())
        visitor = _ASTGuard(state)
        visitor.validate_root(statement)
        state.parameters = _validate_parameters(
            parameters,
            state.parameter_numbers,
            max_parameters=self.max_parameters,
        )
        for number in state.business_limit_parameters:
            value = state.parameters[number - 1]
            if (
                not isinstance(value, int)
                or isinstance(value, bool)
                or not 0 <= value <= MAX_BUSINESS_LIMIT
            ):
                _reject("business limit is invalid")

        if page_index > 0 and not visitor.has_deterministic_order(statement):
            _reject("stable ordering is required")
        rendered = _render_statement(pglast_module, statement)
        original_count = len(state.parameters)
        limit_position = original_count + 1
        offset_position = original_count + 2
        paginated = (
            "SELECT * FROM ("
            + rendered
            + ") AS enterprise_mind_guarded LIMIT $"
            + str(limit_position)
            + " OFFSET $"
            + str(offset_position)
        )
        _validate_pagination_wrapper(
            paginated,
            rendered,
            limit_position=limit_position,
            offset_position=offset_position,
        )
        return GuardedQuery(
            sql=paginated,
            parameters=state.parameters + (page_size + 1, offset),
            referenced_relations=tuple(state.referenced_relations),
            page_size=page_size,
            page_index=page_index,
            validated_query=rendered,
            validated_parameters=state.parameters,
            has_business_limit=state.business_limit_count > 0,
        )

    def revalidate(self, guarded: GuardedQuery) -> GuardedQuery:
        """Reparse the unpaginated query for the independent MCP boundary."""

        if not isinstance(guarded, GuardedQuery) or not guarded.validated_query:
            _reject("invalid text")
        return self.validate(
            guarded.validated_query,
            guarded.validated_parameters,
            page_size=guarded.page_size,
            page_index=guarded.page_index,
        )

    def without_top_level_limit(self, guarded: GuardedQuery) -> GuardedQuery | None:
        """Remove a model-added outer row cap only if the guarded query stays valid."""

        original = self.revalidate(guarded)
        if not original.has_business_limit:
            return original
        parser, statement = _parse_single_select(original.validated_query)
        limit = _get(statement, "limitCount")
        parameters = original.validated_parameters
        if _node_name(limit) == "ParamRef":
            if _get(limit, "number") != len(parameters):
                return None
            parameters = parameters[:-1]
        elif _node_name(limit) != "A_Const":
            return None
        statement.limitCount = None
        try:
            corrected = self.validate(
                _render_statement(parser, statement),
                parameters,
                page_size=original.page_size,
                page_index=original.page_index,
            )
        except SQLGuardError:
            return None
        if (
            corrected.has_business_limit
            or corrected.referenced_relations != original.referenced_relations
        ):
            return None
        return corrected

    def limit_for_export(self, guarded: GuardedQuery, count: int) -> GuardedQuery:
        """Apply the user's export cap without changing predicates or bind values."""

        if isinstance(count, bool) or not isinstance(count, int) or not 1 <= count <= 10_000:
            _reject("business limit is invalid")
        original = self.revalidate(guarded)
        parser, statement = _parse_single_select(original.validated_query)
        if _get(statement, "limitCount") is None:
            # Put the cap on the original SELECT, retaining its ORDER BY.
            statement.limitCount = parser.ast.A_Const(val=parser.ast.Integer(ival=count))
            statement.limitOption = parser.enums.LimitOption.LIMIT_OPTION_COUNT
            bounded = _render_statement(parser, statement)
        else:
            # Preserve an existing semantic cap, including a parameter that
            # may also be used in a predicate. The outer cap can only reduce it.
            names = _ASTGuard(_GuardState(self.catalog, ()))._output_names(statement)
            if not names:
                _reject("unsupported statement structure")
            projection = ", ".join(_quote_identifier(name) for name in names)
            bounded = (
                f"SELECT {projection} FROM ({original.validated_query}) "
                f"AS em_export_prefix LIMIT {count}"
            )
        checked = self.validate(
            bounded,
            original.validated_parameters,
            page_size=original.page_size,
            page_index=original.page_index,
        )
        if checked.referenced_relations != original.referenced_relations:
            _reject("relation is not authorized")
        return checked

    def continuation_available(self, guarded: GuardedQuery) -> bool:
        """Return whether a later offset page has a proven stable ordering.

        The first page is intentionally allowed even when this proof is
        unavailable.  This metadata lets callers preserve a truthful
        ``has_more`` result without presenting an unsafe continuation action.
        """

        if not isinstance(guarded, GuardedQuery) or not guarded.validated_query:
            return False
        _, statement = _parse_single_select(guarded.validated_query)
        state = _GuardState(self.catalog, ())
        visitor = _ASTGuard(state)
        visitor.validate_root(statement)
        return visitor.has_deterministic_order(statement)

    guard = validate

    def __call__(
        self,
        sql: str | GeneratedQuery,
        parameters: Sequence[Any] | None = None,
        *,
        page_size: int = DEFAULT_PAGE_SIZE,
        page_index: int = 0,
    ) -> GuardedQuery:
        return self.validate(sql, parameters, page_size=page_size, page_index=page_index)


def guard_query(
    sql: str | GeneratedQuery,
    catalog: SchemaCatalog | Iterable[SchemaRelation],
    parameters: Sequence[Any] | None = None,
    *,
    page_size: int = DEFAULT_PAGE_SIZE,
    page_index: int = 0,
) -> GuardedQuery:
    return SQLGuard(catalog).validate(sql, parameters, page_size=page_size, page_index=page_index)


validate_query = guard_query


def revalidate_query(
    guarded: GuardedQuery, catalog: SchemaCatalog | Iterable[SchemaRelation]
) -> GuardedQuery:
    """Convenience wrapper for MCP servers that must independently reparse."""

    return SQLGuard(catalog).revalidate(guarded)


__all__ = [
    "DEFAULT_PAGE_SIZE",
    "DatabaseQueryRejected",
    "MAX_PARAMETERS",
    "MAX_BUSINESS_LIMIT",
    "MAX_PAGE_SIZE",
    "MAX_SQL_BYTES",
    "SQLGuard",
    "SQLGuardError",
    "guard_query",
    "revalidate_query",
    "infer_query_kind",
    "summarize_conditions",
    "validate_query",
]
