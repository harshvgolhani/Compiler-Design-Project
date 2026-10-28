"""Pretty printers for tokens and ASTs (console tree, Graphviz DOT)."""
from __future__ import annotations

from .ast_nodes import Node
from .tokens import Token


def token_table(tokens: list[Token]) -> str:
    rows = [("#", "TYPE", "LEXEME", "POS")]
    for i, t in enumerate(tokens):
        rows.append((str(i), t.type.value, t.value if t.type.value != "EOF" else "<EOF>",
                     f"{t.pos}-{t.end}"))
    w = [max(len(r[c]) for r in rows) for c in range(4)]
    lines = []
    for k, r in enumerate(rows):
        lines.append("  ".join(r[c].ljust(w[c]) for c in range(4)))
        if k == 0:
            lines.append("  ".join("-" * w[c] for c in range(4)))
    return "\n".join(lines)


def ast_tree(node: Node, ascii_only: bool = False) -> str:
    """Render an AST as an indented tree:

        BinOp *
        ├── Call SUM/1
        │   └── Range B2:B9  [8×1]
        └── Name TaxRate
    """
    tee, last, pipe, blank = ("|-- ", "`-- ", "|   ", "    ") if ascii_only else \
        ("├── ", "└── ", "│   ", "    ")
    lines = [node.label()]

    def rec(n: Node, prefix: str) -> None:
        kids = n.children()
        for idx, k in enumerate(kids):
            is_last = idx == len(kids) - 1
            lines.append(prefix + (last if is_last else tee) + k.label())
            rec(k, prefix + (blank if is_last else pipe))

    rec(node, "")
    return "\n".join(lines)


def ast_dot(node: Node, title: str = "AST") -> str:
    """Graphviz DOT source for the AST (paste into https://dreampuf.github.io/GraphvizOnline)."""
    out = [f'digraph "{title}" {{', '  node [shape=box, style="rounded,filled", '
           'fillcolor="#eef2ff", fontname="Helvetica"];']
    counter = [0]

    def rec(n: Node) -> str:
        nid = f"n{counter[0]}"
        counter[0] += 1
        label = n.label().replace('"', '\\"')
        out.append(f'  {nid} [label="{label}"];')
        for k in n.children():
            out.append(f"  {nid} -> {rec(k)};")
        return nid

    rec(node)
    out.append("}")
    return "\n".join(out)


def ast_stats(node: Node) -> dict:
    nodes = list(node.walk())

    def depth(n: Node) -> int:
        kids = n.children()
        return 1 + (max(depth(k) for k in kids) if kids else 0)

    kinds: dict[str, int] = {}
    for n in nodes:
        kinds[n.kind] = kinds.get(n.kind, 0) + 1
    return {"nodes": len(nodes), "depth": depth(node), "kinds": kinds}
