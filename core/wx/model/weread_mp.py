"""Collect WeChat Official Account articles through WeRead Web."""

import time
from urllib.parse import quote

import requests
from bs4 import BeautifulSoup

from core.log import logger
from core.models.feed import Feed
from core.print import print_info
from core.wx.model.weread import MpsWeread


WEREAD_WEB_BASE = "https://weread.qq.com"


class WereadMPAPIError(Exception):
    def __init__(self, code, message, retriable=True):
        super().__init__(f"WeRead MP API error {code}: {message}")
        self.code = code
        self.message = message
        self.retriable = retriable


def build_mp_url(original_id: str) -> str:
    original_id = str(original_id or "").strip()
    if not original_id:
        return ""
    article_token = quote(original_id.replace("~", "_"), safe="_-")
    return f"https://mp.weixin.qq.com/s/{article_token}"


def _raise_response_error(payload: dict):
    code = payload.get("errCode", payload.get("errcode", 0))
    try:
        code = int(code or 0)
    except (TypeError, ValueError):
        code = 0
    if code == 0:
        return
    message = payload.get("errMsg") or payload.get("errmsg") or str(code)
    raise WereadMPAPIError(
        code,
        message,
        retriable=code not in (-2041, -2012, -2010),
    )


def parse_mp_articles(payload: dict):
    if not isinstance(payload, dict):
        raise WereadMPAPIError("invalid_response", "response is not an object")
    _raise_response_error(payload)

    groups = payload.get("reviews", []) or []
    articles = []
    for group in groups:
        group_time = group.get("createTime", 0)
        for sub_review in group.get("subReviews", []) or []:
            review = sub_review.get("review") or {}
            mp_info = review.get("mpInfo") or {}
            review_id = review.get("reviewId") or sub_review.get("reviewId")
            if not review_id:
                continue
            create_time = review.get("createTime") or group_time or 0
            update_time = mp_info.get("time") or create_time
            articles.append({
                "aid": review_id,
                "id": review_id,
                "title": mp_info.get("title", ""),
                "link": build_mp_url(mp_info.get("originalId", "")),
                "cover": mp_info.get("pic_url", ""),
                "digest": mp_info.get("content") or review.get("content", ""),
                "content": "",
                "create_time": create_time,
                "update_time": update_time,
                "read_num": mp_info.get("readNum", 0),
                "like_num": mp_info.get("likeNum", 0),
                "item_show_type": 0,
            })
    return articles, len(groups)


def extract_mp_content(html: str) -> str:
    soup = BeautifulSoup(html or "", "html.parser")
    content = soup.select_one("#js_content") or soup.select_one(".rich_media_content")
    if content is None:
        raise WereadMPAPIError("invalid_content", "article body was not found")
    for element in content.select("script, style"):
        element.decompose()
    return content.decode_contents().strip()


