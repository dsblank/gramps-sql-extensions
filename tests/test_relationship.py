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


def test_relationship_path_expected_result(graph):
    path = graph.relationship_path("9BXKQC1PVLPYFMD6IX", "ORFKQC4KLWEGTGR19L")
    assert [node["handle"] for node in path] == [
        "9BXKQC1PVLPYFMD6IX",
        "HKTJQCIJD8RK9RJFO1",
        "TDTJQCGYRS2RCCGQN3",
        "DPUJQCUYKKDPT78JJV",
        "GNUJQCL9MD64AM56OH",
        "46WJQCIOLQ0KOX2XCC",
        "ORFKQC4KLWEGTGR19L",
    ]
    assert [node["relationship_string"] for node in path] == [
        "",
        "father",
        "grandfather",
        "great grandfather",
        "second great grandfather",
        "third great stepgrandmother",
        "second great stepgrandaunt",
    ]
    # the last entry always matches relationship()'s own answer
    rel_str, _, _ = graph.relationship("9BXKQC1PVLPYFMD6IX", "ORFKQC4KLWEGTGR19L")
    assert path[-1]["relationship_string"] == rel_str


def test_relationship_path_same_person(graph):
    assert graph.relationship_path("9BXKQC1PVLPYFMD6IX", "9BXKQC1PVLPYFMD6IX") == [
        {"handle": "9BXKQC1PVLPYFMD6IX", "relationship_string": ""}
    ]


def test_relationship_path_unrelated(graph):
    assert graph.relationship_path("9BXKQC1PVLPYFMD6IX", "ORFKQC4KLWEGTGR19L", depth=5) == []


def test_relationship_path_partner(graph):
    path = graph.relationship_path("cc8205d87831c772e87", "cc8205d872f532ab14e")
    assert path == [
        {"handle": "cc8205d87831c772e87", "relationship_string": ""},
        {"handle": "cc8205d872f532ab14e", "relationship_string": "husband"},
    ]


def test_all_relationship_paths_expected_result(graph):
    h1, h2 = "9BXKQC1PVLPYFMD6IX", "ORFKQC4KLWEGTGR19L"
    paths = graph.all_relationship_paths(h1, h2)
    # 6 distinct common ancestors -> 6 distinct paths, matching the 6
    # ancestors all_relationships() groups into its 2 wording buckets
    assert len(paths) == 6
    # nearest-first, and the very first path always matches the single
    # "best" answer relationship_path()/relationship() report
    assert paths[0] == graph.relationship_path(h1, h2)
    assert paths[0][-1]["relationship_string"] == graph.relationship(h1, h2)[0]
    # every path starts at h1 (with the trivial self entry) and ends at h2
    for path in paths:
        assert path[0] == {"handle": h1, "relationship_string": ""}
        assert path[-1]["handle"] == h2


def test_all_relationship_paths_max_paths(graph):
    h1, h2 = "9BXKQC1PVLPYFMD6IX", "ORFKQC4KLWEGTGR19L"
    full = graph.all_relationship_paths(h1, h2)
    capped = graph.all_relationship_paths(h1, h2, max_paths=2)
    assert capped == full[:2]


def test_all_relationship_paths_order_is_deterministic(graph):
    """Ancestor ordering must not depend on `set` iteration order (which
    is hash-seed-dependent) -- repeated calls must agree."""
    h1, h2 = "9BXKQC1PVLPYFMD6IX", "ORFKQC4KLWEGTGR19L"
    first = graph.all_relationship_paths(h1, h2)
    for _ in range(5):
        assert graph.all_relationship_paths(h1, h2) == first


def test_all_relationship_paths_same_person(graph):
    assert graph.all_relationship_paths("9BXKQC1PVLPYFMD6IX", "9BXKQC1PVLPYFMD6IX") == [
        [{"handle": "9BXKQC1PVLPYFMD6IX", "relationship_string": ""}]
    ]


def test_all_relationship_paths_unrelated(graph):
    assert graph.all_relationship_paths("9BXKQC1PVLPYFMD6IX", "ORFKQC4KLWEGTGR19L", depth=5) == []


def test_all_relationship_paths_partner(graph):
    paths = graph.all_relationship_paths("cc8205d87831c772e87", "cc8205d872f532ab14e")
    assert paths == [
        [
            {"handle": "cc8205d87831c772e87", "relationship_string": ""},
            {"handle": "cc8205d872f532ab14e", "relationship_string": "husband"},
        ]
    ]


def test_relationships_to_explicit_handles(graph):
    h1, h2 = "9BXKQC1PVLPYFMD6IX", "ORFKQC4KLWEGTGR19L"
    result = graph.relationships_to(h1, handles=[h1, h2, "does-not-exist"])
    # the nonexistent handle is silently skipped, matching gramps-web-api's
    # own "non-existing handles are silently skipped" `handles` param
    assert result == {
        "items": [
            {"handle": h1, "relationship_string": ""},
            {"handle": h2, "relationship_string": "second great stepgrandaunt"},
        ],
        "total": 2,
        "page": 0,
        "pagesize": 20,
    }


