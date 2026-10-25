"""Game source files exchanged with the Builder and Fixer (HLD §4 [1], [8])."""

from pydantic import Field

from iterplay.models.base import StrictModel, TolerantModel


class GameFile(TolerantModel):
    """One complete source file. Partial files and diffs are not supported."""

    path: str = Field(description="POSIX path relative to game/, e.g. 'main.js'.")
    content: str = Field(description="Complete file contents.")


class ChangelogEntry(TolerantModel):
    """One change the Fixer made, tied to the issues it addresses."""

    issue_ids: list[str] = Field(min_length=1)
    summary: str


class BuilderResponse(TolerantModel):
    """Response schema for the Builder: the complete ``game/`` directory."""

    files: list[GameFile] = Field(min_length=1)


class FixerResponse(TolerantModel):
    """Response schema for the Fixer: a complete replacement file set plus patch notes."""

    files: list[GameFile] = Field(min_length=1)
    changelog: list[ChangelogEntry]


class GameBundle(StrictModel):
    """A file set that passed the guardrails and is safe to write under ``game/``.

    Produced by :func:`iterplay.agents.guardrails.enforce_guardrails` or read back from disk.
    """

    files: list[GameFile]
