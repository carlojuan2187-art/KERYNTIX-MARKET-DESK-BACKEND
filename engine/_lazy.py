"""Lazy module proxy.

Several lightweight workers (plan scheduler, alerts, notification delivery, ...) import
modules that mention pandas/numpy only inside function bodies or annotations. Importing
pandas+numpy costs roughly 100 MB of RSS per Python process, so those imports are
deferred until the first real use. Behavior is identical once a library is needed.
"""
from __future__ import annotations

import importlib
from typing import Any


class LazyModule:
    __slots__ = ("_lazy_name", "_lazy_module")

    def __init__(self, name: str) -> None:
        object.__setattr__(self, "_lazy_name", name)
        object.__setattr__(self, "_lazy_module", None)

    def _load(self) -> Any:
        module = object.__getattribute__(self, "_lazy_module")
        if module is None:
            module = importlib.import_module(object.__getattribute__(self, "_lazy_name"))
            object.__setattr__(self, "_lazy_module", module)
        return module

    def __getattr__(self, item: str) -> Any:
        return getattr(self._load(), item)

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return f"<LazyModule {object.__getattribute__(self, '_lazy_name')}>"
