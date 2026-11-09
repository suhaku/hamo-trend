"""Load games.json — add a game = one JSON entry. No game-level ranking."""
from __future__ import annotations
import json, os
_BASE = os.path.dirname(os.path.abspath(__file__))
_CFG = json.load(open(os.path.join(_BASE, "games.json"), encoding="utf-8"))
GAMES = _CFG["games"]
SIGNALS_META = _CFG.get("signals", {})

def game_by_id(gid: str):
    for g in GAMES:
        if g["id"] == gid:
            return g
    return None
