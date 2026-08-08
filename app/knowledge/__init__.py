"""The knowledge engine — ApplicantOS's source of truth about the user.

``docs/CONTRACTS.md`` §8 in one namespace. Everything a caller outside this package needs
is re-exported here, so the rest of the application imports from ``app.knowledge`` rather
than reaching into individual modules:

.. code-block:: python

    from app.knowledge import KnowledgeIndexer, KnowledgeRetriever, SourceRef

The package is a pipeline with four stages, and the exports group the same way:

**Analyze** (:mod:`app.knowledge.analyzers`)
    A :class:`SourceRef` — a repository, a website, a folder, a resume — goes to the
    :class:`Analyzer` plugin that handles its kind and comes back as an
    :class:`AnalysisResult`: documents, facts, entities and edges, plus a fingerprint that
    makes the next pass cheap.

**Index** (:mod:`app.knowledge.indexer`)
    :class:`KnowledgeIndexer` runs that result into the database — documents, chunks,
    embeddings, merged facts, upserted graph nodes — and reports what it did as an
    :class:`IndexReport`. It skips unchanged sources, which is what makes continuous
    indexing affordable.

**Store** (:mod:`app.knowledge.graph`, :mod:`app.knowledge.facts`,
:mod:`app.knowledge.memory`, :mod:`app.knowledge.vector`)
    :class:`KnowledgeGraph` owns entities and edges, :class:`FactStore` owns the atomic
    claims every resume bullet must trace back to, :class:`MemoryStore` owns what the user
    has taught the system, and the :class:`VectorStore` backends hold the embeddings all
    three search through.

**Retrieve** (:mod:`app.knowledge.retrieval`)
    :class:`KnowledgeRetriever` answers "what does this user know that is relevant here?"
    by fusing vector similarity, keyword matching and graph expansion into one
    :class:`RetrievalResult`.

:mod:`app.knowledge.extractors` sits underneath all of it: the deterministic text
heuristics — skills, metrics, dates, bullets, impact — that work with zero API keys, and
the LLM-backed :class:`KnowledgeExtractor` that improves on them when a key is present.

Note:
    Importing this package imports :mod:`app.knowledge.analyzers`, which registers every
    built-in analyzer plugin as a side effect. That is deliberate: resolving a source kind
    to an analyzer goes through :mod:`app.plugins.registry`, and the registry has to be
    populated before :class:`KnowledgeIndexer` can resolve anything.

Warning:
    Five names are intentionally **not** re-exported because two modules define different
    things under the same name. Import them from the module you actually mean:

    ==================================  ==========================================
    Name                                Where it lives
    ==================================  ==========================================
    ``reciprocal_rank_fusion``          :mod:`~app.knowledge.facts` returns a ranked
                                        ``list[uuid.UUID]`` (the ranking form);
                                        :mod:`~app.knowledge.retrieval` returns
                                        ``dict[key, float]`` (the scoring form).
    ``RRF_K``                           facts / retrieval
    ``VECTOR_OVERFETCH``                facts / retrieval
    ``KEYWORD_OVERFETCH``               facts / retrieval
    ``MAX_KEYWORDS``                    facts / retrieval
    ==================================  ==========================================

    The two ``reciprocal_rank_fusion`` implementations agree on ``RRF_K`` by construction,
    so the tuning constants are equal today — but they are separately owned, and exporting
    either one from here would quietly pick a winner.
"""

from __future__ import annotations

# Analyzers: the plugin surface that turns a source into extracted content. Imported first
# because importing the sub-package is what registers the built-in analyzer plugins.
from app.knowledge.analyzers import (
    AnalysisResult,
    Analyzer,
    AnalyzerError,
    ExtractedDocument,
    ExtractedEdge,
    ExtractedEntity,
    ExtractedFact,
    SourceAccessDenied,
    SourceRef,
    SourceUnavailableError,
    analyzer_for,
    chunk_text,
    close_http_client,
    compute_fingerprint,
    estimate_tokens,
    get_analyzer,
    http_client,
)

# Deterministic text heuristics plus the LLM-backed extractor built on top of them.
from app.knowledge.extractors import (
    ACTION_VERBS,
    CONCEPT_SKILLS,
    FILLER_PHRASES,
    LEADERSHIP_TERMS,
    MAX_IMPACT,
    MIN_IMPACT,
    MIN_SOURCE_OVERLAP,
    SCALE_TERMS,
    SKILL_VOCABULARY,
    KnowledgeExtractor,
    canonical_skill,
    classify_skills,
    detect_project_name,
    extract_dates,
    extract_entities_rule_based,
    extract_facts_rule_based,
    extract_metrics,
    extract_skills,
    fact_content_hash,
    normalize_fact_text,
    score_impact,
    skill_entity_kind,
    split_bullets,
)

