"""A diff that knows what a configuration is: a tree of sections, not a list of lines.

`git diff` on two configurations tells you that line 212 changed; this says that under `interface GigabitEthernet0/1`
the description went from `uplink` to `uplink-core`. The comparison is done section by section, in order (the order of
the entries in an access list matters, so a moved entry is reported as a change), and a section that exists on one side
only is reported line by line, with its parents as the path.
"""

from __future__ import annotations

import difflib
from dataclasses import dataclass, field
from typing import Literal


@dataclass
class Node:
    text: str
    children: list[Node] = field(default_factory=list)


@dataclass(frozen=True)
class Change:
    kind: Literal["added", "removed"]
    path: tuple[str, ...]  # the section headers above the line, outermost first
    line: str

    def as_dict(self) -> dict[str, object]:
        return {"kind": self.kind, "path": list(self.path), "line": self.line}


def parse(text: str, style: str) -> list[Node]:
    if style == "indent":
        return _parse_indent(text)
    if style == "braces":
        return _parse_braces(text)
    if style == "sections":
        return _parse_sections(text)
    raise ValueError(f"unknown style {style!r}")


def _parse_indent(text: str) -> list[Node]:
    roots: list[Node] = []
    stack: list[tuple[int, Node]] = []
    for raw in text.splitlines():
        line = raw.expandtabs(8)
        stripped = line.strip()
        if not stripped:
            continue
        indent = len(line) - len(line.lstrip(" "))
        node = Node(stripped)
        while stack and stack[-1][0] >= indent:
            stack.pop()
        (stack[-1][1].children if stack else roots).append(node)
        stack.append((indent, node))
    return roots


def _parse_braces(text: str) -> list[Node]:
    roots: list[Node] = []
    stack: list[Node] = []
    for raw in text.splitlines():
        line = raw.strip()
        if not line:
            continue
        if line.startswith("}"):
            if stack:
                stack.pop()
            line = line[1:].strip()
            if not line:
                continue
        opens = line.endswith("{")
        node = Node(line[:-1].rstrip() if opens else line)
        (stack[-1].children if stack else roots).append(node)
        if opens:
            stack.append(node)
    return roots


def _parse_sections(text: str) -> list[Node]:
    roots: list[Node] = []
    current: Node | None = None
    pending = ""
    for raw in text.splitlines():
        line = raw.strip()
        if not line:
            continue
        if pending:
            line = pending + line
            pending = ""
        if line.endswith("\\"):  # RouterOS wraps long commands
            pending = line[:-1].rstrip() + " "
            continue
        if line.startswith("/"):
            current = Node(line)
            roots.append(current)
        elif current is None:
            roots.append(Node(line))
        else:
            current.children.append(Node(line))
    if pending:
        (current.children if current else roots).append(Node(pending.strip()))
    return roots


def diff_nodes(a: list[Node], b: list[Node], path: tuple[str, ...] = ()) -> list[Change]:
    out: list[Change] = []
    sm = difflib.SequenceMatcher(None, [n.text for n in a], [n.text for n in b], autojunk=False)
    for op, i1, i2, j1, j2 in sm.get_opcodes():
        if op == "equal":
            for k in range(i2 - i1):
                if a[i1 + k].children or b[j1 + k].children:
                    out += diff_nodes(a[i1 + k].children, b[j1 + k].children, (*path, a[i1 + k].text))
            continue
        for n in a[i1:i2]:
            _emit(out, "removed", n, path)
        for n in b[j1:j2]:
            _emit(out, "added", n, path)
    return out


def _emit(out: list[Change], kind: Literal["added", "removed"], node: Node, path: tuple[str, ...]) -> None:
    out.append(Change(kind, path, node.text))
    for c in node.children:
        _emit(out, kind, c, (*path, node.text))


def diff_text(old: str, new: str, style: str) -> list[Change]:
    if old == new:
        return []
    return diff_nodes(parse(old, style), parse(new, style))


@dataclass(frozen=True)
class Summary:
    added: int
    removed: int
    sections: tuple[str, ...]  # the top-level section of every change, once, in order


def summarize(changes: list[Change]) -> Summary:
    sections: dict[str, None] = {}
    for c in changes:
        sections[c.path[0] if c.path else c.line] = None
    return Summary(
        sum(1 for c in changes if c.kind == "added"), sum(1 for c in changes if c.kind == "removed"), tuple(sections)
    )


def format_changes(changes: list[Change], limit: int | None = None) -> str:
    """Unified-diff-like text: `+` added, `-` removed, a blank marker for the section headers that give context."""
    lines: list[str] = []
    printed: set[tuple[str, ...]] = set()
    for n, c in enumerate(changes):
        if limit is not None and len(lines) >= limit:
            lines.append(f"  ... {len(changes) - n} more changes")
            break
        for i in range(len(c.path)):
            prefix = c.path[: i + 1]
            if prefix not in printed:
                printed.add(prefix)
                lines.append(f"  {'  ' * i}{c.path[i]}")
        printed.add((*c.path, c.line))
        lines.append(f"{'+' if c.kind == 'added' else '-'} {'  ' * len(c.path)}{c.line}")
    return "\n".join(lines)
