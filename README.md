# gramps-sql-extensions

A collection of SQL-accelerated implementations of operations Gramps
normally performs by walking Python objects. Each module targets one such
operation. So far:

- **Relationship lookup** (`gramps_sql_extensions.relationship`)

## Relationship lookup

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

### Usage

This module has no idea what your database connection is. It needs
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
path = graph.relationship_path(handle1, handle2)
all_paths = graph.all_relationship_paths(handle1, handle2)
paged = graph.relationships_to(handle1, handles=[handle2, handle3])
```

`execute` is called many times per call to any of these, not once, so it
should be a thin, stable wrapper around an already-open connection, not
something that opens a fresh one each time. See
`RelationshipGraph.__init__`'s docstring for the full contract, including
`treeid` (for a multi-tenant Postgres schema; `None` for one-tree-per-file
SQLite).

### Drawing a relationship graph (`relationship_path()`)

`relationship_path(h1, h2)` returns the actual chain of people connecting
`h1` and `h2` through their nearest common ancestor -- the same pairing
`relationship()` reports, just with every intermediate person included
rather than collapsed into one string -- as a list of nodes ordered from
`h1` to `h2`:

```python
graph.relationship_path(h1, h2)
# [
#     {"handle": h1,        "relationship_string": ""},
#     {"handle": "...",     "relationship_string": "father"},
#     {"handle": "...",     "relationship_string": "grandfather"},
#     {"handle": "...",     "relationship_string": "second great grandfather"},
#     {"handle": "...",     "relationship_string": "third great stepgrandmother"},
#     {"handle": h2,        "relationship_string": "second great stepgrandaunt"},
# ]
```

Each dict is one node (a real person's handle) and consecutive dicts are
its edges, so this is meant to be walked directly into a graph/chain
diagram. `relationship_string` is always that node's relationship *to
`h1`*, not to its neighbor in the chain, so `h1`'s own entry is always
`""`. Returns `[]` if the two people aren't related within `depth`
generations, and a single-entry list if `h1 == h2`. Takes the same
`restricted`/`depth` keywords as `relationship()`.

`all_relationship_paths(h1, h2)` is the same idea, generalized the way
`all_relationships()` generalizes `relationship()`: two people can share
more than one common ancestor (cousins who married, or any other
pedigree collapse), and this returns one path per ancestor, nearest
first, rather than just the closest one:

```python
graph.all_relationship_paths(h1, h2)
# [
#     [{"handle": h1, "relationship_string": ""}, ..., {"handle": h2, "relationship_string": "second cousin"}],
#     [{"handle": h1, "relationship_string": ""}, ..., {"handle": h2, "relationship_string": "third cousin once removed"}],
#     ...
# ]
```

`all_relationship_paths(h1, h2)[0]` always equals `relationship_path(h1,
h2)`. Unlike `all_relationships()`, entries here are grouped by ancestor,
not by wording -- two different ancestors that happen to produce
identical wording still come back as two separate paths, since the point
is showing the actual distinct routes, not counting how many ways there
are to say it. Pedigree collapse can in principle surface a common
ancestor for every generation two people's lines cross, so pass
`max_paths=N` to cap how many are returned (`None`, the default, returns
all of them).

### Bulk lookup, paged like gramps-web-api (`relationships_to()`)

`relationships_to(h1, handles=...)` returns `h1`'s relationship to each
of a list of people in one call, paged the same way gramps-web-api's own
object-list resources are: `page` (1-indexed, default `0` meaning "no
paging, return everything") and `pagesize` (default `20`) match that
project's field names, defaults, and semantics exactly, so a caller
already wired up for gramps-web-api-style paging doesn't need a second
convention here.

```python
graph.relationships_to(h1, handles=[h2, h3, "does-not-exist"])
# {
#     "items": [
#         {"handle": h2, "relationship_string": "second cousin"},
#         {"handle": h3, "relationship_string": ""},  # not related within `depth`
#     ],
#     "total": 2,   # "does-not-exist" was silently dropped, same as
#                   # gramps-web-api's own `handles` query param
#     "page": 0,
#     "pagesize": 20,
# }
```

`handles=None` means every person in the tree, ordered by handle,
standing in for "list all objects" the way omitting gramps-web-api's own
`handles` param does. `total` reflects the visible target count *before*
paging, so a caller can compute how many pages there are. A handle that
doesn't exist, or (with `restricted=True`) belongs to a private person,
is silently skipped -- from an explicit `handles` list, and from the
`handles=None` "everyone" listing -- since there's no already-proxied
`db_handle` here to have hidden it upstream.

Note that the default `page=0` computes a relationship for every visible
person in the tree when `handles=None` -- correct, and consistent with
gramps-web-api's own "if omitted, all results are returned" contract,
but genuinely expensive on a large tree (one small query per person, see
`ancestor_map`). Pass an actual `page` to avoid that.

### Public-only search (`restricted=True`)

`relationship()`, `all_relationships()`, `relationship_path()`,
`all_relationship_paths()`, and `relationships_to()` all take a
`restricted` keyword, `False` by default. Pass `restricted=True` when
the caller shouldn't see anyone's
private data, e.g. an anonymous or logged-out visitor to a public family
tree site. It mirrors `PrivateProxyDb`'s three
rules exactly: a private person, a private family, or a private
`ChildRef` all make that link invisible, as if it didn't exist in the
graph at all — not merely redacted after the fact.

```python
# A logged-in owner sees everything:
graph.relationship(h1, h2, restricted=False)  # e.g. "mother"

# The same query from a public, unauthenticated viewer:
graph.relationship(h1, h2, restricted=True)   # "" if the only path
                                               # runs through a private
                                               # person/family/child link
```

Because the check is a live SQL predicate applied on every call rather
than a second precomputed "restricted" copy of the graph, marking someone
private takes effect on the very next query, with nothing to invalidate.
This is what a Gramps Web-style deployment should use for any relationship
lookup made on behalf of a non-owner viewer; use `restricted=False` only
for callers already authorized to see private data.

## License

GPL-2.0-or-later, matching Gramps.
