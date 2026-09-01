from __future__ import annotations

import sys
import unittest
from pathlib import Path
from types import ModuleType

from module_stubs import injected_module, removed_module


class InjectedModuleTests(unittest.TestCase):
    def test_a_module_imported_inside_the_block_survives_it(self) -> None:
        """The whole reason this exists, and what `patch.dict` got wrong.

        `patch.dict(sys.modules, ...)` restores the entire mapping, so a module
        the code under test imported lazily while the stand-in was installed is
        evicted with it. Python 3.14 refuses to load an extension module twice,
        so the eviction breaks every later test reaching that import. See #28.
        """
        imported = "murmly_test_lazily_imported"
        self.addCleanup(sys.modules.pop, imported, None)

        with injected_module("murmly_test_stub", ModuleType("murmly_test_stub")):
            sys.modules[imported] = ModuleType(imported)

        self.assertIn(imported, sys.modules)

    def test_it_removes_a_name_that_was_not_there_before(self) -> None:
        name = "murmly_test_absent"
        self.assertNotIn(name, sys.modules)
        with injected_module(name, ModuleType(name)):
            self.assertIn(name, sys.modules)
        self.assertNotIn(name, sys.modules)

    def test_it_restores_the_module_it_displaced(self) -> None:
        name = "murmly_test_present"
        original = ModuleType(name)
        sys.modules[name] = original
        self.addCleanup(sys.modules.pop, name, None)

        with injected_module(name, ModuleType(name)):
            self.assertIsNot(original, sys.modules[name])

        self.assertIs(original, sys.modules[name])

    def test_a_none_entry_is_preserved_rather_than_treated_as_absent(self) -> None:
        """`sys.modules[name] = None` is how a test spells "importing this fails"."""
        name = "murmly_test_none"
        sys.modules[name] = None
        self.addCleanup(sys.modules.pop, name, None)

        with injected_module(name, ModuleType(name)):
            self.assertIsNotNone(sys.modules[name])

        self.assertIn(name, sys.modules)
        self.assertIsNone(sys.modules[name])

    def test_it_restores_after_the_block_raises(self) -> None:
        name = "murmly_test_raising"
        with self.assertRaises(RuntimeError):
            with injected_module(name, ModuleType(name)):
                raise RuntimeError("boom")
        self.assertNotIn(name, sys.modules)


class RemovedModuleTests(unittest.TestCase):
    def test_it_hides_a_module_and_puts_it_back(self) -> None:
        name = "murmly_test_hidden"
        original = ModuleType(name)
        sys.modules[name] = original
        self.addCleanup(sys.modules.pop, name, None)

        with removed_module(name):
            self.assertNotIn(name, sys.modules)

        self.assertIs(original, sys.modules[name])

    def test_hiding_a_name_that_was_never_there_leaves_it_absent(self) -> None:
        name = "murmly_test_never_there"
        with removed_module(name):
            self.assertNotIn(name, sys.modules)
        self.assertNotIn(name, sys.modules)

    def test_a_module_imported_inside_the_block_survives_it(self) -> None:
        imported = "murmly_test_imported_while_hidden"
        self.addCleanup(sys.modules.pop, imported, None)

        with removed_module("murmly_test_absent_hidden"):
            sys.modules[imported] = ModuleType(imported)

        self.assertIn(imported, sys.modules)


class TheSuiteDoesNotPatchSysModulesDirectly(unittest.TestCase):
    """The convention this module exists to make keepable.

    `patch.dict(sys.modules, ...)` restores the whole mapping, so it evicts
    anything the code under test imported while it was active. That is invisible
    until a module is run on its own, which is exactly when someone is iterating
    on it. A grep is worth more than remembering.
    """

    def test_no_test_module_uses_patch_dict_on_sys_modules(self) -> None:
        # This module and the helper both name the anti-pattern in order to
        # explain it, so neither can be scanned for it.
        explaining = {Path(__file__).name, "module_stubs.py"}
        offenders = []
        for path in sorted(Path(__file__).parent.glob("*.py")):
            if path.name in explaining:
                continue
            for number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
                if "patch.dict(sys.modules" in line:
                    offenders.append(f"{path.name}:{number}")

        self.assertEqual(
            [],
            offenders,
            "Use injected_module or removed_module from module_stubs instead: "
            "patch.dict restores the whole of sys.modules and evicts anything "
            "imported inside the block. See issue #28.",
        )


if __name__ == "__main__":
    unittest.main()
