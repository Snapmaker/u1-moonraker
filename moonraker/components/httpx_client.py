# Wrapper around httpx AsyncClient for Moonraker
#
# Copyright (C) 2025 Scott Huang <shili.huang@snapmaker.com>
#
# This file may be distributed under the terms of the GNU GPLv3 license

from __future__ import annotations

import asyncio
import copy
import logging
import pathlib
import re
import tempfile
import time
from typing import (
    TYPE_CHECKING,
    Any,
    Callable,
    Dict,
    List,
    Optional,
    Tuple,
    Union,
)

import httpx

from ..utils import ServerError
from ..utils import json_wrapper as jsonw
from tornado.escape import url_unescape
from tornado.httputil import HTTPHeaders

if TYPE_CHECKING:
    from ..confighelper import ConfigHelper
    from ..server import Server
    from io import BufferedWriter
    StrOrPath = Union[str, pathlib.Path]


GITHUB_PREFIX = "https://api.github.com/"


class HttpxClient:
    def __init__(self, config: ConfigHelper) -> None:
        self.server = config.get_server()
        # Silence verbose httpx/httpcore request logging which can flood Moonraker logs
        logging.getLogger("httpx").setLevel(logging.WARNING)
        logging.getLogger("httpcore").setLevel(logging.WARNING)
        self.client = httpx.AsyncClient(
            headers={"User-Agent": "Moonraker"},
            follow_redirects=True,
        )
        self.response_cache: Dict[str, HttpResponse] = {}

        self.gh_rate_limit: Optional[int] = None
        self.gh_limit_remaining: Optional[int] = None
        self.gh_limit_reset_time: Optional[float] = None

    def register_cached_url(
        self,
        url: str,
        etag: Optional[str] = None,
        last_modified: Optional[str] = None,
    ) -> None:
        headers = HTTPHeaders()
        if etag is not None:
            headers["etag"] = etag
        if last_modified is not None:
            headers["last-modified"] = last_modified
        if len(headers) == 0:
            raise self.server.error(
                "Either an Etag or Last Modified Date must be specified"
            )
        empty_resp = HttpResponse(url, url, 200, b"", headers, None)
        self.response_cache[url] = empty_resp

    async def request(
        self,
        method: str,
        url: str,
        body: Optional[Union[bytes, str, List[Any], Dict[str, Any]]] = None,
        headers: Optional[Dict[str, Any]] = None,
        connect_timeout: float = 5.0,
        request_timeout: float = 10.0,
        attempts: int = 1,
        retry_pause_time: float = 0.1,
        enable_cache: bool = False,
        send_etag: bool = True,
        send_if_modified_since: bool = True,
        basic_auth_user: Optional[str] = None,
        basic_auth_pass: Optional[str] = None,
    ) -> HttpResponse:
        cache_key = url.split("?", 1)[0]
        method = method.upper()

        req_headers: Dict[str, Any] = {}
        request_body: Optional[bytes]
        if isinstance(body, (list, dict)):
            request_body = jsonw.dumps(body).encode("utf-8")
            req_headers["Content-Type"] = "application/json"
        elif isinstance(body, str):
            request_body = body.encode("utf-8")
        else:
            request_body = body

        cached: Optional[HttpResponse] = None
        if enable_cache:
            cached = self.response_cache.get(cache_key)
            if cached is not None:
                if cached.etag is not None and send_etag:
                    req_headers["If-None-Match"] = cached.etag
                if cached.last_modified and send_if_modified_since:
                    req_headers["If-Modified-Since"] = cached.last_modified

        request_headers: Optional[Dict[str, Any]] = None
        if headers is not None:
            request_headers = headers.copy()
            if req_headers:
                request_headers.update(req_headers)
        elif req_headers:
            request_headers = req_headers.copy()

        timeout = httpx.Timeout(
            connect=connect_timeout,
            read=request_timeout,
            write=request_timeout,
            pool=connect_timeout,
        )
        auth: Optional[Tuple[str, str]] = None
        if basic_auth_user is not None:
            assert basic_auth_pass is not None
            auth = (basic_auth_user, basic_auth_pass)

        err: Optional[BaseException] = None
        ret: Optional[HttpResponse] = None

        for attempt in range(attempts):
            if attempt:
                await asyncio.sleep(retry_pause_time)

            response: Optional[httpx.Response] = None
            try:
                response = await self.client.request(
                    method=method,
                    url=url,
                    headers=request_headers,
                    content=request_body,
                    timeout=timeout,
                    auth=auth,
                )
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                err = exc
                if attempt + 1 == attempts:
                    break
                continue

            try:
                headers_obj = HTTPHeaders(response.headers)
                final_url = str(response.url)
                error: Optional[BaseException] = None

                if response.status_code == 304:
                    err = None
                    if cached is None:
                        if enable_cache:
                            logging.info(
                                "Request returned 304, however no cached item was found"
                            )
                        result = b""
                    else:
                        logging.debug(f"Request returned from cache: {url}")
                        result = cached.content
                else:
                    result = response.content
                    if response.status_code >= 400:
                        try:
                            response.raise_for_status()
                        except httpx.HTTPError as exc:
                            error = exc
                            err = exc

                ret = HttpResponse(
                    url,
                    final_url,
                    response.status_code,
                    result,
                    headers_obj,
                    error,
                )

                if response.status_code == 304:
                    break
                if ret.has_error() and attempt + 1 != attempts:
                    continue
                break
            finally:
                if response is not None:
                    await response.aclose()

        if ret is None:
            ret = HttpResponse(url, url, 500, b"", HTTPHeaders(), err)

        if enable_cache and ret.is_cachable():
            logging.debug(f"Caching HTTP Response: {url}")
            self.response_cache[cache_key] = ret
        else:
            self.response_cache.pop(cache_key, None)
        return ret

    async def get(
        self, url: str, headers: Optional[Dict[str, Any]] = None, **kwargs: Any
    ) -> HttpResponse:
        if "enable_cache" not in kwargs:
            kwargs["enable_cache"] = True
        return await self.request("GET", url, None, headers, **kwargs)

    async def post(
        self,
        url: str,
        body: Union[str, List[Any], Dict[str, Any]] = "",
        headers: Optional[Dict[str, Any]] = None,
        **kwargs: Any,
    ) -> HttpResponse:
        return await self.request("POST", url, body, headers, **kwargs)

    async def delete(
        self,
        url: str,
        headers: Optional[Dict[str, Any]] = None,
        **kwargs: Any,
    ) -> HttpResponse:
        return await self.request("DELETE", url, None, headers, **kwargs)

    async def github_api_request(
        self,
        resource: str,
        attempts: int = 1,
        retry_pause_time: float = 0.1,
    ) -> HttpResponse:
        url = f"{GITHUB_PREFIX}{resource.strip('/')}"
        if (
            self.gh_limit_reset_time is not None
            and self.gh_limit_remaining == 0
        ):
            curtime = time.time()
            if curtime < self.gh_limit_reset_time:
                reset_time = time.ctime(self.gh_limit_reset_time)
                raise self.server.error(
                    f"GitHub Rate Limit Reached\n"
                    f"Request: {url}\n"
                    f"Limit Reset Time: {reset_time}"
                )
        headers = {"Accept": "application/vnd.github.v3+json"}
        resp = await self.get(
            url,
            headers,
            attempts=attempts,
            retry_pause_time=retry_pause_time,
        )
        resp_hdrs = resp.headers
        if "X-Ratelimit-Limit" in resp_hdrs:
            self.gh_rate_limit = int(resp_hdrs["X-Ratelimit-Limit"])
            self.gh_limit_remaining = int(
                resp_hdrs["X-Ratelimit-Remaining"]
            )
            self.gh_limit_reset_time = float(
                resp_hdrs["X-Ratelimit-Reset"]
            )
        return resp

    def github_api_stats(self) -> Dict[str, Any]:
        return {
            "github_rate_limit": self.gh_rate_limit,
            "github_requests_remaining": self.gh_limit_remaining,
            "github_limit_reset_time": self.gh_limit_reset_time,
        }

    async def get_file(
        self,
        url: str,
        content_type: str,
        connect_timeout: float = 5.0,
        request_timeout: float = 180.0,
        attempts: int = 1,
        retry_pause_time: float = 0.1,
        enable_cache: bool = False,
    ) -> bytes:
        headers = {"Accept": content_type}
        resp = await self.get(
            url,
            headers,
            connect_timeout=connect_timeout,
            request_timeout=request_timeout,
            attempts=attempts,
            retry_pause_time=retry_pause_time,
            enable_cache=enable_cache,
        )
        resp.raise_for_status()
        return resp.content

    async def download_file(
        self,
        url: str,
        content_type: str,
        destination_path: Optional[StrOrPath] = None,
        download_size: int = -1,
        progress_callback: Optional[Callable[[int, int, int], None]] = None,
        connect_timeout: float = 5.0,
        request_timeout: float = 180.0,
        attempts: int = 1,
        retry_pause_time: float = 1.0,
    ) -> pathlib.Path:
        from urllib.parse import quote, urlparse, urlunparse

        parsed = urlparse(url)
        encoded_path = quote(parsed.path)
        encoded_url = urlunparse(
            (
                parsed.scheme,
                parsed.netloc,
                encoded_path,
                parsed.params,
                parsed.query,
                parsed.fragment,
            )
        )

        timeout = httpx.Timeout(
            connect=connect_timeout,
            read=request_timeout,
            write=request_timeout,
            pool=connect_timeout,
        )

        for attempt in range(attempts):
            dl = StreamingDownload(
                self.server,
                destination_path,
                download_size,
                progress_callback,
            )

            try:
                async with self.client.stream(
                    "GET",
                    encoded_url,
                    headers={"Accept": content_type},
                    timeout=timeout,
                ) as response:
                    headers_obj = HTTPHeaders(response.headers)
                    dl.process_headers(response.status_code, headers_obj)

                    if response.status_code >= 400:
                        try:
                            response.raise_for_status()
                        except httpx.HTTPError:
                            if attempt + 1 == attempts:
                                raise
                            await asyncio.sleep(retry_pause_time)
                            continue

                    async for chunk in response.aiter_bytes():
                        dl.on_chunk_recd(chunk)
            except httpx.TimeoutException as exc:
                logging.error(f"Timeout downloading file, error: {exc}")
                dl.cleanup_temp_file()
                if attempt + 1 == attempts:
                    raise asyncio.TimeoutError(str(exc))
                await asyncio.sleep(retry_pause_time)
                logging.info(
                    "Retrying download file, attempt %s", attempt + 1
                )
                continue
            except asyncio.CancelledError:
                raise
            except Exception:
                dl.cleanup_temp_file()
                if attempt + 1 == attempts:
                    raise
                await asyncio.sleep(retry_pause_time)
                continue
            finally:
                await dl.close()

            if dl.request_ok:
                return dl.dest_file

            dl.cleanup_temp_file()

            if attempt + 1 != attempts:
                await asyncio.sleep(retry_pause_time)

        raise self.server.error(f"Retries exceeded for request: {url}")

    def wrap_request(self, default_url: str, **kwargs: Any) -> HttpRequestWrapper:
        return HttpRequestWrapper(self, default_url, **kwargs)

    def close(self) -> None:
        if self.client.is_closed:
            return
        event_loop = self.server.get_event_loop()
        event_loop.register_callback(self.client.aclose)


