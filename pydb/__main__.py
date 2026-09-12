"""`python -m pydb <database file>` starts the shell."""

import sys

from pydb.repl import main

sys.exit(main())
