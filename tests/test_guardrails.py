"""Only allowlisted files within the caps ever reach ``game/`` (HLD §4 [1], §9)."""

import re

import pytest

from iterplay.agents.guardrails import enforce_guardrails
from iterplay.contract.layout import MAX_FILE_BYTES, MAX_FILES, MAX_TOTAL_BYTES
from iterplay.errors import GuardrailError
from iterplay.models.game import GameBundle, GameFile


def file(path: str, content: str = "export {};\n") -> GameFile:
    return GameFile(path=path, content=content)


def required() -> list[GameFile]:
    return [file("index.html", "<!doctype html>\n"), file("main.js")]


def modules(count: int) -> list[GameFile]:
    return [file(f"module{index}.js") for index in range(count)]


def sized(path: str, size: int) -> GameFile:
    return file(path, "x" * size)


def test_allowlisted_files_are_kept_and_sorted_by_path() -> None:
    files = [file("main.js", "main"), file("player.js"), required()[0], file("a_b-c.js")]

    result = enforce_guardrails(files)

    assert isinstance(result.bundle, GameBundle)
    assert [f.path for f in result.bundle.files] == [
        "a_b-c.js",
        "index.html",
        "main.js",
        "player.js",
    ]
    assert {f.path: f.content for f in result.bundle.files}["main.js"] == "main"
    assert result.rejected_paths == ()


def test_paths_off_the_allowlist_are_dropped_in_input_order() -> None:
    off_list = [
        "vendor/three.module.js",
        "../main.js",
        "/etc/passwd",
        "./player.js",
        "src/player.js",
        "style.css",
        "C:\\main.js",
        ".hidden.js",
        "main.js\n",
        " main.js",
        "MAIN.JS",
        "",
    ]
    files = [file(path) for path in off_list[:6]] + required() + [file(p) for p in off_list[6:]]

    result = enforce_guardrails(files)

    assert result.rejected_paths == tuple(off_list)
    assert [f.path for f in result.bundle.files] == ["index.html", "main.js"]


def test_rejected_paths_are_listed_every_time_they_appear() -> None:
    result = enforce_guardrails([*required(), file("../x.js"), file("../x.js")])

    assert result.rejected_paths == ("../x.js", "../x.js")


def test_a_repeated_path_raises() -> None:
    with pytest.raises(GuardrailError, match=r"main\.js appears 2 times"):
        enforce_guardrails([*required(), file("main.js", "again")])


def test_paths_that_differ_only_in_case_raise() -> None:
    with pytest.raises(
        GuardrailError, match=r"player\.js and Player\.js differ only in letter case"
    ):
        enforce_guardrails([*required(), file("player.js"), file("Player.js")])


@pytest.mark.parametrize("missing", ["index.html", "main.js"])
def test_a_missing_required_file_raises(missing: str) -> None:
    files = [f for f in required() if f.path != missing]

    with pytest.raises(GuardrailError, match=f"Missing {re.escape(missing)}"):
        enforce_guardrails(files)


def test_an_empty_response_is_missing_every_required_file() -> None:
    with pytest.raises(GuardrailError, match=r"Missing index\.html and main\.js"):
        enforce_guardrails([])


def test_a_required_file_on_a_rejected_path_is_explained() -> None:
    with pytest.raises(GuardrailError) as excinfo:
        enforce_guardrails([required()[0], file("src/main.js")])

    message = str(excinfo.value)
    assert "Missing main.js" in message
    assert 'discarded: "src/main.js"' in message
    assert "no directories" in message


def test_the_file_count_cap_applies_to_kept_files() -> None:
    at_cap = [*required(), *modules(MAX_FILES - 2)]
    rejected = [file(f"extra/{index}.js") for index in range(3)]

    assert len(enforce_guardrails(at_cap + rejected).bundle.files) == MAX_FILES
    with pytest.raises(GuardrailError, match=f"at most {MAX_FILES}"):
        enforce_guardrails([*at_cap, file("one_more.js")])


def test_the_per_file_cap_counts_utf8_bytes() -> None:
    enforce_guardrails([required()[0], sized("main.js", MAX_FILE_BYTES)])

    with pytest.raises(GuardrailError, match=re.escape(f"main.js is {MAX_FILE_BYTES + 1:,} bytes")):
        enforce_guardrails([required()[0], sized("main.js", MAX_FILE_BYTES + 1)])

    wide = file("main.js", "é" * (MAX_FILE_BYTES // 2 + 1))
    assert len(wide.content) < MAX_FILE_BYTES
    with pytest.raises(GuardrailError, match=f"at most {MAX_FILE_BYTES:,} bytes"):
        enforce_guardrails([required()[0], wide])


def test_the_total_cap_counts_utf8_bytes() -> None:
    full_files, remainder = divmod(MAX_TOTAL_BYTES, MAX_FILE_BYTES)
    at_cap = [sized(f.path, MAX_FILE_BYTES) for f in required()]
    at_cap += [sized(f"module{index}.js", MAX_FILE_BYTES) for index in range(full_files - 2)]
    at_cap.append(sized("remainder.js", remainder))
    assert sum(len(f.content.encode("utf-8")) for f in at_cap) == MAX_TOTAL_BYTES

    enforce_guardrails(at_cap)

    with pytest.raises(GuardrailError, match=f"at most {MAX_TOTAL_BYTES:,} bytes"):
        enforce_guardrails([*at_cap, file("one_more.js", "★")])


def test_text_that_is_not_valid_unicode_raises() -> None:
    with pytest.raises(GuardrailError, match=r"main\.js contains an unpaired surrogate"):
        enforce_guardrails([required()[0], file("main.js", "const s = '\ud800';")])


def test_every_problem_is_reported_at_once() -> None:
    files = [file("index.html"), file("index.html"), sized("level.js", MAX_FILE_BYTES + 1)]

    with pytest.raises(GuardrailError) as excinfo:
        enforce_guardrails(files)

    problems = str(excinfo.value).splitlines()
    assert len(problems) == 3
    assert problems[0].startswith("index.html appears 2 times")
    assert problems[1].startswith("Missing main.js")
    assert problems[2].startswith("level.js is")
