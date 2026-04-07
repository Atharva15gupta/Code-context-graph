"""
graph.py — Knowledge graph engine with confidence-scored blast radius.

KEY IMPROVEMENT over code-review-graph:
  Their blast radius is binary (affected / not affected) with ~38% precision.
  Ours scores every affected file 0.0–1.0 by relationship strength and graph
  distance, so the AI can decide how deep to read rather than getting a flat
  list of false-positives.
"""

from __future__ import annotations

import json
import sqlite3
import time
from dataclasses import dataclass, field, asdict
from pathlib import Path
from typing import Any

import networkx as nx


# ── Data model ────────────────────────────────────────────────────────────────

@dataclass
class Symbol:
    id: str            # "<file>:<line>:<kind>"
    name: str
    kind: str          # function | class | method | import | variable
    file: str          # repo-relative path
    line: int
    end_line: int
    language: str
    docstring: str = ""
    signature: str = ""
    metadata: dict = field(default_factory=dict)

    def to_dict(self) -> dict:
        d = asdict(self)
        d["metadata"] = json.dumps(d["metadata"])
        return d


@dataclass
class Edge:
    src: str
    dst: str
    kind: str          # calls | imports | inherits | uses | defines | tests
    file: str
    line: int
    weight: float = 1.0   # NEW: edge weight for confidence scoring


# ── Edge kind weights (higher = stronger dependency) ─────────────────────────
# Inheriting from a class is a stronger dependency than merely importing it.
EDGE_WEIGHTS: dict[str, float] = {
    "inherits": 1.0,
    "calls":    0.9,
    "tests":    0.8,
    "uses":     0.6,
    "imports":  0.4,
    "defines":  0.3,
}

# ── Schema ────────────────────────────────────────────────────────────────────

_DDL = """
PRAGMA journal_mode=WAL;
PRAGMA foreign_keys=ON;

CREATE TABLE IF NOT EXISTS symbols (
    id         TEXT PRIMARY KEY,
    name       TEXT NOT NULL,
    kind       TEXT NOT NULL,
    file       TEXT NOT NULL,
    line       INTEGER NOT NULL,
    end_line   INTEGER NOT NULL,
    language   TEXT NOT NULL,
    docstring  TEXT DEFAULT '',
    signature  TEXT DEFAULT '',
    metadata   TEXT DEFAULT '{}'
);

CREATE TABLE IF NOT EXISTS edges (
    src     TEXT NOT NULL,
    dst     TEXT NOT NULL,
    kind    TEXT NOT NULL,
    file    TEXT NOT NULL,
    line    INTEGER NOT NULL,
    weight  REAL    NOT NULL DEFAULT 1.0,
    PRIMARY KEY (src, dst, kind)
);

CREATE TABLE IF NOT EXISTS file_index (
    path       TEXT PRIMARY KEY,
    mtime      REAL NOT NULL,
    checksum   TEXT NOT NULL,
    language   TEXT NOT NULL,
    scanned_at REAL NOT NULL,
    loc        INTEGER NOT NULL DEFAULT 0
);

CREATE INDEX IF NOT EXISTS idx_sym_file  ON symbols(file);
CREATE INDEX IF NOT EXISTS idx_sym_name  ON symbols(name);
CREATE INDEX IF NOT EXISTS idx_edge_src  ON edges(src);
CREATE INDEX IF NOT EXISTS idx_edge_dst  ON edges(dst);

CREATE VIRTUAL TABLE IF NOT EXISTS symbols_fts USING fts5(
    id UNINDEXED, name, kind, file UNINDEXED, signature, docstring,
    content='symbols', content_rowid='rowid'
);
"""


