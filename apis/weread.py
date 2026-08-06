"""
微信读书(Weread)管理 API

提供 Cookie 配置、连接测试、手动采集等功能
"""

import base64
import json
import os
from collections import defaultdict
from datetime import datetime
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator
from sqlalchemy.orm import Session

from .base import success_response, error_response
from core.auth import get_current_user_or_ak
from core.config import Config, cfg as app_cfg
from core.db import DB
from core.models.base import DATA_STATUS
from core.models.feed import FEATURED_MP_ID, Feed

router = APIRouter(prefix="/weread", tags=["微信读书"])


class WereadCookieRequest(BaseModel):
    cookie: Optional[str] = None
    ticket: Optional[str] = None
    vid: Optional[str] = ""
    name: Optional[str] = ""


class WereadCollectRequest(BaseModel):
    mp_id: str
    mp_name: Optional[str] = ""
    faker_id: Optional[str] = ""  # 书籍 bookId，为空则采集全部书架
    max_page: int = 1
    gather_content: bool = True


class WereadMPTestRequest(BaseModel):
    mp_id: Optional[str] = ""


class WereadSourceImportItem(BaseModel):
    model_config = ConfigDict(extra="forbid")

    book_id: str = Field(
        min_length=1,
        # The suffix is Base64-encoded into Feed.faker_id (String(255)).
        # 189 ASCII suffix bytes encode to 252 bytes; 190 encode to 256.
        max_length=196,
        pattern=r"^MP_WXS_[A-Za-z0-9_-]+$",
    )
    mp_name: str = Field(min_length=1, max_length=255)

    @field_validator("book_id")
    @classmethod
    def validate_book_id(cls, value: str) -> str:
        if value != value.strip():
            raise ValueError("book_id 不能包含首尾空白")
        if value == FEATURED_MP_ID:
            raise ValueError("book_id 是系统保留 ID")
        return value

    @field_validator("mp_name")
    @classmethod
    def validate_mp_name(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("mp_name 不能为空")
        if value != value.strip():
            raise ValueError("mp_name 不能包含首尾空白")
        return value


class WereadSourceImportRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    sources: list[WereadSourceImportItem] = Field(min_length=1)

    @model_validator(mode="after")
    def validate_unique_sources(self):
        book_ids = set()
        normalized_names = set()
        for source in self.sources:
            if source.book_id in book_ids:
                raise ValueError(f"book_id 重复: {source.book_id}")
            normalized_name = source.mp_name.casefold()
            if normalized_name in normalized_names:
                raise ValueError(f"公众号名称重复: {source.mp_name}")
            book_ids.add(source.book_id)
            normalized_names.add(normalized_name)
        return self


class _WereadSourceConflictError(Exception):
    def __init__(self, conflicts: list[dict]):
        super().__init__("微信读书来源存在冲突")
        self.conflicts = conflicts


def _derive_faker_id(book_id: str) -> str:
    suffix = book_id.removeprefix("MP_WXS_")
    return base64.b64encode(suffix.encode("utf-8")).decode("ascii")


def _import_weread_sources_transactionally(
    sources: list[WereadSourceImportItem],
    engine=None,
) -> dict:
    """Add missing WeRead feeds without collection or credential side effects."""
    target_engine = engine or DB.get_engine()
    with target_engine.connect() as raw_connection:
        isolation_level = raw_connection.default_isolation_level
        connection = (
            raw_connection.execution_options(isolation_level=isolation_level)
            if isolation_level
            else raw_connection
        )
        with Session(bind=connection, future=True) as session:
            with session.begin():
                existing_feeds = session.query(Feed).with_for_update().all()
                feeds_by_id = {feed.id: feed for feed in existing_feeds}
                feeds_by_name = defaultdict(list)
                feeds_by_faker_id = defaultdict(list)
                for feed in existing_feeds:
                    feeds_by_name[(feed.mp_name or "").casefold()].append(feed)
                    if feed.faker_id:
                        feeds_by_faker_id[feed.faker_id].append(feed)

                conflicts = []
                statuses = []
                for source in sources:
                    existing = feeds_by_id.get(source.book_id)
                    faker_id = _derive_faker_id(source.book_id)

                    for feed in feeds_by_name[source.mp_name.casefold()]:
                        if feed.id != source.book_id:
                            conflicts.append({
                                "book_id": source.book_id,
                                "code": "name_assigned_to_other_id",
                                "message": "公众号名称已登记到其他 Feed ID",
                            })
                            break

                    for feed in feeds_by_faker_id[faker_id]:
                        if feed.id != source.book_id:
                            conflicts.append({
                                "book_id": source.book_id,
                                "code": "faker_id_assigned_to_other_feed",
                                "message": "派生 faker identity 已被其他 Feed 使用",
                            })
                            break

                    if existing is None:
                        statuses.append((source, "created", faker_id))
                        continue

                    if existing.mp_name != source.mp_name:
                        conflicts.append({
                            "book_id": source.book_id,
                            "code": "book_id_name_mismatch",
                            "message": "bookId 已登记但公众号名称不一致",
                        })
                    if existing.status != DATA_STATUS.ACTIVE:
                        conflicts.append({
                            "book_id": source.book_id,
                            "code": "feed_not_active",
                            "message": "bookId 对应 Feed 不是启用状态",
                        })
                    if existing.faker_id != faker_id:
                        conflicts.append({
                            "book_id": source.book_id,
                            "code": "faker_id_mismatch",
                            "message": "bookId 对应 Feed 的 faker identity 不一致",
                        })
                    statuses.append((source, "unchanged", faker_id))

                if conflicts:
                    raise _WereadSourceConflictError(conflicts)

                now = datetime.now()
                for source, item_status, faker_id in statuses:
                    if item_status != "created":
                        continue
                    session.add(Feed(
                        id=source.book_id,
                        mp_name=source.mp_name,
                        mp_cover="",
                        mp_intro="",
                        status=DATA_STATUS.ACTIVE,
                        sync_time=0,
                        update_time=0,
                        created_at=now,
                        updated_at=now,
                        faker_id=faker_id,
                    ))
                session.flush()

    items = [
        {
            "book_id": source.book_id,
            "mp_name": source.mp_name,
            "status": item_status,
        }
        for source, item_status, _faker_id in statuses
    ]
    created = sum(item["status"] == "created" for item in items)
    unchanged = len(items) - created
    return {
        "total": len(items),
        "created": created,
        "unchanged": unchanged,
        "items": items,
    }


@router.post("/sources/import", summary="批量登记微信读书公众号来源")
async def import_weread_sources(
    req: WereadSourceImportRequest,
    current_user: dict = Depends(get_current_user_or_ak),
):
    """Atomically add missing Feed rows without collection or remote requests."""
    try:
        result = _import_weread_sources_transactionally(req.sources)
    except _WereadSourceConflictError as exc:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=error_response(
                40901,
                "微信读书来源存在冲突，整批未写入",
                {"conflicts": exc.conflicts},
            ),
        ) from exc
    except Exception as exc:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=error_response(
                50001,
                "微信读书来源登记失败，整批未写入",
            ),
        ) from exc

    return success_response(result, "微信读书来源登记完成")