# The fact store. `reciprocal_rank_fusion`, `RRF_K`, `VECTOR_OVERFETCH`,
# `KEYWORD_OVERFETCH` and `MAX_KEYWORDS` are deliberately omitted; see the module warning.
from app.knowledge.facts import (
    FACT_COLLECTION,
    NEAR_DUPLICATE_PROBE_K,
    NEAR_DUPLICATE_THRESHOLD,
    SQL_DEDUPE_SCAN_LIMIT,
    FactStore,
)

# The graph store and the shared embedding/vector plumbing every store inherits.
from app.knowledge.graph import (
    DEFAULT_SUBGRAPH_LIMIT,
    EDGE_ENDPOINT_CONFIDENCE,
    EDGE_EVIDENCE_LOG_KEY,
    ENTITY_CONFIDENCE_REINFORCEMENT,
    MAX_EDGE_EVIDENCE_ENTRIES,
    MAX_NEIGHBOR_DEPTH,
    MAX_NEIGHBOR_NODES,
    MAX_SUBGRAPH_EDGES,
    MAX_SUBGRAPH_LIMIT,
    SQL_IN_CHUNK_SIZE,
    KnowledgeGraph,
    KnowledgeStore,
    chunked,
)

# The indexing pipeline and its stage vocabulary.
from app.knowledge.indexer import (
    CHUNK_COLLECTION,
    DEFAULT_INDEX_CONCURRENCY,
    EMBEDDING_BATCH_SIZE,
    FINGERPRINT_PROBE_TTL_SECONDS,
    INDEX_STAGES,
    PROBE_CACHE_NAMESPACE,
    SQLITE_INDEX_CONCURRENCY,
    STAGE_ANALYZE,
    STAGE_CHUNKS,
    STAGE_DOCUMENTS,
    STAGE_FACTS,
    STAGE_FAILED,
    STAGE_FINALIZE,
    STAGE_FINGERPRINT,
    STAGE_GRAPH,
    STAGE_RESOLVE,
    STAGE_SKIPPED,
    STAGE_START,
    IndexerError,
    IndexReport,
    KnowledgeIndexer,
    SourceNotFoundError,
    UnsupportedSourceError,
    supported_source_kinds,
)

# What the system remembers about the user's corrections, outcomes and preferences.
from app.knowledge.memory import (
    CORRECTION_TEMPLATE,
    KIND_WEIGHTS,
    MAX_MEMORY_WEIGHT,
    MEMORY_COLLECTION,
    MEMORY_HALF_LIFE_DAYS,
    MEMORY_PROMPT_HEADER,
    MEMORY_PROMPT_TOKEN_BUDGET,
    MIN_MEMORY_WEIGHT,
    MIN_RECENCY_FACTOR,
    OUTCOME_WEIGHTS,
    REPEAT_REINFORCEMENT,
    MemoryStore,
    recency_factor,
)

# Hybrid retrieval. Same five names omitted as for `facts`; see the module warning.
from app.knowledge.retrieval import (
    CANDIDATE_OVERFETCH,
    GRAPH_LINK_BOOST,
    MAX_EXPANDED_ENTITIES,
    POSTING_BODY_CHARS,
    POSTING_CHUNK_K,
    REASON_CHUNK_VECTOR,
    REASON_GRAPH,
    REASON_KEYWORD,
    REASON_MEMORY,
    REASON_VECTOR,
    SEED_ENTITY_LIMIT,
    KnowledgeRetriever,
    RetrievalResult,
)

# Vector-store protocol, backends, and the pure-python similarity helpers.
from app.knowledge.vector import (
    DEFAULT_TOP_K,
    FILTER_KEY_KIND,
    FILTER_KEY_USER_ID,
    RESERVED_FILTER_KEYS,
    Filter,
    InMemoryVectorStore,
    PgVectorStore,
    SqliteVecStore,
    VectorHit,
    VectorRecord,
    VectorStore,
    VectorStoreError,
    build_vector_store,
    close_vector_store,
    cosine_similarity,
    dot,
    get_vector_store,
    matches_filters,
    normalize,
    rank_hits,
    reset_vector_store,
)

