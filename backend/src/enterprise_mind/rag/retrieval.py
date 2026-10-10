import asyncio
import math
import re
from dataclasses import dataclass, field
from difflib import SequenceMatcher

from sqlalchemy.exc import SQLAlchemyError

from enterprise_mind.identity.service import CurrentIdentity
from enterprise_mind.rag.config import RAGConfig
from enterprise_mind.rag.keywords import normalize
from enterprise_mind.rag.providers import (
    EmbeddingProvider,
    ProviderFailure,
    RerankProvider,
    TokenCounter,
)
from enterprise_mind.rag.repository import Candidate, RetrievalRepository, Scope, source_order


@dataclass
class ContextBlock:
    candidates: list[Candidate]
    content: str
    token_count: int


@dataclass
class RetrievalResult:
    status: str
    contexts: list[ContextBlock] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)


def overlap(left: Candidate, right: Candidate) -> float:
    if (left.document_id, left.revision_id) != (right.document_id, right.revision_id):
        return 0.0

    def texts(candidate: Candidate) -> dict[int, str]:
        values: dict[int, str] = {}
        for anchor in candidate.anchors:
            values[anchor.source_unit_id] = values.get(anchor.source_unit_id, "") + normalize(
                anchor.quote
            )
        return values

    a, b = texts(left), texts(right)
    denominator = min(sum(map(len, a.values())), sum(map(len, b.values())))
    if not denominator:
        return 0.0
    matched = sum(
        block.size
        for source in a.keys() & b.keys()
        for block in SequenceMatcher(
            None, a[source], b[source], autojunk=False
        ).get_matching_blocks()
    )
    return matched / denominator


def deduplicate(candidates: list[Candidate], threshold: float) -> list[Candidate]:
    result: list[Candidate] = []
    for candidate in candidates:
        if any(overlap(candidate, other) >= threshold for other in result):
            continue
        result.append(candidate)
    return result


def fuse(
    lists: dict[str, list[tuple[int, float]]], candidates: dict[int, Candidate], config: RAGConfig
) -> list[Candidate]:
    weights = {
        "keyword": config.rrf.keyword_weight,
        "metadata": config.rrf.metadata_weight,
        "vector": config.rrf.vector_weight,
    }
    for channel, rows in lists.items():
        seen = set()
        for rank, (chunk_id, score) in enumerate(rows, 1):
            if chunk_id not in candidates or chunk_id in seen:
                continue
            seen.add(chunk_id)
            candidate = candidates[chunk_id]
            candidate.ranks[channel], candidate.scores[channel] = rank, score
            candidate.fused_score += weights[channel] / (config.rrf.k + rank)
    ordered = sorted(
        candidates.values(),
        key=lambda c: (-c.fused_score, min(c.ranks.values(), default=1000000), c.id),
    )
    # Overlapping ties favor the smaller independent context, then stable ID.
    chosen = deduplicate(
        sorted(ordered, key=lambda c: (-c.fused_score, c.token_count, c.id)),
        config.dedup.anchor_overlap_ratio,
    )
    chosen.sort(key=lambda c: (-c.fused_score, min(c.ranks.values(), default=1000000), c.id))
    return chosen[: config.rrf.fused_limit]


def strong_evidence(candidate: Candidate, question: str, consensus_rank: int) -> bool:
    if (
        candidate.ranks.get("keyword", consensus_rank + 1) <= consensus_rank
        and candidate.ranks.get("vector", consensus_rank + 1) <= consensus_rank
    ):
        return True
    # Exact identifiers require a direct anchored content match, not just filename similarity.
    identifiers = re.findall(r"[A-Za-z_][A-Za-z0-9_.\-/]*", question)
    identifiers = [item for item in identifiers if re.search(r"\d|[_./-]|[a-z][A-Z]", item)]
    quotes = "\n".join(normalize(anchor.quote) for anchor in candidate.anchors)
    for identifier in identifiers:
        if re.search(r"(?<![\w.\-/])" + re.escape(normalize(identifier)) + r"(?![\w.\-/])", quotes):
            return True
    return False


def rerank_text(candidate: Candidate, question: str, reranker: RerankProvider) -> str | None:
    if reranker.counter.count(question + "\n" + candidate.content) <= reranker.max_input_tokens:
        return candidate.content
    # Cut only at complete anchor boundaries; never truncate a cell or code fragment.
    pieces: list[str] = []
    for anchor in candidate.anchors:
        trial = "\n".join([*pieces, anchor.quote])
        if reranker.counter.count(question + "\n" + trial) <= reranker.max_input_tokens:
            pieces.append(anchor.quote)
    return "\n".join(pieces) if pieces else None