def _get_weread_config() -> Config:
    """获取微信读书配置文件"""
    lic_path = "./data/wx.lic"
    os.makedirs(os.path.dirname(lic_path), exist_ok=True)
    if not os.path.exists(lic_path):
        with open(lic_path, "w") as f:
            f.write("{}")
    return Config(lic_path)


def _load_weread_data() -> dict:
    """加载微信读书数据"""
    cfg = _get_weread_config()
    data = cfg.get("weread_data", {})
    if isinstance(data, str):
        try:
            data = json.loads(data)
        except Exception:
            data = {}
    return data


def _save_weread_data(data: dict):
    """保存微信读书数据"""
    cfg = _get_weread_config()
    cfg.set("weread_data", data)
    cfg.save_config()
    cfg.reload()


@router.get("", summary="获取微信读书配置状态")
async def get_weread_status(current_user=Depends(get_current_user_or_ak)):
    """获取当前微信读书 Cookie 的配置状态"""
    data = _load_weread_data()
    config_cookie = app_cfg.get("weread.cookie", "")
    config_ticket = app_cfg.get("weread.ticket", "")
    config_vid = app_cfg.get("weread.vid", "")
    cookie = config_cookie or data.get("cookie", "")
    ticket = config_ticket or data.get("ticket", "")
    vid = config_vid or data.get("vid", "")
    name = data.get("name", "")

    # 判断是否已配置
    has_cookie = bool(cookie and vid)

    return success_response({
        "configured": has_cookie,
        "cookie_masked": cookie[:20] + "..." if cookie else "",
        "ticket_masked": ticket[:12] + "..." if ticket else "",
        "has_cookie": bool(cookie),
        "has_ticket": bool(ticket),
        "mp_configured": bool(str(cookie or "").strip()),
        "managed_by_config": bool(config_cookie or config_ticket or config_vid),
        "cookie_managed_by_config": bool(config_cookie),
        "ticket_managed_by_config": bool(config_ticket),
        "vid": vid,
        "name": name,
    })


