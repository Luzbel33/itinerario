import asyncio
import io
import json
import tempfile
import unittest
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

import httpx
from PIL import Image
from pypdf import PdfReader

from itinerario.bot import Bot
from itinerario.geography import directions, encode_polyline, map_views, optimal_order
from itinerario.models import Comparison, Depth, Finding, Intent, Place, Report, Snapshot
from itinerario.pdf import build_pdf
from itinerario.research import Research, clean_report, extract_sources
from itinerario.settings import Settings
from itinerario.store import Store

STAMP = "2026-11-17T20:00:00+00:00"
OWNER = 143
CITED = "https://agenda.example.org/fechas"
POINTS = [
    (-58.447601, -34.591702), (-58.435862, -34.585207), (-58.44195, -34.581425),
    (-58.435264, -34.578827), (-58.439168, -34.579221),
]


def intent() -> Intent:
    return Intent(
        action="report", answer="", objective="Investigá espacios con actividad cultural",
        depth=Depth.STANDARD, delivery="pdf", map_kind="route", area="CABA", country_code="ar",
        origin="-34.591702,-58.447601", finish="", return_to_origin=True,
        transport="drive", sector_size=3, sector_count=0, sort_by="time",
    )


def report() -> Report:
    places = [
        Place(name=f"Espacio {i}", category="Cultura", address=f"Cabrera {4000+i}", locality="CABA",
              fit="Organiza eventos públicos.", action="Consultá si recibe propuestas.",
              public_contact="Contacto institucional", reference="Entrada por el frente",
              date="", sources=[1], address_sources=[1])
        for i in range(1, 5)
    ]
    return Report(
        title="Agenda y recorridos culturales", executive_summary="Cuatro espacios con programación publicada.",
        findings=[
            Finding(title="Programación", detail="La agenda oficial lista fechas.", kind="documentado",
                    date="", sources=[1, 99]),
            Finding(title="Vacío", detail="Confirmar condiciones.", kind="documentado", date="", sources=[99]),
        ],
        places=places,
        comparisons=[Comparison(criterion="Agenda", option="Espacio 1", observation="Agenda publicada", sources=[1])],
        next_steps=["Confirmá horario de atención."], limitations=["Las visitas requieren confirmación."],
        methodology="Búsqueda web y cálculo vial",
    )


def response(text: str, annotations=()):
    return {"status": "completed", "output": [{"type": "message", "content": [
        {"type": "output_text", "text": text, "annotations": list(annotations)}
    ]}]}


def evidence():
    return response("Agenda institucional con direcciones", [
        {"type": "url_citation", "title": "Agenda oficial", "url": CITED},
        {"type": "url_citation", "title": "Unsafe", "url": "javascript:alert(1)"},
    ])


def update(number: int, text: str, owner=OWNER):
    return {"update_id": number, "message": {"text": text, "from": {"id": owner},
                                            "chat": {"type": "private", "id": owner}}}


class MapAndEvidenceTests(unittest.TestCase):
    def test_route_optimization_honors_directed_matrix_and_end(self):
        matrix = [
            [0, 12, 2, 20], [1, 0, 15, 2], [12, 3, 0, 9], [4, 2, 6, 0],
        ]
        self.assertEqual(optimal_order(matrix, True), [0, 2, 1, 3])
        self.assertEqual(optimal_order(matrix, False), [0, 2, 1, 3])
        self.assertIsNone(optimal_order([[0, None], [4, 0]], True))

    def test_polyline_standard_example(self):
        points = [(-120.2, 38.5), (-120.95, 40.7), (-126.453, 43.252)]
        self.assertEqual(encode_polyline(points, precision=5), "_p~iF~ps|U_ulLnnqC_mqNvxq`@")

    def test_only_observed_sources_are_accepted(self):
        sources = extract_sources([evidence()], STAMP)
        self.assertEqual([source.url for source in sources], [CITED])
        result = clean_report(report(), sources, Depth.STANDARD)
        self.assertEqual(result.findings[0].sources, [1])
        self.assertEqual(result.findings[1].kind, "pendiente")
        result.places[0].address_sources = [99]
        result.places[0].sources = [99]
        result = clean_report(result, sources, Depth.QUICK)
        self.assertEqual([place.name for place in result.places], ["Espacio 2", "Espacio 3", "Espacio 4"])


