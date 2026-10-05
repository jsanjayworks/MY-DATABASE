"""Layer 7g: the REPL.

    $ python -m pydb my.db
    pydb> CREATE TABLE people (id INT PRIMARY KEY, name TEXT);
    CREATE TABLE people
    pydb> INSERT INTO people VALUES (1, 'ada');
    INSERT 1
    pydb> SELECT * FROM people;
    id  name
    --  ----
    1   ada
    (1 row)

Statements run when they are terminated by a semicolon, so one can span several
lines. Lines beginning with a dot are commands to the shell rather than SQL --
`.tables`, `.schema`, `.plan`, `.help`, `.quit` -- which is the convention `sqlite3`
uses and there is no reason to invent a different one.

A SQL error prints as a message and the session continues. That matters more than
it sounds: a traceback from a typo would make the thing unusable, and a REPL that
exits on a bad query is not a REPL. A bug in pydb itself does not end the session
either, but it is labelled as one, so it does not read like a mistake in the SQL.
"""

from __future__ import annotations

import sys
from typing import Iterator, TextIO

from pydb.database import Database
from pydb.errors import PydbError
from pydb.sql import Engine, Result

PROMPT = "pydb> "
CONTINUATION = "  ...> "
NULL = "NULL"

HELP = """\
Statements end with a semicolon and may span lines.

  .tables              list the tables in this database
  .indexes [table]     list the indexes, or just one table's
  .schema [table]      show the CREATE statements for one table, or all of them
  .plan [on|off]       show the access path chosen for each query
  .help                this text
  .quit, .exit         leave (Ctrl-D also works)

Supported SQL:
  CREATE TABLE / DROP TABLE / CREATE [UNIQUE] INDEX / DROP INDEX
  INSERT / UPDATE / DELETE / VACUUM
  SELECT [DISTINCT] with WHERE, JOIN (INNER, LEFT, CROSS), GROUP BY, HAVING,
         ORDER BY, LIMIT / OFFSET, and COUNT / SUM / AVG / MIN / MAX
  BEGIN / COMMIT / ROLLBACK, and EXPLAIN in front of any query

Types are INT and TEXT. Prefix any statement with EXPLAIN to see its plan."""


class Repl:
    """The read-eval-print loop, with the input and output injectable for tests."""

    def __init__(
        self,
        engine: Engine,
        stdin: TextIO | None = None,
        stdout: TextIO | None = None,
        interactive: bool | None = None,
    ) -> None:
        self.engine = engine
        self.stdin = stdin or sys.stdin
        self.stdout = stdout or sys.stdout
        # Prompts are noise when the input is a script, and essential when it is a
        # person. `isatty` is the difference.
        self.interactive = (
            self.stdin.isatty() if interactive is None else interactive
        )
        self.show_plan = False
        self.errors = 0

    # ------------------------------------------------------------------
    # the loop
    # ------------------------------------------------------------------

    def run(self) -> int:
        """Read until the input ends. Returns the number of statements that failed."""
        for statement in self._statements():
            try:
                if statement.startswith("."):
                    if self._command(statement):
                        break
                else:
                    self._run_sql(statement)
            except PydbError as error:
                self._fail(error)
            except Exception as error:  # noqa: BLE001 - a REPL must not die on one
                self._fail(error, internal=True)
        return self.errors

    def _statements(self) -> Iterator[str]:
        """Yield complete statements: dot commands, or SQL up to a semicolon."""
        buffer = ""
        while True:
            self._prompt(CONTINUATION if buffer else PROMPT)
            line = self.stdin.readline()
            if not line:  # end of input
                trailing = buffer.strip()
                if trailing and not trailing.startswith("--"):
                    yield trailing  # a last statement with no semicolon
                self._write("\n" if self.interactive else "")
                return
            stripped = line.strip()
            if not buffer and (not stripped or stripped.startswith("--")):
                continue
            if not buffer and stripped.startswith("."):
                yield stripped
                continue
            buffer += line
            # One line can hold several statements, so keep splitting until the
            # rest of the buffer has no terminator left in it.
            while True:
                statement, buffer = _split_statement(buffer)
                if statement is None:
                    break
                if statement.strip().rstrip(";").strip():
                    yield statement.strip()
            # Whatever follows the last semicolon is the start of the next
            # statement, and leading whitespace would make an empty remainder look
            # like a statement still being typed.
            buffer = buffer.lstrip()

    def _run_sql(self, sql: str) -> None:
        result = self.engine.execute(sql)
        if self.show_plan and result.plan:
            self._write(f"-- {result.plan}\n")
        if result.is_query:
            self._write(format_table(result))
        else:
            self._write(result.message + "\n")

    # ------------------------------------------------------------------
    # dot commands
    # ------------------------------------------------------------------

    def _command(self, line: str) -> bool:
        """Run a dot command. Returns True if the session should end."""
        parts = line.split()
        name, arguments = parts[0].lower(), parts[1:]

        if name in (".quit", ".exit"):
            return True
        if name == ".help":
            self._write(HELP + "\n")
            return False
        if name == ".tables":
            names = self.engine.catalog.table_names()
            self._write(("\n".join(names) if names else "no tables") + "\n")
            return False
        if name == ".indexes":
            self._indexes(arguments)
            return False
        if name == ".schema":
            self._schema(arguments)
            return False
        if name == ".plan":
            if arguments:
                self.show_plan = arguments[0].lower() in ("on", "true", "1", "yes")
            else:
                self.show_plan = not self.show_plan
            self._write(f"plan display {'on' if self.show_plan else 'off'}\n")
            return False
        self._write(f"unknown command {name} (try .help)\n")
        self.errors += 1
        return False

    def _schema(self, arguments: list[str]) -> None:
        catalog = self.engine.catalog
        names = arguments or catalog.table_names()
        if not names:
            self._write("no tables\n")
            return
        for name in names:
            self._write(describe_table(catalog.info(name)) + "\n")

    def _indexes(self, arguments: list[str]) -> None:
        catalog = self.engine.catalog
        lines = []
        for name in arguments or catalog.table_names():
            info = catalog.info(name)
            for index in info.indexes:
                kind = (
                    "primary key"
                    if index.primary
                    else ("unique" if index.unique else "index")
                )
                column = info.schema.columns[index.column].name
                lines.append(f"{index.name}  {kind} on {name}({column})")
        self._write(("\n".join(lines) if lines else "no indexes") + "\n")

    # ------------------------------------------------------------------
    # output
    # ------------------------------------------------------------------

    def _prompt(self, text: str) -> None:
        if self.interactive:
            self._write(text)

    def _write(self, text: str) -> None:
        self.stdout.write(text)
        self.stdout.flush()

    def _fail(self, error: Exception, internal: bool = False) -> None:
        self.errors += 1
        if internal:
            self._write(f"internal error ({type(error).__name__}): {error}\n")
        else:
            self._write(f"error: {error}\n")


