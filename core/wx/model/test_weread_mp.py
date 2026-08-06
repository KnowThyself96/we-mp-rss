import unittest
from unittest.mock import Mock, patch

import requests

from core.wx.model.weread_mp import (
    MpsWereadMP,
    WereadMPAPIError,
    build_mp_url,
    extract_mp_content,
    parse_mp_articles,
)


class WereadMpParsingTest(unittest.TestCase):
    def test_parse_mp_articles_maps_article_fields(self):
        payload = {
            "synckey": 1782304237,
            "reviews": [
                {
                    "createTime": 1778580000,
                    "subReviews": [
                        {
                            "reviewId": "MP_WXS_1_review-1",
                            "review": {
                                "reviewId": "MP_WXS_1_review-1",
                                "createTime": 1778580001,
                                "mpInfo": {
                                    "title": "Article title",
                                    "content": "Article summary",
                                    "time": 1778580002,
                                    "originalId": "abc~def",
                                    "pic_url": "https://example.test/cover.jpg",
                                    "readNum": 12,
                                    "likeNum": 3,
                                },
                            },
                        }
                    ],
                }
            ],
        }

        articles, group_count = parse_mp_articles(payload)

        self.assertEqual(group_count, 1)
        self.assertEqual(len(articles), 1)
        self.assertEqual(
            articles[0],
            {
                "aid": "MP_WXS_1_review-1",
                "id": "MP_WXS_1_review-1",
                "title": "Article title",
                "link": "https://mp.weixin.qq.com/s/abc_def",
                "cover": "https://example.test/cover.jpg",
                "digest": "Article summary",
                "content": "",
                "create_time": 1778580001,
                "update_time": 1778580002,
                "read_num": 12,
                "like_num": 3,
                "item_show_type": 0,
            },
        )

    def test_parse_mp_articles_classifies_rate_limit(self):
        with self.assertRaises(WereadMPAPIError) as caught:
            parse_mp_articles({"errCode": -2041, "errMsg": "request blocked"})

        self.assertEqual(caught.exception.code, -2041)
        self.assertFalse(caught.exception.retriable)

    def test_extract_mp_content_returns_article_body(self):
        html = """
        <html><body>
          <div id="js_content"><p>Hello</p><script>bad()</script></div>
        </body></html>
        """

        self.assertEqual(extract_mp_content(html), '<p>Hello</p>')

    def test_build_mp_url_rejects_missing_original_id(self):
        self.assertEqual(build_mp_url(""), "")


