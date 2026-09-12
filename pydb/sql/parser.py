"""Layer 7d: the parser.

Recursive descent, one method per grammar rule, no tables and no generated code.
For a language this size that is the most readable thing there is: the grammar
below and the methods underneath it are the same shape, so a change to one is
obvious in the other.

    statement  := create_table | create_index | drop_table | drop_index
                | insert | select | delete | update | vacuum | explain
                | BEGIN | COMMIT | ROLLBACK
    create_table := CREATE TABLE [IF NOT EXISTS] name '(' coldef {',' coldef} ')'
    coldef     := name type [NOT NULL] [PRIMARY KEY]
    create_index := CREATE [UNIQUE] INDEX [IF NOT EXISTS] name ON table '(' col ')'
    insert     := INSERT INTO name ['(' name {',' name} ')'] VALUES tuple {',' tuple}
    select     := SELECT [DISTINCT] item {',' item} FROM source
                  [WHERE expr] [GROUP BY expr {',' expr}] [HAVING expr]
                  [ORDER BY sort {',' sort}] [LIMIT n [OFFSET n]]
    item       := '*' | qualifier '.' '*' | expr [[AS] alias]
    source     := table_ref {[INNER | LEFT [OUTER] | CROSS] JOIN table_ref [ON expr]}
    table_ref  := name [[AS] alias]
    sort       := (expr | NUMBER) [ASC | DESC]
    delete     := DELETE FROM name [WHERE expr]
    update     := UPDATE name SET name '=' expr {',' ...} [WHERE expr]

    expr       := or_expr
    or_expr    := and_expr {OR and_expr}
    and_expr   := not_expr {AND not_expr}
    not_expr   := NOT not_expr | predicate
    predicate  := operand [ (= | != | <> | < | <= | > | >=) operand
                          | IS [NOT] NULL ]
    operand    := NUMBER | STRING | NULL | ['-' | '+'] NUMBER | column
                | aggregate '(' ('*' | [DISTINCT] expr) ')' | '(' expr ')'
    column     := name ['.' name]

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

# Keywords that cannot be an alias, because seeing one means the FROM clause or
# the select list has ended.
NOT_AN_ALIAS = frozenset(
    """
    FROM WHERE GROUP HAVING ORDER LIMIT OFFSET JOIN INNER LEFT OUTER CROSS ON
    AND OR NOT IS NULL SET VALUES
    """.split()
)


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

    def peek(self, ahead: int = 1) -> Token:
        return self.tokens[min(self.index + ahead, len(self.tokens) - 1)]

    def advance(self) -> Token:
        token = self.current
        if not token.is_end:
            self.index += 1
        return token

    def take_keyword(self, *keywords: str) -> bool:
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

    def at_keyword(self, *keywords: str) -> bool:
        token = self.current
        return token.type is TokenType.KEYWORD and token.value in keywords

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
            "CREATE": self.create,
            "DROP": self.drop,
            "INSERT": self.insert,
            "SELECT": self.select,
            "DELETE": self.delete,
            "UPDATE": self.update,
            "VACUUM": self.vacuum,
            "EXPLAIN": self.explain,
            "BEGIN": self.begin,
            "COMMIT": self.commit,
            "ROLLBACK": self.rollback,
        }
        handler = handlers.get(str(token.value))
        if handler is None:
            self.fail("a statement")
        return handler()

    def create(self) -> nodes.Statement:
        self.expect_keyword("CREATE")
        if self.at_keyword("TABLE"):
            return self.create_table()
        return self.create_index()

    def drop(self) -> nodes.Statement:
        self.expect_keyword("DROP")
        if self.at_keyword("INDEX"):
            self.expect_keyword("INDEX")
            if_exists = False
            if self.take_keyword("IF"):
                self.expect_keyword("EXISTS")
                if_exists = True
            return nodes.DropIndex(self.expect_name(), if_exists)
        self.expect_keyword("TABLE")
        if_exists = False
        if self.take_keyword("IF"):
            self.expect_keyword("EXISTS")
            if_exists = True
        return nodes.DropTable(self.expect_name(), if_exists)

    def create_table(self) -> nodes.CreateTable:
        self.expect_keyword("TABLE")
        if_not_exists = self.if_not_exists()
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

    def create_index(self) -> nodes.CreateIndex:
        unique = self.take_keyword("UNIQUE")
        self.expect_keyword("INDEX")
        if_not_exists = self.if_not_exists()
        name = self.expect_name()
        self.expect_keyword("ON")
        table = self.expect_name()
        self.expect_punctuation("(")
        column = self.expect_name()
        if self.take_punctuation(","):
            raise ParseError("an index covers one column; composite indexes are not supported")
        self.expect_punctuation(")")
        return nodes.CreateIndex(name, table, column, unique, if_not_exists)

    def if_not_exists(self) -> bool:
        if self.take_keyword("IF"):
            self.expect_keyword("NOT")
            self.expect_keyword("EXISTS")
            return True
        return False

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
        distinct = self.take_keyword("DISTINCT")
        items = [self.select_item()]
        while self.take_punctuation(","):
            items.append(self.select_item())
        self.expect_keyword("FROM")
        source = self.source()

        where = self.expression() if self.take_keyword("WHERE") else None

        group_by: list[nodes.Expression] = []
        if self.take_keyword("GROUP"):
            self.expect_keyword("BY")
            group_by.append(self.expression())
            while self.take_punctuation(","):
                group_by.append(self.expression())
        having = self.expression() if self.take_keyword("HAVING") else None

        order_by: list[nodes.OrderBy] = []
        if self.take_keyword("ORDER"):
            self.expect_keyword("BY")
            while True:
                order_by.append(self.sort_key())
                if not self.take_punctuation(","):
                    break

        limit = offset = None
        if self.take_keyword("LIMIT"):
            limit = self.expect_number()
        if self.take_keyword("OFFSET"):
            offset = self.expect_number()
        return nodes.Select(
            source, items, where, group_by, having, order_by, limit, offset, distinct
        )

    def select_item(self) -> nodes.SelectItem:
        if self.take_punctuation("*"):
            return nodes.SelectItem(nodes.Star())
        # `people.*` -- an identifier, a dot and a star.
        if (
            self.current.type is TokenType.IDENTIFIER
            and self.peek().value == "."
            and self.peek(2).value == "*"
        ):
            qualifier = self.expect_name()
            self.expect_punctuation(".")
            self.expect_punctuation("*")
            return nodes.SelectItem(nodes.Star(qualifier))
        value = self.expression()
        return nodes.SelectItem(value, self.alias())

    def alias(self) -> str | None:
        """An optional `AS name`, or a bare name where one cannot be anything else."""
        if self.take_keyword("AS"):
            return self.expect_name()
        if self.current.type is TokenType.IDENTIFIER:
            return self.expect_name()
        return None

    def source(self) -> nodes.TableRef | nodes.Join:
        left: nodes.TableRef | nodes.Join = self.table_ref()
        while True:
            kind = None
            if self.take_punctuation(","):
                kind = "CROSS"  # the comma form of a cross join
            elif self.take_keyword("CROSS"):
                self.expect_keyword("JOIN")
                kind = "CROSS"
            elif self.take_keyword("INNER"):
                self.expect_keyword("JOIN")
                kind = "INNER"
            elif self.take_keyword("LEFT"):
                self.take_keyword("OUTER")
                self.expect_keyword("JOIN")
                kind = "LEFT"
            elif self.take_keyword("JOIN"):
                kind = "INNER"
            if kind is None:
                return left
            right = self.table_ref()
            condition = self.expression() if self.take_keyword("ON") else None
            if kind == "LEFT" and condition is None:
                raise ParseError("a LEFT JOIN needs an ON condition")
            left = nodes.Join(left, right, kind, condition)

    def table_ref(self) -> nodes.TableRef:
        name = self.expect_name()
        alias = None
        if self.take_keyword("AS"):
            alias = self.expect_name()
        elif self.current.type is TokenType.IDENTIFIER:
            alias = self.expect_name()
        return nodes.TableRef(name, alias)

    def sort_key(self) -> nodes.OrderBy:
        # A bare number is a select-list position: `ORDER BY 2` sorts by the
        # second output column, which is standard SQL and handy with aggregates.
        if self.current.type is TokenType.NUMBER:
            value: nodes.Expression | int = self.expect_number()
        else:
            value = self.expression()
        descending = False
        if self.take_keyword("DESC"):
            descending = True
        else:
            self.take_keyword("ASC")
        return nodes.OrderBy(value, descending)

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

    def vacuum(self) -> nodes.Vacuum:
        self.expect_keyword("VACUUM")
        return nodes.Vacuum()

    def explain(self) -> nodes.Explain:
        self.expect_keyword("EXPLAIN")
        return nodes.Explain(self.statement())

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
        if token.type is TokenType.KEYWORD and token.value == "NULL":
            self.index += 1
            return nodes.Literal(None)
        if token.type is TokenType.IDENTIFIER:
            name = self.expect_name()
            if self.current.value == "(" and name.upper() in nodes.AGGREGATES:
                return self.aggregate(name.upper())
            if self.take_punctuation("."):
                return nodes.ColumnRef(self.expect_name(), qualifier=name)
            return nodes.ColumnRef(name)
        if self.take_punctuation("("):
            inner = self.expression()
            self.expect_punctuation(")")
            return inner
        if self.take_punctuation("-"):
            return nodes.Literal(-self.expect_number())
        if self.take_punctuation("+"):
            return nodes.Literal(self.expect_number())
        self.fail("a value or a column name")

    def aggregate(self, name: str) -> nodes.FunctionCall:
        self.expect_punctuation("(")
        if self.take_punctuation("*"):
            self.expect_punctuation(")")
            if name != "COUNT":
                raise ParseError(f"{name}(*) is not valid; only COUNT(*) is")
            return nodes.FunctionCall("COUNT", None)
        distinct = self.take_keyword("DISTINCT")
        argument = self.expression()
        self.expect_punctuation(")")
        if nodes.aggregates_in(argument):
            raise ParseError(f"{name}(...) cannot contain another aggregate")
        return nodes.FunctionCall(name, argument, distinct)
