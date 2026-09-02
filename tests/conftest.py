"""Shared test fixture: an in-memory SQLite database loaded from Gramps'
own bundled example.gramps, independent of any caller (no gramps-web-api,
no external dev fixtures, no network, no files left behind).

example.gramps ships inside the `gramps` package's own resource data --
this package already depends on `gramps`, so it's available wherever this
package is installed.
"""

import os

import pytest
from gramps.gen.db import DbTxn
from gramps.gen.db.utils import make_database
from gramps.gen.lib import ChildRef, ChildRefType, Family, Person
from gramps.gen.user import User
from gramps.gen.utils.grampslocale import GrampsLocale
from gramps.gen.utils.id import create_id
from gramps.gen.utils.resourcepath import ResourcePath
from gramps.plugins.importer.importxml import importData

from gramps_sql_extensions import RelationshipGraph

_resources = ResourcePath()


def _example_gramps_path() -> str:
    path = os.path.join(_resources.doc_dir, "example", "gramps", "example.gramps")
    if not os.path.isfile(path):
        raise FileNotFoundError(
            f"example.gramps not found at {path} -- is `gramps` installed correctly?"
        )
    return path


def locale(lang: str) -> GrampsLocale:
    """A GrampsLocale for `lang` (e.g. 'de_DE.UTF-8') that resolves
    translations via the installed `gramps` package's own locale
    directory explicitly, rather than relying on GRAMPS_RESOURCES or any
    other environment variable being set before the test run."""
    return GrampsLocale(lang=lang, localedir=_resources.locale_dir)


@pytest.fixture(scope="session")
def example_db():
    """A real, SQLite-backed, in-memory Gramps database loaded from
    example.gramps. Session-scoped: importing ~2,150 people isn't free,
    so every test in the run shares one import rather than repeating it."""
    db = make_database("sqlite")
    db.load(":memory:")
    importData(db, _example_gramps_path(), User())
    yield db
    db.close()


@pytest.fixture(scope="session")
def example_execute(example_db):
    """The `execute(sql, params) -> rows` adapter RelationshipGraph needs,
    wired to `example_db`'s own DB-API connection."""

    def execute(sql: str, params: list) -> list[tuple]:
        example_db.dbapi.execute(sql, params)
        try:
            return example_db.dbapi.fetchall()
        except Exception:
            return []  # DDL statements (CREATE/DROP/CREATE INDEX) have no rows

    return execute


@pytest.fixture
def graph(example_execute):
    """A fresh, English-locale RelationshipGraph per test, sharing the
    session-scoped connection/data above (RelationshipGraph itself is
    cheap to construct; it's the import that's expensive)."""
    return RelationshipGraph(example_execute, dialect="sqlite")


@pytest.fixture
def make_graph(example_execute):
    """Factory for a RelationshipGraph in a specific locale, for the
    tests that need one other than English."""

    def _make(lang: str) -> RelationshipGraph:
        return RelationshipGraph(example_execute, dialect="sqlite", locale=locale(lang))

    return _make


# ---------------------------------------------------------------------------
# A small, purpose-built database for privacy filtering: PrivateProxyDb's
# three rules are that a private person, a private family, or a private
# ChildRef are all invisible to a restricted viewer. example.gramps doesn't
# reliably exercise any of these, so this constructs one minimal case of
# each directly rather than hoping to find one in a general demo dataset.
# ---------------------------------------------------------------------------


def _new_person(db, gender: int, private: bool = False) -> str:
    person = Person()
    person.set_handle(create_id())
    person.set_gender(gender)
    person.set_privacy(private)
    with DbTxn("add person", db) as trans:
        db.add_person(person, trans)
    return person.handle


def _new_family(db, father: str, mother: str, children, family_private: bool = False) -> str:
    """`children`: list of (handle, childref_private) pairs."""
    family = Family()
    family.set_handle(create_id())
    family.set_privacy(family_private)
    if father:
        family.set_father_handle(father)
    if mother:
        family.set_mother_handle(mother)
    for child_handle, childref_private in children:
        ref = ChildRef()
        ref.set_reference_handle(child_handle)
        ref.set_father_relation(ChildRefType.BIRTH)
        ref.set_mother_relation(ChildRefType.BIRTH)
        ref.set_privacy(childref_private)
        family.add_child_ref(ref)
    with DbTxn("add family", db) as trans:
        db.add_family(family, trans)
    return family.handle


@pytest.fixture(scope="session")
def privacy_db():
    """Three small families, one exercising each of PrivateProxyDb's three
    privacy rules, plus one ordinary (non-private) link in the first family
    as a control -- restricted filtering shouldn't hide anything it isn't
    supposed to, not just hide what it is."""
    db = make_database("sqlite")
    db.load(":memory:")
    MALE, FEMALE = Person.MALE, Person.FEMALE

    # rule 1: a private person as a parent
    father1 = _new_person(db, MALE)
    mother1 = _new_person(db, FEMALE, private=True)
    child1 = _new_person(db, MALE)
    _new_family(db, father1, mother1, [(child1, False)])

    # rule 2: a private family (both parents public, family itself private)
    father2 = _new_person(db, MALE)
    mother2 = _new_person(db, FEMALE)
    child2 = _new_person(db, MALE)
    _new_family(db, father2, mother2, [(child2, False)], family_private=True)

    # rule 3: a private ChildRef (person/family public, just this link private)
    father3 = _new_person(db, MALE)
    mother3 = _new_person(db, FEMALE)
    child3 = _new_person(db, MALE)
    _new_family(db, father3, mother3, [(child3, True)])

    db.handles = {
        "father1": father1, "mother1": mother1, "child1": child1,
        "father2": father2, "mother2": mother2, "child2": child2,
        "father3": father3, "mother3": mother3, "child3": child3,
    }
    yield db
    db.close()


@pytest.fixture
def privacy_graph(privacy_db):
    def execute(sql: str, params: list) -> list[tuple]:
        privacy_db.dbapi.execute(sql, params)
        try:
            return privacy_db.dbapi.fetchall()
        except Exception:
            return []

    return RelationshipGraph(execute, dialect="sqlite"), privacy_db.handles
