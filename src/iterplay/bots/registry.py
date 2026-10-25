"""Bot lookup by persona name."""

from collections.abc import Mapping
from types import MappingProxyType
from typing import Final

from iterplay.bots.base import Bot
from iterplay.bots.newbie import NewbieBot
from iterplay.bots.random_bot import RandomBot
from iterplay.bots.speedrunner import SpeedrunnerBot
from iterplay.models.playtest import BotName

BOT_TYPES: Final[Mapping[BotName, type[Bot]]] = MappingProxyType(
    {
        BotName.RANDOM: RandomBot,
        BotName.SPEEDRUNNER: SpeedrunnerBot,
        BotName.NEWBIE: NewbieBot,
    }
)
"""Every persona, constructed as ``BOT_TYPES[name](seed, controls)``."""
