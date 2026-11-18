import json
import unittest
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock

import httpx

from itinerario.instagram import COMMENTS, POSTS, STORIES, collect, post_link, profile_handle


PROFILE = "fabrica.salta"
POST = "https://www.instagram.com/p/ABC_123/"
IMAGE = "https://scontent.cdninstagram.com/flyer.jpg"
VIDEO = "https://scontent.cdninstagram.com/story.mp4"


class InstagramTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.calls = []
        self.account_status = 200
        self.limit_status = 200
        self.plan = {"monthlyBasePriceUsd": 0, "tier": "FREE", "monthlyUsageCreditsUsd": 5}
        self.limits = {"limits": {"maxMonthlyUsageUsd": 5}, "current": {"monthlyUsageUsd": 1.40}}
        date = datetime.now(timezone.utc)
        self.rows = {
            POSTS: [{"url": POST, "ownerUsername": PROFILE, "caption": "Buscamos cocinero en Salta.",
                     "timestamp": date.isoformat(), "displayUrl": IMAGE},
                    {"error": "private"}],
            COMMENTS: [{"id": "987654321", "postUrl": POST, "text": "Cierra el viernes.", "timestamp": date.isoformat()},
                       {"postUrl": "https://attacker.test/p/ABCD/", "text": "Should never appear"}],
            STORIES: [{"username": PROFILE, "kind": "story", "mediaType": "video", "isLive": True,
                       "takenAt": date.isoformat(), "storyUrl": f"https://www.instagram.com/stories/{PROFILE}/",
                       "videoUrl": VIDEO},
                      {"username": PROFILE, "kind": "story", "isLive": True,
                       "takenAt": (date - timedelta(days=2)).isoformat(), "videoUrl": VIDEO},
                      {"username": "someone_else", "isPrivate": True, "kind": "story", "isLive": True,
                       "takenAt": date.isoformat(), "videoUrl": VIDEO}],
        }
        self.reader = AsyncMock(return_value="Lectura visual: enviar CV hasta el jueves.")

    async def collect(self, handles=(PROFILE,), urls=(), **kwargs):
        def respond(req):
            self.assertEqual(req.url.host, "api.apify.com")
            self.assertEqual(req.headers["Authorization"], "Bearer test")
            self.assertNotIn("token", req.url.params)
            if req.url.path == "/v2/users/me":
                return httpx.Response(self.account_status, json={"data": {"plan": self.plan}})
            if req.url.path == "/v2/users/me/limits":
                return httpx.Response(self.limit_status, json={"data": self.limits})
            actor = req.url.path.split("/")[3]
            self.assertEqual(req.url.path, f"/v2/actors/{actor}/run-sync-get-dataset-items")
            self.assertEqual(req.method, "POST")
            self.assertEqual(req.url.params["maxTotalChargeUsd"], "0.05")
            self.assertEqual(req.url.params["restartOnError"], "false")
            self.calls.append((actor, json.loads(req.content)))
            if self.calls[-1][1].get("searchType") == "user":
                return httpx.Response(200, json=[
                    {"username": PROFILE, "isBusinessAccount": True, "private": False},
                    {"username": "private", "isBusinessAccount": True, "private": True},
                    {"username": "personal", "isBusinessAccount": False, "private": False},
                ])
            rows = self.rows[actor]
            if isinstance(rows, Exception):
                raise rows
            return httpx.Response(200, json=rows)

        async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
            return await collect(client, "test", handles, urls, visual_reader=self.reader, objective="vacante cocinero",
                                 **kwargs)

    def test_seeds_are_public_instagram_entities(self):
        self.assertEqual(profile_handle("https://instagram.com/Fabrica.Salta/?igsh=secret"), PROFILE)
        self.assertEqual(post_link("https://www.instagram.com/empresa/reel/A9BCd/?igsh=secret"),
                         "https://www.instagram.com/reel/A9BCd/")
        for bad in ("https://instagram.com.evil.test/user", "https://instagram.com@evil.test/user",
                    "https://www.instagram.com/accounts", "@../", "@a..bc", "stories"):
            self.assertFalse(profile_handle(bad))

    async def test_sources_from_public_posts_comments_and_current_stories(self):
        records, coverage = await self.collect()
        self.assertEqual(len(records), 3)
        self.assertEqual(self.reader.await_count, 2)
        self.assertIn("Cierra el viernes", records[-1]["text"])
        self.assertEqual(records[-1]["url"], POST + "c/987654321/")
        self.assertTrue(any("enlace temporal" in r["text"] and r["url"].endswith(f"/{PROFILE}/") for r in records))
        self.assertTrue(any("1 historias" in s for s in coverage))
        self.assertIn("inaccesible", coverage[0])
        self.assertEqual(dict(self.calls)[COMMENTS]["directUrls"], [POST])

    async def test_direct_post_discovers_its_owner_for_stories(self):
        await self.collect((), (POST,))
        self.assertEqual(dict(self.calls)[STORIES]["usernames"], [PROFILE])

    async def test_profile_discovery_without_hardcoded_handles(self):
        await self.collect((), (), discovery_query="Fábrica de Música Salta")
        self.assertEqual(self.calls[0][1]["search"], "Fábrica de Música Salta")
        self.assertEqual(dict(self.calls)[STORIES]["usernames"], [PROFILE])
        self.assertEqual(len(self.calls), 4)

    async def test_scoped_attestation_reserves_credit_before_each_run(self):
        self.account_status = self.limit_status = 403
        self.assertFalse((await self.collect())[0])
        self.assertFalse(self.calls)
        charges = []

        def reserve(cents):
            charges.append(cents)
            return len(charges) <= 2

        await self.collect(reserve_free_credit=reserve)
        self.assertEqual(charges, [5, 5, 5])
        self.assertEqual(len(self.calls), 2)
        self.calls.clear()
        self.account_status = 200
        self.plan["tier"] = "STARTER"
        self.assertFalse((await self.collect(reserve_free_credit=reserve))[0])
        self.account_status = 500
        self.assertFalse((await self.collect(reserve_free_credit=reserve))[0])
        self.assertFalse(self.calls)

    async def test_timeout_does_not_restart_an_actor_or_discard_other_evidence(self):
        self.rows[STORIES] = httpx.ReadTimeout("timeout")
        records, coverage = await self.collect()
        self.assertEqual([a for a, _ in self.calls].count(STORIES), 1)
        self.assertEqual(len(records), 2)
        self.assertTrue(any("historias no disponible" in s for s in coverage))

    async def test_no_charges_with_paid_unknown_overage_or_exhausted_plans(self):
        for field, value in (("tier", "STARTER"), ("monthlyBasePriceUsd", 1), ("monthlyUsageCreditsUsd", None),
                             ("monthlyUsageCreditsUsd", 2), ("monthlyUsageCreditsUsd", float("nan"))):
            with self.subTest(field=field, value=value):
                saved = self.plan[field]
                self.plan[field] = value
                records, _ = await self.collect()
                self.assertFalse(records)
                self.assertFalse(self.calls)
                self.plan[field] = saved
        self.limits["current"]["monthlyUsageUsd"] = 4.99
        self.assertFalse((await self.collect())[0])
        self.assertFalse(self.calls)
