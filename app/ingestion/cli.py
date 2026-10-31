from __future__ import annotations

import json
import logging

from app.config import get_settings
from app.db import Database
from app.ingestion.service import ImportService
from app.logging import configure_logging

logger = logging.getLogger(__name__)


def run() -> dict[str, object]:
    settings = get_settings()
    configure_logging(settings.log_level)
    database = Database(settings.database_url)
    try:
        result = ImportService(
            database,
            snapshot_version=settings.source_snapshot_version,
            normalization_version=settings.normalization_version,
        ).import_directory(settings.data_dir)
        print(json.dumps(result, indent=2, default=str))
        return result
    except Exception:
        logger.exception("source_import_failed")
        raise
    finally:
        database.dispose()


if __name__ == "__main__":
    run()
