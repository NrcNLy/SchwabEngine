"""
macro/semantic_cache.py
=======================
ChromaDB-backed semantic vector cache for the LLM Macroeconomic Gatekeeper.

Prevents redundant Gemini API calls by caching MacroDecisionPayload results
for semantically similar news headlines, keyed by cosine-similarity embedding.

Cache Hit Criteria (from spec / config.yaml):
    Cosine similarity >= 0.92  (config: macro.semantic_cache.similarity_threshold)
    Entry age         <= 1800s  (config: macro.semantic_cache.ttl_minutes = 30)

Embedding model: all-MiniLM-L6-v2 (config: macro.semantic_cache.model)

ChromaDB Distance Note
----------------------
With ``{"hnsw:space": "cosine"}``, ChromaDB stores and returns cosine
distances where:
    distance  = 1 - cosine_similarity
    similarity = 1 - distance

A cache hit therefore requires:
    distance <= (1 - 0.92) = 0.08

MacroDecisionPayload results are JSON-serialised into the ChromaDB
``metadatas`` field (all values must be scalars per ChromaDB constraints).
The timestamp of storage is saved as a UNIX epoch float in metadata.
"""

from __future__ import annotations

import hashlib
import json
import logging
import threading
import time
from typing import Any, Dict, Optional

import chromadb
from chromadb.utils.embedding_functions import SentenceTransformerEmbeddingFunction

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------
_COLLECTION_NAME    = "macro_news_cache"
_METADATA_KEY_TS    = "stored_at"       # UNIX epoch float
_METADATA_KEY_PAYLOAD = "payload_json"  # JSON-serialised MacroDecisionPayload dict


