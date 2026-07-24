"""Thin WeChat Official Account API client (httpx).

Covers the 图文 publish flow plus permanent-material upload for 视频:
  - access_token (cached in-process until ~5 min before expiry)
  - media/uploadimg        -> image URL usable inside 图文 HTML
  - material/add_material   -> permanent media_id (image cover / video)
  - draft/add               -> draft media_id
  - freepublish/submit/get  -> publish + status

WeChat returns ``{"errcode": N, "errmsg": "..."}`` on failure (errcode 0 or
absent == success). Any non-zero errcode raises :class:`WeChatError`.
"""
from __future__ import annotations

import json
import logging
import mimetypes
import time
from pathlib import Path

import httpx

logger = logging.getLogger(__name__)

_API = "https://api.weixin.qq.com/cgi-bin"


class WeChatError(Exception):
    """A WeChat API call returned a non-zero errcode (or transport failed)."""

    def __init__(self, errcode: int, errmsg: str):
        self.errcode = errcode
        self.errmsg = errmsg
        super().__init__(f"WeChat API error {errcode}: {errmsg}")


class WeChatClient:
    def __init__(self, appid: str, appsecret: str, timeout: float = 30.0):
        self.appid = appid
        self.appsecret = appsecret
        self.timeout = timeout
        self._token: str | None = None
        self._token_exp: float = 0.0

    # ── auth ────────────────────────────────────────────────────────────
    def access_token(self, force: bool = False) -> str:
        now = time.time()
        if not force and self._token and now < self._token_exp:
            return self._token
        resp = httpx.get(f"{_API}/token", params={
            "grant_type": "client_credential",
            "appid": self.appid, "secret": self.appsecret,
        }, timeout=self.timeout)
        resp.raise_for_status()
        data = resp.json()
        if not data.get("access_token"):
            raise WeChatError(int(data.get("errcode", -1)),
                              data.get("errmsg", "no access_token returned"))
        self._token = data["access_token"]
        # refresh 5 min before the (usually 7200s) expiry
        self._token_exp = now + int(data.get("expires_in", 7200)) - 300
        return self._token

    # ── helpers ─────────────────────────────────────────────────────────
    @staticmethod
    def _check(data: dict) -> dict:
        if isinstance(data, dict) and data.get("errcode"):
            raise WeChatError(int(data["errcode"]), data.get("errmsg", ""))
        return data

    def _post_json(self, path: str, payload: dict) -> dict:
        # WeChat needs non-ASCII (Chinese) sent as raw UTF-8, not \uXXXX.
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        resp = httpx.post(f"{_API}/{path}",
                          params={"access_token": self.access_token()},
                          content=body,
                          headers={"Content-Type": "application/json"},
                          timeout=self.timeout)
        resp.raise_for_status()
        return self._check(resp.json())

    def _post_file(self, path: str, file_path: Path, params: dict | None = None,
                   data: dict | None = None) -> dict:
        fp = Path(file_path)
        with fp.open("rb") as fh:
            content_type = (
                mimetypes.guess_type(fp.name)[0] or "application/octet-stream")
            files = {"media": (fp.name, fh, content_type)}
            resp = httpx.post(
                f"{_API}/{path}",
                params={"access_token": self.access_token(), **(params or {})},
                files=files, data=(data or None), timeout=self.timeout)
        resp.raise_for_status()
        return self._check(resp.json())

    # ── media ───────────────────────────────────────────────────────────
    def upload_article_image(self, file_path: Path) -> str:
        """media/uploadimg — image used inside 图文 body. Returns a WeChat URL
        (does not consume the material quota; must be < 1MB, jpg/png)."""
        data = self._post_file("media/uploadimg", file_path)
        return data["url"]

    def add_material(self, media_type: str, file_path: Path,
                     title: str | None = None,
                     introduction: str | None = None) -> dict:
        """material/add_material — permanent material. Returns {media_id, url?}.

        For ``video`` a description (title/introduction) is required by WeChat.
        """
        data = None
        if media_type == "video":
            data = {"description": json.dumps(
                {"title": title or "", "introduction": introduction or ""},
                ensure_ascii=False)}
        return self._post_file("material/add_material", file_path,
                               params={"type": media_type}, data=data)

    # ── draft / publish ──────────────────────────────────────────────────
    def add_draft(self, articles: list[dict]) -> str:
        """draft/add — create a draft in 草稿箱. Returns the draft media_id."""
        data = self._post_json("draft/add", {"articles": articles})
        return data["media_id"]

    def freepublish_submit(self, media_id: str) -> str:
        """freepublish/submit — publish a draft. Returns publish_id (async)."""
        data = self._post_json("freepublish/submit", {"media_id": media_id})
        return str(data["publish_id"])

    def freepublish_get(self, publish_id: str) -> dict:
        """freepublish/get — query publish task status."""
        return self._post_json("freepublish/get", {"publish_id": publish_id})


# Memoize one client per appid so the in-process access_token is reused
# across web requests (WeChat rate-limits token fetches).
_CLIENTS: dict[str, WeChatClient] = {}


def get_wechat_client(config) -> WeChatClient | None:
    """Build (or reuse) a client from config; None if credentials are unset."""
    appid = (config.wechat_appid or "").strip()
    secret = (config.wechat_appsecret or "").strip()
    if not appid or not secret:
        return None
    cached = _CLIENTS.get(appid)
    if cached is None or cached.appsecret != secret:
        cached = WeChatClient(appid, secret)
        _CLIENTS[appid] = cached
    return cached