class HttpRequestWrapper:
    def __init__(
        self, client: HttpxClient, default_url: str, **kwargs: Any
    ) -> None:
        self._do_request = client.request
        self._last_response: Optional[HttpResponse] = None
        self.default_request_args: Dict[str, Any] = {
            "method": "GET",
            "url": default_url,
        }
        self.default_request_args.update(kwargs)
        self.request_args = copy.deepcopy(self.default_request_args)
        self.reset()

    async def send(self, **kwargs: Any) -> HttpResponse:
        req_args = copy.deepcopy(self.request_args)
        req_args.update(kwargs)
        method = req_args.pop("method", self.default_request_args["method"])
        url = req_args.pop("url", self.default_request_args["url"])
        self._last_response = await self._do_request(method, url, **req_args)
        return self._last_response

    def set_method(self, method: str) -> None:
        self.request_args["method"] = method

    def set_url(self, url: str) -> None:
        self.request_args["url"] = url

    def set_body(
        self, body: Optional[Union[str, List[Any], Dict[str, Any]]]
    ) -> None:
        self.request_args["body"] = body

    def add_header(self, name: str, value: str) -> None:
        headers = self.request_args.get("headers", {})
        headers[name] = value
        self.request_args["headers"] = headers

    def set_headers(self, headers: Dict[str, str]) -> None:
        self.request_args["headers"] = headers

    def reset(self) -> None:
        self.request_args = copy.deepcopy(self.default_request_args)

    def last_response(self) -> Optional[HttpResponse]:
        return self._last_response


