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
from .models import BUDGETS, Depth, Snapshot
from .pdf import build_pdf
from .providers import ProviderError, public_url
from .research import Research, ResearchError, now
from .settings import Settings
from .store import Store
from .telegram import Telegram

LOG = logging.getLogger(__name__)
DOCUMENT_NAME = re.compile(r"itinerario-([0-9a-f]{12})\.pdf", re.IGNORECASE)
REFERENCE = re.compile(r"\[(\d{1,2})\]")
WELCOME = (
    "<b>Soy Rumbo. ¿Qué necesitás encontrar?</b>\n"
    "Escribime un mensaje normal. Decime <b>qué buscás, en qué zona y qué querés lograr</b>. "
    "Podés sumar presupuesto, fechas o cualquier condición que importe.\n\n"
    "<i>Soy DJ en Salta. Buscá lugares que programen electrónica y pasame vías de contacto. "
    "Quiero un PDF con fuentes.</i>\n\n"
    "Puedo ayudarte a comprar, conseguir clientes, encontrar fechas o eventos, organizar visitas "
    "e investigar un tema. Busco en la web y en publicaciones de redes sociales accesibles al público.\n\n"
    "<b>Elegí cómo recibirlo.</b> Pedime un resumen en el chat, un PDF o un recorrido con mapa. "
    "Para un recorrido, decime de dónde salís y si vas a pie, en bici o en auto.\n\n"
    "/ejemplos · Pedidos listos para usar\n"
    "/ayuda · Fuentes, mapas y uso del bot\n"
    "/modos · Elegí cuánto investigar\n"
    "/historial · Tus informes guardados"
)
HELP = (
    "<b>Cómo usar Rumbo</b>\n"
    "Escribí tu pedido como te salga. Si preferís, agregá <i>«dame un resumen»</i>, "
    "<i>«compará estas opciones»</i> o <i>«armá un PDF con fuentes»</i>. "
    "Si volvés con una duda, podés repreguntarme en este chat.\n\n"
    "<b>Dónde busca</b>\n"
    "Sitios oficiales, medios, directorios, agendas y publicaciones públicas que aparecen en buscadores. "
    "Según el tema, puede incluir Instagram, Facebook, X, TikTok, YouTube y otros. "
    "El informe cita lo que encontró y marca qué datos quedan por confirmar. "
    "En redes, la cobertura se limita a contenido público accesible en la búsqueda.\n\n"
    "<b>Cuánto querés investigar</b>\n"
    "Elegí la profundidad con /modos. Podés combinarla con el formato que te resulte cómodo: "
    "resumen en el chat, PDF o una guía de varias páginas.\n\n"
    "<b>Mapas y recorridos</b>\n"
    "Pedime que marque lugares o que ordene una ruta. Para armarla, sumá el punto de salida y cómo te movés. "
    "Cuando hay suficientes direcciones verificables, puedo dividir la ruta en sectores con colores y paradas numeradas.\n\n"
    "<b>Volvé a un informe</b>\n"
    "Abrí /historial y copiá el comando /ver que aparece debajo del título. Después podés hacer preguntas "
    "sobre ese informe. También podés adjuntar un PDF con texto seleccionable y consultarlo.\n\n"
    "/ejemplos · Ideas para empezar\n"
    "/privacidad · Qué se guarda y cómo borrarlo"
)
EXAMPLES = (
    "<b>Pedidos para copiar y adaptar</b>\n\n"
    "<b>Comprar</b>\n"
    "<code>Buscá bandejas de DJ usadas en Córdoba por hasta $350.000. Compará estado, precio y entrega. Citá los avisos.</code>\n\n"
    "<b>Vender o conseguir clientes</b>\n"
    "<code>Vendo agua en bidones en Salta. Identificá comercios con buen encaje, sus canales comerciales públicos "
    "y a quién visitar primero.</code>\n\n"
    "<b>Armar un recorrido</b>\n"
    "<code>Marcá estos locales de Palermo: [lista]. Salgo del Jardín Botánico a pie. "
    "Ordená las paradas y dividilas en dos sectores.</code>\n\n"
    "<b>Investigar un tema</b>\n"
    "<code>Estoy investigando los cortes de agua en mi ciudad. Armá una cronología, contrastá comunicados "
    "con medios locales y marcá qué falta corroborar.</code>\n\n"
    "<b>Conseguir fechas o eventos</b>\n"
    "<code>Soy DJ de techno en Salta. Buscá convocatorias abiertas, eventos y espacios afines. "
    "Separá los avisos confirmados de los lugares a los que podría ofrecerme.</code>\n\n"
    "<b>Retomar un PDF</b>\n"
    "<code>Del último informe, ¿cuáles son las tres opciones más convenientes y qué tendría que confirmar antes de ir?</code>"
)
GUIDES = {"/start": WELCOME, "/ayuda": HELP, "/ejemplos": EXAMPLES}
LEVELS = {"/rapido": Depth.QUICK, "/detallado": Depth.STANDARD,
          "/profundo": Depth.DEEP, "/extenso": Depth.EXTENSIVE}
