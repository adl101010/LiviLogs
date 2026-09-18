import logging
import sys

import discord

from .config import Config
from .main import RecapBot


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    config = Config.from_env()
    try:
        RecapBot(config).run(config.discord_token, log_handler=None)
    except discord.PrivilegedIntentsRequired:
        sys.exit(
            "Discord refused the connection: turn on MESSAGE CONTENT INTENT under "
            "Bot -> Privileged Gateway Intents in the Discord Developer Portal, then restart."
        )
    except discord.LoginFailure:
        sys.exit("Discord rejected DISCORD_TOKEN. Reset the token in the Developer Portal and update it.")


if __name__ == "__main__":
    main()