class RetrievalService:
    def __init__(
        self,
        repository: RetrievalRepository,
        embedding: EmbeddingProvider,
        reranker: RerankProvider | None,
        config: RAGConfig,
    ) -> None:
        self.repository, self.embedding, self.reranker, self.config = (
            repository,
            embedding,
            reranker,
            config,
        )
        if reranker and (
            config.rerank.model_key != reranker.model_key or config.rerank.min_score is None
        ):
            raise ValueError("rerank model and calibrated threshold must match")

    async def retrieve(
        self,
        identity: CurrentIdentity,
        question: str,
        *,
        scope: Scope | None = None,
        agent_scope: Scope | None = None,
        available_input_tokens: int,
        counter: TokenCounter,
    ) -> RetrievalResult:
        if not question.strip() or len(question) > 4000 or available_input_tokens <= 0:
            return RetrievalResult("insufficient_evidence")
        scope, agent_scope = scope or Scope(), agent_scope or Scope()
        await self.repository.validate_scope(identity, scope)
        await self.repository.validate_scope(identity, agent_scope)
        scope = scope.intersect(agent_scope)

        async def vectors() -> list[tuple[int, float]]:
            result = await self.embedding.embed([question.strip()])
            if len(result) != 1:
                raise ProviderFailure("EMBEDDING_INVALID")
            return await self.repository.vector(identity, scope, result[0])

        calls = await asyncio.gather(
            self.repository.keyword(identity, scope, question),
            self.repository.metadata(identity, scope, question),
            vectors(),
            return_exceptions=True,
        )
        lists = {}
        warnings = []
        for name, result in zip(("keyword", "metadata", "vector"), calls, strict=True):
            if isinstance(result, ProviderFailure | SQLAlchemyError | TimeoutError):
                warnings.append(
                    {
                        "keyword": "精确关键词检索暂不可用。",
                        "metadata": "文档名称和路径检索暂不可用。",
                        "vector": "语义检索暂不可用。",
                    }[name]
                )
            elif isinstance(result, BaseException):
                raise result
            else:
                lists[name] = result
        if not lists:
            return RetrievalResult("unavailable", warnings=warnings)
        ids = list(dict.fromkeys(item[0] for rows in lists.values() for item in rows))
        candidates = await self.repository.load(identity, scope, ids)
        fused = fuse(lists, candidates, self.config)[: self.config.rerank.candidate_limit]
        ranked: list[Candidate] = []
        if self.reranker is not None:
            pairs = [(item, rerank_text(item, question, self.reranker)) for item in fused]
            valid_pairs = [(item, value) for item, value in pairs if value is not None]
            try:
                scores = await self.reranker.rerank(question, [value for _, value in valid_pairs])
                if len(scores) != len(valid_pairs) or any(
                    not math.isfinite(value) for value in scores
                ):
                    raise ProviderFailure("RERANK_INVALID")
                assert self.config.rerank.min_score is not None
                for (candidate, _), score in zip(valid_pairs, scores, strict=True):
                    candidate.rerank_score = score
                    if score >= self.config.rerank.min_score:
                        ranked.append(candidate)
                ranked.sort(key=lambda c: (-float(c.rerank_score or 0), -c.fused_score, c.id))
            except ProviderFailure:
                warnings.append("相关性复核暂不可用，仅使用具有直接支持的强证据。")
                ranked = [
                    c
                    for c in fused
                    if strong_evidence(c, question, self.config.fallback.consensus_rank)
                ]
        else:
            warnings.append("尚未配置并校准重排模型，仅使用具有直接支持的强证据。")
            ranked = [
                c
                for c in fused
                if strong_evidence(c, question, self.config.fallback.consensus_rank)
            ]
        # Re-read permissions and active sources after the external call.
        fresh = await self.repository.load(identity, scope, [c.id for c in ranked])
        ranked = [fresh[c.id] for c in ranked if c.id in fresh]
        budget = min(
            self.config.context.absolute_max_tokens,
            math.floor(available_input_tokens * self.config.context.max_token_ratio),
        )
        contexts: list[ContextBlock] = []
        used: list[Candidate] = []
        spent = 0
        for seed in ranked:
            if len(contexts) >= self.config.context.max_blocks:
                break
            if any(
                seed.id == other.id
                or overlap(seed, other) >= self.config.dedup.anchor_overlap_ratio
                for other in used
            ):
                continue
            content = seed.content
            tokens = counter.count(content)
            if tokens > budget - spent:
                continue
            selected = [seed]
            if self.config.context.neighbor_each_side:
                adjacent = await self.repository.neighbors(identity, scope, seed)
                for neighbor in sorted(adjacent.values(), key=source_order):
                    if neighbor.id in {item.id for item in used}:
                        continue
                    proposed = sorted([*selected, neighbor], key=source_order)
                    trial = "\n\n".join(item.content for item in proposed)
                    if counter.count(trial) <= min(budget - spent, self.config.chunk.max_tokens):
                        selected, content, tokens = proposed, trial, counter.count(trial)
            contexts.append(ContextBlock(selected, content, tokens))
            used.extend(selected)
            spent += tokens
        # This final read also covers revocation during adjacent-context loading.
        allowed = await self.repository.load(identity, scope, [item.id for item in used])
        contexts = [
            block for block in contexts if all(item.id in allowed for item in block.candidates)
        ]
        return RetrievalResult("ok" if contexts else "insufficient_evidence", contexts, warnings)
