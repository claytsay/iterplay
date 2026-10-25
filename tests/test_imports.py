"""Every module imports cleanly: no syntax errors, missing names or import cycles."""

import importlib
import pkgutil

import pytest

import iterplay

MODULES = sorted(
    module.name for module in pkgutil.walk_packages(iterplay.__path__, prefix="iterplay.")
)


@pytest.mark.parametrize("module", MODULES)
def test_module_imports(module: str) -> None:
    importlib.import_module(module)
