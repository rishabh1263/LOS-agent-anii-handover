"""
Central place for all app configuration.
Loads values from environment variables (.env file) so nothing sensitive
is hardcoded in the codebase.
"""

import os
from functools import lru_cache

from dotenv import load_dotenv
from pydantic import BaseModel

load_dotenv()  # reads .env file into process environment


class Settings(BaseModel):
    app_name: str = os.getenv("APP_NAME", "Login Service")
    env: str = os.getenv("ENV", "development")

    # JWT
    jwt_secret_key: str = os.getenv("JWT_SECRET_KEY", "")
    jwt_algorithm: str = os.getenv("JWT_ALGORITHM", "HS256")
    access_token_expire_minutes: int = int(os.getenv("ACCESS_TOKEN_EXPIRE_MINUTES", 60))

    # Dummy user credentials (temporary, until real user DB is added)
    dummy_username: str = os.getenv("DUMMY_USERNAME", "")
    dummy_password: str = os.getenv("DUMMY_PASSWORD", "")

    def validate_required(self) -> None:
        if not self.jwt_secret_key:
            raise RuntimeError("JWT_SECRET_KEY is not set in the environment (.env file).")
        if not self.dummy_username or not self.dummy_password:
            raise RuntimeError("DUMMY_USERNAME / DUMMY_PASSWORD not set in the environment (.env file).")


@lru_cache
def get_settings() -> Settings:
    """
    Cached settings instance so the .env file is read only once.
    Use as a FastAPI dependency: settings: Settings = Depends(get_settings)
    """
    settings = Settings()
    settings.validate_required()
    return settings