class HttpResponse:
    def __init__(
        self,
        url: str,
        final_url: str,
        code: int,
        result: bytes,
        response_headers: HTTPHeaders,
        error: Optional[BaseException],
    ) -> None:
        self._url = url
        self._final_url = final_url
        self._code = code
        self._result: bytes = result
        self._encoding: str = "utf-8"
        self._response_headers: HTTPHeaders = response_headers
        self._etag: Optional[str] = response_headers.get("etag", None)
        self._error = error
        self._last_modified: Optional[str] = response_headers.get(
            "last-modified", None
        )

    def json(self) -> Union[List[Any], Dict[str, Any]]:
        return jsonw.loads(self._result)

    def is_cachable(self) -> bool:
        return self._last_modified is not None or self._etag is not None

    def has_error(self) -> bool:
        return self._error is not None

    def raise_for_status(self, message: Optional[str] = None) -> None:
        if self._error is not None:
            code = 500
            msg = f"HTTP Request Error: {self.url}"
            if isinstance(self._error, httpx.HTTPStatusError):
                code = self._code
                msg = str(self._error)
            if message is not None:
                msg = message
            raise ServerError(msg, code) from self._error

    @property
    def encoding(self) -> str:
        return self._encoding

    @encoding.setter
    def encoding(self, new_enc: str) -> None:
        self._encoding = new_enc

    @property
    def text(self) -> str:
        return self._result.decode(encoding=self._encoding)

    @property
    def content(self) -> bytes:
        return self._result

    @property
    def url(self) -> str:
        return self._url

    @property
    def final_url(self) -> str:
        return self._final_url

    @property
    def status_code(self) -> int:
        return self._code

    @property
    def headers(self) -> HTTPHeaders:
        return self._response_headers

    @property
    def last_modified(self) -> Optional[str]:
        return self._last_modified

    @property
    def etag(self) -> Optional[str]:
        return self._etag

    @property
    def error(self) -> Optional[BaseException]:
        return self._error


