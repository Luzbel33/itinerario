import asyncio
import json
import logging
import os
import re
import sys
from datetime import datetime, timedelta, timezone
from html import escape

import httpx

from .geography import Geoapify, map_views
from .models import Depth, Snapshot
from .pdf import build_pdf
from .providers import ProviderError, public_url
from .research import Research, ResearchError, now
from .settings import Settings
from .store import Store
from .telegram import Telegram

LOG = logging.getLogger(__name__)
DOCUMENT_NAME = re.compile(r"itinerario-([0-9a-f]{12})\.pdf", re.IGNORECASE)
REFERENCE = re.compile(r"\[(\d{1,2})\]")
HELP = (
    "<b>¿Qué querés encontrar?</b>\n"
    "Contame qué buscás, para qué y en qué zona. Para armar un recorrido, "
    "sumá tu punto de salida y cómo te movés.\n\n"
    "<b>Ejemplo</b>\n<i>Buscá hoteles que hagan eventos en Palermo. Salgo de Dorrego en auto "
    "y quiero volver al mismo punto.</i>\n\n"
    "<b>Elegí el nivel</b>\n/rapido · /detallado · /profundo\n"
    "Escribí el pedido después del comando. Podés pedir un PDF, una respuesta breve o mapas por sector.\n\n"
    "<b>Retomá un informe</b>\n/historial muestra tus PDF. Usá /ver con su identificador para volver a abrirlo. "
    "También podés adjuntar un PDF para consultarlo. /privacidad explica qué se guarda."
)
LEVELS = {"/rapido": Depth.QUICK, "/detallado": Depth.STANDARD, "/profundo": Depth.DEEP}


def time_from_now(*, seconds=0, days=0) -> str:
    return (datetime.now(timezone.utc) + timedelta(seconds=seconds, days=days)).isoformat(timespec="seconds")


def elapsed_ms(start: str) -> int:
    return max(0, round((datetime.now(timezone.utc) - datetime.fromisoformat(start)).total_seconds() * 1000))


def reply_links(text: str, report: Snapshot | None) -> str:
    sources = {source.id: source.url for source in report.sources if public_url(source.url)} if report else {}

    def resolve(match):
        content = match.group(0)
        url = sources.get(int(match.group(1)))
        return f'<a href="{escape(url, quote=True)}">{content}</a>' if url else content

    return REFERENCE.sub(resolve, escape(text))


def brief_report(snapshot: Snapshot) -> str:
    report = snapshot.report
    lines = [report.title, report.executive_summary[:520]]
    if report.places:
        for place in report.places[:4]:
            sources = " ".join(f"[{number}]" for number in place.sources[:2])
            address = f" · {place.address}" if place.address_sources else ""
            lines.append(f"• {place.name[:95]}{address[:115]} {sources}".rstrip())
    else:
        for finding in report.findings[:3]:
            sources = " ".join(f"[{number}]" for number in finding.sources[:3])
            lines.append(f"• {finding.kind.capitalize()}. {finding.detail[:200]} {sources}".rstrip())
    if report.next_steps:
        lines.append(f"Próximo paso. {report.next_steps[0][:220]}")
    return "\n\n".join(lines)


async def read_attachment(data: bytes) -> str:
    credentials = {"OPENAI_API_KEY", "TELEGRAM_BOT_TOKEN", "GEOAPIFY_API_KEY"}
    environment = {key: value for key, value in os.environ.items() if key.upper() not in credentials}
    process = await asyncio.create_subprocess_exec(
        sys.executable, "-m", "itinerario.read_pdf", stdin=asyncio.subprocess.PIPE,
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL, env=environment,
    )
    try:
        stdout, _ = await asyncio.wait_for(process.communicate(data), timeout=12)
    except (TimeoutError, asyncio.CancelledError):
        if process.returncode is None:
            process.kill()
            await process.wait()
        raise
    if process.returncode or not stdout.strip():
        raise ValueError("El PDF está protegido, dañado o no tiene texto seleccionable en las primeras 12 páginas.")
    return stdout.decode("utf-8")


