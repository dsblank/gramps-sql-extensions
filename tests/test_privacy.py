"""Tests for the `restricted` privacy-filtering path, mirroring gramps-
core's PrivateProxyDb: a private person, a private family, or a private
ChildRef are each individually invisible to a restricted viewer, and
nothing else should be. See conftest.py's `privacy_db` for the three
minimal cases constructed for this (example.gramps doesn't reliably
exercise any of them, so this doesn't rely on finding one there).
"""


def test_private_person_hidden_when_restricted(privacy_graph):
    graph, h = privacy_graph
    # the private parent (mother1) is invisible to a restricted viewer
    unrestricted, _, _ = graph.relationship(h["child1"], h["mother1"], restricted=False)
    restricted, _, _ = graph.relationship(h["child1"], h["mother1"], restricted=True)
    assert unrestricted == "mother"
    assert restricted == ""


def test_private_person_does_not_hide_other_parent(privacy_graph):
    graph, h = privacy_graph
    # the *other* parent (father1) isn't private and must stay visible,
    # even though mother1 in the same family is
    unrestricted, _, _ = graph.relationship(h["child1"], h["father1"], restricted=False)
    restricted, _, _ = graph.relationship(h["child1"], h["father1"], restricted=True)
    assert unrestricted == restricted == "father"


def test_private_family_hidden_when_restricted(privacy_graph):
    graph, h = privacy_graph
    unrestricted, _, _ = graph.relationship(h["child2"], h["father2"], restricted=False)
    restricted, _, _ = graph.relationship(h["child2"], h["father2"], restricted=True)
    assert unrestricted == "father"
    assert restricted == ""


def test_private_childref_hidden_when_restricted(privacy_graph):
    graph, h = privacy_graph
    unrestricted, _, _ = graph.relationship(h["child3"], h["father3"], restricted=False)
    restricted, _, _ = graph.relationship(h["child3"], h["father3"], restricted=True)
    assert unrestricted == "father"
    assert restricted == ""


def test_all_relationships_respects_privacy(privacy_graph):
    graph, h = privacy_graph
    assert graph.all_relationships(h["child1"], h["mother1"], restricted=True) == [{}]
    assert graph.all_relationships(h["child1"], h["mother1"], restricted=False)[0][
        "relationship_string"
    ] == "mother"


def test_relationship_path_respects_privacy(privacy_graph):
    graph, h = privacy_graph
    assert graph.relationship_path(h["child1"], h["mother1"], restricted=True) == []
    unrestricted = graph.relationship_path(h["child1"], h["mother1"], restricted=False)
    assert [node["handle"] for node in unrestricted] == [h["child1"], h["mother1"]]
    assert unrestricted[-1]["relationship_string"] == "mother"


def test_all_relationship_paths_respects_privacy(privacy_graph):
    graph, h = privacy_graph
    assert graph.all_relationship_paths(h["child1"], h["mother1"], restricted=True) == []
    unrestricted = graph.all_relationship_paths(h["child1"], h["mother1"], restricted=False)
    assert len(unrestricted) == 1
    assert unrestricted[0][-1]["relationship_string"] == "mother"


def test_relationships_to_hides_private_target_person(privacy_graph):
    """mother1 is herself a private Person (not just a private link), so
    a restricted caller shouldn't see her as a target at all -- silently
    dropped from an explicit `handles` list, and absent from the
    `handles=None` "everyone" listing, same as PrivateProxyDb hiding a
    private Person object entirely from a listing endpoint."""
    graph, h = privacy_graph

    explicit = graph.relationships_to(
        h["child1"], handles=[h["father1"], h["mother1"]], restricted=True
    )
    assert [item["handle"] for item in explicit["items"]] == [h["father1"]]
    assert explicit["total"] == 1

    unrestricted_explicit = graph.relationships_to(
        h["child1"], handles=[h["father1"], h["mother1"]], restricted=False
    )
    assert {item["handle"] for item in unrestricted_explicit["items"]} == {
        h["father1"],
        h["mother1"],
    }

    everyone_restricted = graph.relationships_to(h["child1"], restricted=True)
    everyone_unrestricted = graph.relationships_to(h["child1"], restricted=False)
    assert h["mother1"] not in {item["handle"] for item in everyone_restricted["items"]}
    assert h["mother1"] in {item["handle"] for item in everyone_unrestricted["items"]}
    assert everyone_restricted["total"] < everyone_unrestricted["total"]
