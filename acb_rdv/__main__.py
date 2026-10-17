"""``python -m acb_rdv [-c config.toml]``"""

from __future__ import annotations

import argparse
import asyncio
import logging
import signal

from . import config as config_mod
from .server import AcbServer


def main() -> None:
    parser = argparse.ArgumentParser(prog="acb_rdv", description="Assassin's Creed Brotherhood RendezVous server")
    parser.add_argument("-c", "--config", help="TOML config file (ACB_RDV_* environment variables override it)")
    parser.add_argument("--debug", action="store_true", help="log every packet")
    args = parser.parse_args()
    cfg = config_mod.load(args.config)
    logging.basicConfig(
        level=logging.DEBUG if args.debug else getattr(logging, cfg.log_level.upper(), logging.INFO),
        format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
    )
    asyncio.run(run(cfg))


async def run(cfg: config_mod.Config) -> None:
    server = AcbServer(cfg)
    await server.start()
    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, stop.set)
        except NotImplementedError:  # Windows
            pass
    try:
        await stop.wait()
    finally:
        await server.stop()


if __name__ == "__main__":
    main()
