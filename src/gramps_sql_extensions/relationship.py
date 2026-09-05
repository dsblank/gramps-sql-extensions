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

CURRENT STATE: `child_of` is built as a session-scoped SQL temp table on
each call to `ensure_child_of`, extracted fresh from `family.json_data`
every time. That avoids all Person/Family object construction and is
measured at roughly 100ms (SQLite, ~7k people) to 1-1.5s (Postgres, ~100k
people) -- dramatically cheaper than deserializing the whole tree, and it
needs no schema changes to use. The further upgrade validated separately
(not implemented here) is promoting `child_of` to a real, permanent table
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
# (ancestor_map alone issues one query; check_spouse issues one per family
# in a person's family_list), so it should be a thin, stable wrapper over
# an already-open connection, not something that opens a fresh one per call.
# For DDL statements (CREATE/DROP/CREATE INDEX) the return value is ignored,
# so returning an empty list is fine.
ExecuteFn = Callable[[str, list], list[tuple]]


def _is_birth_path(path: str) -> bool:
    """A path (e.g. 'ffMf') is birth-only if every hop is lowercase --
    upper-case codes ('F'/'M') mark a step/adopted/etc. link."""
    return all(c in ("f", "m") for c in path)


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
        d = self._dialect
        t_pp = _tree_clause("pp", self._treeid)
        t_ff = _tree_clause("ff", self._treeid)

        privacy_join = privacy_where = ""
        if restricted:
            privacy_join = (
                f"JOIN person pp ON pp.handle = co.parent {t_pp}\n"
                f"        JOIN family ff ON ff.handle = co.family_handle {t_ff}"
            )
            privacy_where = (
                "AND co.childref_private = 0\n"
                "          AND COALESCE(pp.private, 0) = 0\n"
                "          AND COALESCE(ff.private, 0) = 0"
            )

        # `child_of` here is a session temp table (see ensure_child_of),
        # not yet the permanent trigger-maintained table described in the
        # module docstring. It has no `treeid` column of its own:
        # ensure_child_of already scopes it to the current tree at build
        # time via a tree clause against the source `family` table, so
        # every row in it already belongs to this tree.
        #
        # The privacy predicate is applied BOTH inside the recursive term
        # (so a private link is never traversed) AND again on the final
        # edge SELECT: "child is in the privacy-safe `anc` set" only proves
        # that handle is *reachable* via a safe path, not that every one of
        # its own edges is safe to hand back -- omitting the second filter
        # lets an unrelated private edge leak into the Python-side BFS.
        query = f"""
        WITH RECURSIVE anc(handle) AS (
            SELECT ?{d.param_cast}
            UNION
            SELECT co.parent
            FROM anc
            JOIN child_of co ON co.child = anc.handle
            {privacy_join}
            WHERE 1=1 {privacy_where}
        )
        SELECT co.parent, co.child, co.code, co.relvalue
        FROM child_of co
        {privacy_join}
        WHERE co.child IN (SELECT handle FROM anc)
        {privacy_where}
        """
        edges = self._execute(query, (handle,))

        parent_of: dict[str, list[tuple[str, str, int]]] = {}
        for parent, child, code, relvalue in edges:
            parent_of.setdefault(child, []).append((parent, code, relvalue))

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
        """(Re)build the session temp table `ancestor_map` reads from. Call
        once per logical operation (relationship()/all_relationships()
        already do this) before the first `ancestor_map`/`check_spouse`
        call. Always drops and rebuilds rather than reusing an existing
        temp table: if the underlying DB connection is reused across
        multiple calls (common when `execute` wraps a pooled or long-lived
        connection), a temp table left over from an earlier call would
        otherwise either collide (`CREATE TEMP TABLE` without `IF NOT
        EXISTS` fails outright) or, worse, silently serve data that's gone
        stale since. See the module docstring for the trigger-maintained-
        table upgrade that would remove this per-call rebuild cost."""
        d = self._dialect
        t = _tree_clause("f", self._treeid)
        self._execute("DROP TABLE IF EXISTS child_of", [])
        self._execute(
            f"""
            CREATE TEMP TABLE child_of AS
            SELECT f.handle AS family_handle, f.father_handle AS parent,
                   {d.ref_expr} AS child,
                   CASE WHEN {d.frel_expr} = 1 THEN 'f' ELSE 'F' END AS code,
                   {d.frel_expr} AS relvalue,
                   {d.childref_private_expr} AS childref_private
            FROM family f
            {d.child_ref_from}
            WHERE f.father_handle IS NOT NULL {t}
            UNION ALL
            SELECT f.handle, f.mother_handle,
                   {d.ref_expr},
                   CASE WHEN {d.mrel_expr} = 1 THEN 'm' ELSE 'M' END,
                   {d.mrel_expr},
                   {d.childref_private_expr}
            FROM family f
            {d.child_ref_from}
            WHERE f.mother_handle IS NOT NULL {t}
            """,
            [],
        )
        self._execute("CREATE INDEX idx_child_of_child ON child_of(child)", [])

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

    # -- shared wording helper ---------------------------------------------

    def _string_for_ancestor(self, h1, h2, anc, dist1, path1, pm1, dist2, path2, pm2, gender1, gender2) -> str:
        """Relationship wording for one specific common ancestor `anc`.
        Shared by `relationship()` (single best answer) and
        `all_relationships()` (every distinct answer) -- both reduce to
        "given a chosen ancestor, say the relationship it produces"."""
        Ga, Gb = dist1[anc], dist2[anc]
        if Ga == 1 and Gb == 1:
            sib = self.sibling_type(h1, h2, pm1, pm2)
            return self._calc.get_sibling_relationship_string(sib, gender1, gender2)
        only_birth = _is_birth_path(path1[anc]) and _is_birth_path(path2[anc])
        return self._calc.get_single_relationship_string(
            Ga, Gb, gender1, gender2, path1[anc], path2[anc],
            only_birth=only_birth, in_law_a=False, in_law_b=False,
        )

    # -- top-level entry points ---------------------------------------------

    def relationship(self, h1: str, h2: str, restricted: bool = False, depth: int = 15):
        """Return (relationship_string, distance_common_origin,
        distance_common_other) -- the single most-direct relationship."""
        if h1 == h2:
            return "", -1, -1

        self.ensure_child_of()

        spouse = self.check_spouse(h1, h2, restricted)
        if spouse is not None:
            spouse_type, gender1, gender2 = spouse
            rel_str = self._calc.get_partner_relationship_string(spouse_type, gender1, gender2)
            return rel_str, -1, -1

        m1 = self.ancestor_map(h1, restricted, max_depth=depth)
        m2 = self.ancestor_map(h2, restricted, max_depth=depth)
        dist1, path1, pm1 = m1["dist"], m1["path"], m1["parent_of"]
        dist2, path2, pm2 = m2["dist"], m2["path"], m2["parent_of"]
        common = set(dist1) & set(dist2)
        if not common:
            return "", -1, -1

        best = self._best_common_ancestor(dist1, path1, dist2, path2, common)
        Ga, Gb = dist1[best], dist2[best]
        gender1, gender2 = self.gender(h1), self.gender(h2)
        rel_str = self._string_for_ancestor(h1, h2, best, dist1, path1, pm1, dist2, path2, pm2, gender1, gender2)
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
        dist1, path1, pm1 = m1["dist"], m1["path"], m1["parent_of"]
        dist2, path2, pm2 = m2["dist"], m2["path"], m2["parent_of"]
        common = set(dist1) & set(dist2)
        if not common:
            return result or [{}]

        gender1, gender2 = self.gender(h1), self.gender(h2)
        # nearest relationship first, matching "relstrings is ordered on
        # rank automatic" in gramps-core's own get_all_relationships
        for anc in sorted(common, key=lambda h: dist1[h] + dist2[h]):
            rel_str = self._string_for_ancestor(h1, h2, anc, dist1, path1, pm1, dist2, path2, pm2, gender1, gender2)
            if rel_str in seen:
                result[seen[rel_str]]["common_ancestors"].append(anc)
            else:
                seen[rel_str] = len(result)
                result.append({"relationship_string": rel_str, "common_ancestors": [anc]})

        return result or [{}]

    def _relationship_to(self, h1: str, other: str, restricted: bool, depth: int, dist1, path1, pm1) -> str:
        """Relationship of `other` to `h1`, reusing `h1`'s already-built
        ancestor map (`dist1`/`path1`/`pm1`) rather than recomputing it --
        the piece of `relationship()` that's expensive per call. Only
        `other`'s own map is fetched fresh each call, since -- unlike
        `relationship_path`/`all_relationship_paths`, where every node is
        already known to sit on a specific, already-computed chain --
        `relationships_to` calls this for arbitrary target handles with
        no such shortcut available."""
        spouse = self.check_spouse(h1, other, restricted)
        if spouse is not None:
            spouse_type, gender1, gender2 = spouse
            return self._calc.get_partner_relationship_string(spouse_type, gender1, gender2)

        m2 = self.ancestor_map(other, restricted, max_depth=depth)
        dist2, path2, pm2 = m2["dist"], m2["path"], m2["parent_of"]
        common = set(dist1) & set(dist2)
        if not common:
            return ""

        anc = self._best_common_ancestor(dist1, path1, dist2, path2, common)
        gender1, gender2 = self.gender(h1), self.gender(other)
        return self._string_for_ancestor(h1, other, anc, dist1, path1, pm1, dist2, path2, pm2, gender1, gender2)

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

    def _label_via_ancestor(self, h1: str, other: str, anc: str, dist1, path1, pm1, dist2, path2, pm2, gender1: int) -> str:
        """Relationship to `h1` of `other`, an ancestor of `h2` (or `h2`
        itself) sitting on `h2`'s shortest BFS route to the specific
        common ancestor `anc`. `other`'s own up-path to `anc` is the tail
        of `anc`'s own `path2`/distance beyond `other`'s -- valid only
        because `other` sits on that exact route (true for every node
        `all_relationship_paths`/`relationship_path` ever call this for),
        not for an arbitrary pair of ancestors in the map."""
        Ga = dist1[anc]
        Gb = dist2[anc] - dist2[other]
        path_a = path1[anc]
        path_b = path2[anc][len(path2[other]):]
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

        self.ensure_child_of()

        result = [{"handle": h1, "relationship_string": ""}]

        spouse = self.check_spouse(h1, h2, restricted)
        if spouse is not None:
            spouse_type, gender1, gender2 = spouse
            rel_str = self._calc.get_partner_relationship_string(spouse_type, gender1, gender2)
            result.append({"handle": h2, "relationship_string": rel_str})
            return result

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
            rel_str = self._label_via_ancestor(h1, node, anc, dist1, path1, pm1, dist2, path2, pm2, gender1)
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
                rel_str = self._label_via_ancestor(h1, node, anc, dist1, path1, pm1, dist2, path2, pm2, gender1)
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
        dist1, path1, pm1 = m1["dist"], m1["path"], m1["parent_of"]

        items = []
        for other in target_handles:
            if other == h1:
                rel_str = ""
            else:
                rel_str = self._relationship_to(h1, other, restricted, depth, dist1, path1, pm1)
            items.append({"handle": other, "relationship_string": rel_str})

        return {"items": items, "total": total, "page": page, "pagesize": pagesize}
