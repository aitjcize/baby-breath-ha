from __future__ import annotations

import argparse
import logging
import signal
import sys
from pathlib import Path

from app.config import load_addon_config, load_config
from app.service import BabyRespirationService


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Experimental video respiration signal detector")
    parser.add_argument("--config", default="config.yaml", help="YAML configuration path")
    parser.add_argument("--addon", action="store_true", help="Run as a Home Assistant add-on (read /data/options.json)")
    parser.add_argument("--run-seconds", type=float, help="Stop after N seconds (for smoke testing)")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    config = load_addon_config() if args.addon else load_config(Path(args.config))
    logging.basicConfig(
        level=getattr(logging, config.log_level.upper(), logging.INFO),
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    logging.warning("EXPERIMENTAL SECONDARY MONITOR ONLY — not a medical or life-safety device")
    service = BabyRespirationService(config)

    def request_stop(signum: int, frame: object) -> None:
        del frame
        logging.getLogger(__name__).info("received signal %d", signum)
        service.stop()

    signal.signal(signal.SIGINT, request_stop)
    signal.signal(signal.SIGTERM, request_stop)
    service.run(args.run_seconds)
    return 0


if __name__ == "__main__":
    sys.exit(main())
