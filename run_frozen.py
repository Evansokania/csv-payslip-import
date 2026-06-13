"""
Entry point for the Windows executable (PyInstaller).

Run with: `python run_frozen.py` from the project root (same as the .exe at runtime).
"""

from __future__ import annotations


def main() -> None:
    import uvicorn

    import app.main  # noqa: F401 — PyInstaller static analysis; app loaded by string below.

    from app.config import get_settings

    s = get_settings()
    uvicorn.run(
        "app.main:app",
        host=s.host,
        port=s.port,
        reload=False,
    )


if __name__ == "__main__":
    main()