@router.post("/cookie", summary="保存微信读书 Cookie")
async def save_weread_cookie(
    req: WereadCookieRequest,
    current_user=Depends(get_current_user_or_ak),
):
    """
    保存微信读书 Cookie 和可选的公众号文章列表 ticket
    
    所需 Cookie: wr_vid, wr_skey, wr_gid, wr_fp 等
    可以从浏览器 weread.qq.com 的请求中获取
    """
    data = _load_weread_data()
    config_cookie = app_cfg.get("weread.cookie", "")
    config_ticket = app_cfg.get("weread.ticket", "")
    if config_cookie and req.cookie is not None:
        return error_response(409, "Cookie 由 config.yaml 或环境变量管理，不能在页面覆盖")
    if config_ticket and req.ticket is not None:
        return error_response(409, "x-wr-ticket 由 config.yaml 或环境变量管理，不能在页面覆盖")

    cookie_str = config_cookie or (req.cookie.strip() if req.cookie else data.get("cookie", ""))
    if not cookie_str:
        return error_response(400, "Cookie 不能为空")

    # 提取 vid
    vid = (req.vid or "").strip()
    if not vid:
        for item in cookie_str.split(";"):
            item = item.strip()
            if item.startswith("wr_vid="):
                vid = item.replace("wr_vid=", "").strip()
                break

    if not vid:
        return error_response(400, "Cookie 中未找到 wr_vid，请检查 Cookie 格式")

    if not config_cookie:
        data["cookie"] = cookie_str
    if req.ticket is not None:
        data["ticket"] = req.ticket.strip()
    if not app_cfg.get("weread.vid", ""):
        data["vid"] = vid
    data["name"] = (req.name or "").strip() or data.get("name", "")

    _save_weread_data(data)

    return success_response({
        "vid": vid,
        "name": data.get("name", ""),
    }, "Cookie 保存成功")


@router.post("/test", summary="测试微信读书连接")
async def test_weread_connection(current_user=Depends(get_current_user_or_ak)):
    """
    测试微信读书 Cookie 是否有效
    会尝试获取书架数据来验证
    """
    from core.wx.model.weread import MpsWeread

    wx = MpsWeread()
    result = wx.test_auth()

    if result["ok"]:
        return success_response(result, f"连接成功，书架共 {result['book_count']} 本书")
    else:
        return error_response(400, result.get("error", "连接失败"), result)


