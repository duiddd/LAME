"""Entry point: python -m d.main"""
import logging

import uvicorn

from .api import create_app
from .config import Settings, load_static
from .engine import Engine


def main():
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
    settings = Settings()
    engine = Engine(settings, load_static(settings))
    # One process, one worker: B accepts one D connection and D keeps state in memory (RD19).
    uvicorn.run(create_app(engine), host="0.0.0.0", port=settings.port, workers=1)


if __name__ == "__main__":
    main()
