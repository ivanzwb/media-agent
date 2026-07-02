"""WeChat Official Account (微信公众号) integration.

Publishing pipeline: access_token -> upload body images (media/uploadimg) ->
upload cover as permanent material (material/add_material) -> create draft
(draft/add) -> optionally publish (freepublish/submit).
"""
from __future__ import annotations

from app.wechat.client import WeChatClient, WeChatError, get_wechat_client

__all__ = ["WeChatClient", "WeChatError", "get_wechat_client"]
