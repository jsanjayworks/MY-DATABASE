"""Layer 7d: the parser.

Recursive descent, one method per grammar rule, no tables and no generated code.
For a language this size that is the most readable thing there is: the grammar in
the docstring below and the methods underneath it are the same shape, so a change
to one is obvious in the other.

    statement  := create | drop | insert | select | delete | update
                | BEGIN | COMMIT | ROLLBACK
    create     := CREATE TABLE [IF NOT EXISTS] name '(' coldef {',' coldef} ')'
    coldef     := name type [NOT NULL] [PRIMARY KEY]
    insert     := INSERT INTO name ['(' name {',' name} ')'] VALUES tuple {',' tuple}
    select     := SELECT ('*' | name {',' name}) FROM name [WHERE expr]
                  [ORDER BY name [ASC|DESC] {',' ...}] [LIMIT n [OFFSET n]]
    delete     := DELETE FROM name [WHERE expr]
    update     := UPDATE name SET name '=' expr {',' ...} [WHERE expr]

    expr       := or_expr
    or_expr    := and_expr {OR and_expr}
    and_expr   := not_expr {AND not_expr}
    not_expr   := NOT not_expr | predicate
    predicate  := operand [ (= | != | <> | < | <= | > | >=) operand
                          | IS [NOT] NULL ]
    operand    := NUMBER | STRING | NULL | '-' NUMBER | name | '(' expr ')'

Precedence falls out of the nesting: `OR` binds loosest, then `AND`, then `NOT`,
then comparison. So `a = 1 AND b = 2 OR c = 3` parses as `(a=1 AND b=2) OR c=3`,
which is what SQL says it should.
"""

from __future__ import annotations

from pydb.record import Column, ColumnType
from pydb.sql import nodes
from pydb.sql.errors import ParseError
from pydb.sql.tokenizer import Token, TokenType, tokenize

COMPARISONS = frozenset(("=", "!=", "<>", "<", "<=", ">", ">="))
TYPE_KEYWORDS = {"INT": "INT", "INTEGER": "INT", "TEXT": "TEXT"}


def parse(sql: str) -> nodes.Statement:
    """Parse exactly one statement, with an optional trailing semicolon."""
    parser = Parser(sql)
    statement = parser.statement()
    parser.expect_end()
    return statement


def parse_script(sql: str) -> list[nodes.Statement]:
    """Parse a run of semicolon-separated statements."""
    parser = Parser(sql)
    statements = []
    while not parser.at_end:
        statements.append(parser.statement())
        while parser.take_punctuation(";"):
            pass
    return statements


