"""Shared `CTkFont` objects, cached by their attributes.

Every `CTkFont(...)` call creates a Tk font that Tcl must eventually delete.
When Python's garbage collector fires that `__del__` on a *worker* thread, the
resulting `font delete` call into Tcl can hang, because Tcl calls from a
non-main thread are not safe.

That is not a hypothetical. It was observed here twice. The first time, a
worker thread stalled inside `shutil.which()` while the collector ran a
`Font.__del__`; the fix then was to hand-share a handful of font objects
within one tab. The second time, adding an approval dialog and a questions
panel pushed the churn back over the threshold and a full test run hung
reproducibly — `faulthandler` put the worker inside `tkinter/font.py`
`__del__` during `Path.resolve()`.

Hand-sharing does not survive new UI: every widget added later is another
chance to reintroduce the churn. Going through this cache means the number of
live Font objects is bounded by the number of distinct *styles*, not by the
number of widgets, so adding UI no longer raises the odds of the hang.

Not a full fix: the underlying GC/Tcl/thread interaction is still there. This
removes the main source of pressure on it.
"""

from __future__ import annotations

try:
    import customtkinter as ctk
except ImportError:  # pragma: no cover
    ctk = None

#: (size, weight) -> CTkFont. Never evicted: the set of styles a GUI uses is
#: small and fixed, and evicting would recreate the churn this exists to stop.
_CACHE: dict[tuple[int, str], object] = {}


def font(size: int = 12, weight: str = "normal"):
    """The shared font for this style, created once per process."""
    if ctk is None:  # pragma: no cover - headless
        return None
    key = (size, weight)
    cached = _CACHE.get(key)
    if cached is None:
        cached = ctk.CTkFont(size=size, weight=weight)
        _CACHE[key] = cached
    return cached


def bold(size: int = 12):
    return font(size, "bold")


def cached_count() -> int:
    """Distinct styles currently held. Used by tests to assert sharing."""
    return len(_CACHE)
