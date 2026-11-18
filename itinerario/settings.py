import os
from dataclasses import dataclass
from hashlib import sha256
from hmac import compare_digest
from pathlib import Path

from dotenv import load_dotenv


@dataclass(frozen=True)
class Settings:
    telegram_token: str
    openai_key: str
    geoapify_key: str
    allowed_users: frozenset[int]
    model: str
    data_dir: Path
    chat_requests: int
    reports: int
    global_reports: int
    retention_days: int
    source_code_url: str
    global_chat_requests: int = 60
    public_access: bool = False
    apify_token: str = ""
    apify_free_verified: bool = False

    @classmethod
    def load(cls) -> "Settings":
        load_dotenv(".env.local", interpolate=False)
        load_dotenv(".env", interpolate=False)
        required = ("TELEGRAM_BOT_TOKEN", "OPENAI_API_KEY", "GEOAPIFY_API_KEY", "TELEGRAM_ALLOWED_USERS")
        missing = [name for name in required if not os.environ.get(name, "").strip()]
        if missing:
            raise ValueError(f"Falta configurar {', '.join(missing)}.")
        try:
            users = frozenset(int(value.strip()) for value in os.environ["TELEGRAM_ALLOWED_USERS"].split(","))
        except ValueError:
            raise ValueError("TELEGRAM_ALLOWED_USERS requiere identificadores numéricos separados por coma.") from None
        if not users or min(users) <= 0:
            raise ValueError("TELEGRAM_ALLOWED_USERS requiere al menos un identificador válido.")
        apify = os.getenv("APIFY_TOKEN", "").strip()
        verified = os.getenv("APIFY_FREE_TOKEN_SHA256", "").strip()
        return cls(
            os.environ["TELEGRAM_BOT_TOKEN"].strip(), os.environ["OPENAI_API_KEY"].strip(),
            os.environ["GEOAPIFY_API_KEY"].strip(), users, os.getenv("OPENAI_MODEL", "gpt-6.2-sol"),
            Path(os.getenv("DATA_DIR", "data")), positive_int("CHAT_REQUESTS_PER_DAY", 15),
            positive_int("REPORTS_PER_DAY", 9), positive_int("GLOBAL_REPORTS_PER_DAY", 36),
            positive_int("RETENTION_DAYS", 30), os.getenv("SOURCE_CODE_URL", ""),
            positive_int("GLOBAL_CHAT_REQUESTS_PER_DAY", 60),
            os.getenv("TELEGRAM_PUBLIC_ACCESS", "false").strip().casefold() in {"1", "true"},
            apify, bool(apify and compare_digest(sha256(apify.encode()).hexdigest(), verified)),
        )


def positive_int(name: str, default: int) -> int:
    try:
        value = int(os.getenv(name, str(default)))
    except ValueError:
        raise ValueError(f"{name} requiere un número entero positivo.") from None
    if value < 1:
        raise ValueError(f"{name} requiere un número entero positivo.")
    return value
