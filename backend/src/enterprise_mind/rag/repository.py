"""Every recall list applies current authorization before ranking and LIMIT."""

import hashlib
import json
from dataclasses import dataclass, field
from typing import Any

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from enterprise_mind.api.errors import APIError, ErrorCode
from enterprise_mind.authorization.service import AuthorizationService
from enterprise_mind.identity.security import normalize_name
from enterprise_mind.identity.service import CurrentIdentity
from enterprise_mind.rag.config import RAGConfig
from enterprise_mind.rag.keywords import normalize, query_expression
from enterprise_mind.rag.providers import validate_vector


@dataclass(frozen=True)
class Scope:
    knowledge_base_ids: tuple[int, ...] | None = None
    folder_ids: tuple[int, ...] | None = None
    document_ids: tuple[int, ...] | None = None
    extensions: tuple[str, ...] | None = None
    document_names: tuple[str, ...] | None = None

    def intersect(self, other: "Scope") -> "Scope":
        def narrow(
            left: tuple[Any, ...] | None, right: tuple[Any, ...] | None
        ) -> tuple[Any, ...] | None:
            if left is None:
                return right
            if right is None:
                return left
            return tuple(item for item in left if item in right)

        return Scope(
            *(
                narrow(getattr(self, name), getattr(other, name))
                for name in self.__dataclass_fields__
            )
        )


@dataclass(frozen=True)
class Anchor:
    id: int
    source_unit_id: int
    source_ordinal: int
    element_ordinal: int
    locator_type: str
    locator: dict[str, Any]
    quote: str
    start: int
    end: int
    text_origin: str = "native"


@dataclass
class Candidate:
    id: int
    document_id: int
    revision_id: int
    ordinal: int
    title: str
    path: str
    content: str
    token_count: int
    context_group: str | None
    anchors: list[Anchor]
    ranks: dict[str, int] = field(default_factory=dict)
    scores: dict[str, float] = field(default_factory=dict)
    fused_score: float = 0.0
    rerank_score: float | None = None


# Folder paths and ancestor IDs are live; deleted ancestors remove all descendants.
LEGAL = """
WITH RECURSIVE paths AS (
    SELECT id, knowledge_base_id, name_key::text AS path, ARRAY[id] AS ancestors
    FROM knowledge.folders WHERE parent_id IS NULL AND deleted_at IS NULL
    UNION ALL
    SELECT f.id, f.knowledge_base_id, p.path || '/' || f.name_key, p.ancestors || f.id
    FROM knowledge.folders f JOIN paths p ON f.parent_id=p.id
    WHERE f.deleted_at IS NULL AND NOT f.id=ANY(p.ancestors)
), legal AS MATERIALIZED (
    SELECT c.*, d.name AS title, d.name_key, d.description,
           coalesce(p.path, '') AS folder_path, d.folder_id,
           coalesce(p.path || '/', '') || d.name_key AS path
    FROM knowledge.chunks c
    JOIN knowledge.documents d ON d.id=c.document_id AND d.active_revision_id=c.revision_id
    JOIN knowledge.document_revisions r ON r.id=c.revision_id
    JOIN knowledge.knowledge_bases k ON k.id=d.knowledge_base_id
    JOIN iam.users u ON u.id=:actor
    LEFT JOIN iam.departments dept ON dept.id=u.department_id
    LEFT JOIN paths p ON p.id=d.folder_id
    WHERE u.status='active'
      AND (CAST(:credential AS bigint) IS NULL OR u.credential_version=:credential)
      AND (u.role='admin' OR (u.role='user' AND dept.status='active'
           AND (k.type='public' OR k.department_id=u.department_id)))
      AND k.deleted_at IS NULL AND d.deleted_at IS NULL
      AND (d.folder_id IS NULL OR p.id IS NOT NULL)
      AND r.processing_status IN ('success','partial_success') AND r.retired_at IS NULL
      AND c.status='ready'
      AND (CAST(:bases AS bigint[]) IS NULL OR k.id=ANY(CAST(:bases AS bigint[])))
      AND (CAST(:documents AS bigint[]) IS NULL OR d.id=ANY(CAST(:documents AS bigint[])))
      AND (CAST(:document_names AS text[]) IS NULL
           OR d.name_key=ANY(CAST(:document_names AS text[])))
      AND (CAST(:folders AS bigint[]) IS NULL OR p.ancestors && CAST(:folders AS bigint[]))
      AND (CAST(:extensions AS text[]) IS NULL OR r.file_extension=ANY(CAST(:extensions AS text[])))
      AND EXISTS (SELECT 1 FROM knowledge.chunk_embeddings e
                  WHERE e.chunk_id=c.id AND e.model_key=:model_key
                  AND e.dimensions=:dimensions AND e.status='ready')
      AND EXISTS (SELECT 1 FROM knowledge.chunk_anchors ca
                  JOIN knowledge.source_anchors a ON a.id=ca.anchor_id
                  WHERE ca.chunk_id=c.id AND a.revision_id=c.revision_id)
)
"""