class StoreTests(unittest.TestCase):
    def test_updates_deduplicate_and_recover(self):
        with tempfile.TemporaryDirectory() as directory:
            store = Store(Path(directory))
            try:
                batch = [update(1, "Hola"), update(2, "ignorado", owner=700)]
                store.enqueue(batch, STAMP, frozenset({OWNER}))
                store.enqueue(batch, STAMP, frozenset({OWNER}))
                self.assertEqual(store.offset(STAMP[:10]), 3)
                job = store.next_job()
                self.assertEqual(job["id"], 1)
                self.assertIsNone(store.next_job())
                store.recover()
                interrupted = store.next_delivery(STAMP)
                self.assertIn("interrumpió", json.loads(interrupted["delivery"])["text"])
                self.assertIsNone(interrupted["payload"])
                store.finish(job["id"])
                self.assertIsNone(store.next_delivery(STAMP))
            finally:
                store.close()

    def test_global_quota_is_atomic(self):
        with tempfile.TemporaryDirectory() as directory:
            store = Store(Path(directory))
            try:
                self.assertTrue(store.reserve(STAMP[:10], "report", {OWNER: 1, 0: 1}))
                self.assertFalse(store.reserve(STAMP[:10], "report", {OWNER + 1: 1, 0: 1}))
                self.assertTrue(store.reserve(STAMP[:10], "report", {OWNER + 1: 1}))
                self.assertFalse(store.reserve(STAMP[:10], "report", {OWNER + 1: 1}))
            finally:
                store.close()


class BotIntegrationTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        root = Path(self.directory.name)
        self.store = Store(root)
        self.addCleanup(self.store.close)
        self.settings = Settings("telegram-fixture", "openai-fixture", "maps-fixture", frozenset({OWNER}),
                                 "fixture-model", root, 15, 3, 12, 30, "")
        self.openai_calls = []
        self.telegram_calls = []
        self.map_calls = []
        self.fail_geocodes = False
        self.fail_social = False
        self.empty_initial_posts = False
        self.linkedin_initial_posts = False
        self.report_response = intent()
        fake_map = Image.new("RGB", (1000, 650), color="#eef6f2")
        stream = io.BytesIO()
        fake_map.save(stream, format="PNG")
        self.map = stream.getvalue()
        self.client = httpx.AsyncClient(transport=httpx.MockTransport(self.provider))
        self.addAsyncCleanup(self.client.aclose)
        self.bot = Bot(self.settings, self.client, self.store)

    def provider(self, request: httpx.Request):
        path = request.url.path
        if request.url.host == "api.openai.com":
            payload = json.loads(request.content)
            self.assertEqual(request.headers["authorization"], "Bearer openai-fixture")
            self.assertFalse(payload["store"])
            self.openai_calls.append(payload)
            if payload.get("tools"):
                filters = payload["tools"][0].get("filters")
                if filters:
                    if self.fail_social:
                        return httpx.Response(503)
                    if (self.empty_initial_posts and "instagram.com" in filters["allowed_domains"]
                            and "primer_relevamiento" not in json.loads(payload["input"])):
                        result = response("No encontré publicaciones citables en la búsqueda inicial.")
                    elif (self.linkedin_initial_posts and "linkedin.com" in filters["allowed_domains"]
                          and "primer_relevamiento" not in json.loads(payload["input"])):
                        result = response("Encontré una página comercial de LinkedIn.", [
                            {"type": "url_citation", "title": "Página de la organización",
                             "url": "https://www.linkedin.com/company/ficticia/"},
                        ])
                    else:
                        result = response("Fecha publicada en la red", [
                            {"type": "url_citation", "title": "Publicación del evento",
                             "url": "https://www.instagram.com/p/ficticio/"},
                        ])
                else:
                    result = evidence()
                result["output"].insert(0, {"type": "web_search_call", "status": "completed"})
                return httpx.Response(200, json=result)
            if payload["text"]["format"]["name"] == "intent":
                return httpx.Response(200, json=response(self.report_response.model_dump_json()))
            return httpx.Response(200, json=response(report().model_dump_json()))
        if request.url.host == "api.telegram.org":
            self.telegram_calls.append((path.split("/")[-1], request.content))
            return httpx.Response(200, json={"ok": True, "result": {"message_id": 1}})
        self.assertEqual(request.url.params["apiKey"], "maps-fixture")
        if path.endswith("/geocode/search"):
            address = request.url.params["text"].split(",")[0]
            if not address.startswith("Cabrera "):
                return httpx.Response(200, json={"results": []})
            number = address.split()[-1]
            index = int(number) - 4000
            matched = "4000" if self.fail_geocodes else number
            lon, lat = POINTS[index]
            return httpx.Response(200, json={"results": [{
                "lon": lon, "lat": lat, "result_type": "building", "housenumber": matched, "city": "CABA",
                "street": "Cabrera", "rank": {"confidence": 0.98},
            }]})
        if path.endswith("/routematrix"):
            payload = json.loads(request.content)
            size = len(payload["sources"])
            preferred = {(0, 2), (2, 4), (4, 1), (1, 3), (3, 5)}
            rows = [[{"time": 70 if (i, j) in preferred else 150, "distance": 250}
                     for j in range(size)] for i in range(size)]
            return httpx.Response(200, json={"sources_to_targets": rows})
        if path.endswith("/routing"):
            points = [tuple(reversed([float(value) for value in point.split(",")]))
                      for point in request.url.params["waypoints"].split("|")]
            lines = [points[i:i+2] for i in range(len(points)-1)]
            return httpx.Response(200, json={"features": [{
                "properties": {"legs": [{"distance": 200+i, "time": 70+i} for i in range(len(lines))]},
                "geometry": {"type": "MultiLineString", "coordinates": lines},
            }]})
        if path.endswith("/staticmap"):
            self.map_calls.append(request)
            return httpx.Response(200, content=self.map)
        raise AssertionError(f"Unexpected provider request: {request.url.host}{path}")

    async def enqueue(self, update_id, text):
        self.store.enqueue([update(update_id, text)], STAMP, self.settings.allowed_users)
        return self.store.next_job()

    async def test_research_mapped_pdf_delivery_and_follow_up(self):
        job = await self.enqueue(1, "/detallado Lugares para tocar en CABA")
        await self.bot.handle(job)
        self.assertEqual([call["max_tool_calls"] for call in self.openai_calls if "tools" in call], [3, 2])
        self.assertEqual(self.telegram_calls[0][0], "sendMessage")
        row = self.store.report(OWNER)
        self.assertIsNone(self.store.report(OWNER + 1, row["id"]))
        self.assertIsNone(self.store.select_report(OWNER + 1, row["id"]))
        snapshot = Snapshot.model_validate_json(row["data"])
        geographic = snapshot.geography
        self.assertEqual([point.place_index for point in geographic.points], [None, 1, 3, 0, 2, None])
        self.assertEqual(len(geographic.legs), 5)
        views = map_views(geographic)
        self.assertEqual([v.point_indices for v in views[1:]], [[0, 1, 2], [2, 3, 4], [4, 5]])
        self.assertEqual(len(self.map_calls), 4)
        layers = [json.loads(request.content)["geometries"] for request in self.map_calls]
        self.assertTrue(all(isinstance(json.loads(request.content)["area"], dict) for request in self.map_calls))
        self.assertEqual(len({layer["linecolor"] for layer in layers[0] if layer["linewidth"] == 5}), 3)
        self.assertTrue(all(layer["type"] == "polyline6" for layer in layers[0]))
        first_tramo = parse_qs(urlsplit(directions(geographic, views[1])).query)
        second_tramo = parse_qs(urlsplit(directions(geographic, views[2])).query)
        self.assertEqual(first_tramo["destination"], second_tramo["origin"])
        pdf = PdfReader(io.BytesIO(row["pdf"]))
        body = "\n".join(page.extract_text() for page in pdf.pages)
        self.assertIn("Mapa general", body)
        self.assertIn("Sector 3", body)
        self.assertIn("Comparativas", body)
        self.assertNotIn("Cartografía de fondo no disponible", body)
        annotations = [item.get_object() for page in pdf.pages for item in page.get("/Annots", [])]
        links = [item["/A"]["/URI"] for item in annotations if "/A" in item]
        self.assertIn(CITED, links)
        self.assertEqual(sum("/Dest" in item for item in annotations), 3)
        self.assertEqual([source.channel for source in snapshot.sources], ["web", "social"])

        sender = asyncio.create_task(self.bot.dispatch())
        try:
            async with asyncio.timeout(3):
                while self.store.next_delivery(STAMP) is not None:
                    await asyncio.sleep(0.01)
        finally:
            sender.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await sender
        self.assertIn("sendDocument", [method for method, _ in self.telegram_calls])

        self.report_response = intent().model_copy(update={"action": "chat", "answer": "La agenda está en [1]."})
        await self.bot.handle(await self.enqueue(2, "¿Dónde viste la agenda?"))
        turn = json.loads(self.openai_calls[-1]["input"])
        self.assertIn("Agenda y recorridos culturales", turn["contexto"])
        follow_up = self.store.next_delivery(STAMP)
        self.assertIn(CITED, json.loads(follow_up["delivery"])["text"])

    async def test_ambiguous_geocoding_keeps_evidence_and_omits_fake_route(self):
        self.fail_geocodes = True
        brief = report()
        geo = await self.bot.maps.plan(intent(), brief)
        self.assertEqual(len(geo.excluded), 4)
        self.assertFalse(geo.legs)
        self.assertFalse(geo.points)

    async def test_deep_research_uses_two_bounded_passes(self):
        self.report_response = intent().model_copy(update={"map_kind": "none"})
        prompt = "Soy DJ en Salta Capital. Buscá oportunidades y prepará un PDF."
        await self.bot.handle(await self.enqueue(1, f"/profundo\n\n{prompt}"))
        parsed_input = json.loads(self.openai_calls[0]["input"])
        self.assertEqual(parsed_input["pedido"], prompt)
        self.assertEqual(parsed_input["profundidad_explícita"], Depth.DEEP)
        self.assertEqual([call["max_tool_calls"] for call in self.openai_calls if "tools" in call], [3, 2, 2, 2])
        self.assertEqual(Snapshot.model_validate_json(self.store.report(OWNER)["data"]).depth, Depth.DEEP)

    async def test_extensive_research_requires_explicit_selection_and_two_quota_points(self):
        self.report_response = intent().model_copy(update={"depth": Depth.EXTENSIVE, "map_kind": "none"})
        implicit = await self.bot.research.intent("Quiero un informe largo", "", None)
        self.assertEqual(implicit.depth, Depth.DEEP)
        self.openai_calls.clear()
        await self.bot.handle(await self.enqueue(1, "/extenso\n\nInvestigá la agenda de Salta."))
        self.assertEqual([call["max_tool_calls"] for call in self.openai_calls if "tools" in call], [5, 3, 3, 4, 3])
        self.assertEqual(Snapshot.model_validate_json(self.store.report(OWNER)["data"]).depth, Depth.EXTENSIVE)
        day = self.store.report(OWNER)["created"][:10]
        self.assertFalse(self.store.reserve(day, "report", {OWNER: 3, 0: 12}, units=2))
        self.assertTrue(self.store.reserve(day, "report", {OWNER: 3, 0: 12}))

    async def test_social_failure_preserves_the_general_web_report(self):
        self.fail_social = True
        service = Research(self.client, "openai-fixture", self.settings.model)
        result, sources = await service.report(intent(), STAMP[:10])
        self.assertEqual([source.channel for source in sources], ["web"])
        self.assertTrue(any("Publicaciones en redes" in limit for limit in result.limitations))

    async def test_deep_research_retries_publications_using_discovered_entities(self):
        self.empty_initial_posts = True
        service = Research(self.client, "openai-fixture", self.settings.model)
        selected = intent().model_copy(update={"depth": Depth.DEEP})
        result, sources = await service.report(selected, STAMP[:10])
        targeted = [call for call in self.openai_calls if "primer_relevamiento" in json.loads(call["input"])]
        self.assertEqual(len(targeted), 1)
        self.assertEqual(targeted[0]["max_tool_calls"], 2)
        self.assertTrue(any(source.channel == "social" for source in sources))
        self.assertFalse(any(limit.startswith("Publicaciones en redes ·") for limit in result.limitations))

    async def test_requested_instagram_pass_is_not_satisfied_by_other_networks(self):
        self.linkedin_initial_posts = True
        service = Research(self.client, "openai-fixture", self.settings.model)
        selected = intent().model_copy(update={"depth": Depth.DEEP, "objective": "Priorizá Instagram en Salta."})
        await service.report(selected, STAMP[:10])
        retry = next(call for call in self.openai_calls if "primer_relevamiento" in json.loads(call["input"]))
        self.assertEqual(retry["tools"][0]["filters"]["allowed_domains"], ["instagram.com"])

    async def test_guides_and_guest_onboarding_do_not_call_openai(self):
        self.bot.username = "FixtureBot"
        commands = [(1, OWNER, "/start", "mensaje normal"), (2, OWNER, "/ayuda", "Dónde busca"),
                    (3, OWNER, "/ejemplos", "Comprar")]
        for identifier, owner, command, expected in commands:
            self.store.enqueue([update(identifier, command, owner)], STAMP, self.settings.allowed_users)
            await self.bot.handle(self.store.next_job())
            reply = self.store.next_delivery(STAMP)
            self.assertIn(expected, json.loads(reply["delivery"])["text"])
            self.store.finish(reply["id"])
        await self.bot.handle(await self.enqueue(4, "/invitar"))
        invitation = self.store.next_delivery(STAMP)
        code = json.loads(invitation["delivery"])["text"].split("?start=")[1]
        self.store.finish(4)
        for identifier, guest in [(5, OWNER + 1), (6, OWNER + 2)]:
            self.store.enqueue([update(identifier, f"/start {code}", guest)], STAMP, self.settings.allowed_users)
            await self.bot.handle(self.store.next_job())
        self.assertTrue(self.store.is_guest(OWNER + 1))
        self.assertFalse(self.store.is_guest(OWNER + 2))
        self.assertEqual(self.openai_calls, [])

    async def test_quick_research_can_deliver_a_cited_chat_message(self):
        self.report_response = intent().model_copy(update={"delivery": "telegram", "map_kind": "none"})
        await self.bot.handle(await self.enqueue(1, "/rapido Buscá opciones y contestá por chat."))
        self.assertEqual([call["max_tool_calls"] for call in self.openai_calls if "tools" in call], [1, 1])
        delivery = json.loads(self.store.next_delivery(STAMP)["delivery"])
        self.assertTrue(delivery["html"])
        self.assertIn(CITED, delivery["text"])
        self.assertFalse(self.map_calls)

    async def test_explicit_sector_count_overrides_the_size_default(self):
        geo = await self.bot.maps.plan(intent().model_copy(update={"sector_count": 2}), report())
        sectors = map_views(geo)[1:]
        self.assertEqual([sector.leg_indices for sector in sectors], [[0, 1, 2], [3, 4]])

    async def test_pdf_fallback_distinguishes_base_map_availability(self):
        geo = await self.bot.maps.plan(intent(), report())
        snapshot = Snapshot(
            created=STAMP, objective=intent().objective, report=report(), depth=Depth.STANDARD,
            geography=geo, sources=extract_sources([evidence()], STAMP),
        )
        result = build_pdf(snapshot, [None] * len(map_views(geo)))
        contents = "\n".join(page.extract_text() for page in PdfReader(io.BytesIO(result)).pages)
        self.assertIn("Cartografía de fondo no disponible", contents)