class Parser:
    """A cursor over the token list, plus one method per grammar rule."""

    def __init__(self, sql: str) -> None:
        self.sql = sql
        self.tokens = tokenize(sql)
        self.index = 0

    # ------------------------------------------------------------------
    # cursor
    # ------------------------------------------------------------------

    @property
    def current(self) -> Token:
        return self.tokens[self.index]

    @property
    def at_end(self) -> bool:
        return self.current.is_end

    def advance(self) -> Token:
        token = self.current
        if not token.is_end:
            self.index += 1
        return token

    def take_keyword(self, *keywords: str) -> bool:
        """Consume the next token if it is one of `keywords`."""
        token = self.current
        if token.type is TokenType.KEYWORD and token.value in keywords:
            self.index += 1
            return True
        return False

    def take_punctuation(self, character: str) -> bool:
        token = self.current
        if token.type is TokenType.PUNCTUATION and token.value == character:
            self.index += 1
            return True
        return False

    def expect_keyword(self, *keywords: str) -> str:
        token = self.current
        if token.type is TokenType.KEYWORD and token.value in keywords:
            self.index += 1
            return str(token.value)
        self.fail(" or ".join(keywords))

    def expect_punctuation(self, character: str) -> None:
        if not self.take_punctuation(character):
            self.fail(repr(character))

    def expect_operator(self, operator: str) -> None:
        token = self.current
        if token.type is not TokenType.OPERATOR or token.value != operator:
            self.fail(repr(operator))
        self.index += 1

    def expect_name(self) -> str:
        """An identifier. A keyword here is a mistake worth naming precisely."""
        token = self.current
        if token.type is TokenType.IDENTIFIER:
            self.index += 1
            return str(token.value)
        if token.type is TokenType.KEYWORD:
            self.fail(
                f"a name (got the keyword {token.text!r}; quote it as "
                f'"{token.text}" to use it as a name)'
            )
        self.fail("a name")

    def expect_number(self) -> int:
        token = self.current
        if token.type is not TokenType.NUMBER:
            self.fail("a number")
        self.index += 1
        return int(token.value)  # type: ignore[arg-type]

    def expect_end(self) -> None:
        self.take_punctuation(";")
        if not self.at_end:
            self.fail("the end of the statement")

    def fail(self, expected: str) -> None:
        token = self.current
        raise ParseError(
            f"expected {expected} but found {token} at position {token.position}",
            token.position,
        )

    # ------------------------------------------------------------------
    # statements
    # ------------------------------------------------------------------

    def statement(self) -> nodes.Statement:
        token = self.current
        if token.type is not TokenType.KEYWORD:
            self.fail("a statement")
        handlers = {
            "CREATE": self.create_table,
            "DROP": self.drop_table,
            "INSERT": self.insert,
            "SELECT": self.select,
            "DELETE": self.delete,
            "UPDATE": self.update,
            "BEGIN": self.begin,
            "COMMIT": self.commit,
            "ROLLBACK": self.rollback,
        }
        handler = handlers.get(str(token.value))
        if handler is None:
            self.fail("a statement")
        return handler()

    def create_table(self) -> nodes.CreateTable:
        self.expect_keyword("CREATE")
        self.expect_keyword("TABLE")
        if_not_exists = False
        if self.take_keyword("IF"):
            self.expect_keyword("NOT")
            self.expect_keyword("EXISTS")
            if_not_exists = True
        name = self.expect_name()
        self.expect_punctuation("(")

        columns: list[Column] = []
        primary_key: str | None = None
        while True:
            column_name = self.expect_name()
            type_name = self.expect_keyword(*TYPE_KEYWORDS)
            nullable = True
            is_key = False
            while True:
                if self.take_keyword("NOT"):
                    self.expect_keyword("NULL")
                    nullable = False
                    continue
                if self.take_keyword("PRIMARY"):
                    self.expect_keyword("KEY")
                    is_key = True
                    nullable = False  # a key cannot be NULL, so say so up front
                    continue
                break
            columns.append(
                Column(column_name, ColumnType.parse(TYPE_KEYWORDS[type_name]), nullable)
            )
            if is_key:
                if primary_key is not None:
                    raise ParseError(
                        f"{name} already has a primary key ({primary_key}); "
                        f"composite keys are not supported"
                    )
                primary_key = column_name
            if not self.take_punctuation(","):
                break
        self.expect_punctuation(")")
        return nodes.CreateTable(name, columns, primary_key, if_not_exists)

    def drop_table(self) -> nodes.DropTable:
        self.expect_keyword("DROP")
        self.expect_keyword("TABLE")
        if_exists = False
        if self.take_keyword("IF"):
            self.expect_keyword("EXISTS")
            if_exists = True
        return nodes.DropTable(self.expect_name(), if_exists)

    def insert(self) -> nodes.Insert:
        self.expect_keyword("INSERT")
        self.expect_keyword("INTO")
        table = self.expect_name()
        columns: list[str] | None = None
        if self.take_punctuation("("):
            columns = [self.expect_name()]
            while self.take_punctuation(","):
                columns.append(self.expect_name())
            self.expect_punctuation(")")
        self.expect_keyword("VALUES")
        rows = [self.value_tuple()]
        while self.take_punctuation(","):
            rows.append(self.value_tuple())
        return nodes.Insert(table, columns, rows)

    def value_tuple(self) -> list[nodes.Expression]:
        self.expect_punctuation("(")
        values = [self.expression()]
        while self.take_punctuation(","):
            values.append(self.expression())
        self.expect_punctuation(")")
        return values

    def select(self) -> nodes.Select:
        self.expect_keyword("SELECT")
        columns: list[str] | None = None
        if not self.take_punctuation("*"):
            columns = [self.expect_name()]
            while self.take_punctuation(","):
                columns.append(self.expect_name())
        self.expect_keyword("FROM")
        table = self.expect_name()

        where = self.expression() if self.take_keyword("WHERE") else None
        order_by: list[nodes.OrderBy] = []
        if self.take_keyword("ORDER"):
            self.expect_keyword("BY")
            while True:
                column = self.expect_name()
                descending = False
                if self.take_keyword("DESC"):
                    descending = True
                else:
                    self.take_keyword("ASC")
                order_by.append(nodes.OrderBy(column, descending))
                if not self.take_punctuation(","):
                    break
        limit = offset = None
        if self.take_keyword("LIMIT"):
            limit = self.expect_number()
        if self.take_keyword("OFFSET"):
            offset = self.expect_number()
        return nodes.Select(table, columns, where, order_by, limit, offset)

    def delete(self) -> nodes.Delete:
        self.expect_keyword("DELETE")
        self.expect_keyword("FROM")
        table = self.expect_name()
        where = self.expression() if self.take_keyword("WHERE") else None
        return nodes.Delete(table, where)

    def update(self) -> nodes.Update:
        self.expect_keyword("UPDATE")
        table = self.expect_name()
        self.expect_keyword("SET")
        assignments = []
        while True:
            column = self.expect_name()
            self.expect_operator("=")
            assignments.append((column, self.expression()))
            if not self.take_punctuation(","):
                break
        where = self.expression() if self.take_keyword("WHERE") else None
        return nodes.Update(table, assignments, where)

    def begin(self) -> nodes.Begin:
        self.expect_keyword("BEGIN")
        self.take_keyword("TRANSACTION")
        return nodes.Begin()

    def commit(self) -> nodes.Commit:
        self.expect_keyword("COMMIT")
        self.take_keyword("TRANSACTION")
        return nodes.Commit()

    def rollback(self) -> nodes.Rollback:
        self.expect_keyword("ROLLBACK")
        self.take_keyword("TRANSACTION")
        return nodes.Rollback()

    # ------------------------------------------------------------------
    # expressions
    # ------------------------------------------------------------------

    def expression(self) -> nodes.Expression:
        return self.or_expression()

    def or_expression(self) -> nodes.Expression:
        left = self.and_expression()
        while self.take_keyword("OR"):
            left = nodes.Or(left, self.and_expression())
        return left

    def and_expression(self) -> nodes.Expression:
        left = self.not_expression()
        while self.take_keyword("AND"):
            left = nodes.And(left, self.not_expression())
        return left

    def not_expression(self) -> nodes.Expression:
        if self.take_keyword("NOT"):
            return nodes.Not(self.not_expression())
        return self.predicate()

    def predicate(self) -> nodes.Expression:
        left = self.operand()
        token = self.current
        if token.type is TokenType.OPERATOR and token.value in COMPARISONS:
            self.index += 1
            operator = "!=" if token.value == "<>" else str(token.value)
            return nodes.Compare(operator, left, self.operand())
        if self.take_keyword("IS"):
            negated = self.take_keyword("NOT")
            self.expect_keyword("NULL")
            return nodes.IsNull(left, negated)
        return left

    def operand(self) -> nodes.Expression:
        token = self.current
        if token.type is TokenType.NUMBER:
            self.index += 1
            return nodes.Literal(token.value)
        if token.type is TokenType.STRING:
            self.index += 1
            return nodes.Literal(token.value)
        if token.type is TokenType.IDENTIFIER:
            self.index += 1
            return nodes.ColumnRef(str(token.value))
        if token.type is TokenType.KEYWORD and token.value == "NULL":
            self.index += 1
            return nodes.Literal(None)
        if self.take_punctuation("("):
            inner = self.expression()
            self.expect_punctuation(")")
            return inner
        if self.take_punctuation("-"):
            return nodes.Literal(-self.expect_number())
        if self.take_punctuation("+"):
            return nodes.Literal(self.expect_number())
        self.fail("a value or a column name")
