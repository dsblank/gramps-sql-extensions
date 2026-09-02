"""Install a fake `gi` module before Gramps is ever imported.

`gramps.gen.const` unconditionally does `from gi.repository import GLib`,
just to compute a few XDG-style user directories -- this package never
touches real GTK, and CI has no GTK/GObject-introspection system packages
installed. Rather than pulling in PyGObject and its system-level build
dependencies (girepository dev headers, a C compiler, etc.) just to
satisfy an import this package's actual code path never exercises, stub
the four `GLib` calls `gramps/gen/const.py` makes and move on.

Must run before `import gramps` anywhere -- imported as the first line of
conftest.py for that reason. If `gi` is already a real, importable module
(a dev machine with PyGObject installed), this does nothing and gramps
uses the real thing instead.
"""

import os
import sys
import types


def install() -> None:
    try:
        import gi  # noqa: F401  -- real PyGObject already available, nothing to do

        return
    except ImportError:
        pass

    def _xdg(env_var: str, fallback_rel: str) -> str:
        return os.environ.get(env_var) or os.path.join(os.path.expanduser("~"), fallback_rel)

    glib = types.ModuleType("gi.repository.GLib")
    glib.get_user_data_dir = lambda: _xdg("XDG_DATA_HOME", os.path.join(".local", "share"))
    glib.get_user_config_dir = lambda: _xdg("XDG_CONFIG_HOME", ".config")
    glib.get_user_cache_dir = lambda: _xdg("XDG_CACHE_HOME", ".cache")
    glib.get_user_special_dir = lambda _directory: None  # gen/const.py handles None

    class UserDirectory:
        DIRECTORY_PICTURES = "PICTURES"

    glib.UserDirectory = UserDirectory

    repository = types.ModuleType("gi.repository")
    repository.GLib = glib

    gi_module = types.ModuleType("gi")
    gi_module.require_version = lambda namespace, version: None
    gi_module.repository = repository

    sys.modules["gi"] = gi_module
    sys.modules["gi.repository"] = repository
    sys.modules["gi.repository.GLib"] = glib


install()
