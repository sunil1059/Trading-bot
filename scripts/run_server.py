"""Start the dashboard API + engine runtime (PAPER or SIGNAL_ONLY). Binds to localhost by default."""

import uvicorn

from backend.api.app import create_app
from backend.core.config import AppSettings
from backend.logging.setup import configure_logging

if __name__ == "__main__":
    settings = AppSettings()
    configure_logging(settings.log_level, settings.log_json)
    uvicorn.run(create_app(settings), host=settings.api_host, port=settings.api_port, log_config=None,
                proxy_headers=False)