class StreamingDownload:
    def __init__(
        self,
        server: Server,
        dest_path: Optional[StrOrPath],
        download_size: int,
        progress_callback: Optional[Callable[[int, int, int], None]],
    ) -> None:
        self.server = server
        self.event_loop = server.get_event_loop()
        self.need_content_length: bool = True
        self.need_content_disposition: bool = False
        self.request_ok: bool = False
        if dest_path is None:
            tmp_dir = tempfile.gettempdir()
            loop_time = int(self.event_loop.get_loop_time())
            tmp_fname = f"moonraker.download-{loop_time}.mrd"
            self.dest_file = pathlib.Path(tmp_dir).joinpath(tmp_fname)
            self.need_content_disposition = True
            self.generated_temp: bool = True
        elif isinstance(dest_path, str):
            self.dest_file = pathlib.Path(dest_path)
            self.generated_temp = False
        else:
            self.dest_file = dest_path
            self.generated_temp = False
        self.filename = self.dest_file.name
        self.file_hdl: Optional[BufferedWriter] = None
        self.total_recd: int = 0
        self.download_size: int = download_size
        self.pct_done: int = 0
        self.chunk_buffer: List[bytes] = []
        self.progress_callback = progress_callback
        self.busy_evt: asyncio.Event = asyncio.Event()
        self.busy_evt.set()

    def process_headers(self, status_code: int, headers: HTTPHeaders) -> None:
        self.request_ok = status_code < 400
        if not self.request_ok:
            return
        if self.need_content_length:
            cl_header = headers.get("content-length")
            if cl_header is not None:
                self.download_size = int(cl_header)
                self.need_content_length = False
                logging.debug(
                    "Content-Length header received: size = %s",
                    self.download_size,
                )
        if self.need_content_disposition:
            cd_header = headers.get("content-disposition")
            if cd_header is None:
                return
            fnr = r"filename[^;\n=]*=(['\"])?(utf-8\'\')?([^\n;]*)(?(1)\1|)"
            matches: List[Tuple[str, str, str]] = re.findall(fnr, cd_header)
            is_utf8 = False
            for (_, encoding, fname) in matches:
                encoding = encoding or ""
                if encoding.startswith("utf-8"):
                    self.filename = url_unescape(
                        fname, encoding="utf-8", plus=False
                    )
                    is_utf8 = True
                    break
                self.filename = fname
            self.need_content_disposition = False
            self.dest_file = self.dest_file.parent.joinpath(self.filename)
            logging.debug(
                "Content-Disposition header received: filename = %s, utf8: %s",
                self.filename,
                is_utf8,
            )
        elif self.generated_temp:
            # If no disposition header arrives we keep the temp filename
            pass

    def on_chunk_recd(self, chunk: bytes) -> None:
        if not chunk:
            return
        self.chunk_buffer.append(chunk)
        if not self.busy_evt.is_set():
            return
        self.busy_evt.clear()
        self.event_loop.register_callback(self._process_buffer)

    async def close(self) -> None:
        await self.busy_evt.wait()
        if self.file_hdl is not None:
            await self.event_loop.run_in_thread(self.file_hdl.close)

    async def _process_buffer(self) -> None:
        if self.file_hdl is None:
            self.file_hdl = await self.event_loop.run_in_thread(
                self.dest_file.open, "wb"
            )
        while self.chunk_buffer:
            chunk = self.chunk_buffer.pop(0)
            await self.event_loop.run_in_thread(self.file_hdl.write, chunk)
            self.total_recd += len(chunk)
            if (
                self.download_size > 0
                and self.progress_callback is not None
            ):
                pct = int(self.total_recd / self.download_size * 100 + 0.5)
                pct = min(100, pct)
                if pct != self.pct_done:
                    self.pct_done = pct
                    self.progress_callback(
                        pct, self.download_size, self.total_recd
                    )
        self.busy_evt.set()

    def cleanup_temp_file(self) -> None:
        if not getattr(self, "generated_temp", False):
            return
        try:
            if self.dest_file.exists():
                self.dest_file.unlink()
        except FileNotFoundError:
            pass


def load_component(config: ConfigHelper) -> HttpxClient:
    return HttpxClient(config)

