"""
parsers/treesitter.py — Tree-sitter powered symbol extraction.

KEY IMPROVEMENT over code-review-graph and our own v0.1 regex parsers:
  Tree-sitter produces a real AST, so we correctly handle:
  - Nested functions and closures
  - Decorated functions / classes
  - Arrow functions, async generators
  - Multi-line signatures
  - Actual call sites (not just regex-matched identifiers)

Supported grammars
------------------
  Python, JavaScript, TypeScript, TSX, Go, Rust, Java
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from tree_sitter import Language, Node, Parser
import tree_sitter_python     as _tsp
import tree_sitter_javascript as _tsj
import tree_sitter_typescript as _tsts
import tree_sitter_go         as _tsgo
import tree_sitter_rust       as _tsr
import tree_sitter_java       as _tsja

from ..graph import Edge, Symbol, EDGE_WEIGHTS


# ── Language registry ─────────────────────────────────────────────────────────

_LANGS: dict[str, Language] = {
    "python":     Language(_tsp.language()),
    "javascript": Language(_tsj.language()),
    "typescript": Language(_tsts.language_typescript()),
    "tsx":        Language(_tsts.language_tsx()),
    "go":         Language(_tsgo.language()),
    "rust":       Language(_tsr.language()),
    "java":       Language(_tsja.language()),
}

EXTENSION_TO_LANG: dict[str, str] = {
    ".py":   "python",
    ".js":   "javascript",
    ".mjs":  "javascript",
    ".cjs":  "javascript",
    ".jsx":  "javascript",
    ".ts":   "typescript",
    ".tsx":  "tsx",
    ".go":   "go",
    ".rs":   "rust",
    ".java": "java",
}

SUPPORTED_EXTENSIONS: frozenset[str] = frozenset(EXTENSION_TO_LANG)


# ── Helpers ───────────────────────────────────────────────────────────────────

def _sid(file: str, line: int, kind: str) -> str:
    return f"{file}:{line}:{kind}"


def _node_text(node: Node, source: bytes) -> str:
    return source[node.start_byte:node.end_byte].decode("utf-8", errors="replace")


def _first_child_text(node: Node, type_: str, source: bytes) -> str:
    for child in node.children:
        if child.type == type_:
            return _node_text(child, source)
    return ""


def _docstring_from_body(body: Node, source: bytes) -> str:
    """Extract Python-style docstring from a function/class body."""
    for child in body.children:
        if child.type == "expression_statement":
            for sub in child.children:
                if sub.type == "string":
                    raw = _node_text(sub, source).strip("\"' \n")
                    return raw[:500]
    return ""


# ── Python extractor ──────────────────────────────────────────────────────────

_PY_FUNC_KINDS   = {"function_definition", "async_function_definition"}
_PY_CLASS_KIND   = "class_definition"
_PY_IMPORT_KINDS = {"import_statement", "import_from_statement"}
_PY_CALL_KIND    = "call"


def _extract_python(source: bytes, file_path: str) -> tuple[list[Symbol], list[Edge]]:
    parser = Parser(_LANGS["python"])
    tree = parser.parse(source)
    symbols: list[Symbol] = []
    edges:   list[Edge]   = []

    # name → symbol_id for local resolution
    name_map: dict[str, str] = {}

    def walk(node: Node, parent_kind: str = "") -> None:
        if node.type in _PY_FUNC_KINDS:
            name_node = node.child_by_field_name("name")
            if not name_node:
                return
            name = _node_text(name_node, source)
            kind = "method" if parent_kind == "class" else "function"
            line = node.start_point[0] + 1
            end_line = node.end_point[0] + 1

            # Build signature from first line
            sig_end = source.find(b"\n", node.start_byte)
            sig = source[node.start_byte: sig_end if sig_end != -1 else node.start_byte + 120]
            sig_str = sig.decode("utf-8", errors="replace").strip()[:200]

            # Docstring
            body = node.child_by_field_name("body")
            doc = _docstring_from_body(body, source) if body else ""

            sid = _sid(file_path, line, kind)
            symbols.append(Symbol(
                id=sid, name=name, kind=kind, file=file_path,
                line=line, end_line=end_line, language="python",
                docstring=doc, signature=sig_str,
            ))
            name_map[name] = sid

            # Walk for calls inside this function
            _extract_calls_python(node, sid, file_path, source, edges)

            # Recurse (for nested functions / methods)
            for child in node.children:
                walk(child, parent_kind)

        elif node.type == _PY_CLASS_KIND:
            name_node = node.child_by_field_name("name")
            if not name_node:
                return
            name = _node_text(name_node, source)
            line = node.start_point[0] + 1
            end_line = node.end_point[0] + 1
            sid = _sid(file_path, line, "class")
            symbols.append(Symbol(
                id=sid, name=name, kind="class", file=file_path,
                line=line, end_line=end_line, language="python",
                signature=f"class {name}",
            ))
            name_map[name] = sid

            # Inheritance edges
            superclasses = node.child_by_field_name("superclasses")
            if superclasses:
                for arg in superclasses.children:
                    if arg.type in {"identifier", "attribute"}:
                        base = _node_text(arg, source)
                        if base in name_map:
                            edges.append(Edge(
                                src=sid, dst=name_map[base],
                                kind="inherits", file=file_path, line=line,
                                weight=EDGE_WEIGHTS["inherits"],
                            ))

            for child in node.children:
                walk(child, "class")

        elif node.type in _PY_IMPORT_KINDS:
            line = node.start_point[0] + 1
            sig = _node_text(node, source)[:120]
            # Extract imported names
            for child in node.children:
                if child.type in {"dotted_name", "identifier"}:
                    iname = _node_text(child, source)
                    sid = _sid(file_path, line, "import")
                    symbols.append(Symbol(
                        id=sid, name=iname, kind="import",
                        file=file_path, line=line, end_line=line,
                        language="python", signature=sig,
                    ))
                    break  # one symbol per import line is enough
        else:
            for child in node.children:
                walk(child, parent_kind)

    walk(tree.root_node)
    return symbols, _resolve_edges(edges, name_map)


def _extract_calls_python(
    func_node: Node, caller_id: str, file_path: str,
    source: bytes, edges: list[Edge],
) -> None:
    """Walk a function body and emit call edges."""
    def _walk(node: Node) -> None:
        if node.type == _PY_CALL_KIND:
            fn = node.child_by_field_name("function")
            if fn:
                name = ""
                if fn.type == "identifier":
                    name = _node_text(fn, source)
                elif fn.type == "attribute":
                    attr = fn.child_by_field_name("attribute")
                    if attr:
                        name = _node_text(attr, source)
                if name:
                    line = node.start_point[0] + 1
                    edges.append(Edge(
                        src=caller_id, dst=f"__ref__{name}",
                        kind="calls", file=file_path, line=line,
                        weight=EDGE_WEIGHTS["calls"],
                    ))
        for child in node.children:
            _walk(child)
    _walk(func_node)


def _resolve_edges(edges: list[Edge], name_map: dict[str, str]) -> list[Edge]:
    resolved = []
    for e in edges:
        if e.dst.startswith("__ref__"):
            ref = e.dst[7:]
            if ref in name_map:
                resolved.append(Edge(
                    src=e.src, dst=name_map[ref],
                    kind=e.kind, file=e.file, line=e.line, weight=e.weight,
                ))
        else:
            resolved.append(e)
    return resolved


# ── JavaScript / TypeScript extractor ────────────────────────────────────────

_JS_FUNC_KINDS = {
    "function_declaration",
    "function_expression",
    "arrow_function",
    "method_definition",
    "generator_function_declaration",
}
_JS_CLASS_KINDS = {"class_declaration", "class_expression"}


def _extract_js_ts(source: bytes, file_path: str, language: str) -> tuple[list[Symbol], list[Edge]]:
    lang_key = language if language in _LANGS else "javascript"
    parser = Parser(_LANGS[lang_key])
    tree = parser.parse(source)
    symbols: list[Symbol] = []
    edges:   list[Edge]   = []
    name_map: dict[str, str] = {}

    def walk(node: Node, parent_kind: str = "") -> None:
        if node.type in _JS_FUNC_KINDS:
            name_node = node.child_by_field_name("name")
            name = _node_text(name_node, source) if name_node else "<anon>"
            kind = "method" if parent_kind == "class" else "function"
            line = node.start_point[0] + 1
            sig_end = source.find(b"\n", node.start_byte)
            sig = source[node.start_byte: sig_end if sig_end != -1 else node.start_byte + 120]
            sid = _sid(file_path, line, kind)
            symbols.append(Symbol(
                id=sid, name=name, kind=kind, file=file_path,
                line=line, end_line=node.end_point[0] + 1,
                language=language,
                signature=sig.decode("utf-8", errors="replace").strip()[:200],
            ))
            if name != "<anon>":
                name_map[name] = sid
            _extract_calls_js(node, sid, file_path, source, edges)
            for child in node.children:
                walk(child, kind)

        elif node.type in _JS_CLASS_KINDS:
            name_node = node.child_by_field_name("name")
            name = _node_text(name_node, source) if name_node else "<anon>"
            line = node.start_point[0] + 1
            sid = _sid(file_path, line, "class")
            symbols.append(Symbol(
                id=sid, name=name, kind="class", file=file_path,
                line=line, end_line=node.end_point[0] + 1,
                language=language, signature=f"class {name}",
            ))
            if name != "<anon>":
                name_map[name] = sid
            # inheritance
            heritage = node.child_by_field_name("heritage") or node.child_by_field_name("extends")
            if heritage:
                for child in heritage.children:
                    if child.type == "identifier":
                        base = _node_text(child, source)
                        if base in name_map:
                            edges.append(Edge(
                                src=sid, dst=name_map[base],
                                kind="inherits", file=file_path, line=line,
                                weight=EDGE_WEIGHTS["inherits"],
                            ))
            for child in node.children:
                walk(child, "class")

        elif node.type == "import_statement":
            line = node.start_point[0] + 1
            sig = _node_text(node, source)[:120]
            # Get module specifier
            for child in node.children:
                if child.type == "string":
                    iname = _node_text(child, source).strip("'\"")
                    symbols.append(Symbol(
                        id=_sid(file_path, line, "import"),
                        name=iname, kind="import", file=file_path,
                        line=line, end_line=line, language=language,
                        signature=sig,
                    ))
                    break
        else:
            for child in node.children:
                walk(child, parent_kind)

    walk(tree.root_node)
    return symbols, _resolve_edges(edges, name_map)


def _extract_calls_js(
    func_node: Node, caller_id: str, file_path: str,
    source: bytes, edges: list[Edge],
) -> None:
    def _walk(node: Node) -> None:
        if node.type == "call_expression":
            fn = node.child_by_field_name("function")
            if fn:
                name = ""
                if fn.type == "identifier":
                    name = _node_text(fn, source)
                elif fn.type == "member_expression":
                    prop = fn.child_by_field_name("property")
                    if prop:
                        name = _node_text(prop, source)
                if name:
                    line = node.start_point[0] + 1
                    edges.append(Edge(
                        src=caller_id, dst=f"__ref__{name}",
                        kind="calls", file=file_path, line=line,
                        weight=EDGE_WEIGHTS["calls"],
                    ))
        for child in node.children:
            _walk(child)
    _walk(func_node)


# ── Go extractor ──────────────────────────────────────────────────────────────

def _extract_go(source: bytes, file_path: str) -> tuple[list[Symbol], list[Edge]]:
    parser = Parser(_LANGS["go"])
    tree = parser.parse(source)
    symbols: list[Symbol] = []
    edges:   list[Edge]   = []
    name_map: dict[str, str] = {}

    def walk(node: Node) -> None:
        if node.type == "function_declaration":
            name_node = node.child_by_field_name("name")
            if name_node:
                name = _node_text(name_node, source)
                line = node.start_point[0] + 1
                sid = _sid(file_path, line, "function")
                sig_end = source.find(b"\n", node.start_byte)
                sig = source[node.start_byte: sig_end if sig_end != -1 else node.start_byte + 120]
                symbols.append(Symbol(
                    id=sid, name=name, kind="function", file=file_path,
                    line=line, end_line=node.end_point[0] + 1,
                    language="go",
                    signature=sig.decode("utf-8", errors="replace").strip()[:200],
                ))
                name_map[name] = sid

        elif node.type == "method_declaration":
            name_node = node.child_by_field_name("name")
            if name_node:
                name = _node_text(name_node, source)
                line = node.start_point[0] + 1
                sid = _sid(file_path, line, "method")
                symbols.append(Symbol(
                    id=sid, name=name, kind="method", file=file_path,
                    line=line, end_line=node.end_point[0] + 1,
                    language="go", signature=f"func {name}",
                ))
                name_map[name] = sid

        elif node.type == "type_declaration":
            for child in node.children:
                if child.type == "type_spec":
                    name_node = child.child_by_field_name("name")
                    type_node = child.child_by_field_name("type")
                    if name_node and type_node and type_node.type == "struct_type":
                        name = _node_text(name_node, source)
                        line = node.start_point[0] + 1
                        sid = _sid(file_path, line, "class")
                        symbols.append(Symbol(
                            id=sid, name=name, kind="class", file=file_path,
                            line=line, end_line=node.end_point[0] + 1,
                            language="go", signature=f"type {name} struct",
                        ))
                        name_map[name] = sid

        elif node.type == "import_declaration":
            line = node.start_point[0] + 1
            for child in node.children:
                if child.type == "import_spec_list":
                    for spec in child.children:
                        if spec.type == "import_spec":
                            for sub in spec.children:
                                if sub.type == "interpreted_string_literal":
                                    iname = _node_text(sub, source).strip('"')
                                    symbols.append(Symbol(
                                        id=_sid(file_path, line, "import"),
                                        name=iname, kind="import",
                                        file=file_path, line=line, end_line=line,
                                        language="go", signature=f'import "{iname}"',
                                    ))

        for child in node.children:
            walk(child)

    walk(tree.root_node)
    return symbols, edges


# ── Rust extractor ────────────────────────────────────────────────────────────

def _extract_rust(source: bytes, file_path: str) -> tuple[list[Symbol], list[Edge]]:
    parser = Parser(_LANGS["rust"])
    tree = parser.parse(source)
    symbols: list[Symbol] = []
    name_map: dict[str, str] = {}

    def walk(node: Node) -> None:
        if node.type == "function_item":
            name_node = node.child_by_field_name("name")
            if name_node:
                name = _node_text(name_node, source)
                line = node.start_point[0] + 1
                sig_end = source.find(b"\n", node.start_byte)
                sig = source[node.start_byte: sig_end if sig_end != -1 else node.start_byte + 120]
                sid = _sid(file_path, line, "function")
                symbols.append(Symbol(
                    id=sid, name=name, kind="function", file=file_path,
                    line=line, end_line=node.end_point[0] + 1,
                    language="rust",
                    signature=sig.decode("utf-8", errors="replace").strip()[:200],
                ))
                name_map[name] = sid

        elif node.type in {"struct_item", "enum_item", "trait_item"}:
            name_node = node.child_by_field_name("name")
            if name_node:
                name = _node_text(name_node, source)
                line = node.start_point[0] + 1
                kind = "class"
                symbols.append(Symbol(
                    id=_sid(file_path, line, kind), name=name, kind=kind,
                    file=file_path, line=line, end_line=node.end_point[0] + 1,
                    language="rust", signature=f"{node.type.split('_')[0]} {name}",
                ))

        elif node.type == "use_declaration":
            line = node.start_point[0] + 1
            sig = _node_text(node, source)[:120]
            symbols.append(Symbol(
                id=_sid(file_path, line, "import"),
                name=sig.replace("use ", "").strip(";")[:60],
                kind="import", file=file_path, line=line, end_line=line,
                language="rust", signature=sig,
            ))

        for child in node.children:
            walk(child)

    walk(tree.root_node)
    return symbols, []


# ── Java extractor ────────────────────────────────────────────────────────────

def _extract_java(source: bytes, file_path: str) -> tuple[list[Symbol], list[Edge]]:
    parser = Parser(_LANGS["java"])
    tree = parser.parse(source)
    symbols: list[Symbol] = []
    edges:   list[Edge]   = []
    name_map: dict[str, str] = {}

    def walk(node: Node, parent_kind: str = "") -> None:
        if node.type in {"class_declaration", "interface_declaration", "enum_declaration"}:
            name_node = node.child_by_field_name("name")
            if name_node:
                name = _node_text(name_node, source)
                line = node.start_point[0] + 1
                sid = _sid(file_path, line, "class")
                symbols.append(Symbol(
                    id=sid, name=name, kind="class", file=file_path,
                    line=line, end_line=node.end_point[0] + 1,
                    language="java", signature=f"class {name}",
                ))
                name_map[name] = sid
                # superclass
                superclass = node.child_by_field_name("superclass")
                if superclass:
                    base = _node_text(superclass, source)
                    if base in name_map:
                        edges.append(Edge(
                            src=sid, dst=name_map[base],
                            kind="inherits", file=file_path, line=line,
                            weight=EDGE_WEIGHTS["inherits"],
                        ))
                for child in node.children:
                    walk(child, "class")

        elif node.type == "method_declaration":
            name_node = node.child_by_field_name("name")
            if name_node:
                name = _node_text(name_node, source)
                line = node.start_point[0] + 1
                kind = "method" if parent_kind == "class" else "function"
                sig_end = source.find(b"\n", node.start_byte)
                sig = source[node.start_byte: sig_end if sig_end != -1 else node.start_byte + 120]
                symbols.append(Symbol(
                    id=_sid(file_path, line, kind), name=name, kind=kind,
                    file=file_path, line=line, end_line=node.end_point[0] + 1,
                    language="java",
                    signature=sig.decode("utf-8", errors="replace").strip()[:200],
                ))

        elif node.type == "import_declaration":
            line = node.start_point[0] + 1
            sig = _node_text(node, source)[:120]
            for child in node.children:
                if child.type == "scoped_identifier":
                    iname = _node_text(child, source)
                    symbols.append(Symbol(
                        id=_sid(file_path, line, "import"),
                        name=iname, kind="import",
                        file=file_path, line=line, end_line=line,
                        language="java", signature=sig,
                    ))
                    break
        else:
            for child in node.children:
                walk(child, parent_kind)

    walk(tree.root_node)
    return symbols, edges


# ── Public dispatch ───────────────────────────────────────────────────────────

def parse_file(
    file_path: str | Path,
    file_path_override: str | None = None,
) -> tuple[list[Symbol], list[Edge], str, str, int] | None:
    """
    Parse a source file with Tree-sitter.

    Returns (symbols, edges, language, checksum, loc) or None.
    """
    import hashlib

    path = Path(file_path)
    ext  = path.suffix.lower()
    lang = EXTENSION_TO_LANG.get(ext)
    if lang is None:
        return None

    try:
        raw = path.read_bytes()
    except OSError:
        return None

    checksum = hashlib.md5(raw).hexdigest()
    loc = raw.count(b"\n") + 1
    tag = file_path_override if file_path_override is not None else str(path)

    if lang == "python":
        syms, edges = _extract_python(raw, tag)
    elif lang in ("javascript",):
        syms, edges = _extract_js_ts(raw, tag, "javascript")
    elif lang == "typescript":
        syms, edges = _extract_js_ts(raw, tag, "typescript")
    elif lang == "tsx":
        syms, edges = _extract_js_ts(raw, tag, "tsx")
    elif lang == "go":
        syms, edges = _extract_go(raw, tag)
    elif lang == "rust":
        syms, edges = _extract_rust(raw, tag)
    elif lang == "java":
        syms, edges = _extract_java(raw, tag)
    else:
        syms, edges = [], []

    return syms, edges, lang, checksum, loc
