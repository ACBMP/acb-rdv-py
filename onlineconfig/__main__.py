"""``python -m onlineconfig [-c onlineconfig.toml]``"""

from __future__ import annotations

import argparse
import asyncio
import logging
import signal

from quazal.httpserver import serve

from . import config as config_mod
from .service import OnlineConfigService


def main() -> None:
    parser = argparse.ArgumentParser(prog="onlineconfig",
                                     description="onlineconfigservice.ubi.com replacement for all games")
    parser.add_argument("-c", "--config", help="TOML config file (ONLINECONFIG_* environment variables override it)")
    args = parser.parse_args()
    cfg = config_mod.load(args.config)
    logging.basicConfig(level=getattr(logging, cfg.log_level.upper(), logging.INFO),
                        format="%(asctime)s %(levelname)-7s %(name)s: %(message)s")
    asyncio.run(run(cfg))


async def run(cfg: config_mod.Config) -> None:
    service = OnlineConfigService(cfg.games, cfg.resolved_address())
    server = await serve("online config", cfg.host, cfg.port, service.handle)
    logging.getLogger("onlineconfig").info("serving %s", service.describe())
    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, stop.set)
        except NotImplementedError:
            pass
    try:
        await stop.wait()
    finally:
        server.close()


if __name__ == "__main__":
    main()
