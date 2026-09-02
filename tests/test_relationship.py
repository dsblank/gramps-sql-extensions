"""Tests for RelationshipGraph, self-contained: a real SQLite database
built from Gramps' own bundled example.gramps (see conftest.py), no
gramps-web-api, no external fixtures, nothing left behind.

Several expectations here (handles, exact wording, depth boundary,
translated strings) are the same ones gramps-web-api's own
tests/test_endpoints/test_relations.py validates against the real
gramps.gen.relationship.RelationshipCalculator on this same
example.gramps file -- reused here as ground truth for this
independent implementation, not copied from that test suite itself.
"""


def test_relationship_expected_result(graph):
    rel_str, dist_a, dist_b = graph.relationship(
        "9BXKQC1PVLPYFMD6IX", "ORFKQC4KLWEGTGR19L"
    )
    assert rel_str == "second great stepgrandaunt"
    assert (dist_a, dist_b) == (5, 1)


def test_relationship_same_person(graph):
    assert graph.relationship("9BXKQC1PVLPYFMD6IX", "9BXKQC1PVLPYFMD6IX") == ("", -1, -1)


def test_relationship_depth_boundary(graph):
    """gramps-core's own depth cutoff excludes a generation once it
    reaches exactly `depth`, not after it -- this pair's common ancestor
    sits at generation 5, so depth=5 must NOT find it, depth=6 must."""
    rel_str, _, _ = graph.relationship(
        "9BXKQC1PVLPYFMD6IX", "ORFKQC4KLWEGTGR19L", depth=5
    )
    assert rel_str == ""
    rel_str, _, _ = graph.relationship(
        "9BXKQC1PVLPYFMD6IX", "ORFKQC4KLWEGTGR19L", depth=6
    )
    assert rel_str == "second great stepgrandaunt"


def test_relationship_locale(make_graph):
    graph_de = make_graph("de_DE.UTF-8")
    rel_str, _, _ = graph_de.relationship("9BXKQC1PVLPYFMD6IX", "ORFKQC4KLWEGTGR19L")
    assert rel_str == "Stief-/Adoptivalttante"


def test_relationship_partner(graph):
    rel_str, dist_a, dist_b = graph.relationship(
        "cc8205d87831c772e87", "cc8205d872f532ab14e"
    )
    assert rel_str == "husband"
    assert (dist_a, dist_b) == (-1, -1)


def test_relationship_partner_locale(make_graph):
    graph_it = make_graph("it_IT.UTF-8")
    rel_str, _, _ = graph_it.relationship("cc8205d87831c772e87", "cc8205d872f532ab14e")
    assert rel_str == "marito"


def test_all_relationships_expected_result(graph):
    result = graph.all_relationships("9BXKQC1PVLPYFMD6IX", "ORFKQC4KLWEGTGR19L")
    assert "common_ancestors" in result[0]
    assert result[0]["relationship_string"] == "second great stepgrandaunt"


def test_all_relationships_same_person(graph):
    assert graph.all_relationships("9BXKQC1PVLPYFMD6IX", "9BXKQC1PVLPYFMD6IX") == [{}]


def test_all_relationships_depth_boundary(graph):
    result = graph.all_relationships("9BXKQC1PVLPYFMD6IX", "ORFKQC4KLWEGTGR19L", depth=5)
    assert result == [{}]
    result = graph.all_relationships("9BXKQC1PVLPYFMD6IX", "ORFKQC4KLWEGTGR19L", depth=6)
    assert result[0]["relationship_string"] == "second great stepgrandaunt"


def test_all_relationships_locale(make_graph):
    graph_de = make_graph("de_DE.UTF-8")
    result = graph_de.all_relationships("9BXKQC1PVLPYFMD6IX", "ORFKQC4KLWEGTGR19L")
    assert result[0]["relationship_string"] == "Stief-/Adoptivalttante"


def test_reused_connection_across_calls(graph):
    """The same RelationshipGraph/connection answering multiple queries in
    a row is exactly the case that broke before ensure_child_of() was made
    to always DROP + rebuild rather than assume a fresh connection."""
    first = graph.relationship("9BXKQC1PVLPYFMD6IX", "ORFKQC4KLWEGTGR19L")
    second = graph.relationship("cc8205d87831c772e87", "cc8205d872f532ab14e")
    assert first[0] == "second great stepgrandaunt"
    assert second[0] == "husband"
