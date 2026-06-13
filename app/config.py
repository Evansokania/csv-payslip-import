import os
import sys
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path

from dotenv import load_dotenv


def _repo_root() -> Path:
    """Project root (parent of `app/`) when running from source."""
    return Path(__file__).resolve().parent.parent


def _resource_dir() -> Path:
    """Bundled `templates/` and `static/`. When frozen (PyInstaller), assets live under `_MEIPASS`."""
    if getattr(sys, "frozen", False) and hasattr(sys, "_MEIPASS"):
        return Path(sys._MEIPASS)
    return _repo_root()


def _env_dir() -> Path:
    """Directory containing `.env`. When frozen, use the folder with the `.exe` so teams can edit config."""
    if getattr(sys, "frozen", False):
        return Path(sys.executable).resolve().parent
    return _repo_root()


# Templates / static — must match PyInstaller `datas` targets (see `csv-payslip-import.spec`).
BASE_DIR = _resource_dir()
DOTENV_PATH = _env_dir() / ".env"

# Prefer values from this project's .env over empty/wrong Windows user env vars.
load_dotenv(DOTENV_PATH, override=True)


def _env_str(key: str, default: str = "") -> str:
    v = os.getenv(key, default)
    if v is None:
        return default
    v = str(v).strip()
    if len(v) >= 2 and v[0] == v[-1] and v[0] in "\"'":
        v = v[1:-1]
    return v


@dataclass(frozen=True)
class Settings:
    mysql_host: str
    mysql_port: int
    mysql_user: str
    mysql_password: str
    mysql_database: str
    database_url: str | None
    app_title: str
    host: str
    port: int
    azure_openai_api_key: str
    azure_openai_endpoint: str
    azure_openai_api_version: str
    azure_openai_deployment: str

    @property
    def sqlalchemy_url(self) -> str:
        if self.database_url:
            return self.database_url
        pwd = self.mysql_password.replace("@", "%40").replace(":", "%3A")
        user = self.mysql_user.replace("@", "%40")
        return (
            f"mysql+pymysql://{user}:{pwd}@{self.mysql_host}:{self.mysql_port}/"
            f"{self.mysql_database}?charset=utf8mb4"
        )


@lru_cache
def get_settings() -> Settings:
    # Idempotent; ensures reload / alternate import order still sees disk .env.
    load_dotenv(DOTENV_PATH, override=True)
    db_url = _env_str("DATABASE_URL", "")
    return Settings(
        mysql_host=_env_str("MYSQL_HOST", "127.0.0.1"),
        mysql_port=int(_env_str("MYSQL_PORT", "3306")),
        mysql_user=_env_str("MYSQL_USER", "root"),
        mysql_password=_env_str("MYSQL_PASSWORD", ""),
        mysql_database=_env_str("MYSQL_DATABASE", ""),
        database_url=db_url or None,
        app_title=_env_str("APP_TITLE", "CSV Payslip Import"),
        host=_env_str("HOST", "127.0.0.1"),
        port=int(_env_str("PORT", "8890")),
        azure_openai_api_key=_env_str("AZURE_OPENAI_API_KEY", ""),
        azure_openai_endpoint=_env_str("AZURE_OPENAI_ENDPOINT", "").rstrip("/"),
        azure_openai_api_version=_env_str("AZURE_OPENAI_API_VERSION", "2024-08-01-preview"),
        azure_openai_deployment=_env_str("AZURE_OPENAI_DEPLOYMENT_NAME", ""),
    )


def azure_openai_configured(settings: Settings | None = None) -> bool:
    s = settings or get_settings()
    return bool(s.azure_openai_api_key and s.azure_openai_endpoint and s.azure_openai_deployment)
