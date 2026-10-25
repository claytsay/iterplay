"""Browser-side harness: game server, Chromium sessions, smoke test [2] and playtest
runner [3] (HLD §4).

Generated game code only ever runs inside these Playwright browser sessions; it is never
executed with Node or Python (HLD §9).
"""
