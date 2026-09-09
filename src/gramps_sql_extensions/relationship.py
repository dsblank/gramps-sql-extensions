#
# gramps-sql-extensions
#
# This program is free software; you can redistribute it and/or modify
# it under the terms of the GNU Affero General Public License as published by
# the Free Software Foundation; either version 3 of the License, or
# (at your option) any later version.
#
# This program is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU Affero General Public License for more details.
#
# You should have received a copy of the GNU Affero General Public License
# along with this program. If not, see <https://www.gnu.org/licenses/>.
#

"""Fast relationship lookup via a small SQL-derived ancestry graph.

Replaces Gramps' `RelationshipCalculator`'s exponential path-enumerating
walk (`gramps.gen.relationship.RelationshipCalculator.__apply_filter` /
`get_relationship_distance_new`) -- and, in a caller that was building a
full in-memory object cache before running it (e.g. Gramps Web API's old
`CachePeopleFamiliesProxy`, which deserialized every Person/Family in the
tree before answering a single query) -- with:

1. A small SQL query pulling parent/child edges straight out of
   `family.json_data`'s `child_ref_list` via each backend's native JSON
   functions -- no Person/Family object construction at all.
2. A plain breadth-first search over that edge set (bounded to the two
   people's own reachable ancestors, not the whole tree), which is what
   actually fixes the exponential blowup: each node is visited once,
   the search cost tracks distinct people, never distinct paths to them.
3. Gramps' own, unmodified locale-aware string formatting
   (`get_single_relationship_string` / `get_sibling_relationship_string` /
   `get_partner_relationship_string`) for the actual wording -- only the
   *search* is replaced here, not how a found relationship gets said.

Privacy filtering (mirroring `PrivateProxyDb`'s three rules: a private
person, a private family, or a private `ChildRef` are all invisible) is
applied live as extra SQL predicates rather than via two precomputed
"restricted" and "full" copies of the graph -- there is only ever one
graph, so there's nothing that can go stale between an edit and the next
read.

This module has no dependency on any particular database driver or
connection object. The caller supplies a single `execute` callable --
`execute(sql: str, params: list) -> list[tuple]` -- and everything else
(dialect selection, privacy filtering, the search itself) happens here.
See `RelationshipGraph.__init__` for the exact contract that callable
needs to satisfy.

CURRENT STATE: `ensure_child_of` loads every parent/child edge into this
`RelationshipGraph` instance's own Python memory on each call, via one
plain `SELECT` against `family.json_data` (no DDL of any kind -- no temp
table, no index, nothing written to the database at all, so this works
against a genuinely read-only connection, not just an application-level
"read-only" convention). `ancestor_map` and `_family_partner` then walk
that in-memory edge index directly instead of issuing further SQL. That
avoids all Person/Family object construction and, on the same real
101,518-person / 46,315-family Postgres tree used for the numbers in the
README, measures faster end-to-end than an earlier version of this module
that built a session-scoped SQL temp table (with an index on it) for the
same purpose -- both for a single lookup and, especially, for
`relationships_to`'s bulk case, where the whole edge index is loaded once
and then every target is answered from memory with no further queries at
all. The tradeoff: `ensure_child_of` now pulls the *entire* tree's edges
across the connection on every call, rather than a temp table's much
smaller per-query result sets -- on a real (non-loopback) network link,
that transfer cost matters more than it did here. The further upgrade
validated separately (not implemented here) is a real, permanent table
maintained incrementally by `AFTER INSERT/UPDATE/DELETE` triggers on
`family` (and `person`, for the privacy-flip case) -- tested end-to-end in
SQLite and roughly 1000x faster again once indexed, but that's a
gramps-core/addon schema change, out of scope for this package alone.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Callable, Optional

from gramps.gen.relationship import get_relationship_calculator
from gramps.gen.const import GRAMPS_LOCALE as glocale

# ChildRefType / EventType / FamilyRelType values (gramps.gen.lib), inlined
# rather than imported so this module pulls in nothing beyond what it
# actually uses -- these are stable, documented enum values, not internals.
_BIRTH, _UNKNOWN_REL = 1, 6
_DIVORCE, _ANNULMENT = 7, 9
_FAM_MARRIED, _FAM_UNMARRIED, _FAM_CIVIL_UNION = 0, 1, 2
(
    _PARTNER_MARRIED,
    _PARTNER_UNMARRIED,
    _PARTNER_CIVIL_UNION,
    _PARTNER_UNKNOWN_REL,
    _PARTNER_EX_MARRIED,
    _PARTNER_EX_UNMARRIED,
    _PARTNER_EX_CIVIL_UNION,
    _PARTNER_EX_UNKNOWN_REL,
) = range(1, 9)

_NORM_SIB, _HALF_SIB_FATHER, _HALF_SIB_MOTHER, _STEP_SIB, _UNKNOWN_SIB = range(5)

# The `execute` callable's contract: given a SQL string (using `?` as the
# placeholder, regardless of dialect -- Postgres queries below are written
# with `?` too, since translating that is the caller's adapter's job, not
# this module's) and a list of positional parameters, run it and return
# every row as a list of tuples. Called many times per logical operation
# (check_spouse issues one query per family in a person's family_list), so
# it should be a thin, stable wrapper over an already-open connection, not
# something that opens a fresh one per call. Every statement this module
# issues is a plain SELECT -- no DDL, nothing written to the connection at
# all -- so a read-only connection/role works here with no special casing.
ExecuteFn = Callable[[str, list], list[tuple]]


def _is_birth_path(path: str) -> bool:
    """A path (e.g. 'ffMf') is birth-only if it contains none of the
    three non-birth codes -- 'F'/'M' (a lone step/adopted/etc. parent
    link) or 'A' (a family-collapsed link, see `_famrel_from_persrel`,
    where *neither* parent is a birth parent). Port of gramps-core's own
    `RelationshipCalculator.only_birth`: note that's an exclusion list,
    not an allow-list, so the other two family codes -- 'a' (both birth
    parents) and 'b'/'c' (birth via just one side of the family) --
    count as birth-only too, same as gramps-core."""
    return not any(c in ("F", "M", "A") for c in path)


# ---------------------------------------------------------------------------
# Dialect fragments: the two backends need less adaptation than you'd think.
# `person.private` / `family.private` are plain INTEGER columns on both, so
# the privacy predicate itself never changes -- only how a scalar gets
# pulled out of one `child_ref_list` JSON array element does. `treeid` is
# handled separately (see `_tree_clause` below), not as a dialect fragment,
# since it's a structural difference (multi-tenant Postgres vs. one SQLite
# file per tree), not a syntax one.
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class _Dialect:
    param_cast: str  # "" for sqlite; "::text" for postgres (see ancestor_map)
    child_ref_from: str
    ref_expr: str
    frel_expr: str
    mrel_expr: str
    childref_private_expr: str
    family_type_expr: str
    event_ref_from: str
    event_ref_handle_expr: str
    event_type_expr: str


_POSTGRESQL = _Dialect(
    param_cast="::text",
    child_ref_from="JOIN LATERAL jsonb_array_elements(f.json_data::jsonb -> 'child_ref_list') AS c ON true",
    ref_expr="c ->> 'ref'",
    frel_expr="(c -> 'frel' ->> 'value')::int",
    mrel_expr="(c -> 'mrel' ->> 'value')::int",
    childref_private_expr="COALESCE((c ->> 'private')::boolean::int, 0)",
    family_type_expr="(f.json_data::jsonb -> 'type' ->> 'value')::int",
    event_ref_from="LEFT JOIN LATERAL jsonb_array_elements(f.json_data::jsonb -> 'event_ref_list') AS er ON true",
    event_ref_handle_expr="er.value ->> 'ref'",
    event_type_expr="(e.json_data::jsonb -> 'type' ->> 'value')::int",
)

_SQLITE = _Dialect(
    param_cast="",
    child_ref_from="JOIN json_each(f.json_data, '$.child_ref_list') AS c ON true",
    ref_expr="json_extract(c.value, '$.ref')",
    frel_expr="json_extract(c.value, '$.frel.value')",
    mrel_expr="json_extract(c.value, '$.mrel.value')",
    childref_private_expr="COALESCE(json_extract(c.value, '$.private'), 0)",
    family_type_expr="json_extract(f.json_data, '$.type.value')",
    event_ref_from="LEFT JOIN json_each(f.json_data, '$.event_ref_list') AS er ON true",
    event_ref_handle_expr="json_extract(er.value, '$.ref')",
    event_type_expr="json_extract(e.json_data, '$.type.value')",
)

DIALECTS = {"sqlite": _SQLITE, "postgresql": _POSTGRESQL, "sharedpostgresql": _POSTGRESQL}


def _dialect_for(dbid: str) -> _Dialect:
    try:
        return DIALECTS[dbid]
    except KeyError:
        raise ValueError(f"Unsupported database backend for fast relationship lookup: {dbid!r}")


def _tree_clause(alias: str, treeid: Optional[int]) -> str:
    """`treeid` is expected to be resolved by the caller before it ever
    reaches here (never user input), so it's inlined as a validated int
    literal rather than fought over as a bind parameter repeated across
    many places in one query."""
    if treeid is None:
        return ""  # SQLite: one tree per file, no such column at all
    return f"AND {alias}.treeid = {int(treeid)}"


class RelationshipGraph:
    """Answers relationship queries via a small SQL-derived ancestry graph.

    Needs exactly one thing from the caller: `execute`, a callable of the
    shape `execute(sql: str, params: list) -> list[tuple]` that runs SQL
    against whatever connection the caller already has open and returns
    the resulting rows. This module has no idea what that connection is,
    a raw DB-API cursor, an ORM's connection, a pooled connection, a
    Gramps `Connection` wrapper, anything with that one shape works. See
    `ExecuteFn` above for the full contract.

    No DDL, ever: `ensure_child_of()` (called by every public method here)
    loads every parent/child edge into this instance's own Python memory
    via a single plain `SELECT`, then `ancestor_map()`/`_family_partner()`
    walk that in-memory index -- no `CREATE`/`DROP`/`INDEX` statement is
    ever sent through `execute`. That makes this class safe to use against
    a connection that's genuinely read-only at the database/role level
    (e.g. a Postgres role granted only `SELECT`, a read replica), not just
    one where "read-only" is an unenforced application-level convention
    (as with Gramps' own `DbGeneric.load(..., readonly=True)`, which
    gramps-core itself documents as *not enforced by Gramps* -- enforcement
    is left to the caller). The tradeoff for that: `ensure_child_of()`
    pulls the *entire* tree's parent/child edges across the connection on
    every call (not just the smaller result set a targeted query would
    return), so on a real (non-loopback) network link to the database,
    that transfer cost is the one to watch, especially on a very large
    tree.

    `dialect` is `"sqlite"`, `"postgresql"`, or `"sharedpostgresql"`.
    `treeid` is the backend's own integer tree-scoping column value (only
    meaningful for a multi-tenant Postgres schema; `None` for SQLite,
    where one tree is one file and there's no such column). Note this is
    NOT necessarily the same value as whatever your application calls a
    "tree id" elsewhere -- resolve that translation before calling in.
    """

    def __init__(self, execute: ExecuteFn, dialect: str, treeid: Optional[int] = None, locale=glocale):
        self._execute_fn = execute
        self._dialect = _dialect_for(dialect)
        self._treeid = treeid
        # Populated by ensure_child_of(); None until then so a caller that
        # skips it gets a clear error rather than a confusing KeyError deep
        # inside ancestor_map()/_family_partner().
        self._edges_all: Optional[dict[str, list[tuple[str, str, int]]]] = None
        self._edges_safe: Optional[dict[str, list[tuple[str, str, int]]]] = None
        self._family_partner_map: Optional[dict[tuple[str, str], str]] = None
        self._calc = get_relationship_calculator(reinit=True, clocale=locale)
        # get_relationship_calculator() picks the right calculator *class*
        # for the locale (e.g. rel_it.py's subclass), but string translation
        # itself is gated on self._locale being set on the *instance* --
        # gramps-core's own get_one_relationship() does this as its first
        # line; skipping it means every string silently falls back to
        # English regardless of which locale was requested.
        self._calc._locale = locale

    def _execute(self, sql: str, params: tuple = ()) -> list[tuple]:
        return self._execute_fn(sql, list(params))

    # -- graph extraction -----------------------------------------------

    def ancestor_map(self, handle: str, restricted: bool, max_depth: int = 15) -> dict:
        """Return a dict describing every ancestor of `handle` reachable
        within `max_depth` generations, keyed:

        - `"dist"`: handle -> generation distance from `handle` (shortest
          route only -- the visited-once BFS invariant that avoids the
          exponential blowup).
        - `"path"`: handle -> path code string for that same shortest
          route (e.g. `"ffMf"`).
        - `"prev"`: handle -> the one child it was discovered from along
          that shortest route, letting a caller (see `relationship_path`)
          reconstruct the actual chain of handles rather than just its
          length/wording.
        - `"parent_of"`: child handle -> list of `(parent, code,
          relvalue)` edges actually fetched (every edge, not just
          shortest-route ones), needed by `sibling_type`.

        A dict, not a tuple, deliberately: this is a public method with
        no fixed arity to preserve, and a caller reading `m["dist"]`
        keeps working if a later version adds a new key (e.g. gender,
        family_handle) -- unlike positional unpacking, which breaks the
        moment the shape grows."""
        if self._edges_all is None:
            raise RuntimeError("ensure_child_of() must be called before ancestor_map()")
        edges = self._edges_safe if restricted else self._edges_all

        # Unbounded reachability closure from `handle`, entirely in Python
        # over the in-memory edge index -- mirrors what the old SQL
        # recursive CTE computed here (that had no depth limit either;
        # only the generation-distance walk below is bounded by
        # max_depth). Kept as a separate pass so `parent_of` ends up with
        # every edge for every reachable ancestor, even one beyond
        # max_depth generations, matching the old query's "child is in
        # `anc`" semantics exactly -- sibling_type()/_typed_parents() need
        # every edge *type* for a child, not just the one edge the BFS
        # below happens to use to reach it first.
        anc = {handle}
        frontier = [handle]
        while frontier:
            nxt = []
            for h in frontier:
                for parent, _code, _rel in edges.get(h, ()):
                    if parent not in anc:
                        anc.add(parent)
                        nxt.append(parent)
            frontier = nxt

        parent_of: dict[str, list[tuple[str, str, int]]] = {
            child: edges[child] for child in anc if child in edges
        }

        # gramps-core's own depth cutoff (RelationshipCalculator.__apply_filter)
        # excludes a generation once its internal counter (which starts at 1
        # for the root person, so generation G there is depth G+1) exceeds
        # max_depth -- i.e. it includes generations G < max_depth, not
        # G <= max_depth. Match that exactly, not "depth < max_depth" one
        # iteration too generous, or a boundary case (Ga exactly max_depth)
        # gets found here when the real endpoint would report "not related".
        dist = {handle: 0}
        path = {handle: ""}
        prev: dict[str, Optional[str]] = {handle: None}
        frontier = [handle]
        depth = 0
        while frontier and depth < max_depth - 1:
            depth += 1
            nxt = []
            for h in frontier:
                for parent, code, _rel in parent_of.get(h, ()):
                    if parent not in dist:
                        dist[parent] = depth
                        path[parent] = path[h] + code
                        prev[parent] = h
                        nxt.append(parent)
            frontier = nxt
        return {"dist": dist, "path": path, "prev": prev, "parent_of": parent_of}

    def ensure_child_of(self) -> None:
        """(Re)load this instance's in-memory parent/child edge index --
        `ancestor_map()`/`_family_partner()` read it directly, no further
        SQL involved. Call once per logical operation
        (relationship()/all_relationships() already do this) before the
        first `ancestor_map()`/`check_spouse()` call. Always reloads
        rather than trusting a previous load: if this `RelationshipGraph`
        instance is reused across multiple logical operations (common when
        it wraps a pooled or long-lived connection, e.g. one instance per
        web app process rather than per request), skipping the reload
        would risk answering with edges that have gone stale since a tree
        edit between calls. See the module docstring for the trigger-
        maintained-table upgrade that would remove this per-call reload
        cost -- and for why this reload is one plain `SELECT`, no DDL."""
        d = self._dialect
        t_family = _tree_clause("f", self._treeid)
        rows = self._execute(
            f"""
            SELECT f.handle AS family_handle, f.father_handle AS parent,
                   {d.ref_expr} AS child,
                   CASE WHEN {d.frel_expr} = 1 THEN 'f' ELSE 'F' END AS code,
                   {d.frel_expr} AS relvalue,
                   {d.childref_private_expr} AS childref_private
            FROM family f
            {d.child_ref_from}
            WHERE f.father_handle IS NOT NULL {t_family}
            UNION ALL
            SELECT f.handle, f.mother_handle,
                   {d.ref_expr},
                   CASE WHEN {d.mrel_expr} = 1 THEN 'm' ELSE 'M' END,
                   {d.mrel_expr},
                   {d.childref_private_expr}
            FROM family f
            {d.child_ref_from}
            WHERE f.mother_handle IS NOT NULL {t_family}
            """,
            [],
        )

        # Privacy inputs for edges_safe below, mirroring PrivateProxyDb's
        # three rules exactly (childref_private is already per-edge, from
        # the query above): which persons and which families are private,
        # tree-scoped the same way the edge query itself is.
        t_person = _tree_clause("person", self._treeid)
        private_persons = {
            row[0]
            for row in self._execute(
                f"SELECT handle FROM person WHERE 1=1 {t_person} AND COALESCE(private, 0) != 0", []
            )
        }
        private_families = {
            row[0]
            for row in self._execute(
                f"SELECT handle FROM family f WHERE 1=1 {t_family} AND COALESCE(f.private, 0) != 0", []
            )
        }

        edges_all: dict[str, list[tuple[str, str, int]]] = {}
        edges_safe: dict[str, list[tuple[str, str, int]]] = {}
        # (family_handle, child) -> parents in that family producing that
        # child -- almost always at most 2 (father, mother), grouped here
        # so _family_partner() below can look up "the other one" in O(1)
        # instead of re-scanning every call.
        family_children: dict[tuple[str, str], list[str]] = {}

        for family_handle, parent, child, code, relvalue, childref_private in rows:
            edges_all.setdefault(child, []).append((parent, code, relvalue))
            safe = not childref_private and parent not in private_persons and family_handle not in private_families
            if safe:
                edges_safe.setdefault(child, []).append((parent, code, relvalue))
            family_children.setdefault((family_handle, child), []).append(parent)

        # No privacy filtering here, deliberately -- see _family_partner()'s
        # own docstring: this is only ever used as an unconfirmed candidate,
        # checked against the caller's own (already privacy-aware) common-
        # ancestor set before being treated as anything more than that.
        partner_map: dict[tuple[str, str], str] = {}
        for (_family_handle, child), parents in family_children.items():
            if len(parents) < 2:
                continue
            for i, p in enumerate(parents):
                for q in parents[i + 1 :]:
                    if q != p:
                        partner_map.setdefault((child, p), q)
                        partner_map.setdefault((child, q), p)

        self._edges_all = edges_all
        self._edges_safe = edges_safe
        self._family_partner_map = partner_map

    # -- spouse / sibling -------------------------------------------------

    def _family_list(self, handle: str) -> list[str]:
        d = self._dialect
        t = _tree_clause("person", self._treeid)
        if d is _POSTGRESQL:
            # jsonb columns come back already deserialized as Python lists.
            rows = self._execute(
                f"SELECT json_data::jsonb -> 'family_list' FROM person WHERE handle = ? {t}",
                (handle,),
            )
            return rows[0][0] if rows and rows[0][0] else []
        # SQLite's json_extract returns the array as a JSON-encoded string.
        rows = self._execute(
            "SELECT json_extract(json_data, '$.family_list') FROM person WHERE handle = ?",
            (handle,),
        )
        if not rows or not rows[0][0]:
            return []
        return json.loads(rows[0][0])

    def check_spouse(self, h1: str, h2: str, restricted: bool):
        """Mirror `_get_spouse_type`'s `val[-1]` semantics: walk h1's own
        `family_list` in its recorded order and return the LAST family
        where h2 is the other parent, not an arbitrary one -- matters when
        the same couple has multiple family records (remarriage, or an
        unmarried-partner record later formalized). Returns
        `(spouse_type, gender1, gender2)` or `None`."""
        d = self._dialect
        family_handles = self._family_list(h1)
        if not family_handles:
            return None

        t_f = _tree_clause("f", self._treeid)
        t_e = _tree_clause("e", self._treeid)
        privacy_where = ""
        if restricted:
            privacy_where = "AND f.private = 0"

        best = None
        for fam_handle in family_handles:
            rows = self._execute(
                f"""
                SELECT {d.family_type_expr} AS fam_type,
                       {d.event_type_expr} AS event_type
                FROM family f
                {d.event_ref_from}
                LEFT JOIN event e ON e.handle = {d.event_ref_handle_expr} {t_e}
                WHERE f.handle = ? {t_f}
                  AND ((f.father_handle = ? AND f.mother_handle = ?) OR (f.father_handle = ? AND f.mother_handle = ?))
                  {privacy_where}
                """,
                (fam_handle, h1, h2, h2, h1),
            )
            if not rows:
                continue
            fam_type = rows[0][0]
            is_ex = any(r[1] in (_DIVORCE, _ANNULMENT) for r in rows if r[1] is not None)
            best = (fam_type, is_ex)  # keep overwriting -- last family_list match wins
        if best is None:
            return None
        fam_type, is_ex = best
        return self._spouse_type_of(fam_type, is_ex), self.gender(h1), self.gender(h2)

    @staticmethod
    def _spouse_type_of(fam_type, is_ex) -> int:
        if fam_type == _FAM_MARRIED:
            return _PARTNER_EX_MARRIED if is_ex else _PARTNER_MARRIED
        elif fam_type == _FAM_UNMARRIED:
            return _PARTNER_EX_UNMARRIED if is_ex else _PARTNER_UNMARRIED
        elif fam_type == _FAM_CIVIL_UNION:
            return _PARTNER_EX_CIVIL_UNION if is_ex else _PARTNER_CIVIL_UNION
        return _PARTNER_EX_UNKNOWN_REL if is_ex else _PARTNER_UNKNOWN_REL

    def gender(self, handle: str) -> int:
        t = _tree_clause("person", self._treeid)
        rows = self._execute(f"SELECT gender FROM person WHERE handle = ? {t}", (handle,))
        return rows[0][0]

    @staticmethod
    def _typed_parents(handle, parent_of_map, want_birth: bool):
        mother = father = None
        for parent, code, rel in parent_of_map.get(handle, ()):
            is_birth = rel == _BIRTH
            is_nonbirth = rel != _BIRTH and rel != _UNKNOWN_REL
            if (want_birth and not is_birth) or (not want_birth and not is_nonbirth):
                continue
            if code in ("f", "F"):
                father = parent
            else:
                mother = parent
        return mother, father

    def sibling_type(self, h1, h2, pm1, pm2) -> int:
        m1, f1 = self._typed_parents(h1, pm1, True)
        m2, f2 = self._typed_parents(h2, pm2, True)
        if f1 and m1 and f2 and m2:
            if f1 == f2 and m1 == m2:
                return _NORM_SIB
            elif f1 == f2:
                return _HALF_SIB_FATHER
            elif m1 == m2:
                return _HALF_SIB_MOTHER
            return _STEP_SIB
        h1_nb = [x for x in self._typed_parents(h1, pm1, False) if x]
        if f2 and f2 in h1_nb:
            return _HALF_SIB_MOTHER if (m2 and m2 == m1) else _STEP_SIB
        if m2 and m2 in h1_nb:
            return _HALF_SIB_FATHER if (f2 and f2 == f1) else _STEP_SIB
        h2_nb = [x for x in self._typed_parents(h2, pm2, False) if x]
        if f1 and f1 in h2_nb:
            return _HALF_SIB_MOTHER if (m1 and m2 == m1) else _STEP_SIB
        if m1 and m1 in h2_nb:
            return _HALF_SIB_FATHER if (f2 and f2 == f1) else _STEP_SIB
        return _UNKNOWN_SIB

    @staticmethod
    def _ancestor_sort_key(dist1, path1, dist2, path2, h):
        """Full ordering over candidate common ancestors, low (best) to
        high: nearer total generation-distance first; among ties,
        gramps-core's own priority order (direct relation > birth-line >
        mother-line-over-father-line, matching (Ga, Gb, gender,
        only_birth)-dependent English wording); finally the handle
        itself, purely so that ties surviving even that (genuinely
        interchangeable in wording) still sort the same way on every
        call rather than depending on `set` iteration order, which is
        hash-seed-dependent and not stable across processes. Shared by
        `_best_common_ancestor` (single best) and `all_relationship_paths`
        (every candidate, nearest first) so the first entry of the
        latter always agrees with the former's pick."""
        p1, p2 = path1[h], path2[h]
        direct = dist1[h] == 0 or dist2[h] == 0
        birth = _is_birth_path(p1) and _is_birth_path(p2)
        code_rank = {"m": 0, "f": 1, "M": 2, "F": 3}
        c1 = code_rank.get(p1[-1], -1) if p1 else -1
        c2 = code_rank.get(p2[-1], -1) if p2 else -1
        return (dist1[h] + dist2[h], not direct, not birth, c1, c2, h)

    @classmethod
    def _best_common_ancestor(cls, dist1, path1, dist2, path2, common):
        """Pedigree-collapse tie-break shared by `relationship()` and
        `relationship_path()`: the single nearest/best common ancestor,
        per `_ancestor_sort_key`."""
        return min(common, key=lambda h: cls._ancestor_sort_key(dist1, path1, dist2, path2, h))

    @staticmethod
    def _chain_to_ancestor(prev: dict, anc: str) -> list[str]:
        """Walk `prev` (i.e. `ancestor_map(...)["prev"]`) from `anc` back
        to the handle its map was built for, returning handles in root ->
        `anc` order."""
        chain = [anc]
        while prev[chain[-1]] is not None:
            chain.append(prev[chain[-1]])
        chain.reverse()
        return chain

    @staticmethod
    def _nearest_common_ancestors(common: set, path1: dict, path2: dict) -> set:
        """Drop a common ancestor that sits *behind* a nearer one on both
        people's own routes to it. Mirrors gramps-core's own
        `__apply_filter`: `other_person`'s search stops the moment a
        branch crosses into `orig_person`'s known ancestors, so a
        still-more-distant shared ancestor further up that exact same
        two-sided route is never even visited there, let alone reported
        -- whereas this module's BFS happily walks both ancestor maps to
        `max_depth` independently and then intersects, which finds those
        farther, redundant ancestors too. A common ancestor `anc2` is
        redundant here if some *other* common ancestor `anc1` lies on
        both `anc2`'s route from `h1` and its route from `h2` -- i.e.
        `path1[anc1]` is a strict prefix of `path1[anc2]` and
        `path2[anc1]` is a strict prefix of `path2[anc2]`."""
        redundant = set()
        for anc2 in common:
            p1, p2 = path1[anc2], path2[anc2]
            for anc1 in common:
                if anc1 is anc2:
                    continue
                q1, q2 = path1[anc1], path2[anc1]
                if len(q1) < len(p1) and len(q2) < len(p2) and p1.startswith(q1) and p2.startswith(q2):
                    redundant.add(anc2)
                    break
        return common - redundant

    def _family_partner(self, anc: str, child: str) -> Optional[str]:
        """The other parent of `child` in whichever family produced
        `child`'s parent-link to `anc` -- i.e. `anc`'s spouse in that
        specific family -- or `None` if there isn't one. Used only as a
        *candidate* for family-path collapsing (see `_collapsed_paths`);
        the caller still has to confirm that candidate is itself one of
        the two people's own common ancestors, reached via this exact
        pairing, before treating it as anything more than a hypothesis,
        so this needs no privacy filtering of its own -- an unsafe
        candidate simply won't pass that later check."""
        return self._family_partner_map.get((child, anc))

    @staticmethod
    def _famrel_from_persrel(persrel_a: str, persrel_b: str) -> str:
        """Port of gramps-core's `RelationshipCalculator._famrel_from_persrel`:
        combine two parents' single-person path codes ('m'/'f'/'M'/'F')
        for the same family into one family-level code ('a'/'b'/'c'/'A'),
        matching `collapse_relations`'s own pairing -- so locale
        calculators that key off the *last* path character (most
        non-English ones do, to tell a full relation from a half one)
        see the two parents as one shared-ancestor family rather than
        two unrelated single-parent links."""
        if persrel_a == persrel_b:
            return persrel_a
        pair = {persrel_a, persrel_b}
        if pair == {"m", "f"}:
            return "a"  # both birth parents: REL_FAM_BIRTH
        if pair == {"m", "F"}:
            return "b"  # birth mother, non-birth father: REL_FAM_BIRTH_MOTH_ONLY
        if pair == {"f", "M"}:
            return "c"  # birth father, non-birth mother: REL_FAM_BIRTH_FATH_ONLY
        return "A"  # REL_FAM_NONBIRTH

    def _collapsed_paths(self, anc, dist1, path1, prev1, dist2, path2, prev2, common):
        """If `anc`'s spouse in the relevant family is *also* a common
        ancestor of `h1`/`h2` -- reached from both people via that exact
        same family -- return the family-collapsed `(path_a, path_b,
        partner)` triple gramps-core's own `collapse_relations` would
        produce for this pairing: the last hop's lone-parent code
        ('m'/'f'/'M'/'F') becomes a family code ('a'/'b'/'c'/'A'). Many
        locale calculators derive "full" vs. "half" relation wording
        from that last character alone, so without this a shared
        ancestor *couple* silently reads as a half relation through only
        one of its two members. Returns `(path1[anc], path2[anc], None)`
        unchanged when there's no such partner (e.g. `anc` is `h1`
        itself, or the two routes go through different families)."""
        path_a, path_b = path1[anc], path2[anc]
        x1, x2 = prev1.get(anc), prev2.get(anc)
        if not path_a or not path_b or x1 is None or x2 is None:
            return path_a, path_b, None
        partner = self._family_partner(anc, x1)
        if (
            partner is None
            or partner != self._family_partner(anc, x2)
            or partner not in common
            or prev1.get(partner) != x1
            or prev2.get(partner) != x2
        ):
            return path_a, path_b, None
        new_a = path_a[:-1] + self._famrel_from_persrel(path_a[-1], path1[partner][-1])
        new_b = path_b[:-1] + self._famrel_from_persrel(path_b[-1], path2[partner][-1])
        return new_a, new_b, partner

    # -- shared wording helper ---------------------------------------------

    def _string_for_ancestor(
        self, h1, h2, anc, dist1, path1, prev1, pm1, dist2, path2, prev2, pm2, gender1, gender2, common
    ):
        """Relationship wording for one specific common ancestor `anc`,
        as `(relationship_string, collapsed_partner_or_None)`. Shared by
        `relationship()` (single best answer), `all_relationships()`
        (every distinct answer), and `_relationship_to()` -- all three
        reduce to "given a chosen ancestor, say the relationship it
        produces". `collapsed_partner` is `anc`'s spouse when
        `_collapsed_paths` folded the two of them into one family-level
        answer (see there) -- callers that report common ancestors as a
        list (`all_relationships()`) need it to list both and to skip
        the partner as a redundant, separate entry of its own; callers
        that only report the single chosen ancestor can ignore it."""
        Ga, Gb = dist1[anc], dist2[anc]
        if Ga == 1 and Gb == 1:
            sib = self.sibling_type(h1, h2, pm1, pm2)
            return self._calc.get_sibling_relationship_string(sib, gender1, gender2), None
        path_a, path_b, partner = self._collapsed_paths(anc, dist1, path1, prev1, dist2, path2, prev2, common)
        only_birth = _is_birth_path(path_a) and _is_birth_path(path_b)
        rel_str = self._calc.get_single_relationship_string(
            Ga, Gb, gender1, gender2, path_a, path_b,
            only_birth=only_birth, in_law_a=False, in_law_b=False,
        )
        return rel_str, partner

    # -- top-level entry points ---------------------------------------------

    def relationship(self, h1: str, h2: str, restricted: bool = False, depth: int = 15):
        """Return (relationship_string, distance_common_origin,
        distance_common_other) -- the single most-direct relationship."""
        if h1 == h2:
            return "", -1, -1

        # Checked before ensure_child_of()/ancestor_map(): those build and
        # query the whole-tree edge table, work a spouse match never needs
        # and this call is about to return without using anyway.
        spouse = self.check_spouse(h1, h2, restricted)
        if spouse is not None:
            spouse_type, gender1, gender2 = spouse
            rel_str = self._calc.get_partner_relationship_string(spouse_type, gender1, gender2)
            return rel_str, -1, -1

        self.ensure_child_of()

        m1 = self.ancestor_map(h1, restricted, max_depth=depth)
        m2 = self.ancestor_map(h2, restricted, max_depth=depth)
        dist1, path1, prev1, pm1 = m1["dist"], m1["path"], m1["prev"], m1["parent_of"]
        dist2, path2, prev2, pm2 = m2["dist"], m2["path"], m2["prev"], m2["parent_of"]
        common = set(dist1) & set(dist2)
        if not common:
            return "", -1, -1

        best = self._best_common_ancestor(dist1, path1, dist2, path2, common)
        Ga, Gb = dist1[best], dist2[best]
        gender1, gender2 = self.gender(h1), self.gender(h2)
        rel_str, _partner = self._string_for_ancestor(
            h1, h2, best, dist1, path1, prev1, pm1, dist2, path2, prev2, pm2, gender1, gender2, common
        )
        return rel_str, Ga, Gb

    def all_relationships(self, h1: str, h2: str, restricted: bool = False, depth: int = 15):
        """Return a list of {relationship_string, common_ancestors} dicts --
        every distinct relationship between h1 and h2, not just the most
        direct one (two people can be related more than one way, most
        commonly cousins who married). Mirrors gramps-core's
        get_all_relationships(): entries ordered nearest-relationship-
        first, ancestors that produce identical wording are grouped into
        the same entry's `common_ancestors` list. A result of `[{}]` means
        no relationship was found at all.

        One known gap versus gramps-core's literal get_all_relationships:
        this only reports each ancestor's *shortest* path (the same
        visited-once BFS invariant that makes the single-answer lookup
        fast), whereas gramps-core's all_dist=True search can also surface
        a *longer*, differently-worded path to the very same ancestor under
        heavy pedigree collapse -- a narrow edge case, under-reported here
        rather than silently wrong."""
        if h1 == h2:
            return [{}]

        self.ensure_child_of()

        result = []
        seen: dict[str, int] = {}

        spouse = self.check_spouse(h1, h2, restricted)
        if spouse is not None:
            spouse_type, gender1, gender2 = spouse
            rel_str = self._calc.get_partner_relationship_string(spouse_type, gender1, gender2)
            seen[rel_str] = len(result)
            result.append({"relationship_string": rel_str, "common_ancestors": []})

        m1 = self.ancestor_map(h1, restricted, max_depth=depth)
        m2 = self.ancestor_map(h2, restricted, max_depth=depth)
        dist1, path1, prev1, pm1 = m1["dist"], m1["path"], m1["prev"], m1["parent_of"]
        dist2, path2, prev2, pm2 = m2["dist"], m2["path"], m2["prev"], m2["parent_of"]
        common = set(dist1) & set(dist2)
        if not common:
            return result or [{}]

        # Drop ancestors sitting behind a nearer common ancestor on both
        # people's own routes -- see `_nearest_common_ancestors`.
        common = self._nearest_common_ancestors(common, path1, path2)

        gender1, gender2 = self.gender(h1), self.gender(h2)
        # nearest relationship first, matching "relstrings is ordered on
        # rank automatic" in gramps-core's own get_all_relationships --
        # the full tie-break (not just total distance) so iteration order
        # is deterministic rather than depending on `set` hash order.
        consumed = set()
        for anc in sorted(common, key=lambda h: self._ancestor_sort_key(dist1, path1, dist2, path2, h)):
            if anc in consumed:
                continue
            rel_str, partner = self._string_for_ancestor(
                h1, h2, anc, dist1, path1, prev1, pm1, dist2, path2, prev2, pm2, gender1, gender2, common
            )
            handles = [anc] if partner is None else [anc, partner]
            if partner is not None:
                consumed.add(partner)
            if rel_str in seen:
                result[seen[rel_str]]["common_ancestors"].extend(handles)
            else:
                seen[rel_str] = len(result)
                result.append({"relationship_string": rel_str, "common_ancestors": handles})

        return result or [{}]

    def _relationship_to(self, h1: str, other: str, restricted: bool, depth: int, dist1, path1, prev1, pm1) -> str:
        """Relationship of `other` to `h1`, reusing `h1`'s already-built
        ancestor map (`dist1`/`path1`/`prev1`/`pm1`) rather than
        recomputing it -- the piece of `relationship()` that's expensive
        per call. Only `other`'s own map is fetched fresh each call,
        since -- unlike `relationship_path`/`all_relationship_paths`,
        where every node is already known to sit on a specific,
        already-computed chain -- `relationships_to` calls this for
        arbitrary target handles with no such shortcut available."""
        spouse = self.check_spouse(h1, other, restricted)
        if spouse is not None:
            spouse_type, gender1, gender2 = spouse
            return self._calc.get_partner_relationship_string(spouse_type, gender1, gender2)

        m2 = self.ancestor_map(other, restricted, max_depth=depth)
        dist2, path2, prev2, pm2 = m2["dist"], m2["path"], m2["prev"], m2["parent_of"]
        common = set(dist1) & set(dist2)
        if not common:
            return ""

        anc = self._best_common_ancestor(dist1, path1, dist2, path2, common)
        gender1, gender2 = self.gender(h1), self.gender(other)
        rel_str, _partner = self._string_for_ancestor(
            h1, other, anc, dist1, path1, prev1, pm1, dist2, path2, prev2, pm2, gender1, gender2, common
        )
        return rel_str

    def _label_direct_ancestor(self, h1: str, node: str, dist1, path1, gender1: int) -> str:
        """Relationship to `h1` of `node`, one of `h1`'s own ancestors
        (present in `h1`'s ancestor map `dist1`/`path1`) -- the common
        ancestor of this particular pairing is `node` itself, so Gb is
        always 0, e.g. `get_single_relationship_string(2, 0, ...)` ->
        "grandfather"/"grandmother"."""
        gender_node = self.gender(node)
        only_birth = _is_birth_path(path1[node])
        return self._calc.get_single_relationship_string(
            dist1[node], 0, gender1, gender_node, path1[node], "",
            only_birth=only_birth, in_law_a=False, in_law_b=False,
        )

    def _label_via_ancestor(
        self, h1: str, other: str, anc: str, dist1, path1, prev1, pm1, dist2, path2, prev2, pm2, common, gender1: int
    ) -> str:
        """Relationship to `h1` of `other`, an ancestor of `h2` (or `h2`
        itself) sitting on `h2`'s shortest BFS route to the specific
        common ancestor `anc`. `other`'s own up-path to `anc` is the tail
        of `anc`'s own `path2`/distance beyond `other`'s -- valid only
        because `other` sits on that exact route (true for every node
        `all_relationship_paths`/`relationship_path` ever call this for),
        not for an arbitrary pair of ancestors in the map. `anc`'s path
        pair is run through the same family-collapsing as
        `_string_for_ancestor` (see `_collapsed_paths`) before the
        `other`-relative tail is cut from it, so a shared-ancestor
        *couple* reads correctly here too, not just at the chain's `h2`
        endpoint."""
        Ga = dist1[anc]
        Gb = dist2[anc] - dist2[other]
        path_a, full_path_b, _partner = self._collapsed_paths(anc, dist1, path1, prev1, dist2, path2, prev2, common)
        path_b = full_path_b[len(path2[other]):]
        gender_other = self.gender(other)
        if Ga == 1 and Gb == 1:
            sib = self.sibling_type(h1, other, pm1, pm2)
            return self._calc.get_sibling_relationship_string(sib, gender1, gender_other)
        only_birth = _is_birth_path(path_a) and _is_birth_path(path_b)
        return self._calc.get_single_relationship_string(
            Ga, Gb, gender1, gender_other, path_a, path_b,
            only_birth=only_birth, in_law_a=False, in_law_b=False,
        )

    @staticmethod
    def _chain_pair(prev1, prev2, anc):
        """The two half-chains meeting at `anc`: `([h1, ..., anc], [h2,
        ..., anc])`, per `_chain_to_ancestor`."""
        return (
            RelationshipGraph._chain_to_ancestor(prev1, anc),
            RelationshipGraph._chain_to_ancestor(prev2, anc),
        )

    def relationship_path(self, h1: str, h2: str, restricted: bool = False, depth: int = 15):
        """Return the chain of people connecting `h1` and `h2` through
        their nearest common ancestor -- the same pairing `relationship()`
        reports -- as a list of `{"handle", "relationship_string"}` dicts
        ordered from `h1` to `h2` inclusive. Meant for drawing a
        relationship graph/chain: each dict is one node, consecutive
        dicts are its edges, and `relationship_string` is always that
        node's relationship *to `h1`* (e.g. "father", "grandmother",
        "second great stepgrandaunt"), not to its neighbor in the chain --
        so `h1`'s own entry is always `""` (it's the reference person
        every other entry's wording is relative to).

        Returns `[]` if `h1` and `h2` aren't related within `depth`
        generations, or `[{"handle": h1, "relationship_string": ""}]` if
        they're the same person.
        """
        if h1 == h2:
            return [{"handle": h1, "relationship_string": ""}]

        result = [{"handle": h1, "relationship_string": ""}]

        # Checked before ensure_child_of()/ancestor_map(): those build and
        # query the whole-tree edge table, work a spouse match never needs
        # and this call is about to return without using anyway.
        spouse = self.check_spouse(h1, h2, restricted)
        if spouse is not None:
            spouse_type, gender1, gender2 = spouse
            rel_str = self._calc.get_partner_relationship_string(spouse_type, gender1, gender2)
            result.append({"handle": h2, "relationship_string": rel_str})
            return result

        self.ensure_child_of()

        m1 = self.ancestor_map(h1, restricted, max_depth=depth)
        m2 = self.ancestor_map(h2, restricted, max_depth=depth)
        dist1, path1, prev1, pm1 = m1["dist"], m1["path"], m1["prev"], m1["parent_of"]
        dist2, path2, prev2, pm2 = m2["dist"], m2["path"], m2["prev"], m2["parent_of"]
        common = set(dist1) & set(dist2)
        if not common:
            return []

        anc = self._best_common_ancestor(dist1, path1, dist2, path2, common)
        chain1, chain2 = self._chain_pair(prev1, prev2, anc)  # [h1,...,anc], [h2,...,anc]

        gender1 = self.gender(h1)
        for node in chain1[1:]:
            rel_str = self._label_direct_ancestor(h1, node, dist1, path1, gender1)
            result.append({"handle": node, "relationship_string": rel_str})
        for node in reversed(chain2[:-1]):
            rel_str = self._label_via_ancestor(
                h1, node, anc, dist1, path1, prev1, pm1, dist2, path2, prev2, pm2, common, gender1
            )
            result.append({"handle": node, "relationship_string": rel_str})

        return result

    def all_relationship_paths(
        self, h1: str, h2: str, restricted: bool = False, depth: int = 15, max_paths: Optional[int] = None
    ):
        """Return every distinct chain of people connecting `h1` and `h2`,
        one per common ancestor, nearest-relationship-first -- the
        `relationship_path()` analogue of how `all_relationships()`
        relates to `relationship()`. Unlike `all_relationships()`, this
        groups by ancestor rather than by wording: two different
        ancestors that happen to produce identical wording still come
        back as two separate paths here, since the whole point is
        showing the actual distinct routes between the two people, not
        just how many different ways to say it there are.

        Each entry has the same shape `relationship_path()` returns: a
        list of `{"handle", "relationship_string"}` dicts from `h1` to
        `h2`, each node's string relative to `h1`.

        `max_paths` caps how many paths are returned (nearest first) --
        `None` (the default) returns all of them, matching
        `all_relationships()`'s own uncapped behavior, but pedigree
        collapse can produce a common ancestor for every generation two
        people's lines happen to cross, so pass a small `max_paths` when
        you only want the first handful for display. Paths are ordered
        so that `all_relationship_paths(...)[0]` always equals
        `relationship_path(...)`.

        Under heavy pedigree collapse the *same* person can legitimately
        appear twice within one path -- once as `h1`'s own ancestor and
        again, independently, as an ancestor of `h2`'s route to a more
        distant common ancestor through that same person's spouse. That
        isn't a bug: it reflects the two people's lines genuinely
        crossing more than once, the same real-world situation
        `all_relationships()` reports as more than one distinct
        `relationship_string` for the same pair.

        Returns `[]` if `h1` and `h2` aren't related within `depth`
        generations, and `[[{"handle": h1, "relationship_string": ""}]]`
        (a single trivial one-node "path") if they're the same person.
        """
        if h1 == h2:
            return [[{"handle": h1, "relationship_string": ""}]]

        self.ensure_child_of()

        paths = []

        spouse = self.check_spouse(h1, h2, restricted)
        if spouse is not None:
            spouse_type, gender1, gender2 = spouse
            rel_str = self._calc.get_partner_relationship_string(spouse_type, gender1, gender2)
            paths.append([
                {"handle": h1, "relationship_string": ""},
                {"handle": h2, "relationship_string": rel_str},
            ])

        m1 = self.ancestor_map(h1, restricted, max_depth=depth)
        m2 = self.ancestor_map(h2, restricted, max_depth=depth)
        dist1, path1, prev1, pm1 = m1["dist"], m1["path"], m1["prev"], m1["parent_of"]
        dist2, path2, prev2, pm2 = m2["dist"], m2["path"], m2["prev"], m2["parent_of"]
        common = set(dist1) & set(dist2)
        if not common:
            return paths

        # Drop ancestors sitting behind a nearer common ancestor on both
        # people's own routes -- see `_nearest_common_ancestors`. Without
        # this, pedigree collapse at generation N would spuriously also
        # report every one of that ancestor's own ancestors (through
        # generation `depth`) as if they were separate, more-distant
        # relationships, when gramps-core's own search never visits them.
        common = self._nearest_common_ancestors(common, path1, path2)

        gender1 = self.gender(h1)
        ancestors = sorted(common, key=lambda h: self._ancestor_sort_key(dist1, path1, dist2, path2, h))
        if max_paths is not None:
            ancestors = ancestors[:max_paths]

        for anc in ancestors:
            chain1, chain2 = self._chain_pair(prev1, prev2, anc)  # [h1,...,anc], [h2,...,anc]

            path = [{"handle": h1, "relationship_string": ""}]
            for node in chain1[1:]:
                rel_str = self._label_direct_ancestor(h1, node, dist1, path1, gender1)
                path.append({"handle": node, "relationship_string": rel_str})
            for node in reversed(chain2[:-1]):
                rel_str = self._label_via_ancestor(
                    h1, node, anc, dist1, path1, prev1, pm1, dist2, path2, prev2, pm2, common, gender1
                )
                path.append({"handle": node, "relationship_string": rel_str})

            paths.append(path)

        return paths

    # -- bulk lookup, paged like gramps-web-api's own object-list resources -

    def _all_person_handles(self, restricted: bool) -> list[str]:
        """Every person handle in this tree, ordered by handle for a
        stable paging order across calls. A private person is excluded
        entirely for a restricted viewer -- not just their links -- since
        this feeds `relationships_to`'s "list everyone" mode, the
        equivalent of a person-listing endpoint, not a link traversal."""
        t = _tree_clause("person", self._treeid)
        where = f"WHERE 1=1 {t}"
        if restricted:
            where += " AND COALESCE(private, 0) = 0"
        rows = self._execute(f"SELECT handle FROM person {where} ORDER BY handle", [])
        return [row[0] for row in rows]

    def _visible_handles(self, handles: list[str], restricted: bool) -> set:
        """The subset of `handles` that both exist and, if `restricted`,
        aren't privacy-hidden -- mirrors gramps-web-api's own `handles`
        query param ("non-existing handles are silently skipped"),
        extended here to also silently skip private people for a
        restricted caller, same as `_all_person_handles`."""
        if not handles:
            return set()
        t = _tree_clause("person", self._treeid)
        placeholders = ",".join("?" for _ in handles)
        where_private = "AND COALESCE(private, 0) = 0" if restricted else ""
        rows = self._execute(
            f"SELECT handle FROM person WHERE handle IN ({placeholders}) {t} {where_private}",
            list(handles),
        )
        return {row[0] for row in rows}

    def relationships_to(
        self,
        h1: str,
        handles: Optional[list[str]] = None,
        restricted: bool = False,
        depth: int = 15,
        page: int = 0,
        pagesize: int = 20,
    ):
        """Return the relationship string of `h1` to each of `handles`,
        paged the same way gramps-web-api's own object-list resources
        are (`page`/`pagesize`, both matching that project's field
        defaults exactly): `page` is 1-indexed, and the default `page=0`
        means "no paging, return everything" rather than "page zero".
        `handles=None` means every person in the tree, ordered by
        handle, standing in for "list all objects" -- there's no
        object-list endpoint to delegate that to here, so it's built
        from a plain `SELECT handle FROM person` instead.

        A handle that doesn't exist, or (when `restricted=True`) belongs
        to a private person, is silently skipped -- from `handles`
        itself, and from the "everyone" listing when `handles is None`
        -- mirroring gramps-web-api's own "non-existing handles are
        silently skipped" `handles` param, extended to privacy since
        there's no proxied `db_handle` here to have already done that.

        Returns `{"items": [...], "total": N, "page": page, "pagesize":
        pagesize}`. `items` is a list of `{"handle", "relationship_string"}`
        dicts for the requested page (or everything, if `page=0`);
        `total` is the count of visible target handles *before* paging,
        letting a caller compute how many pages there are. `h1`'s own
        entry (if it appears in `handles`, or always when `handles is
        None`) gets `relationship_string=""`, same self-convention as
        `relationship_path()`.

        Note that `page=0` computes a relationship for every visible
        person in the tree, one small query per person (see
        `ancestor_map`) -- correct, and consistent with gramps-web-api's
        own "if omitted, all results are returned" contract for object
        listings, but genuinely expensive on a large tree. Pass an
        actual `page` to avoid that.
        """
        self.ensure_child_of()

        if handles is None:
            target_handles = self._all_person_handles(restricted)
        else:
            visible = self._visible_handles(handles, restricted)
            target_handles = [h for h in handles if h in visible]

        total = len(target_handles)
        if page > 0:
            offset = (page - 1) * pagesize
            target_handles = target_handles[offset : offset + pagesize]

        m1 = self.ancestor_map(h1, restricted, max_depth=depth)
        dist1, path1, prev1, pm1 = m1["dist"], m1["path"], m1["prev"], m1["parent_of"]

        items = []
        for other in target_handles:
            if other == h1:
                rel_str = ""
            else:
                rel_str = self._relationship_to(h1, other, restricted, depth, dist1, path1, prev1, pm1)
            items.append({"handle": other, "relationship_string": rel_str})

        return {"items": items, "total": total, "page": page, "pagesize": pagesize}
