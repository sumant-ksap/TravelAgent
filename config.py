import os
from dataclasses import dataclass

from dotenv import load_dotenv

load_dotenv()


@dataclass(frozen=True)
class Config:
    telegram_token: str
    ollama_host: str
    ollama_model: str
    ollama_api_key: str | None
    postgres_dsn: str
    history_limit: int


def load_config() -> Config:
    return Config(
        telegram_token=_require("TELEGRAM_BOT_TOKEN"),
        ollama_host=os.getenv("OLLAMA_API_HOST", "http://localhost:11434"),
        ollama_model=os.getenv("OLLAMA_MODEL", "gemma4:31b-cloud"),
        # Set OLLAMA_API_HOST=https://ollama.com and OLLAMA_API_KEY to talk to
        # Ollama Cloud directly over HTTPS, with no local `ollama serve` needed -
        # this is what makes running the bot on a host like Render viable.
        ollama_api_key=os.getenv("OLLAMA_API_KEY") or None,
        postgres_dsn=os.getenv(
            "POSTGRES_DSN", "postgresql://postgres:postgres@localhost:5432/telegram_chat"
        ),
        history_limit=int(os.getenv("HISTORY_LIMIT", "20")),
    )


def _require(name: str) -> str:
    value = os.getenv(name)
    if not value:
        raise RuntimeError(f"Missing required environment variable: {name}")
    return value
