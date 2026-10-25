"""Registered sources. Add an adapter here only after it passes the compliance checklist
in the README (official API or robots-allowed, license tracked, no evasion)."""
from __future__ import annotations

from .met import Met
from .openverse import Openverse
from .pexels import Pexels
from .wikimedia import Wikimedia

SOURCES = {
    "openverse": Openverse(),
    "wikimedia": Wikimedia(),
    "pexels": Pexels(),
    "met": Met(),
}


def get_source(name):
    return SOURCES.get(name)