@router.post("/mp/test", summary="测试微信读书公众号采集连接")
async def test_weread_mp_connection(
    req: WereadMPTestRequest,
    current_user=Depends(get_current_user_or_ak),
):
    """Use an existing MP feed to validate the Cookie and optional ticket."""
    from core.db import DB
    from core.models.feed import Feed
    from core.wx.model.weread_mp import MpsWereadMP, WereadMPAPIError, parse_mp_articles

    mp_id = (req.mp_id or "").strip()
    if not mp_id:
        session = DB.get_session()
        try:
            feed = session.query(Feed.id).filter(Feed.id.like("MP_WXS_%")).first()
            mp_id = feed[0] if feed else ""
        finally:
            session.close()
    if not mp_id:
        return error_response(400, "请先导入至少一个 MP_WXS_ 公众号再测试")

    wx = MpsWereadMP()
    wx._load_weread_auth()
    try:
        payload = wx._get_mp_articles_page(mp_id, offset=0)
        articles, _group_count = parse_mp_articles(payload)
    except WereadMPAPIError as exc:
        return error_response(400, str(exc), {
            "code": exc.code,
            "retriable": exc.retriable,
        })

    return success_response({
        "mp_id": mp_id,
        "article_count": len(articles),
    }, "公众号文章列表连接有效")


@router.post("/bookshelf", summary="获取书架书籍列表")
async def get_bookshelf(current_user=Depends(get_current_user_or_ak)):
    """
    获取微信读书书架上的所有书籍
    用于选择要采集哪本书的笔记
    """
    from core.wx.model.weread import MpsWeread

    wx = MpsWeread()
    wx._load_weread_auth()

    if not wx._weread_cookies:
        return error_response(400, "请先配置微信读书 Cookie")

    if not wx._weread_vid:
        return error_response(400, "无法从 Cookie 提取 vid")

    books = wx._get_shelf_books()
    if books is None:
        return error_response(500, "获取书架失败，Cookie 可能已过期")

    return success_response({
        "total": len(books),
        "books": books,
    })


@router.post("/collect", summary="手动采集微信读书笔记")
async def collect_weread_notes(
    req: WereadCollectRequest,
    current_user=Depends(get_current_user_or_ak),
):
    """
    手动触发微信读书笔记采集

    如果指定了 faker_id (bookId)，则只采集该书的笔记
    如果未指定，则采集整个书架上所有书的笔记
    """
    if not req.mp_id:
        return error_response(400, "mp_id 不能为空")

    from core.wx.model.weread import MpsWeread
    from core.models import Article

    wx = MpsWeread()
    wx._load_weread_auth()

    if not wx._weread_cookies:
        return error_response(400, "请先配置微信读书 Cookie")

    articles = []

    def save_callback(data: dict) -> bool:
        """回调：将笔记存入数据库"""
        from core.db import DB
        from datetime import datetime

        try:
            art = {
                "id": data.get("id", ""),
                "mp_id": data.get("mp_id", ""),
                "title": data.get("title", ""),
                "url": data.get("url", data.get("link", "")),
                "pic_url": data.get("cover", data.get("pic_url", "")),
                "content": data.get("content", ""),
                "publish_time": data.get("publish_time", data.get("update_time", 0)),
            }
            # 直接使用 DB 添加文章
            DB.add_article(art, check_exist=True)
            articles.append(art)
            return True
        except Exception as e:
            from core.print import print_error
            print_error(f"保存笔记失败: {e}")
            return False

    wx.get_Articles(
        faker_id=req.faker_id or None,
        Mps_id=req.mp_id,
        Mps_title=req.mp_name or req.faker_id or "微信读书",
        CallBack=save_callback,
        MaxPage=1,
        interval=3,
        Gather_Content=req.gather_content,
    )

    return success_response({
        "collected": len(articles),
        "articles": articles[:20],  # 只返回前20条
    }, f"采集完成，共 {len(articles)} 条笔记")


@router.delete("/cookie", summary="清除微信读书 Cookie")
async def clear_weread_cookie(current_user=Depends(get_current_user_or_ak)):
    """清除已保存的微信读书 Cookie"""
    if any(app_cfg.get(key, "") for key in ("weread.cookie", "weread.ticket", "weread.vid")):
        return error_response(409, "凭据由 config.yaml 或环境变量管理，请在部署配置中清除")
    data = _load_weread_data()
    data["cookie"] = ""
    data["ticket"] = ""
    data["vid"] = ""
    # 保留 name
    _save_weread_data(data)

    return success_response(message="Cookie 已清除")
