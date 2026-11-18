import json
import unittest
from unittest.mock import AsyncMock, patch

from itinerario.models import BUDGETS, Depth, Intent, Report, Source
from itinerario.providers import ProviderError
from itinerario.research import Research, attach_instagram_sources, instagram_targets


def result(text, url=None):
    annotations = [{"type": "url_citation", "title": "Aviso", "url": url}] if url else []
    return {"status": "completed", "output": [{"type": "message", "content": [
        {"type": "output_text", "text": text, "annotations": annotations},
    ]}]}


def job(country="ar", depth=Depth.DEEP, instagram="opcional"):
    return Intent(action="report", answer="", objective="Vacantes de recepcionista de hotel", area="Salta",
                  country_code=country, job_search=True, instagram=instagram, instagram_query="empleos Salta",
                  depth=depth, delivery="pdf", map_kind="none", origin="", finish="", return_to_origin=False,
                  transport="walk", sector_size=5, sector_count=0, sort_by="time")


class ResearchSocialTests(unittest.IsolatedAsyncioTestCase):
    def test_seeds_are_user_supplied_or_cited_and_canonical(self):
        def source(url):
            return Source(id=1, title="Fuente", url=url, accessed="")

        handles, posts = instagram_targets(
            "Mirá @Hotel.Norte y www.instagram.com/OTRO/reel/Ab_b2/?share=x. Correo: prueba@ejemplo.com",
            [source("https://www.instagram.com/stories/agencia/123/"), source("https://instagram.com/Hotel.Norte/"),
             source("https://www.instagram.com/explore/"), source("https://instagram.com.evil.example/org/"),
             source("https://www.instagram.com/p/Flyer/"), source("https://www.instagram.com/p/Flyer/?igsh=x")],
            allow_mentions=True)
        self.assertEqual(handles, ["hotel.norte", "otro", "agencia"])
        self.assertEqual(posts, ["https://www.instagram.com/reel/Ab_b2/", "https://www.instagram.com/p/Flyer/"])
        self.assertEqual(instagram_targets("en X @ejemplo", [], allow_mentions=False), ([], []))

    def test_multiple_ephemeral_stories_keep_separate_evidence(self):
        url = "https://www.instagram.com/stories/agencia/"
        sources = [Source(id=1, title="Agencia", url=url, accessed="", channel="social")]
        records = [{"url": url, "title": f"Historia {day}", "text": f"Flyer del día {day}"} for day in (2, 3)]
        evidence = attach_instagram_sources(sources, records, "2026-11-18")
        self.assertEqual(len(sources), 1)
        self.assertEqual([r["id"] for r in evidence], [1, 1])
        self.assertEqual([r["contenido"] for r in evidence], [r["text"] for r in records])

    async def run_report(self, intent, collect, *, text="", fail_web=False):
        service = Research(None, "test-key", "modelo", "fixture")
        calls = []
        main_response = dict(title="Vacantes", executive_summary="Resumen", findings=[], places=[], comparisons=[],
                             next_steps=[], limitations=[], methodology="Prueba")

        async def call(instructions, data, **kwargs):
            calls.append((data, kwargs))
            if kwargs.get("schema") is Report:
                return result(json.dumps(main_response))
            if fail_web:
                raise ProviderError("test", "search", 503)
            if data.get("portal") == "Computrabajo":
                raise ProviderError("test", "search", 429)
            if data.get("portal") == "LinkedIn Jobs":
                return result("Aviso", "https://www.linkedin.com/jobs/view/123/")
            return result("Resultados sin cuenta identificada", "https://agenda.example.org/vacantes")

        service.call = call
        with patch("itinerario.research.instagram.collect", collect):
            report, sources = await service.report(intent, "2026-11-18", user_text=text)
        return report, sources, calls

    async def test_job_boards_budget_discovery_and_visual_evidence_reach_report(self):
        selected = job()
        records = [{"url": "https://www.instagram.com/p/Prueba/", "title": "Flyer de convocatoria",
                    "text": "Texto visible en la imagen: se busca recepcionista; contactar al hotel."}]
        summary = "Instagram vía Apify: 1 publicación, 0 comentarios, 0 historias activas y 1 lectura visual."
        collect = AsyncMock(return_value=(records, [summary]))
        report, sources, calls = await self.run_report(selected, collect)
        self.assertEqual(collect.await_args.args[2:], ([], []))
        self.assertEqual(collect.await_args.kwargs["discovery_query"], "empleos Salta")
        self.assertTrue(callable(collect.await_args.kwargs["visual_reader"]))
        boards = {data["portal"]: kw["domains"] for data, kw in calls if "portal" in data}
        self.assertEqual(boards, {"LinkedIn Jobs": ("linkedin.com",), "Computrabajo": ("computrabajo.com",)})
        self.assertLessEqual(sum(kw.get("searches", 0) for _, kw in calls), BUDGETS[selected.depth].searches)
        self.assertTrue(any("Computrabajo · consulta no disponible" in limit for limit in report.limitations))
        self.assertTrue(report.methodology.startswith(summary))
        self.assertNotIn(summary, report.limitations)
        source = next(source for source in sources if source.url == records[0]["url"])
        final = calls[-1][0]["instagram_directo"]
        self.assertEqual(final[0]["id"], source.id)
        self.assertEqual(final[0]["contenido"], records[0]["text"])
        self.assertEqual(next(data["instagram_para_contrastar"] for data, _ in calls
                              if "instagram_para_contrastar" in data), records)

    async def test_quick_explicit_handle_survives_web_outage(self):
        collect = AsyncMock(return_value=([{
            "url": "https://www.instagram.com/p/Caso/", "title": "Post", "text": "Aviso institucional",
        }], []))
        _, sources, _ = await self.run_report(job(depth=Depth.QUICK, instagram="priorizar"), collect,
                                              text="Buscá en Instagram @agencia", fail_web=True)
        self.assertEqual(collect.await_args.args[2], ["agencia"])
        self.assertEqual(len(sources), 1)

    async def test_explicit_exclusion_and_market_respected(self):
        collect = AsyncMock()
        _, _, calls = await self.run_report(job(country="uk", instagram="omitir"), collect)
        collect.assert_not_awaited()
        self.assertEqual([data["portal"] for data, _ in calls if "portal" in data], ["LinkedIn Jobs"])