class Bot:
    def __init__(self, settings: Settings, client: httpx.AsyncClient, store: Store):
        self.settings = settings
        self.store = store
        self.telegram = Telegram(client, settings.telegram_token)
        self.research = Research(client, settings.openai_key, settings.model)
        self.maps = Geoapify(client, settings.geoapify_key)
        self.wakeup = asyncio.Event()
        self.delivery_wakeup = asyncio.Event()

    async def run(self):
        info = await self.telegram.whoami()
        LOG.info("Conectado como @%s", info["username"])
        self.store.recover()
        async with asyncio.TaskGroup() as tasks:
            tasks.create_task(self.poll())
            tasks.create_task(self.work())
            tasks.create_task(self.dispatch())

    async def poll(self):
        backoff = 2
        while True:
            stamp = now()
            try:
                updates = await self.telegram.updates(self.store.offset(stamp[:10]))
                self.store.enqueue(updates, now(), self.settings.allowed_users)
            except ProviderError as error:
                if error.status in {401, 404, 409}:
                    raise
                LOG.warning("%s", error)
                await asyncio.sleep(backoff)
                backoff = min(60, backoff * 2)
                continue
            if updates:
                self.wakeup.set()
            backoff = 2

    async def work(self):
        last_cleanup = ""
        while True:
            today = now()[:10]
            if today != last_cleanup:
                self.store.prune(time_from_now(days=-self.settings.retention_days), time_from_now(days=-7))
                last_cleanup = today
            self.wakeup.clear()
            job = self.store.next_job()
            if job is None:
                try:
                    await asyncio.wait_for(self.wakeup.wait(), timeout=15)
                except TimeoutError:
                    pass
                continue
            try:
                await self.handle(job)
            except (ResearchError, ProviderError) as error:
                LOG.warning("%s", error)
                if isinstance(error, ProviderError) and error.status == 429:
                    self.answer(job, "El proveedor alcanzó su límite de uso. Volvé a intentar más tarde.")
                else:
                    self.answer(job, "No pude completar el pedido. Volvé a enviarlo para intentarlo de nuevo.")
            except TimeoutError:
                self.answer(job, "El archivo o la solicitud tardó más de lo permitido. Probá con un pedido más corto.")
            except Exception as error:
                LOG.error("Error al procesar un pedido (%s)", type(error).__name__)
                self.answer(job, "Ocurrió un error al procesar el pedido. Podés volver a enviarlo.")
            finally:
                self.delivery_wakeup.set()

    def answer(self, job, text: str, *, html: bool = False, remember: str = ""):
        self.store.deliver(job["id"], {"text": text, "html": html}, remember, now())

    async def dispatch(self):
        while True:
            self.delivery_wakeup.clear()
            job = self.store.next_delivery(now())
            if job is None:
                try:
                    await asyncio.wait_for(self.delivery_wakeup.wait(), timeout=5)
                except TimeoutError:
                    pass
                continue
            delivery = json.loads(job["delivery"])
            owner = job["owner"]
            try:
                report_id = delivery.get("report_id")
                if report_id and "text" not in delivery:
                    row = self.store.report(owner, report_id)
                    if row:
                        await self.telegram.document(owner, row["pdf"], f"itinerario-{report_id}.pdf",
                                                     f"{row['title']}\n\n/ver {report_id}")
                        LOG.info("telegram_pdf_ms=%d", elapsed_ms(job["created"]))
                    else:
                        await self.telegram.send(owner, "Ese informe venció o fue eliminado.")
                else:
                    await self.telegram.send(owner, delivery["text"], html=delivery.get("html", False))
                self.store.finish(job["id"])
            except ProviderError as error:
                LOG.warning("%s", error)
                if error.status in {400, 403, 404} or job["attempts"] >= 6:
                    self.store.finish(job["id"])
                else:
                    self.store.retry_delivery(job["id"], time_from_now(seconds=min(180, 10 * 2 ** job["attempts"])))

    def command(self, job, command: str, payload: str) -> bool:
        owner = job["owner"]
        if command == "/miid":
            self.answer(job, f"Tu identificador de Telegram es {owner}.")
            return True
        if owner not in self.settings.allowed_users:
            self.store.finish(job["id"])
            return True
        if command in {"/start", "/ayuda"}:
            self.answer(job, HELP, html=True)
            return True
        if command == "/privacidad":
            days = self.settings.retention_days
            self.answer(job, (
                f"El bot guarda tus mensajes e informes hasta {days} días para que puedas retomarlos. "
                "OpenAI procesa la consulta y Geoapify procesa las direcciones necesarias para el mapa. "
                "Telegram conserva los mensajes y archivos según sus propias políticas. "
                "Para borrar del bot tu historial y tus informes, usá /borrar confirmar."
            ))
            return True
        if command == "/codigo":
            url = self.settings.source_code_url
            self.answer(job, url if public_url(url) else "El enlace al código todavía no está configurado.")
            return True
        if command == "/historial":
            rows = self.store.list_reports(owner)
            contents = "\n".join(f"{row['created'][:10]} · {row['title']}\n/ver {row['id']}" for row in rows)
            self.answer(job, contents[:4000] if rows else "Todavía no generaste informes.")
            return True
        if command == "/ver":
            row = self.store.select_report(owner, payload.strip().lower())
            if not row:
                self.answer(job, "No encontré ese informe en tu historial. Usá /historial para ver los identificadores.")
            else:
                self.store.deliver(job["id"], {"report_id": row["id"]})
            return True
        if command == "/borrar":
            if payload.strip().casefold() == "confirmar":
                self.store.purge_owner(owner, job["id"])
                self.answer(job, "Borré los informes y el historial guardados por el bot.")
            else:
                self.answer(job, "Mandá /borrar confirmar para borrar tus informes y tu historial del bot.")
            return True
        return False

    async def handle(self, job):
        message = json.loads(job["payload"])
        text = (message.get("text") or message.get("caption") or "").strip()
        word, _, content = text.partition(" ")
        command = word.split("@")[0].casefold() if word.startswith("/") else ""
        if self.command(job, command, content):
            return
        owner = job["owner"]
        depth = LEVELS.get(command)
        if depth or command == "/informe":
            if not content.strip():
                self.answer(job, "Escribí el pedido en el mismo mensaje, después del comando.")
                return
            text = content.strip()
            if command == "/informe":
                text = f"Armá un informe descargable sobre este pedido. {text}"
        elif command:
            self.answer(job, "Ese comando no existe. Usá /ayuda para ver las opciones.")
            return
        document = message.get("document")
        if not text and not document:
            self.answer(job, "Mandame texto, o un PDF con texto seleccionable junto con tu consulta.")
            return
        stamp = now()
        if not self.store.reserve(stamp[:10], "chat", {owner: self.settings.chat_requests}):
            self.answer(job, "Alcanzaste el límite diario de pedidos. Se renueva a las 00:00 UTC.")
            return
        acknowledged = False
        try:
            if depth in {Depth.STANDARD, Depth.DEEP} or command == "/informe":
                await self.telegram.send(owner, "Recibí el pedido. Te aviso en este chat cuando esté listo.")
                acknowledged = True
            else:
                await self.telegram.call("sendChatAction", {"chat_id": owner, "action": "typing"})
            LOG.info("telegram_acuse_ms=%d modo=%s", elapsed_ms(job["created"]),
                     "mensaje" if acknowledged else "escribiendo")
        except ProviderError as error:
            LOG.warning("%s", error)
        reply_document = (message.get("reply_to_message") or {}).get("document") or {}
        own_report = DOCUMENT_NAME.fullmatch(reply_document.get("file_name") or "")
        row = self.store.select_report(owner, own_report.group(1).lower()) if own_report else self.store.report(owner)
        previous_report = Snapshot.model_validate_json(row["data"]) if row else None
        if document:
            try:
                attachment_text = await read_attachment(await self.telegram.attachment(document))
            except ValueError as error:
                self.answer(job, str(error))
                return
            text = (text or "Registrá este PDF para poder consultarlo después.") + "\n\nPDF aportado:\n" + attachment_text
        history = self.store.history(owner)
        context = "\n".join(f"{entry['role']}: {entry['text'][:10000]}" for entry in history)[-28000:]
        if previous_report:
            context += "\nInforme seleccionado:\n" + previous_report.model_dump_json()[:36000]
        self.store.remember(owner, stamp, text[:14000])
        intent = await self.research.intent(text[:14000], context, depth)
        if intent.action != "report":
            answer = intent.answer or "Contame un poco más sobre lo que necesitás."
            self.answer(job, reply_links(answer, previous_report), html=True, remember=answer)
            return
        if not self.store.reserve(stamp[:10], "report", {owner: self.settings.reports, 0: self.settings.global_reports}):
            answer = "Se alcanzó el límite de informes de hoy. Se renueva a las 00:00 UTC."
            self.answer(job, answer, remember=answer)
            return
        inline = intent.delivery == "telegram" and intent.map_kind == "none"
        if not acknowledged:
            try:
                message = "Estoy consultando las fuentes." if inline else f"Estoy preparando tu informe {intent.depth.value}."
                await self.telegram.send(owner, message)
            except ProviderError as error:
                LOG.warning("%s", error)
        report, sources = await self.research.report(intent, stamp[:10])
        geography = await self.maps.plan(intent, report)
        if intent.answer:
            geography.notes.append(intent.answer)
        snapshot = Snapshot(created=stamp, objective=intent.objective, report=report, depth=intent.depth,
                            sources=sources, geography=geography)
        images = []
        for view in map_views(geography):
            try:
                images.append(await self.maps.static_map(geography, view))
            except ProviderError as error:
                LOG.warning("%s", error)
                images.append(None)
        pdf = await asyncio.to_thread(build_pdf, snapshot, images)
        brief = brief_report(snapshot) if inline else ""
        delivery = {"text": reply_links(brief, snapshot), "html": True} if inline else {}
        self.store.deliver(job["id"], delivery, brief or report.title, now(), (snapshot, pdf))
        LOG.info("informe_listo_ms=%d nivel=%s bytes=%d", elapsed_ms(job["created"]), intent.depth.value, len(pdf))
