from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    database_url: str = "sqlite:///./finance.db"
    secret_key: str
    data_encryption_key: str
    admin_user: str = "admin"
    # argon2 hash of the admin password: python -m app.tools hash-password
    admin_password_hash: str = ""
    # same hash, base64-encoded: avoids "$" interpolation problems in compose/.env files
    admin_password_hash_b64: str = ""
    # optional TOTP secret (base32) for a second factor: python -m app.tools totp-secret
    admin_totp_secret: str = ""
    public_base_url: str = "http://localhost:8000"
    data_dir: str = "./data"

    anthropic_api_key: str = ""
    anthropic_model: str = "claude-sonnet-5-5"

    zammad_url: str = ""
    zammad_token: str = ""

    bexio_client_id: str = ""
    bexio_client_secret: str = ""
    bexio_auth_url: str = "https://auth.bexio.com/realms/bexio/protocol/openid-connect/auth"
    bexio_token_url: str = "https://auth.bexio.com/realms/bexio/protocol/openid-connect/token"
    bexio_userinfo_url: str = "https://auth.bexio.com/realms/bexio/protocol/openid-connect/userinfo"
    bexio_api_url: str = "https://api.bexio.com"
    # company_profile: liefert company_id/company_name in /userinfo (Firmenpruefung)
    bexio_scopes: str = "openid profile company_profile offline_access accounting contact_edit file kb_bill_show bank_account_show"

    # bearer token for the MCP endpoint (/mcp); empty = endpoint disabled
    mcp_token: str = ""

    pushover_app_token: str = ""
    pushover_user_key: str = ""

    tz: str = "Europe/Zurich"
    worker_interval_seconds: int = 300
    # how many tickets to pull from Zammad per group and tick
    zammad_batch: int = 20
    # auto-book only if the LLM confidence for amount and date is at least this
    auto_book_min_confidence: float = 0.9
    # max attachment size we rasterise/upload
    max_pdf_bytes: int = 20_000_000
    # daily summary push (local hour)
    summary_hour: int = 9


@lru_cache
def get_settings() -> Settings:
    s = Settings()
    if s.admin_password_hash_b64 and not s.admin_password_hash:
        import base64

        s.admin_password_hash = base64.b64decode(s.admin_password_hash_b64).decode().strip()
    return s