class MacroSemanticCache:
    """
    In-memory ChromaDB semantic vector cache for macro gatekeeper decisions.

    Uses the ``all-MiniLM-L6-v2`` sentence-transformer model to embed incoming
    news headlines and retrieve semantically similar past decisions without
    making a new Gemini API call.

    Thread-safe: all public methods are guarded by a single RLock.

    Usage
    -----
        cache = MacroSemanticCache(cfg)
        hit = cache.lookup("Fed raises rates by 75bps — hawkish surprise")
        if hit:
            return hit   # MacroDecisionPayload dict
        # ... call Gemini ...
        cache.store("Fed raises rates by 75bps — hawkish surprise", payload_dict)
    """

    def __init__(self, cfg: dict) -> None:
        """
        Args:
            cfg: Top-level config dict loaded from config.yaml.
        """
        sc_cfg = cfg.get("macro", {}).get("semantic_cache", {})

        self._model_name:  str   = sc_cfg.get("model", "all-MiniLM-L6-v2")
        self._threshold:   float = float(sc_cfg.get("similarity_threshold", 0.92))
        self._ttl_sec:     float = float(sc_cfg.get("ttl_minutes", 30)) * 60.0

        # distance <= (1 - threshold) is a cache hit in cosine space
        self._max_distance: float = 1.0 - self._threshold

        self._lock = threading.RLock()

        # Initialise ephemeral (in-memory) ChromaDB client
        # The 30-minute TTL makes persistence unnecessary; a fresh process
        # starts with an empty cache, which is the correct and safe default.
        self._client = chromadb.Client()

        # Sentence-transformer embedding function
        try:
            self._ef = SentenceTransformerEmbeddingFunction(
                model_name=self._model_name
            )
            # Create collection with cosine space as specified
            self._collection = self._client.get_or_create_collection(
                name=_COLLECTION_NAME,
                metadata={"hnsw:space": "cosine"},
                embedding_function=self._ef,
            )
        except Exception as e:
            logger.warning(
                "SentenceTransformerEmbeddingFunction unavailable (%s) — semantic caching disabled.", e
            )
            self._collection = None

        logger.info(
            "MacroSemanticCache initialised — model=%s, "
            "similarity_threshold=%.2f (max_distance=%.2f), ttl=%ds",
            self._model_name, self._threshold,
            self._max_distance, int(self._ttl_sec),
        )

    # ------------------------------------------------------------------
    # Public interface
    # ------------------------------------------------------------------

    def lookup(self, headline: str) -> Optional[Dict[str, Any]]:
        """
        Search for a semantically similar cached decision.

        Args:
            headline: The news headline or macro context string to evaluate.

        Returns:
            The cached MacroDecisionPayload dict if a valid cache hit is found
            (similarity >= threshold AND age <= TTL). None otherwise.
        """
        if not headline or not headline.strip() or self._collection is None:
            return None

        with self._lock:
            try:
                count = self._collection.count()
                if count == 0:
                    return None

                results = self._collection.query(
                    query_texts=[headline],
                    n_results=min(1, count),
                    include=["documents", "metadatas", "distances"],
                )

                if not results or not results.get("distances"):
                    return None

                distances = results["distances"][0]
                metadatas = results["metadatas"][0]

                if not distances or not metadatas:
                    return None

                distance = distances[0]
                metadata = metadatas[0]

                # --- Similarity check ---
                # ChromaDB cosine distance: d = 1 - cos_similarity
                if distance > self._max_distance:
                    similarity = 1.0 - distance
                    logger.debug(
                        "MacroSemanticCache: MISS — similarity=%.4f < %.2f",
                        similarity, self._threshold,
                    )
                    return None

                # --- TTL check ---
                stored_at = float(metadata.get(_METADATA_KEY_TS, 0.0))
                age_sec   = time.time() - stored_at
                if age_sec > self._ttl_sec:
                    logger.debug(
                        "MacroSemanticCache: EXPIRED — age=%.0fs > TTL=%.0fs",
                        age_sec, self._ttl_sec,
                    )
                    return None

                # --- Cache hit ---
                payload_json = metadata.get(_METADATA_KEY_PAYLOAD, "")
                if not payload_json:
                    return None

                payload = json.loads(payload_json)
                similarity = 1.0 - distance
                logger.info(
                    "MacroSemanticCache: HIT — similarity=%.4f age=%.0fs | "
                    "trade_permitted=%s",
                    similarity, age_sec,
                    payload.get("trade_permitted", "?"),
                )
                return payload

            except Exception as exc:  # noqa: BLE001
                logger.warning(
                    "MacroSemanticCache: lookup error (returning None): %s", exc
                )
                return None

    def store(self, headline: str, payload: Dict[str, Any]) -> None:
        """
        Embed and store a headline → MacroDecisionPayload result in the cache.

        A stable document ID is derived from the SHA-256 hash of the headline
        text, ensuring that re-storing the same headline overwrites the prior
        entry rather than creating a duplicate.

        Args:
            headline: The news headline string that was evaluated.
            payload:  The MacroDecisionPayload fields as a dict (will be
                      JSON-serialised into the ChromaDB metadata).
        """
        if not headline or not headline.strip() or self._collection is None:
            return

        with self._lock:
            try:
                doc_id = _sha256_id(headline)

                metadata = {
                    _METADATA_KEY_TS:      time.time(),
                    _METADATA_KEY_PAYLOAD: json.dumps(payload),
                }

                # upsert handles both first-time add and overwrite
                self._collection.upsert(
                    ids=[doc_id],
                    documents=[headline],
                    metadatas=[metadata],
                )

                logger.debug(
                    "MacroSemanticCache: stored entry id=%s | trade_permitted=%s",
                    doc_id[:12], payload.get("trade_permitted", "?"),
                )

            except Exception as exc:  # noqa: BLE001
                logger.warning(
                    "MacroSemanticCache: store error (cache miss will occur): %s", exc
                )

    def clear(self) -> None:
        """Delete all cache entries (e.g., on new trading session)."""
        with self._lock:
            try:
                ids = self._collection.get(include=[])["ids"]
                if ids:
                    self._collection.delete(ids=ids)
                logger.info("MacroSemanticCache: cleared %d entries.", len(ids))
            except Exception as exc:  # noqa: BLE001
                logger.warning("MacroSemanticCache: clear error: %s", exc)

    @property
    def entry_count(self) -> int:
        """Current number of entries in the cache."""
        with self._lock:
            try:
                return self._collection.count()
            except Exception:  # noqa: BLE001
                return 0


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _sha256_id(text: str) -> str:
    """Derive a stable 64-hex document ID from text."""
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


# ---------------------------------------------------------------------------
# Factory
# ---------------------------------------------------------------------------

def build_from_config(cfg: dict) -> MacroSemanticCache:
    """
    Construct a MacroSemanticCache from the loaded config.yaml dict.

    Args:
        cfg: Top-level config dict.

    Returns:
        Initialised MacroSemanticCache.
    """
    return MacroSemanticCache(cfg)