class MpsWereadMP(MpsWeread):
    """WeRead-backed collector for existing ``MP_WXS_*`` feeds."""

    def _get_feed_update_time(self, mp_id: str) -> int:
        from core.db import DB

        session = DB.get_session()
        try:
            row = session.query(Feed.update_time).filter(Feed.id == mp_id).first()
            return int(row[0] or 0) if row else 0
        finally:
            session.close()

    def _get_catchup_page_limit(self, requested_pages: int) -> int:
        from core.config import cfg

        configured_limit = int(cfg.get("weread.mp_max_pages", 20) or 20)
        return max(requested_pages, configured_limit, 1)

    def _get_content_interval(self) -> float:
        from core.config import cfg

        return max(float(cfg.get("weread.content_interval", 2) or 0), 0)

    def _get_page_interval(self) -> float:
        from core.config import cfg

        return max(float(cfg.get("weread.page_interval", 1) or 0), 0)

    def _request_headers(self, include_ticket=False):
        cookie = getattr(self, "_weread_cookies", "")
        if not str(cookie or "").strip():
            raise WereadMPAPIError(
                "missing_cookie",
                "WEREAD_COOKIE is required for WeRead Web requests",
                retriable=False,
            )
        headers = {
            "Cookie": cookie,
            "User-Agent": self.user_agent,
            "Accept": "application/json, text/plain, */*",
            "Accept-Language": "zh-CN,zh;q=0.9",
            "Referer": "https://weread.qq.com/",
        }
        ticket = getattr(self, "_weread_ticket", "")
        if include_ticket and str(ticket or "").strip():
            headers["x-wr-ticket"] = ticket
        return headers

    def _get_mp_articles_page(self, book_id: str, offset=0):
        try:
            response = requests.get(
                f"{WEREAD_WEB_BASE}/web/mp/articles",
                params={"bookId": book_id, "offset": offset},
                headers=self._request_headers(include_ticket=True),
                proxies=self._get_proxies(),
                timeout=(10, 30),
            )
        except requests.RequestException as exc:
            raise WereadMPAPIError("network_error", str(exc)) from exc
        if response.status_code != 200:
            raise WereadMPAPIError(
                response.status_code,
                f"article list returned HTTP {response.status_code}",
            )
        try:
            payload = response.json()
        except ValueError as exc:
            raise WereadMPAPIError("invalid_json", "article list is not JSON") from exc
        if not isinstance(payload, dict):
            raise WereadMPAPIError("invalid_response", "article list is not an object")
        _raise_response_error(payload)
        return payload

    def _get_mp_content(self, review_id: str):
        headers = self._request_headers()
        headers["Accept"] = "text/html,application/xhtml+xml,*/*"
        try:
            response = requests.get(
                f"{WEREAD_WEB_BASE}/web/mp/content",
                params={"reviewId": review_id},
                headers=headers,
                proxies=self._get_proxies(),
                timeout=(10, 30),
            )
        except requests.RequestException as exc:
            raise WereadMPAPIError("network_error", str(exc)) from exc
        if response.status_code != 200:
            raise WereadMPAPIError(
                response.status_code,
                f"article content returned HTTP {response.status_code}",
            )
        return extract_mp_content(response.text)

    def get_Articles(
        self,
        faker_id: str = None,
        Mps_id: str = None,
        Mps_title="",
        CallBack=None,
        start_page: int = 0,
        MaxPage: int = 1,
        interval=10,
        Gather_Content=False,
        Item_Over_CallBack=None,
        Over_CallBack=None,
    ):
        """Collect one existing WeChat feed without changing its RSS identity."""
        self.articles = []
        self.aids = []
        self.get_token()
        self.start_time = time.time()
        self.response_valid = False
        self.last_error = None
        self._load_weread_auth()

        if not self._weread_cookies:
            raise WereadMPAPIError(
                "missing_cookie",
                "WEREAD_COOKIE is required",
                retriable=False,
            )

        book_id = Mps_id if str(Mps_id or "").startswith("MP_WXS_") else faker_id
        if not str(book_id or "").startswith("MP_WXS_"):
            raise WereadMPAPIError(
                "invalid_book_id",
                "the feed id must use the MP_WXS_ prefix",
                retriable=False,
            )

        gather_content = bool(Gather_Content or self.Gather_Content)
        start_page = max(int(start_page or 0), 0)
        end_page = max(int(MaxPage or 1), start_page + 1)
        offset = start_page
        latest_publish_time = 0
        content_failures = []
        previous_update_time = self._get_feed_update_time(Mps_id)
        requested_pages = end_page - start_page
        page_limit = (
            self._get_catchup_page_limit(requested_pages)
            if previous_update_time
            else requested_pages
        )
        reached_previous_update = not previous_update_time
        content_request_count = 0
        content_interval = self._get_content_interval()
        page_interval = self._get_page_interval()

        print_info(f"微信读书公众号采集模式: {Mps_title} ({book_id})")
        try:
            for page in range(start_page, start_page + page_limit):
                if page > start_page and page_interval:
                    time.sleep(page_interval)
                payload = self._get_mp_articles_page(book_id, offset=offset)
                articles, group_count = parse_mp_articles(payload)
                self.response_valid = True

                for item in articles:
                    if super().HasGathered(item["aid"]):
                        continue
                    item["mp_id"] = Mps_id
                    latest_publish_time = max(
                        latest_publish_time,
                        int(item.get("update_time") or 0),
                    )
                    if previous_update_time and int(item.get("update_time") or 0) <= previous_update_time:
                        reached_previous_update = True
                    if gather_content:
                        if content_request_count and content_interval:
                            time.sleep(content_interval)
                        content_request_count += 1
                        try:
                            item["content"] = self._get_mp_content(item["aid"])
                        except WereadMPAPIError as exc:
                            logger.warning(f"微信读书正文获取失败 [{item['aid']}]: {exc}")
                            content_failures.append(item["aid"])
                            continue
                    if CallBack is not None:
                        super().FillBack(
                            CallBack=CallBack,
                            data=item,
                            Ext_Data={"mp_title": Mps_title, "mp_id": Mps_id},
                        )
                    if Item_Over_CallBack is not None:
                        Item_Over_CallBack(item)

                if group_count == 0:
                    reached_previous_update = True
                    break
                # WeRead defines offset in top-level review groups, not subReviews.
                offset += group_count
                if previous_update_time and reached_previous_update:
                    break

            if content_failures:
                raise WereadMPAPIError(
                    "content_incomplete",
                    f"{len(content_failures)} article bodies could not be fetched",
                )
            if not reached_previous_update:
                raise WereadMPAPIError(
                    "backlog_incomplete",
                    f"catch-up did not reach the previous update time after {page_limit} pages",
                )

            self.update_mps(
                Mps_id,
                Feed(
                    sync_time=int(time.time()),
                    update_time=latest_publish_time or None,
                ),
            )
        except Exception as exc:
            self.last_error = {
                "message": str(exc),
                "code": getattr(exc, "code", "weread_mp_error"),
                "retriable": getattr(exc, "retriable", True),
            }
            super().Over(CallBack=Over_CallBack)
            raise

        super().Over(CallBack=Over_CallBack)
