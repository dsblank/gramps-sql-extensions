"""Tests for the two trickiest, most bug-prone pieces of RelationshipGraph,
each purpose-built (see builders.py) rather than hoping to find the shape
in example.gramps: the pedigree-collapse tie-break, and same-couple
remarriage ordering. Also covers half/step sibling wording, another case
easy to get subtly wrong.

Both the tie-break and remarriage cases here are real bugs this
implementation shipped with at one point and then fixed -- kept as tests
so they can't come back silently.
"""

from gramps.gen.db.utils import make_database
from gramps.gen.lib import FamilyRelType, Person

from gramps_sql_extensions import RelationshipGraph

from builders import BIRTH, STEPCHILD, link_person_to_family, new_family, new_person


def _execute_for(db):
    def execute(sql: str, params: list) -> list[tuple]:
        db.dbapi.execute(sql, params)
        try:
            return db.dbapi.fetchall()
        except Exception:
            return []

    return execute


def _new_db():
    db = make_database("sqlite")
    db.load(":memory:")
    return db


def test_pedigree_collapse_tie_break_prefers_birth_line():
    """Two candidate common ancestors tied at the same generation distance,
    one reached by an all-birth path, the other through a step link --
    gramps-core's own priority order prefers the birth-line one, so the
    result should be "first cousin", never "first stepcousin", even
    though the tie-break has no reason to pick one over the other by
    generation distance alone."""
    db = _new_db()
    MALE, FEMALE = Person.MALE, Person.FEMALE

    h1, h2 = new_person(db, MALE), new_person(db, FEMALE)
    f1, m1 = new_person(db, MALE), new_person(db, FEMALE)
    f2, m2 = new_person(db, MALE), new_person(db, FEMALE)
    ancestor_birth = new_person(db, MALE)  # reached by an all-birth path on both sides
    ancestor_step = new_person(db, FEMALE)  # reached via a step link on h1's side

    new_family(db, ancestor_birth, None, [(f1, BIRTH, BIRTH), (f2, BIRTH, BIRTH)])
    new_family(db, None, ancestor_step, [(m1, STEPCHILD, STEPCHILD), (m2, BIRTH, BIRTH)])
    new_family(db, f1, m1, [(h1, BIRTH, BIRTH)])
    new_family(db, f2, m2, [(h2, BIRTH, BIRTH)])

    graph = RelationshipGraph(_execute_for(db), dialect="sqlite")
    rel_str, Ga, Gb = graph.relationship(h1, h2)
    assert rel_str == "first cousin"
    assert (Ga, Gb) == (2, 2)


def test_remarriage_uses_last_family_in_family_list_order():
    """The same couple recorded in two family records (e.g. an unmarried-
    partner record later formalized by marriage) -- gramps-core's
    _get_spouse_type takes the LAST match in the person's own family_list
    order, not an arbitrary one, so the wording must reflect the second
    (married) record, not the first (unmarried) one."""
    db = _new_db()
    MALE, FEMALE = Person.MALE, Person.FEMALE
    husband, wife = new_person(db, MALE), new_person(db, FEMALE)

    fam1 = new_family(db, husband, wife, rel_type=FamilyRelType.UNMARRIED)
    link_person_to_family(db, husband, fam1)
    fam2 = new_family(db, husband, wife, rel_type=FamilyRelType.MARRIED)
    link_person_to_family(db, husband, fam2)

    graph = RelationshipGraph(_execute_for(db), dialect="sqlite")
    rel_str, Ga, Gb = graph.relationship(husband, wife)
    assert rel_str == "wife"  # not "partner" (fam1's wording), fam2 is last in family_list
    assert (Ga, Gb) == (-1, -1)


def test_half_sibling_wording():
    db = _new_db()
    MALE, FEMALE = Person.MALE, Person.FEMALE
    father = new_person(db, MALE)
    mother1, mother2 = new_person(db, FEMALE), new_person(db, FEMALE)
    full1, full2 = new_person(db, MALE), new_person(db, FEMALE)
    half = new_person(db, MALE)

    new_family(db, father, mother1, [(full1, BIRTH, BIRTH), (full2, BIRTH, BIRTH)])
    new_family(db, father, mother2, [(half, BIRTH, BIRTH)])

    graph = RelationshipGraph(_execute_for(db), dialect="sqlite")
    assert graph.relationship(full1, full2)[0] == "sister"
    assert graph.relationship(full1, half)[0] == "half-brother"
