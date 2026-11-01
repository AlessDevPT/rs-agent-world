"""Tokenising and parsing the expression language.

The language exists because `condition` edges and `input_mapping` need to say
things like "run this step only if the research found more than three
competitors". Choosing how to express that is a security decision, and the
choice here is deliberately joyless:

**Not Python `eval`. Not a sandbox. Not a template engine with attribute
access.** A sandbox in a multi-tenant product is a vulnerability with a
release schedule - not a question of whether an escape exists, only of when
someone finds it. So there is no sandbox to escape: the grammar below has no
attribute access, no function call beyond the two named here, no loops, no
assignment, and no way to name a Python object at all.

The grammar, in full:

    expression  := or_expr
    or_expr     := and_expr ( "or" and_expr )*
    and_expr    := not_expr ( "and" not_expr )*
    not_expr    := "not" not_expr | comparison
    comparison  := primary ( ( "==" | "!=" | "<" | "<=" | ">" | ">=" | "in" ) primary )?
    primary     := literal | reference | call | "(" expression ")"
    call        := ( "exists" | "length" ) "(" expression ")"
    reference   := "$" ( "." name | "[" integer "]" )*

Comparison does not chain: `a < b < c` is a parse error rather than something
whose meaning a reader has to guess.

Three bounds are enforced here rather than left to the runtime, because "no
recursion limit to blow" is a promise this module keeps and not one Python
makes: source length, token count and nesting depth. An expression is written
once by a person and evaluated on every edge of every execution, so there is
no legitimate use for a deep one.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Final

from agentworld.domain.errors import ValidationFailedError

#: Bounds. Generous for anything a person would write, small enough that a
#: generated or malicious expression hits them long before anything else.
MAX_SOURCE_LENGTH: Final = 2_000
MAX_TOKENS: Final = 400
MAX_DEPTH: Final = 24

#: The only callable names. Not a registry a later phase extends without this
#: list and its tests changing: an expression language that can call an
#: arbitrary function is a sandbox.
FUNCTIONS: Final = frozenset({"exists", "length"})

KEYWORDS: Final = frozenset({"and", "or", "not", "in", "true", "false", "null"})

#: Longest first, so `<=` is never read as `<` followed by `=`.
COMPARISONS: Final = ("==", "!=", "<=", ">=", "<", ">")

_NAME = re.compile(r"[A-Za-z_][A-Za-z0-9_-]*")
_NUMBER = re.compile(r"-?\d+(\.\d+)?")
_WHITESPACE = re.compile(r"\s+")


# ----------------------------------------------------------------- the AST ---


@dataclass(frozen=True, slots=True)
class Constant:
    """A literal. Named `Constant` so the node does not shadow `typing.Literal`."""

    value: str | float | bool | None


@dataclass(frozen=True, slots=True)
class Reference:
    """A path into the scope the orchestrator supplied, and nothing else.

    Each segment is a dictionary key or a list index. There is no segment
    kind that reaches a Python attribute, which makes "cannot reach anything
    the orchestrator did not put in scope" a property of the data structure
    rather than a check someone has to remember to write.
    """

    path: tuple[str | int, ...]


@dataclass(frozen=True, slots=True)
class Call:
    name: str
    argument: Node


@dataclass(frozen=True, slots=True)
class Not:
    operand: Node


@dataclass(frozen=True, slots=True)
class BinaryOp:
    operator: str
    left: Node
    right: Node


Node = Constant | Reference | Call | Not | BinaryOp


# ------------------------------------------------------------ the tokeniser ---


@dataclass(frozen=True, slots=True)
class Token:
    kind: str
    value: str | float | bool | None = None
    position: int = 0
    #: Only a reference has one.
    path: tuple[str | int, ...] = ()


def tokenise(source: str) -> list[Token]:
    if len(source) > MAX_SOURCE_LENGTH:
        raise invalid(
            f"an expression is at most {MAX_SOURCE_LENGTH} characters", length=len(source)
        )

    tokens: list[Token] = []
    index = 0
    while index < len(source):
        if match := _WHITESPACE.match(source, index):
            index = match.end()
            continue
        if len(tokens) >= MAX_TOKENS:
            raise invalid(f"an expression is at most {MAX_TOKENS} tokens", position=index)

        char = source[index]

        if char == "$":
            path, index = _reference_path(source, index)
            tokens.append(Token("reference", position=index, path=path))
            continue
        if char in "'\"":
            token, index = _string(source, index)
            tokens.append(token)
            continue
        if char in "(),":
            tokens.append(Token("punct", char, index))
            index += 1
            continue

        operator = next((op for op in COMPARISONS if source.startswith(op, index)), None)
        if operator is not None:
            tokens.append(Token("comparison", operator, index))
            index += len(operator)
            continue

        if match := _NUMBER.match(source, index):
            text = match.group()
            tokens.append(Token("number", float(text), index))
            index = match.end()
            continue

        if match := _NAME.match(source, index):
            word = match.group()
            if word in KEYWORDS:
                tokens.append(Token("keyword", word, index))
            elif word in FUNCTIONS:
                tokens.append(Token("function", word, index))
            else:
                # A bare name is the shape of every attempted escape -
                # `open`, `__class__`, `self`. There is no identifier
                # namespace at all, so saying so plainly is both the truth
                # and the most useful hint.
                raise invalid(
                    f"'{word}' is not something this expression language knows; values "
                    f"come from `$`, and the only functions are {', '.join(sorted(FUNCTIONS))}",
                    position=index,
                    name=word,
                )
            index = match.end()
            continue

        raise invalid(f"unexpected character {char!r}", position=index)

    return tokens


def _reference_path(source: str, index: int) -> tuple[tuple[str | int, ...], int]:
    """`$`, then dotted names and integer indexes. Nothing else is a segment."""
    index += 1
    path: list[str | int] = []

    while index < len(source):
        if source[index] == "." and (match := _NAME.match(source, index + 1)):
            path.append(match.group())
            index = match.end()
        elif source[index] == ".":
            raise invalid("a `.` must be followed by a name", position=index)
        elif source[index] == "[":
            closing = source.find("]", index)
            inner = source[index + 1 : closing] if closing != -1 else ""
            if closing == -1 or not inner.lstrip("-").isdigit():
                raise invalid("an index is an integer, as in `[0]`", position=index)
            path.append(int(inner))
            index = closing + 1
        else:
            break

    return tuple(path), index


def _string(source: str, index: int) -> tuple[Token, int]:
    quote = source[index]
    closing = source.find(quote, index + 1)
    if closing == -1:
        raise invalid("unterminated string", position=index)
    text = source[index + 1 : closing]
    # No escape sequences, deliberately: they are the part of string lexing
    # that grows, and a workflow condition has never needed one.
    if "\\" in text:
        raise invalid("escape sequences are not supported in expressions", position=index)
    return Token("string", text, index), closing + 1


# --------------------------------------------------------------- the parser ---


class _Parser:
    """Recursive descent, with the depth counted rather than hoped about."""

    def __init__(self, tokens: list[Token]) -> None:
        self._tokens = tokens
        self._at = 0
        self._depth = 0

    def parse(self) -> Node:
        node = self._or()
        if self._at < len(self._tokens):
            token = self._tokens[self._at]
            raise invalid(
                f"unexpected {token.value!r} after a complete expression",
                position=token.position,
            )
        return node

    # Each level enters and leaves the depth counter, so the bound covers
    # nesting through parentheses, `not`, and function arguments alike.
    def _deeper(self) -> None:
        self._depth += 1
        if self._depth > MAX_DEPTH:
            raise invalid(f"an expression nests at most {MAX_DEPTH} deep", depth=self._depth)

    def _or(self) -> Node:
        self._deeper()
        node = self._and()
        while self._keyword("or"):
            node = BinaryOp("or", node, self._and())
        self._depth -= 1
        return node

    def _and(self) -> Node:
        self._deeper()
        node = self._not()
        while self._keyword("and"):
            node = BinaryOp("and", node, self._not())
        self._depth -= 1
        return node

    def _not(self) -> Node:
        if self._keyword("not"):
            self._deeper()
            node = Not(self._not())
            self._depth -= 1
            return node
        return self._comparison()

    def _comparison(self) -> Node:
        self._deeper()
        left = self._primary()
        token = self._peek()
        if token is not None and (
            token.kind == "comparison" or (token.kind == "keyword" and token.value == "in")
        ):
            self._at += 1
            right = self._primary()
            # Non-associative on purpose: `a < b < c` would otherwise mean
            # whatever the reader assumed, and the two plausible readings
            # differ.
            after = self._peek()
            if after is not None and (
                after.kind == "comparison" or (after.kind == "keyword" and after.value == "in")
            ):
                raise invalid("comparisons do not chain; use `and`", position=after.position)
            self._depth -= 1
            return BinaryOp(str(token.value), left, right)
        self._depth -= 1
        return left

    def _primary(self) -> Node:
        token = self._peek()
        if token is None:
            raise invalid("the expression ends before it is complete")

        if token.kind == "reference":
            self._at += 1
            return Reference(token.path)
        if token.kind in ("string", "number"):
            self._at += 1
            return Constant(token.value)
        if token.kind == "keyword" and token.value in ("true", "false", "null"):
            self._at += 1
            return Constant({"true": True, "false": False, "null": None}[str(token.value)])
        if token.kind == "function":
            return self._call()
        if token.kind == "punct" and token.value == "(":
            self._at += 1
            self._deeper()
            node = self._or()
            self._expect(")")
            self._depth -= 1
            return node

        raise invalid(f"unexpected {token.value!r}", position=token.position)

    def _call(self) -> Node:
        token = self._tokens[self._at]
        self._at += 1
        self._expect("(")
        self._deeper()
        argument = self._or()
        self._depth -= 1
        self._expect(")")
        return Call(str(token.value), argument)

    # ------------------------------------------------------------ cursor ---

    def _peek(self) -> Token | None:
        return self._tokens[self._at] if self._at < len(self._tokens) else None

    def _keyword(self, word: str) -> bool:
        token = self._peek()
        if token is not None and token.kind == "keyword" and token.value == word:
            self._at += 1
            return True
        return False

    def _expect(self, punct: str) -> None:
        token = self._peek()
        if token is None or token.kind != "punct" or token.value != punct:
            raise invalid(
                f"expected {punct!r}",
                position=token.position if token else len(self._tokens),
            )
        self._at += 1


def parse(source: str) -> Node:
    """Parse `source`, or raise `ValidationFailedError` saying where it broke.

    Separate from evaluation so a workflow's expressions can be checked when
    it is published rather than when it runs - a condition that cannot parse
    should fail validation, not strand an execution at three in the morning.
    """
    tokens = tokenise(source)
    if not tokens:
        raise invalid("an expression cannot be empty")
    return _Parser(tokens).parse()


def invalid(detail: str, **context: object) -> ValidationFailedError:
    return ValidationFailedError(detail, field="expression", **context)
