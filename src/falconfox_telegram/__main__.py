"""Run the FalconFox Telegram client."""

from __future__ import annotations

import asyncio
import logging

from .bot import BotConfig, FalconFoxTelegramBot


def main() -> None:
    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s %(levelname)s %(name)s %(message)s")
    try:
        config = BotConfig.from_env()
    except ValueError as error:
        raise SystemExit(f"falconfox-telegram: {error}") from error
    asyncio.run(FalconFoxTelegramBot(config).run())


if __name__ == "__main__":
    main()
