"""API entry point: configures file logging before Uvicorn starts."""
import argparse
import logging

import uvicorn

from app.core.config import Settings
from app.core.logging import configure_logging, logging_config


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--host', default='127.0.0.1')
    parser.add_argument('--port', type=int, default=8000)
    parser.add_argument('--workers', type=int, default=1)
    args = parser.parse_args()
    if args.workers < 1 or not 1 <= args.port <= 65535:
        parser.error('workers must be positive and port must be between 1 and 65535')
    settings = Settings()
    configure_logging(settings)
    logger = logging.getLogger('app.server')
    logger.info('Starting API host=%s port=%s workers=%s log_dir=%s',
                args.host, args.port, args.workers, settings.log_path)
    try:
        uvicorn.run('app.main:app', host=args.host, port=args.port, workers=args.workers,
                    log_config=logging_config(settings), access_log=True)
    except Exception:
        logger.exception('API server failed')
        raise SystemExit(1) from None
    except SystemExit as exc:
        if exc.code:
            logger.error('API server exited with code %s', exc.code)
        raise
    finally:
        logger.info('API server stopped')
        logging.shutdown()


if __name__ == '__main__':
    main()
