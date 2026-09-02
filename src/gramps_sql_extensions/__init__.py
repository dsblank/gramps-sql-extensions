"""gramps-sql-extensions: SQL-accelerated implementations of Gramps operations.

Currently: fast relationship lookup (`RelationshipGraph`), replacing
`gramps.gen.relationship.RelationshipCalculator`'s exponential
path-enumerating walk with a small SQL-derived ancestry graph and a
proper breadth-first search. See `gramps_sql_extensions.relationship`
for the full design notes.
"""

from .relationship import RelationshipGraph, ExecuteFn, DIALECTS

__all__ = ["RelationshipGraph", "ExecuteFn", "DIALECTS"]

__version__ = "0.1.0"