class WereadMpRequestTest(unittest.TestCase):
    def make_collector(self):
        collector = object.__new__(MpsWereadMP)
        collector._weread_cookies = "wr_vid=1; wr_skey=skey"
        collector._weread_ticket = "ticket-value"
        collector.user_agent = "test-agent"
        collector.proxy_enabled = False
        collector.http_proxy_url = ""
        return collector

    @patch("requests.get")
    def test_article_list_request_uses_official_endpoint_and_ticket(self, get):
        response = Mock(status_code=200)
        response.json.return_value = {"reviews": []}
        get.return_value = response
        collector = self.make_collector()

        payload = collector._get_mp_articles_page("MP_WXS_1", offset=20)

        self.assertEqual(payload, {"reviews": []})
        _, kwargs = get.call_args
        self.assertEqual(get.call_args.args[0], "https://weread.qq.com/web/mp/articles")
        self.assertEqual(
            kwargs["params"],
            {"bookId": "MP_WXS_1", "offset": 20},
        )
        self.assertEqual(kwargs["headers"]["x-wr-ticket"], "ticket-value")
        self.assertEqual(kwargs["timeout"], (10, 30))
        get.assert_called_once()

    @patch("requests.get")
    def test_article_list_allows_cookie_only_and_omits_ticket_header(self, get):
        response = Mock(status_code=200)
        response.json.return_value = {"reviews": []}
        get.return_value = response
        collector = self.make_collector()
        collector._weread_ticket = ""

        payload = collector._get_mp_articles_page("MP_WXS_1", offset=0)

        self.assertEqual(payload, {"reviews": []})
        self.assertNotIn("x-wr-ticket", get.call_args.kwargs["headers"])
        self.assertEqual(get.call_args.kwargs["headers"]["Cookie"], "wr_vid=1; wr_skey=skey")
        get.assert_called_once()

    @patch("requests.get")
    def test_article_list_rejects_missing_cookie_before_request(self, get):
        collector = self.make_collector()

        for cookie in ("", " \t"):
            with self.subTest(cookie=repr(cookie)):
                collector._weread_cookies = cookie
                with self.assertRaises(WereadMPAPIError) as caught:
                    collector._get_mp_articles_page("MP_WXS_1", offset=0)

                self.assertEqual(caught.exception.code, "missing_cookie")
                self.assertFalse(caught.exception.retriable)
        get.assert_not_called()

    @patch("requests.get")
    def test_article_list_reports_http_auth_failure_without_retry(self, get):
        get.return_value = Mock(status_code=401)
        collector = self.make_collector()

        with self.assertRaises(WereadMPAPIError) as caught:
            collector._get_mp_articles_page("MP_WXS_1", offset=0)

        self.assertEqual(caught.exception.code, 401)
        get.assert_called_once()

    @patch("requests.get")
    def test_article_list_reports_rate_limit_without_retry(self, get):
        response = Mock(status_code=200)
        response.json.return_value = {"errCode": -2041, "errMsg": "request blocked"}
        get.return_value = response
        collector = self.make_collector()

        with self.assertRaises(WereadMPAPIError) as caught:
            collector._get_mp_articles_page("MP_WXS_1", offset=0)

        self.assertEqual(caught.exception.code, -2041)
        self.assertFalse(caught.exception.retriable)
        get.assert_called_once()

    @patch("requests.get")
    def test_article_list_reports_network_failure_without_retry(self, get):
        collector = self.make_collector()

        for error in (requests.Timeout("timed out"), requests.ConnectionError("offline")):
            with self.subTest(error=type(error).__name__):
                get.reset_mock()
                get.side_effect = error
                with self.assertRaises(WereadMPAPIError) as caught:
                    collector._get_mp_articles_page("MP_WXS_1", offset=0)

                self.assertEqual(caught.exception.code, "network_error")
                get.assert_called_once()

    @patch("requests.get")
    def test_article_list_rejects_invalid_response_without_retry(self, get):
        collector = self.make_collector()
        cases = (
            (ValueError("not json"), "invalid_json"),
            (["not", "an", "object"], "invalid_response"),
        )

        for result, expected_code in cases:
            with self.subTest(expected_code=expected_code):
                get.reset_mock()
                response = Mock(status_code=200)
                if isinstance(result, Exception):
                    response.json.side_effect = result
                else:
                    response.json.return_value = result
                get.side_effect = None
                get.return_value = response

                with self.assertRaises(WereadMPAPIError) as caught:
                    collector._get_mp_articles_page("MP_WXS_1", offset=0)

                self.assertEqual(caught.exception.code, expected_code)
                get.assert_called_once()

    @patch("requests.get")
    def test_content_request_extracts_official_article_html(self, get):
        response = Mock(
            status_code=200,
            text='<html><div id="js_content"><p>Full text</p></div></html>',
        )
        get.return_value = response
        collector = self.make_collector()

        content = collector._get_mp_content("MP_WXS_1_review-1")

        self.assertEqual(content, "<p>Full text</p>")
        self.assertEqual(get.call_args.args[0], "https://weread.qq.com/web/mp/content")
        self.assertEqual(
            get.call_args.kwargs["params"],
            {"reviewId": "MP_WXS_1_review-1"},
        )


