# Changelog

All notable changes to this project are documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [0.2.2] - 2026-09-09

### Fixed

- `ensure_child_of()` no longer builds a session-scoped SQL temp table
  (`DROP TABLE`/`CREATE TEMP TABLE`/`CREATE INDEX`); it loads every
  parent/child edge into the `RelationshipGraph` instance's own Python
  memory via a single plain `SELECT` instead, and `ancestor_map()`/
  the shared-ancestor-couple lookup walk that in-memory index rather
  than issuing further SQL. This removes all DDL from the module, so it
  now works against a connection that's genuinely read-only at the
  database/role level, not just one where "read-only" is an unenforced
  application-level convention (as with Gramps' own
  `DbGeneric.load(..., readonly=True)`).
- Along the way, this also fixes a real performance bug in the old
  design: the temp table was never `ANALYZE`d after being indexed, so
  Postgres planned the recursive-CTE lookup as a full sequential scan
  instead of using the index it had just built (confirmed via `EXPLAIN
  ANALYZE`: 291ms vs. 0.26ms on the identical query against the real
  101,518-person/46,315-family tree used for this project's benchmarks).
  At `relationships_to()` bulk scale that made the shipped code roughly
  500x slower than intended -- an all-tree sweep that should take
  seconds was instead taking hours. The new in-memory design has no
  equivalent step to get wrong, and measures faster than even a
  correctly-`ANALYZE`d version of the old one.

## [0.2.1] - 2026-09-09

### Fixed

- `relationship()`/`all_relationships()`/`relationships_to()`: a shared
  ancestor *couple* (both members of a marriage are common ancestors) is
  now collapsed into one family-level path, matching gramps-core's own
  `collapse_relations`. Several locale calculators (e.g. `rel_de`,
  `rel_ru`, `rel_pl`, `rel_sv`, `rel_uk`, `rel_tr`) derive "full" vs.
  "half" relation wording from the relationship path's last character,
  so without this fix a shared couple silently read as a half relation
  through only one of its two members.
- `all_relationships()`/`all_relationship_paths()`: no longer report a
  common ancestor that sits behind a nearer one on both people's own
  routes to it (e.g. every generation above a real common ancestor) as
  if it were a separate, more distant relationship -- gramps-core's own
  search never visits those in the first place.
- `all_relationships()`: entries are now ordered with the same full
  tie-break `all_relationship_paths()` already used, instead of a bare
  distance sum whose ties depended on `set` iteration order (and so on
  `PYTHONHASHSEED`).
- `relationship()`/`relationship_path()`: a spouse lookup no longer pays
  the cost of building the whole-tree `child_of` edge table, since both
  functions return immediately on a spouse match without ever using it.

## [0.2.0] - 2026-09-05

### Added

- `relationship_path()`/`all_relationship_paths()` for drawing the
  actual chain of intermediate people connecting two people, not just
  the relationship wording.
- `relationships_to()` for bulk, paged relationship lookups against a
  list of people (or the whole tree), matching gramps-web-api's own
  object-list paging conventions.

### Fixed

- `relationship_path()` no longer issues a separate `ancestor_map()`
  query per node in the returned chain.

## [0.1.0] - 2026-09-02

### Added

- Initial release: `RelationshipGraph`, a fast relationship lookup that
  replaces `gramps.gen.relationship.RelationshipCalculator`'s
  exponential path-enumerating walk with a small SQL-derived ancestry
  graph and a breadth-first search, bounded to the two people's own
  reachable ancestors.
- Privacy filtering mirroring `PrivateProxyDb`'s three rules (private
  person, private family, private `ChildRef`), applied live as SQL
  predicates.
- SQLite and PostgreSQL support.
