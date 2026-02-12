"""Entry point for garuda-pilot: python -m garuda_pilot"""

from __future__ import annotations

import uvicorn
from .config import Config
from .app import create_app


def main() -> None:
    config = Config.load()
    app = create_app(config)
    uvicorn.run(app, host=config.host, port=config.port)


if __name__ == "__main__":
    main()
