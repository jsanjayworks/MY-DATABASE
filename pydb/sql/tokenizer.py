from __future__ import annotations

from dataclasses import dataclass
from enum import Enum, auto

from pydb.sql.errors import ParseError

KEYWORDS = frozenset(
    """
    AND AS ASC BEGIN BY COMMIT CREATE CROSS DELETE DESC DISTINCT DROP EXISTS
    EXPLAIN FROM GROUP HAVING IF INDEX INNER INSERT INT INTEGER INTO IS JOIN KEY
    LEFT LIMIT NOT NULL OFFSET ON OR ORDER OUTER PRIMARY ROLLBACK SELECT SET
    TABLE TEXT TRANSACTION UNIQUE UPDATE VACUUM VALUES WHERE
    """.split()
)

OPERATORS = ("<=", ">=", "<>", "!=", "=", "<", ">")
PUNCTUATION = ",();*.+-"


class TokenType(Enum):
    KEYWORD = auto()
    IDENTIFIER = auto()
    NUMBER = auto()
    STRING = auto()
    OPERATOR = auto()
    PUNCTUATION = auto()
    END = auto()


@dataclass(frozen=True)
class Token:
    type: TokenType
    value: object
    text: str
    position: int

    def __str__(self) -> str:
        return self.text or "end of statement"

    @property
    def is_end(self) -> bool:
        return self.type is TokenType.END


def tokenize(sql: str) -> list[Token]:
    tokens: list[Token] = []
    index = 0
    length = len(sql)
    while index < length:
        char = sql[index]

        if char.isspace():
            index += 1
            continue

        if char == "-" and sql.startswith("--", index):
            newline = sql.find("\n", index)
            index = length if newline < 0 else newline + 1
            continue

        start = index

        if char == "'":
            value, index = _read_quoted(sql, index, "'")
            tokens.append(Token(TokenType.STRING, value, sql[start:index], start))
            continue

        if char == '"':
            value, index = _read_quoted(sql, index, '"')
            tokens.append(Token(TokenType.IDENTIFIER, value, sql[start:index], start))
            continue

        if char.isdigit():
            while index < length and sql[index].isdigit():
                index += 1
            if index < length and (sql[index] == "." or sql[index].isalpha()):
                raise ParseError(
                    f"only whole numbers are supported, got {sql[start:index + 1]!r} "
                    f"at position {start}",
                    start,
                )
            text = sql[start:index]
            tokens.append(Token(TokenType.NUMBER, int(text), text, start))
            continue

        if char.isalpha() or char == "_":
            while index < length and (sql[index].isalnum() or sql[index] == "_"):
                index += 1
            text = sql[start:index]
            upper = text.upper()
            if upper in KEYWORDS:
                tokens.append(Token(TokenType.KEYWORD, upper, text, start))
            else:
                tokens.append(Token(TokenType.IDENTIFIER, text.lower(), text, start))
            continue

        for operator in OPERATORS:
            if sql.startswith(operator, index):
                index += len(operator)
                tokens.append(
                    Token(TokenType.OPERATOR, operator, sql[start:index], start)
                )
                break
        else:
            if char in PUNCTUATION:
                index += 1
                tokens.append(Token(TokenType.PUNCTUATION, char, char, start))
                continue
            raise ParseError(f"unexpected character {char!r} at position {start}", start)

    tokens.append(Token(TokenType.END, None, "", length))
    return tokens


def _read_quoted(sql: str, index: int, quote: str) -> tuple[str, int]:
    start = index
    index += 1
    pieces: list[str] = []
    while True:
        if index >= len(sql):
            raise ParseError(
                f"unterminated {quote}quoted{quote} value starting at position {start}",
                start,
            )
        char = sql[index]
        if char == quote:
            if sql.startswith(quote * 2, index):
                pieces.append(quote)
                index += 2
                continue
            return "".join(pieces), index + 1
        pieces.append(char)
        index += 1