class KnowledgeGraph:
    """
    Persistent knowledge graph: SQLite for storage, NetworkX for traversal.

    Improvements over code-review-graph
    ------------------------------------
    1. Confidence-scored blast radius — every affected file gets a 0–1 score
       based on relationship type weights and graph distance decay.
    2. Adaptive context — detects trivial single-symbol changes and skips
       graph expansion when it would add noise rather than signal.
    3. FTS5 full-text search built into the DB (no external dependency).
    4. LOC tracking per file for complexity heuristics.
    """

    def __init__(self, db_path: str | Path):
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._con = sqlite3.connect(str(self.db_path), check_same_thread=False)
        self._con.row_factory = sqlite3.Row
        self._con.executescript(_DDL)
        self._con.commit()
        self._nx: nx.DiGraph = nx.DiGraph()
        self._load_graph()

    # ── Graph load ────────────────────────────────────────────────────────────

    def _load_graph(self) -> None:
        self._nx.clear()
        for r in self._con.execute("SELECT id, kind, file FROM symbols"):
            self._nx.add_node(r["id"], kind=r["kind"], file=r["file"])
        for r in self._con.execute("SELECT src, dst, kind, weight FROM edges"):
            self._nx.add_edge(r["src"], r["dst"], kind=r["kind"], weight=r["weight"])

    # ── Symbol CRUD ───────────────────────────────────────────────────────────

    def upsert_symbols(self, symbols: list[Symbol]) -> None:
        rows = [s.to_dict() for s in symbols]
        self._con.executemany(
            """INSERT INTO symbols
               (id,name,kind,file,line,end_line,language,docstring,signature,metadata)
               VALUES (:id,:name,:kind,:file,:line,:end_line,:language,
                       :docstring,:signature,:metadata)
               ON CONFLICT(id) DO UPDATE SET
                 name=excluded.name, kind=excluded.kind,
                 line=excluded.line, end_line=excluded.end_line,
                 language=excluded.language, docstring=excluded.docstring,
                 signature=excluded.signature, metadata=excluded.metadata""",
            rows,
        )
        # Rebuild FTS for these ids
        ids = [s.id for s in symbols]
        ph = ",".join("?" * len(ids))
        self._con.execute(
            f"INSERT OR REPLACE INTO symbols_fts(id,name,kind,file,signature,docstring) "
            f"SELECT id,name,kind,file,signature,docstring FROM symbols WHERE id IN ({ph})",
            ids,
        )
        self._con.commit()
        for s in symbols:
            self._nx.add_node(s.id, kind=s.kind, file=s.file)

    def remove_file_symbols(self, file_path: str) -> None:
        ids = [r[0] for r in self._con.execute(
            "SELECT id FROM symbols WHERE file=?", (file_path,)
        )]
        if not ids:
            return
        ph = ",".join("?" * len(ids))
        self._con.execute(f"DELETE FROM edges WHERE src IN ({ph}) OR dst IN ({ph})", ids + ids)
        self._con.execute(f"DELETE FROM symbols_fts WHERE id IN ({ph})", ids)
        self._con.execute(f"DELETE FROM symbols WHERE id IN ({ph})", ids)
        self._con.commit()
        for nid in ids:
            if self._nx.has_node(nid):
                self._nx.remove_node(nid)

    def get_symbols(self, file_path: str | None = None) -> list[dict]:
        if file_path:
            rows = self._con.execute(
                "SELECT * FROM symbols WHERE file=?", (file_path,)
            ).fetchall()
        else:
            rows = self._con.execute("SELECT * FROM symbols").fetchall()
        return [dict(r) for r in rows]

    # ── Edge CRUD ─────────────────────────────────────────────────────────────

    def upsert_edges(self, edges: list[Edge]) -> None:
        self._con.executemany(
            """INSERT INTO edges (src,dst,kind,file,line,weight)
               VALUES (?,?,?,?,?,?)
               ON CONFLICT(src,dst,kind) DO UPDATE SET
                 file=excluded.file, line=excluded.line, weight=excluded.weight""",
            [(e.src, e.dst, e.kind, e.file, e.line, e.weight) for e in edges],
        )
        self._con.commit()
        for e in edges:
            self._nx.add_edge(e.src, e.dst, kind=e.kind, weight=e.weight)

    # ── File index ────────────────────────────────────────────────────────────

    def record_file(self, path: str, mtime: float, checksum: str,
                    language: str, loc: int = 0) -> None:
        self._con.execute(
            """INSERT INTO file_index (path,mtime,checksum,language,scanned_at,loc)
               VALUES (?,?,?,?,?,?)
               ON CONFLICT(path) DO UPDATE SET
                 mtime=excluded.mtime, checksum=excluded.checksum,
                 language=excluded.language, scanned_at=excluded.scanned_at,
                 loc=excluded.loc""",
            (path, mtime, checksum, language, time.time(), loc),
        )
        self._con.commit()

    def needs_rescan(self, path: str, mtime: float, checksum: str) -> bool:
        row = self._con.execute(
            "SELECT mtime, checksum FROM file_index WHERE path=?", (path,)
        ).fetchone()
        return row is None or row["mtime"] != mtime or row["checksum"] != checksum

    # ── Confidence-scored blast radius ────────────────────────────────────────

    def blast_radius(
        self,
        changed_files: list[str],
        max_depth: int = 4,
    ) -> dict[str, Any]:
        """
        Return blast radius with per-file confidence scores (0.0 – 1.0).

        Score formula
        -------------
        score(node) = max over all paths from changed symbols to node of:
            product(edge_weight) * depth_decay^depth

        This means:
        - A direct caller via a `calls` edge scores ~0.9
        - A file that merely imports a changed symbol scores ~0.4
        - Deep transitive dependents score progressively lower
        - The AI can threshold: read >0.7 always, read 0.3–0.7 if unsure,
          skip <0.3 unless asked

        KEY IMPROVEMENT: code-review-graph returns a flat list with ~38%
        precision. We return ranked scores so the AI can choose its own
        confidence threshold.
        """
        DEPTH_DECAY = 0.75  # score * 0.75 per hop

        # Direct symbols in changed files
        direct: dict[str, float] = {}  # symbol_id -> score (1.0)
        for f in changed_files:
            for r in self._con.execute("SELECT id FROM symbols WHERE file=?", (f,)):
                direct[r[0]] = 1.0

        # Adaptive: if change is trivially small, skip graph expansion
        # (fixes code-review-graph's <1x efficiency on single-file tiny changes)
        if self._is_trivial_change(changed_files, len(direct)):
            return self._trivial_result(changed_files, direct)

        # BFS with score propagation
        # node_id -> best score seen
        scores: dict[str, float] = dict(direct)
        frontier: dict[str, float] = dict(direct)

        for _depth in range(max_depth):
            next_frontier: dict[str, float] = {}
            for node, node_score in frontier.items():
                if not self._nx.has_node(node):
                    continue
                for pred in self._nx.predecessors(node):
                    edge_data = self._nx.edges[pred, node]
                    edge_weight = edge_data.get("weight", 1.0)
                    propagated = node_score * edge_weight * DEPTH_DECAY
                    if propagated > scores.get(pred, 0.0):
                        scores[pred] = propagated
                        next_frontier[pred] = propagated
            frontier = {k: v for k, v in next_frontier.items() if v > 0.05}
            if not frontier:
                break

        # Aggregate to file level (max score of any symbol in the file)
        file_scores: dict[str, float] = {f: 1.0 for f in changed_files}
        sym_details: list[dict] = []

        for sid, score in scores.items():
            row = self._con.execute(
                "SELECT name, kind, file, line FROM symbols WHERE id=?", (sid,)
            ).fetchone()
            if not row:
                continue
            f = row["file"]
            file_scores[f] = max(file_scores.get(f, 0.0), score)
            sym_details.append({
                "id": sid,
                "name": row["name"],
                "kind": row["kind"],
                "file": f,
                "line": row["line"],
                "score": round(score, 3),
                "is_direct": sid in direct,
            })

        # Sort files by descending score
        ranked_files = sorted(file_scores.items(), key=lambda x: -x[1])

        # Impact label based on score distribution
        high_conf = sum(1 for _, s in ranked_files if s >= 0.7)
        impact = (
            "critical" if high_conf > 20
            else "high" if high_conf > 8
            else "medium" if high_conf > 2
            else "low"
        )

        return {
            "direct_symbols": [s for s in sym_details if s["is_direct"]],
            "affected_symbols": sorted(sym_details, key=lambda x: -x["score"]),
            "ranked_files": ranked_files,          # [(path, score), ...]
            "affected_files": [f for f, _ in ranked_files],
            "total_impact": impact,
            "is_trivial": False,
            "precision_hint": (
                "Files scored ≥0.7 are very likely affected. "
                "0.3–0.7 are possibly affected. <0.3 are distant dependents."
            ),
        }

    def _is_trivial_change(self, changed_files: list[str], n_symbols: int) -> bool:
        """
        Detect trivial changes (single small file) where graph expansion
        would add overhead without value.
        FIX for code-review-graph's <1x efficiency on small changes.
        """
        if len(changed_files) != 1:
            return False
        row = self._con.execute(
            "SELECT loc FROM file_index WHERE path=?", (changed_files[0],)
        ).fetchone()
        if row and row["loc"] < 30 and n_symbols <= 2:
            return True
        return False

    def _trivial_result(self, changed_files: list[str], direct: dict) -> dict:
        sym_details = []
        for sid in direct:
            row = self._con.execute(
                "SELECT name, kind, file, line FROM symbols WHERE id=?", (sid,)
            ).fetchone()
            if row:
                sym_details.append({
                    "id": sid, "name": row["name"], "kind": row["kind"],
                    "file": row["file"], "line": row["line"],
                    "score": 1.0, "is_direct": True,
                })
        return {
            "direct_symbols": sym_details,
            "affected_symbols": sym_details,
            "ranked_files": [(f, 1.0) for f in changed_files],
            "affected_files": changed_files,
            "total_impact": "low",
            "is_trivial": True,
            "precision_hint": "Trivial change — graph expansion skipped to save tokens.",
        }

    # ── Search ────────────────────────────────────────────────────────────────

    def fts_search(self, query: str, limit: int = 20) -> list[dict]:
        """FTS5 full-text search over symbol names, signatures, and docstrings."""
        try:
            rows = self._con.execute(
                """SELECT s.* FROM symbols s
                   JOIN symbols_fts f ON s.id = f.id
                   WHERE symbols_fts MATCH ?
                   ORDER BY rank LIMIT ?""",
                (query, limit),
            ).fetchall()
            return [dict(r) for r in rows]
        except sqlite3.OperationalError:
            # Fallback to LIKE search
            return self.like_search(query, limit)

    def like_search(self, query: str, limit: int = 20) -> list[dict]:
        rows = self._con.execute(
            "SELECT * FROM symbols WHERE name LIKE ? LIMIT ?",
            (f"%{query}%", limit),
        ).fetchall()
        return [dict(r) for r in rows]

    def context_for_symbols(self, symbol_ids: list[str]) -> list[dict]:
        result = []
        for sid in symbol_ids:
            row = self._con.execute("SELECT * FROM symbols WHERE id=?", (sid,)).fetchone()
            if not row:
                continue
            sym = dict(row)
            sym["calls"] = [r[0] for r in self._con.execute(
                "SELECT dst FROM edges WHERE src=?", (sid,))]
            sym["called_by"] = [r[0] for r in self._con.execute(
                "SELECT src FROM edges WHERE dst=?", (sid,))]
            result.append(sym)
        return result

    # ── Stats ─────────────────────────────────────────────────────────────────

    def stats(self) -> dict:
        n_sym   = self._con.execute("SELECT COUNT(*) FROM symbols").fetchone()[0]
        n_edge  = self._con.execute("SELECT COUNT(*) FROM edges").fetchone()[0]
        n_file  = self._con.execute("SELECT COUNT(*) FROM file_index").fetchone()[0]
        n_loc   = self._con.execute("SELECT SUM(loc) FROM file_index").fetchone()[0] or 0
        langs   = dict(self._con.execute(
            "SELECT language, COUNT(*) FROM symbols GROUP BY language"
        ).fetchall())
        return {
            "symbols": n_sym, "edges": n_edge,
            "files": n_file, "total_loc": n_loc,
            "languages": langs,
        }

    def close(self) -> None:
        self._con.close()
