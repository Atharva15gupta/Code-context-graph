"""
search.py — Hybrid BM25 + graph-proximity search.

This search combines multiple signals for accurate results:
  1. BM25 text ranking on name + signature + docstring
  2. Graph proximity — symbols closer to your current file rank higher
  3. Kind boost — functions/classes rank above imports

Result: relevant symbols float to the top even with short or
camelCase / snake_case query mismatches.
"""

from __future__ import annotations

import re
from typing import TYPE_CHECKING

from rank_bm25 import BM25Okapi

if TYPE_CHECKING:
    from .graph import KnowledgeGraph


def _tokenize(text: str) -> list[str]:
    """
    Split on word boundaries, handling camelCase and snake_case.
    'getUserById' → ['get', 'user', 'by', 'id']
    'auth_service' → ['auth', 'service']
    """
    # Split camelCase
    text = re.sub(r"([a-z])([A-Z])", r"\1 \2", text)
    # Split on non-alphanumeric
    tokens = re.split(r"[^a-z0-9]+", text.lower())
    return [t for t in tokens if len(t) > 1]


KIND_BOOST: dict[str, float] = {
    "class":    1.2,
    "function": 1.1,
    "method":   1.0,
    "variable": 0.8,
    "import":   0.5,
}


class HybridSearch:
    """
    Build a BM25 index over all symbols and combine with graph proximity.

    Usage
    -----
    >>> hs = HybridSearch(kg)
    >>> hs.build()
    >>> results = hs.search("authenticate user", anchor_file="src/api.py", top_k=10)
    """

    def __init__(self, kg: "KnowledgeGraph"):
        self.kg = kg
        self._corpus: list[dict] = []
        self._bm25: BM25Okapi | None = None

    def build(self) -> None:
        """Index all symbols. Call after a scan."""
        self._corpus = self.kg.get_symbols()
        tokenized = []
        for sym in self._corpus:
            text = " ".join([
                sym.get("name", ""),
                sym.get("signature", ""),
                sym.get("docstring", ""),
                sym.get("file", ""),
            ])
            tokenized.append(_tokenize(text))
        self._bm25 = BM25Okapi(tokenized) if tokenized else None

    def search(
        self,
        query: str,
        anchor_file: str | None = None,
        top_k: int = 20,
        kind_filter: str | None = None,
    ) -> list[dict]:
        """
        Search symbols by query with optional file-proximity boosting.

        Parameters
        ----------
        query       : natural language or identifier query
        anchor_file : repo-relative path of the file the user is editing;
                      symbols from the same file and its neighbours rank higher
        top_k       : number of results to return
        kind_filter : restrict to 'function', 'class', etc.
        """
        if not self._corpus or self._bm25 is None:
            # Fall back to DB LIKE search
            return self.kg.fts_search(query, limit=top_k)

        q_tokens = _tokenize(query)
        if not q_tokens:
            return []

        # BM25 scores
        bm25_scores = self._bm25.get_scores(q_tokens)

        # Graph proximity scores
        prox_scores = self._proximity_scores(anchor_file) if anchor_file else {}

        # Combine
        scored: list[tuple[float, dict]] = []
        for i, sym in enumerate(self._corpus):
            if kind_filter and sym.get("kind") != kind_filter:
                continue
            bm25 = float(bm25_scores[i])
            prox = prox_scores.get(sym["id"], 0.0)
            kind_b = KIND_BOOST.get(sym.get("kind", ""), 1.0)
            combined = (bm25 * kind_b) + (prox * 0.3)
            if combined > 0:
                scored.append((combined, sym))

        scored.sort(key=lambda x: -x[0])
        results = []
        for score, sym in scored[:top_k]:
            sym = dict(sym)
            sym["search_score"] = round(score, 4)
            results.append(sym)
        return results

    def _proximity_scores(self, anchor_file: str) -> dict[str, float]:
        """
        Score symbols by graph proximity to the anchor file.
        Direct neighbours = 1.0, 2-hop = 0.5, 3-hop = 0.25.
        """
        scores: dict[str, float] = {}
        nx = self.kg._nx

        # Find all symbol ids in the anchor file
        anchor_ids = {
            r[0]
            for r in self.kg._con.execute(
                "SELECT id FROM symbols WHERE file=?", (anchor_file,)
            )
        }

        for anchor_id in anchor_ids:
            if not nx.has_node(anchor_id):
                continue
            scores[anchor_id] = 1.0
            for nbr in list(nx.successors(anchor_id)) + list(nx.predecessors(anchor_id)):
                scores[nbr] = max(scores.get(nbr, 0.0), 1.0)
                for nbr2 in list(nx.successors(nbr)) + list(nx.predecessors(nbr)):
                    scores[nbr2] = max(scores.get(nbr2, 0.0), 0.5)

        return scores
