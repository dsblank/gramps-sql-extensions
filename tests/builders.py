"""Shared helpers for constructing small, purpose-built test databases via
Gramps' own object model, used by tests that need a specific structural
shape (a tie between two common ancestors, a couple with more than one
family record, siblings of a specific type) rather than whatever
example.gramps happens to contain.

Not a pytest fixture module itself -- individual test files build their
own fixtures on top of these, since each needs a differently-shaped
database.
"""

from gramps.gen.db import DbTxn
from gramps.gen.lib import ChildRef, ChildRefType, Family, Person
from gramps.gen.utils.id import create_id

BIRTH = ChildRefType.BIRTH
STEPCHILD = ChildRefType.STEPCHILD


def new_person(db, gender: int) -> str:
    person = Person()
    person.set_handle(create_id())
    person.set_gender(gender)
    with DbTxn("add person", db) as trans:
        db.add_person(person, trans)
    return person.handle


def new_family(db, father, mother, children=(), rel_type=None) -> str:
    """`children`: list of (handle, father_relation, mother_relation)
    triples -- both relation types default to BIRTH if omitted via
    `new_family(db, f, m, [(child, None, None)])`... no default shorthand,
    always pass both explicitly (BIRTH/STEPCHILD/etc.) so the test reading
    it says exactly what it means."""
    family = Family()
    family.set_handle(create_id())
    if rel_type is not None:
        family.set_relationship(rel_type)
    if father:
        family.set_father_handle(father)
    if mother:
        family.set_mother_handle(mother)
    for child_handle, frel, mrel in children:
        ref = ChildRef()
        ref.set_reference_handle(child_handle)
        ref.set_father_relation(frel)
        ref.set_mother_relation(mrel)
        family.add_child_ref(ref)
    with DbTxn("add family", db) as trans:
        db.add_family(family, trans)
    return family.handle


def link_person_to_family(db, person_handle: str, family_handle: str) -> None:
    """`add_family()` alone doesn't update the referenced persons' own
    `family_list` (that reciprocal update is application-layer behavior,
    not something gramps-core's raw add_family/commit_family does) --
    call this too whenever a test relies on family_list order, e.g. the
    "which of several marriages between the same couple wins" case."""
    with DbTxn("link person to family", db) as trans:
        person = db.get_person_from_handle(person_handle)
        person.add_family_handle(family_handle)
        db.commit_person(person, trans)
