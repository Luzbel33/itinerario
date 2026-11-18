import argparse
import asyncio
import logging
import os
import re
import signal
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path

import httpx
from dotenv import load_dotenv

from .bot import Bot
from .providers import ProviderError
from .settings import Settings
from .store import Store
from .telegram import Telegram

LOG = logging.getLogger(__name__)


@contextmanager
def exclusive_instance(data_dir: Path):
    data_dir.mkdir(parents=True, exist_ok=True)
    with (data_dir / ".worker.lock").open("a+b") as lock:
        try:
            if os.name == "nt":
                import msvcrt
                lock.seek(0)
                msvcrt.locking(lock.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            raise ValueError("Otro proceso ya usa este directorio de datos.") from None
        yield


async def discover_id(client: httpx.AsyncClient):
    load_dotenv(".env.local", interpolate=False)
    load_dotenv(".env", interpolate=False)
    token = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
    if not token:
        raise ValueError("Falta configurar TELEGRAM_BOT_TOKEN.")
    telegram = Telegram(client, token)
    me = await telegram.whoami()
    print(f"Abrí @{me['username']} en Telegram y mandá /miid. Esta lectura vence en dos minutos.", flush=True)
    started = int(datetime.now(timezone.utc).timestamp())
    offset = None
    async with asyncio.timeout(120):
        while True:
            updates = await telegram.updates(offset)
            if updates:
                offset = max(update["update_id"] for update in updates) + 1
            for update in updates:
                message = update.get("message") or {}
                sender_id = (message.get("from") or {}).get("id")
                chat = message.get("chat") or {}
                if (chat.get("type") == "private" and sender_id == chat.get("id") and sender_id
                        and message.get("date", 0) >= started and message.get("text") == "/miid"):
                    print(f"Identificador de Telegram recibido: {sender_id}")
                    return


async def run(identify: bool, check: bool, webhook: bool = False):
    async with httpx.AsyncClient(timeout=30, follow_redirects=False, limits=httpx.Limits(max_connections=10)) as client:
        if identify:
            await discover_id(client)
            return
        settings = Settings.load()
        if check:
            result = await Telegram(client, settings.telegram_token).whoami()
            LOG.info("Configuración local cargada. Telegram responde para @%s.", result["username"])
            return
        secret = os.getenv("TELEGRAM_WEBHOOK_SECRET", "") if webhook else ""
        database_url = os.getenv("DATABASE_URL", "")
        if webhook and (not re.fullmatch(r"[A-Za-z0-9_-]{24,256}", secret) or not database_url):
            raise ValueError("El webhook requiere TELEGRAM_WEBHOOK_SECRET y una base DATABASE_URL persistente.")
        with exclusive_instance(settings.data_dir):
            store = Store(settings.data_dir, database_url)
            try:
                await serve(Bot(settings, client, store), secret)
            finally:
                store.close()


async def serve(bot: Bot, webhook_secret: str = ""):
    task = asyncio.current_task()
    loop = asyncio.get_running_loop()
    stopping = False

    def stop():
        nonlocal stopping
        stopping = True
        task.cancel()

    if os.name != "nt":
        loop.add_signal_handler(signal.SIGTERM, stop)
    try:
        if webhook_secret:
            from .webhook import serve_webhook

            await serve_webhook(bot, webhook_secret)
        else:
            await bot.run()
    except asyncio.CancelledError:
        if not stopping:
            raise
        LOG.info("Worker detenido. El estado pendiente se recuperará al reiniciar.")
    finally:
        if os.name != "nt":
            loop.remove_signal_handler(signal.SIGTERM)


def main() -> int:
    parser = argparse.ArgumentParser(description="Bot de investigación e itinerarios por Telegram")
    modes = parser.add_mutually_exclusive_group()
    modes.add_argument("--mi-id", action="store_true", help="Recibir tu identificador desde el bot")
    modes.add_argument("--check", action="store_true", help="Validar configuración y autenticación con Telegram")
    modes.add_argument("--webhook", action="store_true", help="Recibir mensajes mediante un webhook HTTPS")
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("httpcore").setLevel(logging.WARNING)
    try:
        asyncio.run(run(args.mi_id, args.check, args.webhook))
    except KeyboardInterrupt:
        return 0
    except (ValueError, ProviderError) as error:
        LOG.error("%s", error)
        return 1
    except TimeoutError:
        LOG.error("Venció la ventana de lectura. Volvé a ejecutar el comando y mandá /miid.")
        return 1
    except ExceptionGroup as errors:
        for error in errors.exceptions:
            LOG.error("%s", error if isinstance(error, ProviderError) else type(error).__name__)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
