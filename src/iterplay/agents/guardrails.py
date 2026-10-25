"""Guardrails on every file set the model returns (HLD §4 [1], §9)."""

import json
from collections.abc import Sequence
from dataclasses import dataclass

from iterplay.contract.layout import (
    ALLOWED_PATH,
    MAX_FILE_BYTES,
    MAX_FILES,
    MAX_TOTAL_BYTES,
    REQUIRED_FILES,
    VENDOR_DIR,
)
from iterplay.errors import GuardrailError
from iterplay.models.game import GameBundle, GameFile


@dataclass(frozen=True, slots=True)
class GuardrailResult:
    bundle: GameBundle
    """The files that passed, safe to write under ``game/``."""

    rejected_paths: tuple[str, ...]
    """Paths dropped because they are not on the allowlist."""


def enforce_guardrails(files: Sequence[GameFile]) -> GuardrailResult:
    """Keep only allowlisted paths and enforce the caps in :mod:`iterplay.contract.layout`.

    Files whose path is not on the allowlist, including anything under ``vendor/``, absolute
    paths and ``..``, are dropped and never written. Paths must match the allowlist exactly;
    nothing is normalised. The caps apply to the files kept, with sizes in UTF-8 bytes, and
    the bundle is sorted by path. Paths that differ only in letter case count as repeats,
    because they are the same file on a case-insensitive file system.

    Raises:
        GuardrailError: A required file is missing, a path repeats, or a cap is exceeded. The
            message lists every problem and how to fix it, since it is fed back to the model.
    """
    kept: list[GameFile] = []
    rejected: list[str] = []
    for file in files:
        if ALLOWED_PATH.fullmatch(file.path):
            kept.append(file)
        else:
            rejected.append(file.path)
    problems = [*_repeated_paths(kept), *_missing_files(kept, rejected), *_cap_problems(kept)]
    if problems:
        raise GuardrailError("\n".join(problems))
    bundle = GameBundle(files=sorted(kept, key=lambda file: file.path))
    return GuardrailResult(bundle=bundle, rejected_paths=tuple(rejected))


def _repeated_paths(files: Sequence[GameFile]) -> list[str]:
    spellings: dict[str, list[str]] = {}
    for file in files:
        spellings.setdefault(file.path.casefold(), []).append(file.path)
    problems: list[str] = []
    for paths in spellings.values():
        distinct = list(dict.fromkeys(paths))
        if len(distinct) > 1:
            problems.append(
                f"{' and '.join(distinct)} differ only in letter case, so they would overwrite "
                "each other on disk. Give every file a distinct name and update its imports."
            )
        elif len(paths) > 1:
            problems.append(
                f"{paths[0]} appears {len(paths)} times. Return each file exactly once, in full."
            )
    return problems


def _missing_files(files: Sequence[GameFile], rejected: Sequence[str]) -> list[str]:
    missing = sorted(REQUIRED_FILES - {file.path for file in files})
    if not missing:
        return []
    problem = (
        f"Missing {' and '.join(missing)}. Every response must include "
        f"{' and '.join(sorted(REQUIRED_FILES))}, each complete."
    )
    if rejected:
        problem += (
            f" These paths were discarded: {', '.join(json.dumps(path) for path in rejected)}."
            " Allowed paths are index.html and flat ES modules such as player.js (letters,"
            f" digits, _ and -): no directories, and nothing under {VENDOR_DIR}/."
        )
    return [problem]


def _cap_problems(files: Sequence[GameFile]) -> list[str]:
    problems: list[str] = []
    if len(files) > MAX_FILES:
        problems.append(
            f"There are {len(files)} files, but at most {MAX_FILES} are allowed. Merge small "
            "modules into fewer files."
        )
    total = 0
    for file in files:
        try:
            size = len(file.content.encode("utf-8"))
        except UnicodeEncodeError:
            problems.append(
                f"{file.path} contains an unpaired surrogate, which is not valid UTF-8. "
                "Write plain Unicode text."
            )
            continue
        total += size
        if size > MAX_FILE_BYTES:
            problems.append(
                f"{file.path} is {size:,} bytes, but each file may be at most "
                f"{MAX_FILE_BYTES:,} bytes. Split it into smaller modules or simplify it."
            )
    if total > MAX_TOTAL_BYTES:
        problems.append(
            f"The files total {total:,} bytes, but together they may be at most "
            f"{MAX_TOTAL_BYTES:,} bytes. Remove unused code and generate repetitive level data "
            "procedurally."
        )
    return problems