def test_relationships_to_unrelated_is_empty_string(graph):
    h1 = "9BXKQC1PVLPYFMD6IX"
    result = graph.relationships_to(h1, handles=[h1, "004KQCGYT27EEPQHK"])
    assert result["items"][1]["relationship_string"] == ""
    # "" here means "not related within depth", same as relationship()'s
    # own return for this exact pair
    assert graph.relationship(h1, "004KQCGYT27EEPQHK")[0] == ""


def test_relationships_to_all_people_is_paged(graph):
    h1 = "9BXKQC1PVLPYFMD6IX"
    page1 = graph.relationships_to(h1, page=1, pagesize=5)
    page2 = graph.relationships_to(h1, page=2, pagesize=5)
    assert page1["page"] == 1 and page1["pagesize"] == 5
    assert len(page1["items"]) == 5
    assert len(page2["items"]) == 5
    assert page1["items"] != page2["items"]
    # total reflects every person in the tree, not just this page
    assert page1["total"] == page2["total"] > 100
    # handles=None with page=0 (the default) means "everything", matching
    # gramps-web-api's own page=0 contract -- verify by grabbing everyone
    # via a large page and comparing against relationship() directly
    everyone = graph.relationships_to(h1, page=1, pagesize=page1["total"])["items"]
    sample = everyone[10]
    if sample["handle"] != h1:
        assert graph.relationship(h1, sample["handle"])[0] == sample["relationship_string"]


def test_relationships_to_page_zero_returns_everything(graph):
    h1, h2 = "9BXKQC1PVLPYFMD6IX", "ORFKQC4KLWEGTGR19L"
    result = graph.relationships_to(h1, handles=[h1, h2])
    assert result["page"] == 0
    assert len(result["items"]) == result["total"] == 2


def test_ancestor_map_returns_dict_with_expected_keys(graph):
    """ancestor_map() returns a dict, not a positional tuple, precisely so
    a future added key (e.g. gender, family_handle) can't silently break
    a caller unpacking it -- lock down the current key set."""
    h1 = "9BXKQC1PVLPYFMD6IX"
    graph.ensure_child_of()
    m = graph.ancestor_map(h1, restricted=False)
    assert isinstance(m, dict)
    assert set(m.keys()) == {"dist", "path", "prev", "parent_of"}


def test_ancestor_map_root_entry(graph):
    h1 = "9BXKQC1PVLPYFMD6IX"
    graph.ensure_child_of()
    m = graph.ancestor_map(h1, restricted=False)
    assert m["dist"][h1] == 0
    assert m["path"][h1] == ""
    assert m["prev"][h1] is None


def test_ancestor_map_direct_parent(graph):
    """The known father from the relationship_path() chain tests: one
    generation up, path code 'f' (birth father), discovered from h1."""
    h1 = "9BXKQC1PVLPYFMD6IX"
    father = "HKTJQCIJD8RK9RJFO1"
    graph.ensure_child_of()
    m = graph.ancestor_map(h1, restricted=False)
    assert m["dist"][father] == 1
    assert m["path"][father] == "f"
    assert m["prev"][father] == h1
    assert (father, "f", 1) in m["parent_of"][h1]


def test_ancestor_map_depth_boundary(graph):
    """Matches relationship()'s own depth semantics: max_depth=1 finds
    only the root itself (dist=0), max_depth=2 also finds its direct
    parents (dist=1) but no further."""
    h1 = "9BXKQC1PVLPYFMD6IX"
    graph.ensure_child_of()
    m1 = graph.ancestor_map(h1, restricted=False, max_depth=1)
    assert set(m1["dist"].values()) == {0}
    m2 = graph.ancestor_map(h1, restricted=False, max_depth=2)
    assert set(m2["dist"].values()) == {0, 1}


def test_ancestor_map_respects_privacy(privacy_graph):
    graph, h = privacy_graph
    graph.ensure_child_of()
    m_open = graph.ancestor_map(h["child1"], restricted=False)
    m_restricted = graph.ancestor_map(h["child1"], restricted=True)
    assert h["mother1"] in m_open["dist"]
    assert h["mother1"] not in m_restricted["dist"]
    # the other, non-private parent stays visible either way
    assert h["father1"] in m_open["dist"]
    assert h["father1"] in m_restricted["dist"]


def test_reused_connection_across_calls(graph):
    """The same RelationshipGraph/connection answering multiple queries in
    a row is exactly the case that broke before ensure_child_of() was made
    to always DROP + rebuild rather than assume a fresh connection."""
    first = graph.relationship("9BXKQC1PVLPYFMD6IX", "ORFKQC4KLWEGTGR19L")
    second = graph.relationship("cc8205d87831c772e87", "cc8205d872f532ab14e")
    assert first[0] == "second great stepgrandaunt"
    assert second[0] == "husband"
