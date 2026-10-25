"""Registered sources. Add an adapter here only after it passes the compliance checklist
in the README (official API or robots-allowed, license tracked, no evasion)."""
from __future__ import annotations

from .openverse import Openverse

SOURCES = {
    "openverse": Openverse(),
}


def get_source(name):
    return SOURCES.get(name)
