"""Dependency-free test runner (pytest also works: `pytest -q`)."""
import importlib, pkgutil, sys, traceback, pathlib
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))
passed = failed = 0
for m in pkgutil.iter_modules([str(pathlib.Path(__file__).parent)]):
    if not m.name.startswith("test_"):
        continue
    mod = importlib.import_module(f"tests.{m.name}")
    for n in dir(mod):
        if n.startswith("test_"):
            try:
                getattr(mod, n)(); passed += 1
            except Exception:
                failed += 1; print(f"FAIL {m.name}.{n}"); traceback.print_exc(limit=3)
print(f"{passed} passed, {failed} failed"); sys.exit(1 if failed else 0)
