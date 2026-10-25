"""Guardrails on every file set the model returns (HLD §4 [1], §9)."""

from collections.abc import Sequence
from dataclasses import dataclass

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
    paths and ``..``, are dropped and never written.

    Raises:
        GuardrailError: A required file is missing, a path repeats, or a cap is exceeded.
    """
    raise NotImplementedError
