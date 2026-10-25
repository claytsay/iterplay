"""The query string the harness appends to ``index.html`` follows HLD §3.2."""

from urllib.parse import parse_qsl

import pytest

from iterplay.contract.url import GameUrlParams


def test_query_matches_the_documented_example() -> None:
    query = GameUrlParams(seed=7, bot="newbie").to_query()

    assert query == "seed=7&fixedStep=1&harness=1&bot=newbie"


@pytest.mark.parametrize(
    ("params", "query"),
    [
        (GameUrlParams(seed=1), "seed=1&fixedStep=1&harness=1"),
        (GameUrlParams(seed=1, fixed_step=False), "seed=1&harness=1"),
        (GameUrlParams(seed=1, harness_hud=False), "seed=1&fixedStep=1"),
        (GameUrlParams(seed=1, fixed_step=False, harness_hud=False), "seed=1"),
        (
            GameUrlParams(seed=1, fixed_step=False, harness_hud=False, bot="random"),
            "seed=1&bot=random",
        ),
        (GameUrlParams(seed=-3, harness_hud=False), "seed=-3&fixedStep=1"),
    ],
)
def test_disabled_flags_and_a_missing_bot_are_omitted(params: GameUrlParams, query: str) -> None:
    assert params.to_query() == query


def test_the_bot_name_is_encoded() -> None:
    bot = "new bie&seed=2#hud"

    query = GameUrlParams(seed=9, bot=bot).to_query()

    assert "#" not in query
    assert parse_qsl(query) == [
        ("seed", "9"),
        ("fixedStep", "1"),
        ("harness", "1"),
        ("bot", bot),
    ]