MODE_DESCRIPTIONS = {
    Depth.QUICK: "Panorama breve para decidir por dónde empezar.",
    Depth.STANDARD: "Más opciones, comparación y próximos pasos.",
    Depth.DEEP: "Web, redes, hilos públicos y una ronda para contrastar hallazgos.",
    Depth.EXTENSIVE: "Más evidencia, una búsqueda dedicada a fuentes locales y del rubro y un informe más desarrollado.",
}
MENU = {
    "start": "Empezar con Rumbo",
    "ejemplos": "Ver pedidos para copiar y adaptar",
    "ayuda": "Cómo buscar, usar mapas y leer informes",
    "modos": "Elegir profundidad y ver los cupos",
    "rapido": "Consultar con una búsqueda breve",
    "detallado": "Buscar más opciones y compararlas",
    "profundo": "Investigar en profundidad y cruzar fuentes",
    "extenso": "Pedir un informe largo con mayor contraste",
    "historial": "Abrir mis informes",
    "privacidad": "Consultar el uso de mis datos",
}


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
        self.username = ""

    async def run(self, *, polling: bool = True):
        while not self.store.try_worker_lock():
            await asyncio.sleep(2)
        info = await self.telegram.whoami()
        self.username = info["username"]
        LOG.info("Conectado como @%s", self.username)
        LOG.info("telegram_public_access=%s", "enabled" if self.settings.public_access else "disabled")
        try:
            await self.telegram.call("setMyCommands", {
                "commands": [{"command": command, "description": description} for command, description in MENU.items()]
            })
        except ProviderError as error:
            LOG.warning("%s", error)
        self.store.recover()
        async with asyncio.TaskGroup() as tasks:
            if polling:
                tasks.create_task(self.poll())
            tasks.create_task(self.work())
            tasks.create_task(self.dispatch())

    def accept(self, updates: list[dict], *, polling: bool = True):
        self.store.enqueue(updates, now(), self.settings.allowed_users, public_access=self.settings.public_access,
                           remember_offset=polling)
        if updates:
            self.wakeup.set()

    async def poll(self):
        backoff = 2
        while True:
            stamp = now()
            try:
                updates = await self.telegram.updates(self.store.offset(stamp[:10]))
                self.accept(updates)
            except ProviderError as error:
                if error.status in {401, 404, 409}:
                    raise
                LOG.warning("%s", error)
                await asyncio.sleep(backoff)
                backoff = min(60, backoff * 2)
                continue
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
        admin = owner in self.settings.allowed_users
        if not self.settings.public_access and not admin and not self.store.is_guest(owner):
            if command == "/start":
                code = re.fullmatch(r"inv_([A-Za-z0-9_-]{24})", payload)
                if code and self.store.redeem(owner, code[1], now()):
                    self.answer(job, WELCOME, html=True)
                else:
                    self.answer(job, "Para usar Rumbo, pedile un enlace de invitación a quien te lo recomendó.")
            else:
                self.store.finish(job["id"])
            return True
        if command == "/invitar" and admin:
            code = self.store.invite(time_from_now(days=3))
            self.answer(job, (
                "Compartí este enlace con una persona. Es de un solo uso y vence en tres días.\n\n"
                f"https://t.me/{self.username}?start=inv_{code}"
            ))
            return True
        guide = GUIDES.get(command)
        if guide:
            self.answer(job, guide, html=True)
            return True
        if command == "/modos":
            lines = [f"<b>Modelo de esta instalación</b> · <code>{escape(self.settings.model)}</code>",
                     "Los niveles cambian el alcance de la investigación y el detalle del informe."]
            for name, depth in LEVELS.items():
                budget = BUDGETS[depth]
                unit_label = "punto" if budget.quota_units == 1 else "puntos"
                lines.append(f"<b>{name}</b> · {MODE_DESCRIPTIONS[depth]}\n"
                             f"Presupuesto de búsqueda: {budget.searches} · {budget.quota_units} {unit_label} del cupo.")
            lines += [
                f"El cupo diario es de {self.settings.reports} puntos por persona y "
                f"{self.settings.global_reports} para todo el bot. Se renueva a las 00:00 UTC.",
                "Poné el comando y el pedido en el mismo mensaje. Un informe extenso puede tardar varios minutos. "
                "Podés pedir una extensión aproximada, y Rumbo la ajusta según las fuentes disponibles.",
                "<code>/extenso\n\nInvestigá el tema y armá un PDF de unas ocho páginas si hay evidencia suficiente.</code>",
            ]
            self.answer(job, "\n\n".join(lines), html=True)
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
        word, *rest = text.split(maxsplit=1) or [""]
        content = rest[0] if rest else ""
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
        if not self.store.reserve(stamp[:10], "chat", {owner: self.settings.chat_requests,
                                                       0: self.settings.global_chat_requests}):
            self.answer(job, "Alcanzaste el límite diario de pedidos. Se renueva a las 00:00 UTC.")
            return
        acknowledged = False
        try:
            if depth in {Depth.STANDARD, Depth.DEEP, Depth.EXTENSIVE} or command == "/informe":
                notice = f"Recibí el pedido en modo {depth.value}." if depth else "Recibí el pedido."
                if depth == Depth.EXTENSIVE:
                    notice += " La investigación puede tardar varios minutos."
                await self.telegram.send(owner, f"{notice} Te aviso en este chat cuando esté listo.")
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
        units = BUDGETS[intent.depth].quota_units
        limits = {owner: self.settings.reports, 0: self.settings.global_reports}
        if not self.store.reserve(stamp[:10], "report", limits, units=units):
            answer = "No quedan suficientes puntos para este modo. El cupo se renueva a las 00:00 UTC."
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
