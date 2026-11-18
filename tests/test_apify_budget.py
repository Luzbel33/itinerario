from datetime import datetime, timedelta, timezone
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import TestCase

from itinerario.store import Store


class FreeCreditTests(TestCase):
    def test_reservations_survive_restart_and_daily_quota_pruning(self):
        with TemporaryDirectory() as directory:
            store = Store(Path(directory))
            today = datetime.now(timezone.utc).date()
            try:
                for days, cents in [(32, 400), (31, 395)]:
                    store.db.execute("INSERT INTO apify_credit VALUES (?, ?)",
                                     ((today - timedelta(days=days)).isoformat(), cents))
                store.db.commit()
                self.assertTrue(store.reserve_apify_credit(5))
                store.prune(today.isoformat(), today.isoformat())
            finally:
                store.close()
            store = Store(Path(directory))
            try:
                self.assertFalse(store.reserve_apify_credit(5))
            finally:
                store.close()
