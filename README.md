# gramps-sql-extensions

SQL-accelerated implementations of operations Gramps normally performs by
walking Python objects. The first (and so far only) piece is fast
relationship lookup.

## Why

Gramps' own `RelationshipCalculator` finds the relationship between two
people by recursively walking `Person`/`Family` objects and enumerating
every distinct path to a common ancestor, not just the shortest one. Under
pedigree collapse (shared distant ancestors, or a search that reaches past
a tree's actual recorded depth), a normal genealogical pattern, that
becomes exponential: a pair connected through a handful of real ancestors
can take minutes and pin a CPU core, regardless of how big the tree is
overall.

`RelationshipGraph` in `gramps_sql_extensions.relationship` replaces the
*search* (not the wording) with:

1. Parent/child edges pulled directly from `family.json_data`'s
   `child_ref_list` via each backend's native JSON functions
   (`jsonb_array_elements` on Postgres, `json_each` on SQLite), no
   `Person`/`Family` object construction at all.
2. A plain breadth-first search over that edge set, each node visited
   once, so cost tracks distinct people, never distinct paths to them.
3. Gramps' own, unmodified, locale-aware string formatting for the actual
   wording.

Privacy filtering (mirroring `PrivateProxyDb`'s rules: a private person, a
private family, or a private `ChildRef` are all invisible) is a live SQL
predicate, not a second precomputed copy of the graph.

## Usage

This package has no idea what your database connection is. It needs
exactly one thing from you: an `execute` callable.

```python
from gramps_sql_extensions import RelationshipGraph

def execute(sql: str, params: list) -> list[tuple]:
    cursor = my_connection.cursor()
    cursor.execute(sql, params)
    try:
        return cursor.fetchall()
    except Exception:
        return []  # DDL statements (CREATE/DROP/CREATE INDEX) have no rows

graph = RelationshipGraph(execute, dialect="sqlite")  # or "postgresql"
rel_str, dist_a, dist_b = graph.relationship(handle1, handle2)
all_rels = graph.all_relationships(handle1, handle2)
```

`execute` is called many times per call to `relationship()`/
`all_relationships()`, not once, so it should be a thin, stable wrapper
around an already-open connection, not something that opens a fresh one
each time. See `RelationshipGraph.__init__`'s docstring for the full
contract, including `treeid` (for a multi-tenant Postgres schema; `None`
for one-tree-per-file SQLite).

## Status

Extracted from a real, tested integration into
[gramps-web-api](https://github.com/gramps-project/gramps-web-api).
Validated (in that integration) against live Postgres and SQLite
databases, including the pedigree-collapse tie-break, remarriage
ordering, and privacy filtering, and against gramps-web-api's own
`tests/test_endpoints/test_relations.py`. Not yet independently packaged,
versioned, or tested standalone, this scaffold is the first step toward
that.

## License

AGPL-3.0-or-later, matching Gramps and Gramps Web API.