def _split_statement(buffer: str) -> tuple[str | None, str]:
    """Split off the first complete statement. Returns `(statement, rest)`.

    A statement ends at the first semicolon that is not inside a string literal or
    a comment -- `INSERT INTO t VALUES ('a;b');` is one statement, not two. When
    there is no such semicolon the statement is still being typed, so the whole
    buffer comes back as the remainder.
    """
    in_string = False
    index = 0
    while index < len(buffer):
        char = buffer[index]
        if in_string:
            if char == "'":
                if buffer.startswith("''", index):
                    index += 2
                    continue
                in_string = False
        elif char == "'":
            in_string = True
        elif char == "-" and buffer.startswith("--", index):
            newline = buffer.find("\n", index)
            if newline < 0:
                return None, buffer  # the comment has not been closed by a newline
            index = newline
        elif char == ";":
            return buffer[: index + 1], buffer[index + 1 :]
        index += 1
    return None, buffer


def describe_table(info) -> str:
    """The `CREATE` statements that would rebuild this table and its indexes."""
    parts = []
    for column in info.schema:
        piece = f"{column.name} {column.type}"
        if column.name == info.primary_key_name:
            piece += " PRIMARY KEY"
        elif not column.nullable:
            piece += " NOT NULL"
        parts.append(piece)
    lines = [f"CREATE TABLE {info.name} (\n  " + ",\n  ".join(parts) + "\n);"]
    for index in info.indexes:
        if index.primary:
            continue  # already shown as PRIMARY KEY above
        unique = "UNIQUE " if index.unique else ""
        column = info.schema.columns[index.column].name
        lines.append(f"CREATE {unique}INDEX {index.name} ON {info.name}({column});")
    return "\n".join(lines)


def format_table(result: Result) -> str:
    """A result as aligned columns, with a row count underneath."""
    headers = [str(name) for name in result.columns]
    body = [[_cell(value) for value in row] for row in result.rows]
    widths = [len(header) for header in headers]
    for row in body:
        for index, cell in enumerate(row):
            widths[index] = max(widths[index], len(cell))

    lines = ["  ".join(h.ljust(w) for h, w in zip(headers, widths)).rstrip()]
    lines.append("  ".join("-" * w for w in widths))
    for row in body:
        lines.append("  ".join(c.ljust(w) for c, w in zip(row, widths)).rstrip())
    count = len(body)
    lines.append(f"({count} row{'' if count == 1 else 's'})")
    return "\n".join(lines) + "\n"


def _cell(value: object) -> str:
    return NULL if value is None else str(value)


def main(argv: list[str] | None = None) -> int:
    """`python -m pydb <database>`. Returns a process exit status."""
    arguments = list(sys.argv[1:] if argv is None else argv)
    if not arguments or arguments[0] in ("-h", "--help"):
        sys.stdout.write("usage: python -m pydb <database file>\n")
        return 0 if arguments else 2

    path = arguments[0]
    with Database(path) as db:
        engine = Engine(db)
        repl = Repl(engine)
        if repl.interactive:
            repl._write(
                f"pydb - a database built from scratch. {path}\n"
                f"Type .help for commands, .quit to leave.\n"
            )
        try:
            failures = repl.run()
        except KeyboardInterrupt:
            repl._write("\ninterrupted\n")
            return 130
    return 1 if failures else 0
