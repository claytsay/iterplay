"""Playtest bot personas (HLD §4 [3]): Random, Speedrunner and Newbie.

Bots are pure policies over :class:`~iterplay.contract.state.GameState` with no browser
access, so they can be unit-tested with synthetic snapshots.
"""
