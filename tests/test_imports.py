"""Every module must import without optional model dependencies installed."""
import importlib
import pkgutil

import studiobench


def test_all_modules_import():
    failures = []
    for info in pkgutil.walk_packages(studiobench.__path__, prefix="studiobench."):
        if ".workers" in info.name or info.name.endswith("__main__"):
            continue
        try:
            importlib.import_module(info.name)
        except Exception as exc:  # noqa: BLE001
            failures.append("%s: %r" % (info.name, exc))
    assert not failures, "\n".join(failures)
