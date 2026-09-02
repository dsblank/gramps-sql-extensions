"""Smoke tests for RelationshipGraph against a real SQLite fixture.

Not yet a proper standalone fixture for this package (uses a path into a
sibling project's dev fixture data) -- placeholder until this package has
its own small test database. Skips cleanly if that fixture isn't present
(e.g. in CI, or on a machine that doesn't have it checked out).
"""

import os
import sqlite3

import pytest

from gramps_sql_extensions import RelationshipGraph

_DB = os.path.expanduser(
    "~/gramps/gramps-connect/dev-fixtures/scale-100k/api-fixture/gramps-home/"
    "gramps/grampsdb/77ca9109-07b0-416a-9f3e-7ff0bebd3c52/sqlite.db"
)


def _make_graph():
    con = sqlite3.connect(f"file:{_DB}?mode=ro", uri=True)

    def execute(sql, params):
        cur = con.cursor()
        cur.execute(sql, params)
        try:
            return cur.fetchall()
        except Exception:
            return []

    return RelationshipGraph(execute, dialect="sqlite")


pytestmark = pytest.mark.skipif(not os.path.exists(_DB), reason="dev fixture not present")


def test_spouse():
    graph = _make_graph()
    rel_str, Ga, Gb = graph.relationship(
        "1012c4672d5f3b65871e49e64094", "1012c4672d6a2dc5b6afdeb5c4a0"
    )
    assert rel_str == "wife"
    assert (Ga, Gb) == (-1, -1)


def test_sibling():
    graph = _make_graph()
    rel_str, Ga, Gb = graph.relationship(
        "1012c4672d7e40687b0e48025641", "1012c4672d8d6bdc2dd100142bf1"
    )
    assert rel_str == "sister"
    assert (Ga, Gb) == (1, 1)


def test_cousin():
    graph = _make_graph()
    rel_str, Ga, Gb = graph.relationship(
        "1012c47b0e3120eb76e0394cef84", "1012c4762b7415fa21f9d9c33ea5"
    )
    assert rel_str == "tenth stepcousin"
    assert (Ga, Gb) == (11, 11)


def test_all_relationships_reuses_connection():
    """The same RelationshipGraph/connection answering a second query in a
    row is exactly the case that broke before ensure_child_of() was made
    to always DROP + rebuild rather than assume a fresh connection."""
    graph = _make_graph()
    graph.relationship("1012c4672d5f3b65871e49e64094", "1012c4672d6a2dc5b6afdeb5c4a0")
    result = graph.all_relationships(
        "1012c47b0e3120eb76e0394cef84", "1012c4762b7415fa21f9d9c33ea5"
    )
    assert result[0]["relationship_string"] == "tenth stepcousin"