class RetrievalRepository:
    def __init__(
        self,
        sessions: async_sessionmaker[AsyncSession],
        config: RAGConfig,
        model_key: str,
        dimensions: int,
    ) -> None:
        self.sessions, self.config = sessions, config
        self.model_key, self.dimensions = model_key, dimensions

    async def parameters(
        self, session: AsyncSession, identity: CurrentIdentity, scope: Scope
    ) -> dict[str, Any]:
        current = await AuthorizationService.revalidate(session, identity)
        return {
            "actor": current.user.id,
            "credential": current.authenticated_credential_version,
            "bases": list(scope.knowledge_base_ids)
            if scope.knowledge_base_ids is not None
            else None,
            "documents": list(scope.document_ids) if scope.document_ids is not None else None,
            "document_names": [
                normalize_name(name, max_length=255) for name in scope.document_names
            ]
            if scope.document_names is not None
            else None,
            "folders": list(scope.folder_ids) if scope.folder_ids is not None else None,
            "extensions": list(scope.extensions) if scope.extensions is not None else None,
            "model_key": self.model_key,
            "dimensions": self.dimensions,
        }

    async def validate_scope(self, identity: CurrentIdentity, scope: Scope) -> None:
        async with self.sessions() as session, session.begin():
            params = await self.parameters(session, identity, scope)
            # Explicit resource selection cannot enumerate missing or unauthorized resources.
            for ids, table in (
                (scope.knowledge_base_ids, "knowledge_bases"),
                (scope.document_ids, "documents"),
                (scope.folder_ids, "folders"),
            ):
                if not ids:
                    continue
                if len(ids) > 100 or any(type(item) is not int or item <= 0 for item in ids):
                    raise APIError(ErrorCode.RESOURCE_NOT_FOUND)
                join = (
                    ""
                    if table == "knowledge_bases"
                    else "JOIN knowledge.knowledge_bases k ON k.id=x.knowledge_base_id"
                )
                alias = "x" if table == "knowledge_bases" else "k"
                sql = f"""SELECT x.id FROM knowledge.{table} x {join}
                    JOIN iam.users u ON u.id=:actor
                    WHERE x.id=ANY(CAST(:ids AS bigint[])) AND x.deleted_at IS NULL
                      AND {alias}.deleted_at IS NULL AND (u.role='admin'
                      OR {alias}.type='public' OR {alias}.department_id=u.department_id)"""
                found = set(
                    (await session.execute(text(sql), {**params, "ids": list(ids)})).scalars()
                )
                if found != set(ids):
                    raise APIError(ErrorCode.RESOURCE_NOT_FOUND)

    async def keyword(
        self, identity: CurrentIdentity, scope: Scope, question: str
    ) -> list[tuple[int, float]]:
        expression = query_expression(question)
        if not expression:
            return []
        async with self.sessions() as session, session.begin():
            params = await self.parameters(session, identity, scope)
            rows = await session.execute(
                text(
                    LEGAL
                    + """
SELECT id, ts_rank_cd(search_vector, to_tsquery('simple', :query)) AS score
FROM legal WHERE search_vector @@ to_tsquery('simple', :query)
ORDER BY score DESC, id LIMIT :limit
"""
                ),
                {**params, "query": expression, "limit": self.config.keyword.content_limit},
            )
            return [(row.id, float(row.score)) for row in rows]

    async def metadata(
        self, identity: CurrentIdentity, scope: Scope, question: str
    ) -> list[tuple[int, float]]:
        async with self.sessions() as session, session.begin():
            params = await self.parameters(session, identity, scope)
            rows = await session.execute(
                text(
                    LEGAL
                    + """
, documents_ranked AS (
    SELECT document_id, max(greatest(similarity(name_key, :question),
           word_similarity(:question, name_key), similarity(coalesce(description,''), :question),
           word_similarity(:question, coalesce(description,'')),
           word_similarity(:question, folder_path))) AS score
    FROM legal GROUP BY document_id
    HAVING max(greatest(similarity(name_key, :question), word_similarity(:question, name_key),
           similarity(coalesce(description,''), :question),
           word_similarity(:question, coalesce(description,'')),
           word_similarity(:question, folder_path))) >= :threshold
    ORDER BY score DESC, document_id LIMIT :document_limit
), mapped AS (
    SELECT l.id, d.score, row_number() OVER (PARTITION BY l.document_id ORDER BY
        ts_rank_cd(l.search_vector, to_tsquery('simple', :query)) DESC, l.ordinal, l.id) AS position
    FROM legal l JOIN documents_ranked d ON d.document_id=l.document_id
)
SELECT id, score FROM mapped WHERE position <= :per_document ORDER BY score DESC, id
"""
                ),
                {
                    **params,
                    "question": normalize(question),
                    "query": query_expression(question),
                    "threshold": self.config.keyword.trigram_threshold,
                    "document_limit": self.config.keyword.metadata_document_limit,
                    "per_document": self.config.keyword.metadata_chunks_per_document,
                },
            )
            return [(row.id, float(row.score)) for row in rows]

    async def vector(
        self, identity: CurrentIdentity, scope: Scope, vector: list[float]
    ) -> list[tuple[int, float]]:
        value = json.dumps(validate_vector(vector, self.dimensions), allow_nan=False)
        async with self.sessions() as session, session.begin():
            params = await self.parameters(session, identity, scope)
            params.update(vector=value, limit=self.config.vector.candidate_limit)
            count = int(
                (
                    await session.execute(text(LEGAL + "SELECT count(*) FROM legal"), params)
                ).scalar_one()
            )
            exact = (
                LEGAL
                + """
, vectors AS MATERIALIZED (
    SELECT l.id, e.embedding FROM legal l JOIN knowledge.chunk_embeddings e ON e.chunk_id=l.id
    WHERE e.model_key=:model_key AND e.dimensions=:dimensions AND e.status='ready'
)
SELECT id, embedding <=> CAST(:vector AS vector) AS distance
FROM vectors ORDER BY distance, id LIMIT :limit
"""
            )
            # HNSW vector indexes support up to 2000 dimensions. Larger models use exact search.
            if count > self.config.vector.exact_scan_max_chunks and self.dimensions <= 2000:
                hnsw = self.config.hnsw
                for name, value_setting in {
                    "ef_search": hnsw.ef_search,
                    "iterative_scan": hnsw.iterative_scan,
                    "max_scan_tuples": hnsw.max_scan_tuples,
                    "scan_mem_multiplier": hnsw.scan_mem_multiplier,
                }.items():
                    await session.execute(
                        text("SELECT set_config(:name, :value, true)"),
                        {"name": "hnsw." + name, "value": str(value_setting)},
                    )
                # Literal model predicates allow PostgreSQL to select the matching partial index.
                from enterprise_mind.rag.vector_index import index_name, model_literal

                approximate = (
                    LEGAL
                    + f"""
SELECT e.chunk_id AS id, e.embedding::vector({self.dimensions})
       <=> CAST(:vector AS vector({self.dimensions})) AS distance
FROM knowledge.chunk_embeddings e
WHERE e.model_key={model_literal(self.model_key)} AND e.dimensions={self.dimensions}
  AND e.status='ready' AND (e.chunk_id IN (SELECT id FROM legal)) IS TRUE
ORDER BY e.embedding::vector({self.dimensions}) <=> CAST(:vector AS vector({self.dimensions}))
LIMIT :limit
"""
                )
                plan = (
                    await session.execute(text("EXPLAIN (FORMAT JSON) " + approximate), params)
                ).scalar_one()
                if index_name(self.model_key, self.dimensions) in json.dumps(plan):
                    rows = (await session.execute(text(approximate), params)).all()
                    if len(rows) >= min(count, self.config.vector.candidate_limit):
                        return [
                            (row.id, float(row.distance))
                            for row in sorted(rows, key=lambda item: (item.distance, item.id))
                        ]
            rows = (await session.execute(text(exact), params)).all()
            return [(row.id, float(row.distance)) for row in rows]

    async def load(
        self, identity: CurrentIdentity, scope: Scope, ids: list[int]
    ) -> dict[int, Candidate]:
        if not ids:
            return {}
        async with self.sessions() as session, session.begin():
            params = await self.parameters(session, identity, scope)
            params["ids"] = ids
            rows = (
                (
                    await session.execute(
                        text(LEGAL + "SELECT * FROM legal WHERE id=ANY(CAST(:ids AS bigint[]))"),
                        params,
                    )
                )
                .mappings()
                .all()
            )
            found = {
                row["id"]: Candidate(
                    row["id"],
                    row["document_id"],
                    row["revision_id"],
                    row["ordinal"],
                    row["title"],
                    row["path"],
                    row["content"],
                    row["token_count"],
                    row["context_group"],
                    [],
                )
                for row in rows
            }
            if not found:
                return {}
            anchors = (
                await session.execute(
                    text("""
SELECT ca.*, a.revision_id, a.source_unit_id, a.locator_type, a.locator, a.source_text,
       a.source_text_sha256, s.ordinal AS source_ordinal, coalesce(e.ordinal,0) AS element_ordinal,
       coalesce(e.structured_payload->>'text_origin','native') AS text_origin
FROM knowledge.chunk_anchors ca JOIN knowledge.source_anchors a ON a.id=ca.anchor_id
JOIN knowledge.source_units s ON s.id=a.source_unit_id
LEFT JOIN knowledge.content_elements e ON e.id=a.element_id
WHERE ca.chunk_id=ANY(CAST(:ids AS bigint[])) ORDER BY ca.chunk_id, ca.ordinal
"""),
                    {"ids": list(found)},
                )
            ).mappings()
            invalid: set[int] = set()
            for row in anchors:
                candidate = found[row["chunk_id"]]
                source = row["source_text"] or ""
                quote = row["quote_text"]
                if (
                    row["revision_id"] != candidate.revision_id
                    or not quote
                    or quote not in source
                    or candidate.content[row["content_start"] : row["content_end"]] != quote
                    or hashlib.sha256(source.encode()).hexdigest() != row["source_text_sha256"]
                ):
                    invalid.add(candidate.id)
                    continue
                candidate.anchors.append(
                    Anchor(
                        row["anchor_id"],
                        row["source_unit_id"],
                        row["source_ordinal"],
                        row["element_ordinal"],
                        row["locator_type"],
                        row["locator"],
                        quote,
                        row["content_start"],
                        row["content_end"],
                        row["text_origin"],
                    )
                )
            return {
                key: value for key, value in found.items() if value.anchors and key not in invalid
            }

    async def neighbors(
        self, identity: CurrentIdentity, scope: Scope, candidate: Candidate
    ) -> dict[int, Candidate]:
        if not candidate.context_group:
            return {}
        async with self.sessions() as session, session.begin():
            params = await self.parameters(session, identity, scope)
            rows = (
                (
                    await session.execute(
                        text(
                            LEGAL
                            + """
, ordered AS (
    SELECT l.id, row_number() OVER (ORDER BY a.source_ordinal, a.element_ordinal,
           a.char_start, l.ordinal, l.id) AS position
    FROM legal l
    JOIN LATERAL (
        SELECT s.ordinal AS source_ordinal, coalesce(e.ordinal,0) AS element_ordinal,
               coalesce((sa.locator->>'char_start')::bigint,0) AS char_start
        FROM knowledge.chunk_anchors ca JOIN knowledge.source_anchors sa ON sa.id=ca.anchor_id
        JOIN knowledge.source_units s ON s.id=sa.source_unit_id
        LEFT JOIN knowledge.content_elements e ON e.id=sa.element_id
        WHERE ca.chunk_id=l.id ORDER BY s.ordinal, e.ordinal, char_start, ca.ordinal LIMIT 1
    ) a ON true
    WHERE l.document_id=:document AND l.revision_id=:revision AND l.context_group=:group
)
SELECT n.id FROM ordered n JOIN ordered seed ON seed.id=:seed
WHERE abs(n.position-seed.position)=1 ORDER BY n.position LIMIT 2
"""
                        ),
                        {
                            **params,
                            "document": candidate.document_id,
                            "revision": candidate.revision_id,
                            "group": candidate.context_group,
                            "seed": candidate.id,
                        },
                    )
                )
                .scalars()
                .all()
            )
        return await self.load(identity, scope, list(rows))


def source_order(candidate: Candidate) -> tuple[int, int, int, int]:
    anchor = min(
        candidate.anchors,
        key=lambda item: (
            item.source_ordinal,
            item.element_ordinal,
            item.locator.get("char_start", 0),
        ),
    )
    return (
        anchor.source_ordinal,
        anchor.element_ordinal,
        int(anchor.locator.get("char_start", 0)),
        candidate.ordinal,
    )
