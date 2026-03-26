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
        self.system_req_topic = "system/request"

        self.server.register_endpoint(
            "/camera/get_timelapse_instance", RequestType.POST, self._handle_camera_timelapse_request,
            transports=(TransportType.all() & ~TransportType.HTTP)
        )

        self.server.register_endpoint(
            "/camera/delete_timelapse_instance", RequestType.POST, self._handle_camera_timelapse_request,
            transports=(TransportType.all() & ~TransportType.HTTP)
        )

        self.server.register_endpoint(
            "/camera/upload_timelapse_instance", RequestType.POST, self._handle_camera_timelapse_request,
            transports=(TransportType.all() & ~TransportType.HTTP)
        )

        self.server.register_endpoint("/camera/start_monitor", RequestType.POST,
            self._handle_camera_timelapse_request,
            transports=(TransportType.all() & ~TransportType.HTTP)
            )

        self.server.register_endpoint("/camera/stop_monitor",
            RequestType.POST,
            self._handle_camera_timelapse_request,
            transports=(TransportType.all() & ~TransportType.HTTP)
            )

        self.server.register_endpoint(
            "/system/get_device_info", RequestType.POST, self._handle_system_service_request,
            transports=(TransportType.all() & ~TransportType.HTTP)
        )

        self.server.register_endpoint(
            "/system/collect_sysinfo", RequestType.POST, self._handle_system_service_request,
            transports=(TransportType.all() & ~TransportType.HTTP)
        )

        self.server.register_endpoint(
            "/system/upgrade", RequestType.POST, self._handle_system_service_request,
            transports=(TransportType.all() & ~TransportType.HTTP)
        )

        self.server.register_endpoint(
            "/system/upgrade_check_remote", RequestType.POST, self._handle_system_service_request,
            transports=(TransportType.all() & ~TransportType.HTTP)
        )

        self.server.register_endpoint(
            "/system/upgrade_download_firmware", RequestType.POST, self._handle_system_service_request,
            transports=(TransportType.all() & ~TransportType.HTTP)
        )

    async def component_init(self) -> None:
        self.mqtt = self.server.lookup_component("mqtt", None)
        if self.mqtt is None:
            logging.info("smcloud: MQTT doesn't exist")
            return

        self.mqtt_camera_resp = self.mqtt.subscribe_topic(
                                    "camera/response",
                                    self._handle_internal_service_response,
                                    qos=1)
        self.mqtt_system_resp = self.mqtt.subscribe_topic(
                                    "system/response",
                                    self._handle_internal_service_response,
                                    qos=1)
    async def _handle_internal_service_response(self,
                                    data: bytes
                                    ) -> None:
        """
        Handle the response from camera/system.
        """
        self.mqtt.publish_topic(self.mqtt.api_resp_topic, data, self.mqtt.api_qos)

    async def _handle_camera_timelapse_request(self,
                                web_request: WebRequest
                                ) -> Any:
        """
        Handle the request to get the timelapse instance.
        """
        req_id = web_request.get_int("req_id", None)
        if req_id is None:
            logging.error(f"{web_request.get_endpoint()}: req_id is required")
        endpoint = web_request.get_endpoint()
        # Remove leading '/' and replace '/' with '.'
        endpoint = endpoint[1:].replace('/', '.')
        mesg = {
            "jsonrpc": "2.0",
            "method": endpoint,
            "params": web_request.get_args(),
            "id": req_id
        }
        logging.info(f"{web_request.get_endpoint()}: {req_id}")
        self.mqtt.publish_topic(self.camera_req_topic, jsonw.dumps(mesg), self.mqtt.api_qos)
        return None

    async def _handle_system_service_request(self,
                                web_request: WebRequest
                                ) -> Any:
        """
        Handle the request to collect system information.
        """
        req_id = web_request.get_int("req_id", None)
        if req_id is None:
            logging.error(f"{web_request.get_endpoint()}: req_id is required")
        endpoint = web_request.get_endpoint()
        # Remove leading '/' and replace '/' with '.'
        endpoint = endpoint[1:].replace('/', '.')
        mesg = {
            "jsonrpc": "2.0",
            "method": endpoint,
            "params": web_request.get_args(),
            "id": req_id
        }
        logging.debug(f"{web_request.get_endpoint()}: {web_request.get_args()}")
        self.mqtt.publish_topic(self.system_req_topic, jsonw.dumps(mesg), self.mqtt.api_qos)
        return None

def load_component(config: ConfigHelper) -> Repeater:
    return Repeater(config)

