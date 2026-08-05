import asyncio
import sys
import unittest
from datetime import datetime
from types import ModuleType
from unittest.mock import patch

from fastapi import HTTPException
from pydantic import ValidationError
from sqlalchemy import create_engine, text
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session

from apis.weread import (
    WereadCollectRequest,
    WereadCookieRequest,
    WereadMPTestRequest,
    WereadSourceImportRequest,
    _derive_faker_id,
    _import_weread_sources_transactionally,
    _WereadSourceConflictError,
    clear_weread_cookie,
    collect_weread_notes,
    get_weread_status,
    import_weread_sources,
    router,
    save_weread_cookie,
    test_weread_mp_connection,
)
from core.auth import get_current_user_or_ak
from core.models.article import Article
from core.models.feed import FEATURED_MP_ID, Feed


class WereadConfigAPITest(unittest.TestCase):
    @patch("apis.weread._save_weread_data")
    @patch("apis.weread._load_weread_data", return_value={})
    def test_save_cookie_persists_article_list_ticket(self, _load, save):
        request = WereadCookieRequest(
            cookie="wr_vid=123; wr_skey=skey",
            ticket="ticket-value",
        )

        asyncio.run(save_weread_cookie(request, current_user={"id": "test"}))

        saved = save.call_args.args[0]
        self.assertEqual(saved["vid"], "123")
        self.assertEqual(saved["ticket"], "ticket-value")

    @patch("apis.weread._load_weread_data")
    def test_status_reports_ticket_separately_from_notes_configuration(self, load):
        load.return_value = {
            "cookie": "wr_vid=123; wr_skey=skey",
            "vid": "123",
            "ticket": "",
        }

        response = asyncio.run(get_weread_status(current_user={"id": "test"}))

        self.assertTrue(response["data"]["configured"])
        self.assertFalse(response["data"]["mp_configured"])
        self.assertFalse(response["data"]["has_ticket"])

    @patch("apis.weread.app_cfg.get")
    @patch("apis.weread._load_weread_data", return_value={})
    def test_status_reports_environment_managed_credentials(self, _load, config_get):
        values = {
            "weread.cookie": "wr_vid=456; wr_skey=env",
            "weread.ticket": "env-ticket",
            "weread.vid": "456",
        }
        config_get.side_effect = lambda key, default="": values.get(key, default)

        response = asyncio.run(get_weread_status(current_user={"id": "test"}))

        self.assertTrue(response["data"]["configured"])
        self.assertTrue(response["data"]["mp_configured"])
        self.assertTrue(response["data"]["managed_by_config"])

    @patch("apis.weread._save_weread_data")
    @patch("apis.weread._load_weread_data")
    def test_ticket_update_reuses_saved_cookie(self, load, save):
        load.return_value = {
            "cookie": "wr_vid=123; wr_skey=skey",
            "vid": "123",
            "ticket": "old-ticket",
        }
        request = WereadCookieRequest(ticket="new-ticket")

        asyncio.run(save_weread_cookie(request, current_user={"id": "test"}))

        saved = save.call_args.args[0]
        self.assertEqual(saved["cookie"], "wr_vid=123; wr_skey=skey")
        self.assertEqual(saved["ticket"], "new-ticket")

    @patch("apis.weread._save_weread_data")
    @patch("apis.weread._load_weread_data")
    def test_clear_cookie_also_clears_ticket(self, load, save):
        load.return_value = {
            "cookie": "wr_vid=123; wr_skey=skey",
            "vid": "123",
            "ticket": "ticket-value",
        }

        asyncio.run(clear_weread_cookie(current_user={"id": "test"}))

        saved = save.call_args.args[0]
        self.assertEqual(saved["cookie"], "")
        self.assertEqual(saved["vid"], "")
        self.assertEqual(saved["ticket"], "")

    @patch("apis.weread.app_cfg.get", return_value="env-value")
    @patch("apis.weread._save_weread_data")
    def test_clear_rejects_environment_managed_credentials(self, save, _config_get):
        response = asyncio.run(clear_weread_cookie(current_user={"id": "test"}))

        self.assertEqual(response["code"], 409)
        save.assert_not_called()

    @patch("core.wx.model.weread_mp.MpsWereadMP")
    def test_mp_connection_rejects_invalid_ticket(self, collector_class):
        from core.wx.model.weread_mp import WereadMPAPIError

        collector = collector_class.return_value
        collector._get_mp_articles_page.side_effect = WereadMPAPIError(
            -2041,
            "ticket expired",
            retriable=False,
        )

        response = asyncio.run(test_weread_mp_connection(
            WereadMPTestRequest(mp_id="MP_WXS_1"),
            current_user={"id": "test"},
        ))

        self.assertEqual(response["code"], 400)
        self.assertEqual(response["data"]["code"], -2041)

    @patch("core.db.DB.add_article", return_value=True)
    @patch("core.wx.model.weread.MpsWeread")
    def test_manual_note_collect_preserves_normalized_publish_time(self, collector_class, add_article):
        collector = collector_class.return_value
        collector._weread_cookies = "wr_vid=123; wr_skey=skey"
        collector._weread_ticket = "ticket-value"

        def collect(**kwargs):
            kwargs["CallBack"]({
                "id": "article-1",
                "mp_id": "MP_WXS_1",
                "title": "Article title",
                "url": "https://mp.weixin.qq.com/s/article-1",
                "pic_url": "https://example.test/cover.jpg",
                "content": "<p>Full text</p>",
                "publish_time": 1778580002,
            })

        collector.get_Articles.side_effect = collect
        request = WereadCollectRequest(mp_id="MP_WXS_1", mp_name="Feed")

        response = asyncio.run(
            collect_weread_notes(request, current_user={"id": "test"})
        )

        self.assertEqual(response["data"]["collected"], 1)
        saved = add_article.call_args.args[0]
        self.assertEqual(saved["publish_time"], 1778580002)


