"""
Central place for all app configuration.
Loads values from environment variables (.env file) so nothing sensitive
is hardcoded in the codebase.
"""

import os
from functools import lru_cache
from pathlib import Path

from dotenv import load_dotenv
from pydantic import BaseModel

load_dotenv()  # reads .env file into process environment


class Settings(BaseModel):
    app_name: str = os.getenv("APP_NAME", "Login Service")
    env: str = os.getenv("ENV", "development")

    # JWT (RS256)
    jwt_algorithm: str = os.getenv("JWT_ALGORITHM", "RS256")
    jwt_kid: str = os.getenv("JWT_KID", "los-rs256-1")
    jwt_issuer: str = os.getenv("JWT_ISSUER", "los-local")
    jwt_audience: str = os.getenv("JWT_AUDIENCE", "los-agentic-ai")
    jwt_private_key_path: str = os.getenv("JWT_PRIVATE_KEY_PATH", "keys/private.pem")
    jwt_public_key_path: str = os.getenv("JWT_PUBLIC_KEY_PATH", "keys/public.pem")
    access_token_expire_minutes: int = int(os.getenv("ACCESS_TOKEN_EXPIRE_MINUTES", 120))

    # Dummy user credentials (temporary, until real user DB is added)
    dummy_username: str = os.getenv("DUMMY_USERNAME", "")
    dummy_password: str = os.getenv("DUMMY_PASSWORD", "")
    dummy_role: str = os.getenv("DUMMY_ROLE", "fos")
    dummy_scope: str = os.getenv(
        "DUMMY_SCOPE",
        "read_applicant read_application read_documents read_verification "
        "read_pending_items read_next_action create_applicant update_applicant "
        "create_application upload_document modify_application",
    )

    @property
    def private_key(self) -> str:
        return Path(self.jwt_private_key_path).read_text()

    @property
    def public_key(self) -> str:
        return Path(self.jwt_public_key_path).read_text()

    def validate_required(self) -> None:
        if not Path(self.jwt_private_key_path).is_file():
            raise RuntimeError(f"Private key not found: {self.jwt_private_key_path}")
        if not Path(self.jwt_public_key_path).is_file():
            raise RuntimeError(f"Public key not found: {self.jwt_public_key_path}")
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