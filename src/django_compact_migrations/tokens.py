from __future__ import annotations

import ast
from dataclasses import dataclass

from tokenize_rt import NON_CODING_TOKENS, Token  # type: ignore[import-untyped]

OPENERS = frozenset("([{")
CLOSERS = frozenset(")]}")


@dataclass(frozen=True)
class Segment:
    start: int
    end: int
    """Index of the segment's last code token (inclusive)."""
    comma: int | None


@dataclass(frozen=True)
class Container:
    kind: str
    """One of "list", "tuple", "set", "dict"."""
    opener: int
    closer: int
    segments: tuple[Segment, ...]


def _is_op(token: Token, chars: frozenset[str]) -> bool:
    return token.name == "OP" and token.src in chars


def matching_closer(tokens: list[Token], opener: int) -> int:
    """The bracket closing ``opener``: parsed source always has one."""
    depth = 0
    i = opener
    while True:
        tok = tokens[i]
        if _is_op(tok, OPENERS):
            depth += 1
        elif _is_op(tok, CLOSERS):
            depth -= 1
            if depth == 0:
                return i
        i += 1


def split_segments(tokens: list[Token], opener: int, closer: int) -> list[Segment]:
    segments: list[Segment] = []
    depth = 0
    start: int | None = None
    last_code = opener
    for i in range(opener + 1, closer):
        tok = tokens[i]
        if tok.name in NON_CODING_TOKENS:
            continue
        if depth == 0 and tok.matches(name="OP", src=","):
            assert start is not None
            segments.append(Segment(start, last_code, i))
            start = None
            continue
        if start is None:
            start = i
        if _is_op(tok, OPENERS):
            depth += 1
        elif _is_op(tok, CLOSERS):
            depth -= 1
        last_code = i
    if start is not None:
        segments.append(Segment(start, last_code, None))
    return segments


_KINDS: dict[type[ast.expr], tuple[str, str]] = {
    ast.List: ("list", "["),
    ast.Tuple: ("tuple", "("),
    ast.Set: ("set", "{"),
    ast.Dict: ("dict", "{"),
}


def container_for_node(
    tokens: list[Token], index: dict[tuple[int, int], int], node: ast.expr
) -> Container | None:
    kind_open = _KINDS.get(type(node))
    if kind_open is None:
        return None
    kind, open_char = kind_open
    opener = index.get((node.lineno, node.col_offset))
    if opener is None or not tokens[opener].matches(name="OP", src=open_char):
        return None
    closer = matching_closer(tokens, opener)
    end = tokens[closer]
    if (end.line, (end.utf8_byte_offset or 0) + 1) != (
        node.end_lineno,
        node.end_col_offset,
    ):
        return None
    segments = split_segments(tokens, opener, closer)
    if isinstance(node, ast.Dict):
        count = len(node.keys)
        if any(k is None for k in node.keys):
            return None
    else:
        assert isinstance(node, (ast.List, ast.Tuple, ast.Set))
        count = len(node.elts)
    if len(segments) != count:
        return None
    return Container(kind, opener, closer, tuple(segments))
