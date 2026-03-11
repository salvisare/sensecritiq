from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    database_url: str = "postgresql+asyncpg://localhost/sensecritiq"
    unkey_api_id: str = ""

    stripe_secret_key: str = ""
    stripe_webhook_secret: str = ""

    assemblyai_api_key: str = ""
    anthropic_api_key: str = ""
    openai_api_key: str = ""

    r2_bucket_name: str = "sensecritiq-uploads"
    r2_access_key_id: str = ""
    r2_secret_access_key: str = ""
    r2_endpoint_url: str = ""

    @property
    def async_database_url(self) -> str:
        url = self.database_url
        if url.startswith("postgresql://"):
            return url.replace("postgresql://", "postgresql+asyncpg://", 1)
        return url


settings = Settings()