class WereadMpCollectorTest(unittest.TestCase):
    def make_collector(self):
        collector = object.__new__(MpsWereadMP)
        collector.articles = []
        collector.aids = []
        collector.start_time = None
        collector.Gather_Content = False
        collector._weread_cookies = "wr_vid=1; wr_skey=skey"
        collector._weread_ticket = "ticket-value"
        collector._weread_vid = "1"
        collector._weread_name = "tester"
        collector._cookies = {}
        collector.user_agent = "test-agent"
        collector.proxy_enabled = False
        collector.http_proxy_url = ""
        collector.get_token = Mock()
        collector._load_weread_auth = Mock()
        collector.update_mps = Mock()
        collector._get_feed_update_time = Mock(return_value=0)
        collector._get_content_interval = Mock(return_value=0)
        collector._get_page_interval = Mock(return_value=0)
        return collector

    @patch("core.wx.base.RSS.clear_cache")
    @patch("core.wx.base.setStatus")
    def test_get_articles_collects_mp_article_and_advances_after_success(
        self, _set_status, _clear_cache
    ):
        collector = self.make_collector()
        collector._get_mp_articles_page = Mock(return_value={
            "reviews": [{
                "subReviews": [{
                    "review": {
                        "reviewId": "MP_WXS_1_review-1",
                        "createTime": 1778580001,
                        "mpInfo": {
                            "title": "Article title",
                            "content": "Summary",
                            "time": 1778580002,
                            "originalId": "abc~def",
                            "pic_url": "https://example.test/cover.jpg",
                        },
                    }
                }]
            }]
        })
        collector._get_mp_content = Mock(return_value="<p>Full text</p>")
        saved = []

        collector.get_Articles(
            faker_id="legacy-fake-id",
            Mps_id="MP_WXS_1",
            Mps_title="Feed title",
            CallBack=lambda article: saved.append(article) or True,
            MaxPage=1,
            Gather_Content=True,
            interval=0,
        )

        collector._get_mp_articles_page.assert_called_once_with("MP_WXS_1", offset=0)
        self.assertEqual(saved[0]["url"], "https://mp.weixin.qq.com/s/abc_def")
        self.assertEqual(saved[0]["content"], "<p>Full text</p>")
        self.assertEqual(saved[0]["publish_time"], 1778580002)
        self.assertEqual(collector.all_count(), 1)
        collector.update_mps.assert_called_once()
        updated_feed = collector.update_mps.call_args.args[1]
        self.assertEqual(updated_feed.update_time, 1778580002)

    @patch("core.wx.base.RSS.clear_cache")
    def test_get_articles_does_not_advance_after_list_failure(self, _clear_cache):
        collector = self.make_collector()
        collector._get_mp_articles_page = Mock(
            side_effect=WereadMPAPIError(-2041, "blocked", retriable=False)
        )

        with self.assertRaises(WereadMPAPIError):
            collector.get_Articles(
                Mps_id="MP_WXS_1",
                Mps_title="Feed title",
                CallBack=lambda article: True,
                MaxPage=1,
                interval=0,
            )

        collector.update_mps.assert_not_called()

    @patch("core.wx.base.RSS.clear_cache")
    def test_pagination_counts_groups_not_articles(self, _clear_cache):
        collector = self.make_collector()
        collector._get_mp_articles_page = Mock(side_effect=[
            {
                "reviews": [
                    {"subReviews": [{} for _ in range(20)]},
                ],
            },
            {"reviews": []},
        ])

        collector.get_Articles(
            Mps_id="MP_WXS_1",
            Mps_title="Feed title",
            CallBack=lambda article: True,
            MaxPage=2,
            interval=0,
        )

        self.assertEqual(
            collector._get_mp_articles_page.call_args_list,
            [
                unittest.mock.call("MP_WXS_1", offset=0),
                unittest.mock.call("MP_WXS_1", offset=1),
            ],
        )

    @patch("core.wx.base.RSS.clear_cache")
    def test_scheduled_run_catches_up_to_previous_update_time(self, _clear_cache):
        collector = self.make_collector()
        collector._get_feed_update_time.return_value = 1778580001
        collector._get_catchup_page_limit = Mock(return_value=3)
        collector._get_mp_articles_page = Mock(side_effect=[
            {
                "reviews": [{
                    "subReviews": [{
                        "review": {
                            "reviewId": "MP_WXS_1_new",
                            "createTime": 1778580003,
                            "mpInfo": {"title": "New", "originalId": "new"},
                        }
                    }]
                }]
            },
            {
                "reviews": [{
                    "subReviews": [{
                        "review": {
                            "reviewId": "MP_WXS_1_old",
                            "createTime": 1778580001,
                            "mpInfo": {"title": "Old", "originalId": "old"},
                        }
                    }]
                }]
            },
        ])

        collector.get_Articles(
            Mps_id="MP_WXS_1",
            Mps_title="Feed title",
            CallBack=lambda article: True,
            MaxPage=1,
            interval=0,
        )

        self.assertEqual(
            collector._get_mp_articles_page.call_args_list,
            [
                unittest.mock.call("MP_WXS_1", offset=0),
                unittest.mock.call("MP_WXS_1", offset=1),
            ],
        )
        collector.update_mps.assert_called_once()

    @patch("core.wx.base.RSS.clear_cache")
    def test_incomplete_catchup_does_not_mark_feed_synced(self, _clear_cache):
        collector = self.make_collector()
        collector._get_feed_update_time.return_value = 1778580001
        collector._get_catchup_page_limit = Mock(return_value=1)
        collector._get_mp_articles_page = Mock(return_value={
            "reviews": [{
                "subReviews": [{
                    "review": {
                        "reviewId": "MP_WXS_1_new",
                        "createTime": 1778580003,
                        "mpInfo": {"title": "New", "originalId": "new"},
                    }
                }]
            }]
        })

        with self.assertRaises(WereadMPAPIError) as caught:
            collector.get_Articles(
                Mps_id="MP_WXS_1",
                Mps_title="Feed title",
                CallBack=lambda article: True,
                MaxPage=1,
                interval=0,
            )

        self.assertEqual(caught.exception.code, "backlog_incomplete")
        collector.update_mps.assert_not_called()

    @patch("core.wx.model.weread_mp.time.sleep")
    @patch("core.wx.base.RSS.clear_cache")
    def test_fulltext_requests_are_throttled_between_articles(self, _clear_cache, sleep):
        collector = self.make_collector()
        collector._get_content_interval.return_value = 2
        collector._get_mp_articles_page = Mock(return_value={
            "reviews": [{
                "subReviews": [
                    {
                        "review": {
                            "reviewId": "MP_WXS_1_a",
                            "createTime": 1778580002,
                            "mpInfo": {"title": "A", "originalId": "a"},
                        }
                    },
                    {
                        "review": {
                            "reviewId": "MP_WXS_1_b",
                            "createTime": 1778580001,
                            "mpInfo": {"title": "B", "originalId": "b"},
                        }
                    },
                ]
            }]
        })
        collector._get_mp_content = Mock(return_value="<p>Full text</p>")

        collector.get_Articles(
            Mps_id="MP_WXS_1",
            Mps_title="Feed title",
            CallBack=lambda article: True,
            MaxPage=1,
            Gather_Content=True,
            interval=0,
        )

        sleep.assert_called_once_with(2)

    @patch("core.wx.base.RSS.clear_cache")
    def test_fulltext_failure_is_not_saved_or_marked_synced(self, _clear_cache):
        collector = self.make_collector()
        collector._get_mp_articles_page = Mock(return_value={
            "reviews": [{
                "subReviews": [{
                    "review": {
                        "reviewId": "MP_WXS_1_review-1",
                        "createTime": 1778580001,
                        "mpInfo": {
                            "title": "Article title",
                            "originalId": "abc~def",
                        },
                    }
                }]
            }]
        })
        collector._get_mp_content = Mock(
            side_effect=WereadMPAPIError(503, "content unavailable")
        )
        saved = []

        with self.assertRaises(WereadMPAPIError) as caught:
            collector.get_Articles(
                Mps_id="MP_WXS_1",
                Mps_title="Feed title",
                CallBack=lambda article: saved.append(article) or True,
                MaxPage=1,
                Gather_Content=True,
                interval=0,
            )

        self.assertEqual(caught.exception.code, "content_incomplete")
        self.assertEqual(saved, [])
        collector.update_mps.assert_not_called()

if __name__ == "__main__":
    unittest.main()
