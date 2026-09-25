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

<<<<<<< HEAD
    # JWT — RS256 (asymmetric): sign with private key, verify with public key
=======
    # JWT (RS256)
>>>>>>> dev/aniket
    jwt_algorithm: str = os.getenv("JWT_ALGORITHM", "RS256")
    jwt_kid: str = os.getenv("JWT_KID", "los-rs256-1")
    jwt_issuer: str = os.getenv("JWT_ISSUER", "los-local")
    jwt_audience: str = os.getenv("JWT_AUDIENCE", "los-agentic-ai")
<<<<<<< HEAD
    # Prefer full PEM content from env; fall back to file paths
    jwt_private_key: str = os.getenv("JWT_PRIVATE_KEY", "")
    jwt_public_key: str = os.getenv("JWT_PUBLIC_KEY", "")
    jwt_private_key_path: str = os.getenv("JWT_PRIVATE_KEY_PATH", "")
    jwt_public_key_path: str = os.getenv("JWT_PUBLIC_KEY_PATH", "")
    access_token_expire_minutes: int = int(os.getenv("ACCESS_TOKEN_EXPIRE_MINUTES", 120))
    refresh_token_expire_days: int = int(os.getenv("REFRESH_TOKEN_EXPIRE_DAYS", 7))

    # Default claims for issued tokens (can override per-user later)
    jwt_default_role: str = os.getenv("JWT_DEFAULT_ROLE", "fos")
    jwt_default_scope: str = os.getenv(
        "JWT_DEFAULT_SCOPE",
        "read_applicant read_application read_documents read_verification "
        "read_pending_items read_next_action create_applicant update_applicant "
        "create_application upload_document modify_application",
    )
=======
    jwt_private_key_path: str = os.getenv("JWT_PRIVATE_KEY_PATH", "keys/private.pem")
    jwt_public_key_path: str = os.getenv("JWT_PUBLIC_KEY_PATH", "keys/public.pem")
    access_token_expire_minutes: int = int(os.getenv("ACCESS_TOKEN_EXPIRE_MINUTES", 120))
>>>>>>> dev/aniket

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

    def _load_key(self, pem_content: str, path: str, label: str) -> str:
        if pem_content.strip():
            # Support both literal newlines and \n-escaped env values
            return pem_content.replace("\\n", "\n").strip()
        if path.strip():
            try:
                with open(path.strip(), "r", encoding="utf-8") as f:
                    return f.read().strip()
            except OSError as e:
                raise RuntimeError(f"Could not read {label} from path '{path}': {e}") from e
        raise RuntimeError(
            f"{label} is not set. Provide JWT_PRIVATE_KEY / JWT_PUBLIC_KEY "
            f"(PEM content) or JWT_PRIVATE_KEY_PATH / JWT_PUBLIC_KEY_PATH."
        )

    def get_private_key(self) -> str:
        return self._load_key(self.jwt_private_key, self.jwt_private_key_path, "JWT private key")

    def get_public_key(self) -> str:
        return self._load_key(self.jwt_public_key, self.jwt_public_key_path, "JWT public key")

    def validate_required(self) -> None:
        # Force key resolution early so misconfiguration fails at startup
        self.get_private_key()
        self.get_public_key()
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