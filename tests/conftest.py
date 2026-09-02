"""Shared test fixture: an in-memory SQLite database loaded from Gramps'
own bundled example.gramps, independent of any caller (no gramps-web-api,
no external dev fixtures, no network, no files left behind).

example.gramps ships inside the `gramps` package's own resource data --
this package already depends on `gramps`, so it's available wherever this
package is installed.
"""

import os

import pytest
from gramps.gen.db.utils import make_database
from gramps.gen.user import User
from gramps.gen.utils.grampslocale import GrampsLocale
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