class WereadSourceImportAPITest(unittest.TestCase):
    def setUp(self):
        self.engine = create_engine(
            "sqlite:///:memory:",
            isolation_level="AUTOCOMMIT",
        )
        Feed.__table__.create(self.engine)

    def tearDown(self):
        self.engine.dispose()

    def _seed_feed(
        self,
        book_id: str,
        mp_name: str,
        *,
        status: int = 1,
        faker_id: str = None,
        engine=None,
    ):
        target_engine = engine or self.engine
        old_time = datetime(2025, 1, 2, 3, 4, 5)
        with Session(target_engine) as session:
            session.add(Feed(
                id=book_id,
                mp_name=mp_name,
                mp_cover="old-cover",
                mp_intro="old-intro",
                status=status,
                sync_time=100,
                update_time=200,
                created_at=old_time,
                updated_at=old_time,
                faker_id=faker_id if faker_id is not None else _derive_faker_id(book_id),
            ))
            session.commit()

    @staticmethod
    def _request(*sources):
        return WereadSourceImportRequest(sources=list(sources))

    def test_route_uses_shared_authentication_dependency(self):
        route = next(
            route
            for route in router.routes
            if route.path == "/weread/sources/import"
        )

        dependency_calls = [dependency.call for dependency in route.dependant.dependencies]
        self.assertIn(get_current_user_or_ak, dependency_calls)

    def test_request_rejects_invalid_reserved_and_duplicate_sources(self):
        boundary_id = "MP_WXS_" + "a" * 189
        self.assertEqual(
            WereadSourceImportRequest.model_validate({
                "sources": [{"book_id": boundary_id, "mp_name": "A"}],
            }).sources[0].book_id,
            boundary_id,
        )
        self.assertLessEqual(len(_derive_faker_id(boundary_id)), 255)

        invalid_payloads = [
            {"sources": []},
            {"sources": [{"book_id": "not-a-book", "mp_name": "A"}]},
            {"sources": [{"book_id": "MP_WXS_" + "a" * 190, "mp_name": "A"}]},
            {"sources": [{"book_id": FEATURED_MP_ID, "mp_name": "A"}]},
            {"sources": [{"book_id": "MP_WXS_a", "mp_name": " A"}]},
            {"sources": [
                {"book_id": "MP_WXS_a", "mp_name": "A"},
                {"book_id": "MP_WXS_a", "mp_name": "B"},
            ]},
            {"sources": [
                {"book_id": "MP_WXS_a", "mp_name": "Publisher"},
                {"book_id": "MP_WXS_b", "mp_name": "publisher"},
            ]},
            {"sources": [{
                "book_id": "MP_WXS_a",
                "mp_name": "A",
                "unexpected": True,
            }]},
            {
                "sources": [{"book_id": "MP_WXS_a", "mp_name": "A"}],
                "unexpected": True,
            },
        ]

        for payload in invalid_payloads:
            with self.subTest(payload=payload):
                with self.assertRaises(ValidationError):
                    WereadSourceImportRequest.model_validate(payload)

    def test_three_created_one_unchanged_and_idempotent_rerun(self):
        self._seed_feed("MP_WXS_existing", "罗斯基")
        request = self._request(
            {"book_id": "MP_WXS_existing", "mp_name": "罗斯基"},
            {"book_id": "MP_WXS_two", "mp_name": "公众号二"},
            {"book_id": "MP_WXS_three", "mp_name": "公众号三"},
            {"book_id": "MP_WXS_four", "mp_name": "公众号四"},
        )

        first = _import_weread_sources_transactionally(
            request.sources,
            engine=self.engine,
        )
        with Session(self.engine) as session:
            first_timestamps = {
                feed.id: (feed.created_at, feed.updated_at)
                for feed in session.query(Feed).all()
            }
            existing = session.get(Feed, "MP_WXS_existing")
            created = session.get(Feed, "MP_WXS_two")
            self.assertEqual(existing.mp_cover, "old-cover")
            self.assertEqual(created.mp_cover, "")
            self.assertEqual(created.mp_intro, "")
            self.assertEqual(created.status, 1)
            self.assertEqual(created.sync_time, 0)
            self.assertEqual(created.update_time, 0)
            self.assertEqual(created.faker_id, _derive_faker_id("MP_WXS_two"))

        second = _import_weread_sources_transactionally(
            request.sources,
            engine=self.engine,
        )
        with Session(self.engine) as session:
            second_timestamps = {
                feed.id: (feed.created_at, feed.updated_at)
                for feed in session.query(Feed).all()
            }

        self.assertEqual(first["created"], 3)
        self.assertEqual(first["unchanged"], 1)
        self.assertEqual(first["total"], 4)
        self.assertEqual(
            [item["status"] for item in first["items"]],
            ["unchanged", "created", "created", "created"],
        )
        self.assertEqual(second["created"], 0)
        self.assertEqual(second["unchanged"], 4)
        self.assertEqual(second["total"], 4)
        self.assertEqual(first_timestamps, second_timestamps)

    def test_existing_identity_conflicts_are_rejected(self):
        cases = [
            (
                {
                    "book_id": "MP_WXS_same_id",
                    "mp_name": "Old Name",
                },
                {"book_id": "MP_WXS_same_id", "mp_name": "New Name"},
                "book_id_name_mismatch",
            ),
            (
                {
                    "book_id": "MP_WXS_other",
                    "mp_name": "Publisher",
                },
                {"book_id": "MP_WXS_new", "mp_name": "publisher"},
                "name_assigned_to_other_id",
            ),
            (
                {
                    "book_id": "MP_WXS_disabled",
                    "mp_name": "Disabled",
                    "status": 0,
                },
                {"book_id": "MP_WXS_disabled", "mp_name": "Disabled"},
                "feed_not_active",
            ),
            (
                {
                    "book_id": "MP_WXS_other",
                    "mp_name": "Other",
                    "faker_id": _derive_faker_id("MP_WXS_new"),
                },
                {"book_id": "MP_WXS_new", "mp_name": "New"},
                "faker_id_assigned_to_other_feed",
            ),
            (
                {
                    "book_id": "MP_WXS_same_id",
                    "mp_name": "Same",
                    "faker_id": "wrong-faker-id",
                },
                {"book_id": "MP_WXS_same_id", "mp_name": "Same"},
                "faker_id_mismatch",
            ),
        ]

        for index, (existing, requested, expected_code) in enumerate(cases):
            engine = create_engine(
                f"sqlite:///file:weread-conflict-{index}?mode=memory&cache=shared&uri=true",
                isolation_level="AUTOCOMMIT",
            )
            try:
                Feed.__table__.create(engine)
                self._seed_feed(engine=engine, **existing)
                request = self._request(requested)

                with self.subTest(expected_code=expected_code):
                    with self.assertRaises(_WereadSourceConflictError) as caught:
                        _import_weread_sources_transactionally(
                            request.sources,
                            engine=engine,
                        )

                    codes = {item["code"] for item in caught.exception.conflicts}
                    self.assertIn(expected_code, codes)
            finally:
                engine.dispose()

    def test_conflict_returns_409_and_does_not_create_other_rows(self):
        self._seed_feed("MP_WXS_existing", "Existing")
        request = self._request(
            {"book_id": "MP_WXS_new", "mp_name": "New"},
            {"book_id": "MP_WXS_existing", "mp_name": "Changed"},
        )

        with patch("apis.weread.DB.get_engine", return_value=self.engine):
            with self.assertRaises(HTTPException) as caught:
                asyncio.run(import_weread_sources(
                    request,
                    current_user={"id": "test"},
                ))

        self.assertEqual(caught.exception.status_code, 409)
        self.assertEqual(caught.exception.detail["code"], 40901)
        with Session(self.engine) as session:
            self.assertIsNone(session.get(Feed, "MP_WXS_new"))
            self.assertEqual(session.query(Feed).count(), 1)

    def test_database_failure_rolls_back_entire_batch(self):
        with self.engine.begin() as connection:
            connection.execute(text("""
                CREATE TRIGGER fail_second_feed
                BEFORE INSERT ON feeds
                WHEN NEW.id = 'MP_WXS_fail'
                BEGIN
                    SELECT RAISE(ABORT, 'forced transaction failure');
                END;
            """))
        request = self._request(
            {"book_id": "MP_WXS_first", "mp_name": "First"},
            {"book_id": "MP_WXS_fail", "mp_name": "Fail"},
        )

        with self.assertRaises(SQLAlchemyError):
            _import_weread_sources_transactionally(
                request.sources,
                engine=self.engine,
            )

        with patch("apis.weread.DB.get_engine", return_value=self.engine):
            with self.assertRaises(HTTPException) as caught:
                asyncio.run(import_weread_sources(
                    request,
                    current_user={"id": "test"},
                ))

        self.assertEqual(caught.exception.status_code, 500)
        self.assertEqual(caught.exception.detail["code"], 50001)
        with Session(self.engine) as session:
            self.assertEqual(session.query(Feed).count(), 0)

    def test_endpoint_has_no_article_queue_remote_avatar_or_cookie_side_effects(self):
        Article.__table__.create(self.engine)
        request = self._request(
            {"book_id": "MP_WXS_new", "mp_name": "New"},
        )

        class UnexpectedImport(ModuleType):
            def __getattr__(self, name):
                raise AssertionError(f"unexpected side-effect import: {self.__name__}.{name}")

        blocked_modules = {
            module_name: UnexpectedImport(module_name)
            for module_name in (
                "core.queue",
                "core.res",
                "core.wx",
                "core.wx.model.weread",
                "core.wx.model.weread_mp",
            )
        }

        with (
            patch("apis.weread.DB.get_engine", return_value=self.engine),
            patch("apis.weread._load_weread_data") as load_credentials,
            patch("apis.weread._save_weread_data") as save_credentials,
            patch.dict(sys.modules, blocked_modules),
        ):
            response = asyncio.run(import_weread_sources(
                request,
                current_user={"id": "test"},
            ))

        self.assertEqual(response["code"], 0)
        self.assertEqual(response["data"]["created"], 1)
        load_credentials.assert_not_called()
        save_credentials.assert_not_called()
        with Session(self.engine) as session:
            self.assertEqual(session.query(Article).count(), 0)


if __name__ == "__main__":
    unittest.main()
