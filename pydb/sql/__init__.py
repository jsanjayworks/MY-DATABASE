"""Layer 7: SQL.

The pipeline the roadmap asks for, one module per stage:

| Module | Stage |
|--------|-------|
| `tokenizer` | text -> tokens |
| `nodes` | the AST the parser builds |
| `parser` | tokens -> AST |
| `planner` | AST + catalog -> an access path and a chain of generators |
| `engine` | the front door: `Engine.execute(sql) -> Result` |

The table definitions the whole thing reads live one level up, in `pydb.catalog`,
because a catalog is storage that happens to describe storage.
"""

from pydb.sql.engine import Engine, Result
from pydb.sql.errors import ParseError, PlanError, SqlError, ValueTypeError
from pydb.sql.parser import parse, parse_script
from pydb.sql.tokenizer import tokenize

__all__ = [
    "Engine",
    "Result",
    "SqlError",
    "ParseError",
    "PlanError",
    "ValueTypeError",
    "parse",
    "parse_script",
    "tokenize",
]
