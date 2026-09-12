"""Layer 7b: the tokenizer.

Turns `SELECT name FROM people WHERE id = 42` into a flat list of tokens. This is
the only part of the SQL layer that looks at characters; everything after it works
on tokens, which is what keeps the parser readable.

Three details worth knowing, because each is a place SQL is not like most
languages:

* **Keywords are case-insensitive, and so are identifiers.** `SELECT`, `select`
  and `Select` are one keyword. Identifiers are folded to lower case unless they
  are quoted with `"`, which is how a table called `Order` can exist at all.
* **Strings use single quotes, and a quote inside one is doubled**: `'it''s'`.
  Double quotes mean an identifier, not a string -- the opposite of most
  languages.
* **A keyword can be an identifier** in a position where no keyword is expected.
  This tokenizer does not try to be clever about that; the parser decides, which
  is why `Token.text` keeps the original spelling.
"""

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

# Longest first: `<=` has to be tried before `<`, or `<= 3` tokenizes as `<`, `=`.
OPERATORS = ("<=", ">=", "<>", "!=", "=", "<", ">")
PUNCTUATION = ",();*.+-"  # `-` is here for negative literals; `--` is a comment


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
    value: object  # folded name, keyword, operator, or literal value
    text: str  # exactly as it appeared, for error messages
    position: int

    def __str__(self) -> str:
        return self.text or "end of statement"

    @property
    def is_end(self) -> bool:
        return self.type is TokenType.END


def tokenize(sql: str) -> list[Token]:
    """The whole statement as tokens, ending with a single `END` token."""
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
            # A quoted identifier keeps its case and may contain anything.
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
    """Read a quoted run starting at `index`, where a doubled quote is a literal one."""
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
