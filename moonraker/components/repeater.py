# Repeater for Snapmaker internal API
#
# Copyright (C) 2025 Scott Huang <shili.huang@snapmaker.com>
#
# This file may be distributed under the terms of the GNU GPLv3 license.

from __future__ import annotations
import os, time, sys
import asyncio
import logging
import queue, threading
import pathlib, random
import hashlib
import logging.handlers
import fcntl, select, re
from queue import SimpleQueue
from ..loghelper import LocalQueueHandler
from ..common import RequestType, JobEvent, KlippyState, UserInfo, WebRequest, TransportType
from ..utils import json_wrapper as jsonw
from urllib.parse import urlparse
from urllib.parse import unquote

from typing import (
    TYPE_CHECKING,
    Awaitable,
    Optional,
    Dict,
    List,
    Union,
    Any,
    Callable,
    cast,
)
if TYPE_CHECKING:
    from .application import InternalTransport
    from ..confighelper import ConfigHelper
    from .websockets import WebsocketManager
    from ..common import JsonRPC
    from .database import MoonrakerDatabase
    from .klippy_apis import KlippyAPI
    from .job_state import JobState
    from .machine import Machine
    from .file_manager.file_manager import FileManager
    from .http_client import HttpClient
    from .power import PrinterPower
    from .announcements import Announcements
    from .webcam import WebcamManager, WebCam
    from .klippy_connection import KlippyConnection
    from .mqtt import MQTTClient


class Repeater:
    def __init__(self,  config: ConfigHelper) -> None:
        self.server = config.get_server()
        self.mqtt: MQTTClient = None

        self.camera_req_topic = "camera/request"
        self.camera_resp_topic = "camera/response"
        self.system_req_topic = "system/request"
        self.system_resp_topic = "system/response"

        # Register all camera endpoints (proxied to unisrv via MQTT)
        camera_endpoints = [
            "/camera/start_monitor",
            "/camera/stop_monitor",
            # "/camera/take_a_photo",
            # "/camera/start_timelapse",
            # "/camera/stop_timelapse",
            "/camera/get_timelapse_instance",
            "/camera/delete_timelapse_instance",
            # "/camera/upload_timelapse_instance",
            # "/camera/get_status",
            # "/camera/detect_capture",
        ]
        for ep in camera_endpoints:
            self.server.register_endpoint(
                ep, RequestType.POST,
                self._handle_camera_request,
                transports=(TransportType.all() & ~TransportType.HTTP)
            )

        # Register all system endpoints (proxied to unisrv via MQTT)
        system_endpoints = [
            "/system/get_device_info",
            # "/system/collect_sysinfo",
            "/system/upgrade",
            "/system/upgrade_check_remote",
            "/system/upgrade_download_firmware",
        ]
        for ep in system_endpoints:
            self.server.register_endpoint(
                ep, RequestType.POST,
                self._handle_system_request,
                transports=(TransportType.all() & ~TransportType.HTTP)
            )

    async def component_init(self) -> None:
        self.mqtt = self.server.lookup_component("mqtt", None)
        if self.mqtt is None:
            logging.info("smcloud: MQTT doesn't exist")
            return

        # Subscribe to camera/system MQTT responses and forward to cloud
        self.mqtt_camera_resp = self.mqtt.subscribe_topic(
                                    self.camera_resp_topic,
                                    self._forward_to_cloud,
                                    qos=1)
        self.mqtt_system_resp = self.mqtt.subscribe_topic(
                                    self.system_resp_topic,
                                    self._forward_to_cloud,
                                    qos=1)

    async def _forward_to_cloud(self, data: bytes) -> None:
        await self.mqtt.publish_topic(self.mqtt.api_resp_topic, data, self.mqtt.api_qos)

    async def _handle_camera_request(self, web_request: WebRequest) -> Any:
        return await self._handle_internal_request(
            web_request, self.camera_req_topic, self.camera_resp_topic)

    async def _handle_system_request(self, web_request: WebRequest) -> Any:
        return await self._handle_internal_request(
            web_request, self.system_req_topic, self.system_resp_topic)

    async def _handle_internal_request(self,
                                web_request: WebRequest,
                                req_topic: str,
                                resp_topic: str
                                ) -> Any:
        req_id = web_request.get_int("req_id", None)
        if req_id is None:
            logging.error(f"{web_request.get_endpoint()}: req_id is required")
        endpoint = web_request.get_endpoint()
        # Remove leading '/' and replace '/' with '.'
        method = endpoint[1:].replace('/', '.')
        mesg = {
            "jsonrpc": "2.0",
            "method": method,
            "params": web_request.get_args(),
            "id": req_id
        }
        logging.info(f"Repeater {method}: req_id={req_id}")
        try:
            resp_bytes = await self.mqtt.publish_topic_with_response(
                req_topic, resp_topic,
                jsonw.dumps(mesg), self.mqtt.api_qos,
                timeout=10)
            resp = jsonw.loads(resp_bytes)
            if "error" in resp:
                err = resp["error"]
                raise self.server.error(
                    err.get("message", "Internal MQTT error"),
                    err.get("code", 500)
                )
            return resp.get("result", {})
        except self.server.error:
            raise
        except Exception as e:
            logging.error(f"Repeater {method}: error={e}")
            return {"state": "failed", "message": str(e)}

def load_component(config: ConfigHelper) -> Repeater:
    return Repeater(config)