from __future__ import annotations

import argparse
import sys
from pathlib import Path

from pydantic import ValidationError

from mail2rss.config import ConfigError, load_config
from mail2rss.daemon import run
from mail2rss.logging import configure_logging


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--config", type=Path, default=None)
    args = parser.parse_args()
    try:
        config = load_config(args.config)
        configure_logging(config.log.level)
        return run(config)
    except (ConfigError, ValidationError) as exc:
        print(f"configuration error: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
