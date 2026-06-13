"""Allow `python -m app` to start uvicorn."""

import sys

from app.config import get_settings


def main() -> None:
    import uvicorn

    s = get_settings()
    reload = not getattr(sys, "frozen", False)
    uvicorn.run("app.main:app", host=s.host, port=s.port, reload=reload)


if __name__ == "__main__":
    main()