__all__ = [
    "ACTION_VERBS",
    "CANDIDATE_OVERFETCH",
    "CHUNK_COLLECTION",
    "CONCEPT_SKILLS",
    "CORRECTION_TEMPLATE",
    "DEFAULT_INDEX_CONCURRENCY",
    "DEFAULT_SUBGRAPH_LIMIT",
    "DEFAULT_TOP_K",
    "EDGE_ENDPOINT_CONFIDENCE",
    "EDGE_EVIDENCE_LOG_KEY",
    "EMBEDDING_BATCH_SIZE",
    "ENTITY_CONFIDENCE_REINFORCEMENT",
    "FACT_COLLECTION",
    "FILLER_PHRASES",
    "FILTER_KEY_KIND",
    "FILTER_KEY_USER_ID",
    "FINGERPRINT_PROBE_TTL_SECONDS",
    "GRAPH_LINK_BOOST",
    "INDEX_STAGES",
    "KIND_WEIGHTS",
    "LEADERSHIP_TERMS",
    "MAX_EDGE_EVIDENCE_ENTRIES",
    "MAX_EXPANDED_ENTITIES",
    "MAX_IMPACT",
    "MAX_MEMORY_WEIGHT",
    "MAX_NEIGHBOR_DEPTH",
    "MAX_NEIGHBOR_NODES",
    "MAX_SUBGRAPH_EDGES",
    "MAX_SUBGRAPH_LIMIT",
    "MEMORY_COLLECTION",
    "MEMORY_HALF_LIFE_DAYS",
    "MEMORY_PROMPT_HEADER",
    "MEMORY_PROMPT_TOKEN_BUDGET",
    "MIN_IMPACT",
    "MIN_MEMORY_WEIGHT",
    "MIN_RECENCY_FACTOR",
    "MIN_SOURCE_OVERLAP",
    "NEAR_DUPLICATE_PROBE_K",
    "NEAR_DUPLICATE_THRESHOLD",
    "OUTCOME_WEIGHTS",
    "POSTING_BODY_CHARS",
    "POSTING_CHUNK_K",
    "PROBE_CACHE_NAMESPACE",
    "REASON_CHUNK_VECTOR",
    "REASON_GRAPH",
    "REASON_KEYWORD",
    "REASON_MEMORY",
    "REASON_VECTOR",
    "REPEAT_REINFORCEMENT",
    "RESERVED_FILTER_KEYS",
    "SCALE_TERMS",
    "SEED_ENTITY_LIMIT",
    "SKILL_VOCABULARY",
    "SQLITE_INDEX_CONCURRENCY",
    "SQL_DEDUPE_SCAN_LIMIT",
    "SQL_IN_CHUNK_SIZE",
    "STAGE_ANALYZE",
    "STAGE_CHUNKS",
    "STAGE_DOCUMENTS",
    "STAGE_FACTS",
    "STAGE_FAILED",
    "STAGE_FINALIZE",
    "STAGE_FINGERPRINT",
    "STAGE_GRAPH",
    "STAGE_RESOLVE",
    "STAGE_SKIPPED",
    "STAGE_START",
    "AnalysisResult",
    "Analyzer",
    "AnalyzerError",
    "ExtractedDocument",
    "ExtractedEdge",
    "ExtractedEntity",
    "ExtractedFact",
    "FactStore",
    "Filter",
    "InMemoryVectorStore",
    "IndexReport",
    "IndexerError",
    "KnowledgeExtractor",
    "KnowledgeGraph",
    "KnowledgeIndexer",
    "KnowledgeRetriever",
    "KnowledgeStore",
    "MemoryStore",
    "PgVectorStore",
    "RetrievalResult",
    "SourceAccessDenied",
    "SourceNotFoundError",
    "SourceRef",
    "SourceUnavailableError",
    "SqliteVecStore",
    "UnsupportedSourceError",
    "VectorHit",
    "VectorRecord",
    "VectorStore",
    "VectorStoreError",
    "analyzer_for",
    "build_vector_store",
    "canonical_skill",
    "chunk_text",
    "chunked",
    "classify_skills",
    "close_http_client",
    "close_vector_store",
    "compute_fingerprint",
    "cosine_similarity",
    "detect_project_name",
    "dot",
    "estimate_tokens",
    "extract_dates",
    "extract_entities_rule_based",
    "extract_facts_rule_based",
    "extract_metrics",
    "extract_skills",
    "fact_content_hash",
    "get_analyzer",
    "get_vector_store",
    "http_client",
    "matches_filters",
    "normalize",
    "normalize_fact_text",
    "rank_hits",
    "recency_factor",
    "reset_vector_store",
    "score_impact",
    "skill_entity_kind",
    "split_bullets",
    "supported_source_kinds",
]
