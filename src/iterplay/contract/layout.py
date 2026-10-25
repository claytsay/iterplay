"""Files a generated game may contain (HLD §3.1) and the guardrail limits on them (HLD §4 [1]).

Generated files are untrusted. Only paths on this allowlist are ever written, and only under
``runs/<run_id>/vN/game/`` (HLD §9).
"""

import re
from typing import Final

GAME_DIR: Final = "game"
"""Directory, inside each version directory, that holds the game."""

ENTRY_HTML: Final = "index.html"
ENTRY_MODULE: Final = "main.js"
REQUIRED_FILES: Final = frozenset({ENTRY_HTML, ENTRY_MODULE})

VENDOR_DIR: Final = "vendor"
"""Reserved for files iterplay supplies. Generated files may never be written here."""

THREE_MODULE_PATH: Final = f"{VENDOR_DIR}/three.module.js"
"""How game modules import Three.js: ``import * as THREE from "./vendor/three.module.js"``."""

ALLOWED_PATH: Final = re.compile(r"(?:index\.html|[A-Za-z0-9][A-Za-z0-9_-]*\.js)")
"""Allowlist for generated paths, applied with ``fullmatch``: ``index.html`` plus flat ES
modules. Anything else, including subdirectories, is rejected."""

MAX_FILES: Final = 16
MAX_FILE_BYTES: Final = 256 * 1024
MAX_TOTAL_BYTES: Final = 1024 * 1024
