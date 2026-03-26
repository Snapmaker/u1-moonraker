# Snapmaker Exception Manager
#
# Copyright (C) 2025 Scott Huang <shili.huang@snapmaker.com>
#
# This file may be distributed under the terms of the GNU GPLv3 license.

from __future__ import annotations
import os, time, sys
import asyncio
import logging
import multiprocessing, threading
import pathlib, random
import hashlib
import logging.handlers
import fcntl, select
from queue import SimpleQueue
from ..loghelper import LocalQueueHandler
from ..common import RequestType, KlippyState, WebRequest, TransportType
from ..utils import json_wrapper as jsonw
from urllib.parse import urlparse
from urllib.parse import unquote

from typing import (
    TYPE_CHECKING,
    Awaitable,
    Optional,
    Dict,
    List,
    Tuple,
    Any,
    Callable,
)
if TYPE_CHECKING:
    from .application import InternalTransport
    from ..confighelper import ConfigHelper
    from ..common import JsonRPC
    from .klippy_apis import KlippyAPI
    from .machine import Machine
    from .announcements import Announcements
    from .klippy_connection import KlippyConnection
    from .mqtt import MQTTClient

class ExceptionManager:
    """
    klipper exception id: 522
    klipper toolhead id: 523
    camera id: 524
    system id: 2052
    """
    MUDOLE_ID_MOTION     = 522
    MUDOLE_ID_TOOLHEAD   = 523
    MUDOLE_ID_CAMERA     = 524
    MUDOLE_ID_SYSTEM     = 2052

    CODE_MOTION_COMMON       = 0
    CODE_MOTION_DISCONNECTED = 1
    CODE_MOTION_SHUTDOWN     = 2

    def __init__(self, config: ConfigHelper) -> None:
        self.server = config.get_server()
        self.eventloop = self.server.get_event_loop()
        self.mqtt: MQTTClient = None
        self.klippy_apis: KlippyAPI = self.server.lookup_component('klippy_apis')

        # id: int, index: int, number: int
        self.system_excep_status: Dict[int, Dict[int, List[int]]] = {}
        self.system_excep_cache: List[Dict[int, int, int]] = []

        self.motion_excep_status: Dict[int, Dict[int, List[int]]] = {}
        self.motion_excep_cache: List[Dict[int, int, int]] = []

        self.server.register_notification("snapmaker:exception_notification")
        self.server.register_notification("snapmaker:exception_status")

        # Register State Events
        # self.server.register_event_handler(
        #     "server:klippy_started", self._on_klippy_startup)
        self.server.register_event_handler(
            "server:klippy_ready", self._on_klippy_ready)
        self.server.register_event_handler(
            "server:klippy_shutdown", self._on_klippy_shutdown)
        self.server.register_event_handler(
            "server:klippy_disconnect", self._on_klippy_disconnected)

        self.server.register_remote_method("clear_exception", self._on_klippy_clear_exception)
        self.server.register_remote_method("raise_exception", self._on_klippy_raise_exception)

        # API for clients to query the exception status
        self.server.register_endpoint("/server/exception/query", RequestType.POST,
                                        self._query_exception,
                                        transports=(TransportType.all() & ~TransportType.HTTP)
                                    )

        self.server.register_endpoint("/server/exception/clear", RequestType.POST,
                                        self._on_exception_clear,
                                        transports=(TransportType.all() & ~TransportType.HTTP)
                                    )
        self.server.register_endpoint("/server/exception/raise", RequestType.POST,
                                        self._on_exception_raise,
                                        transports=(TransportType.all() & ~TransportType.HTTP)
                                    )

    async def component_init(self) -> None:
        self.mqtt = self.server.lookup_component("mqtt", None)
        if self.mqtt is None:
            logging.warning("excepmgr: MQTT doesn't exist")
            return
        self.mqtt.register_notification("snapmaker:exception_notification")
        self.mqtt.register_notification("snapmaker:exception_status")

    async def _on_klippy_clear_exception(self, id:int, index:int, code:int) -> None:
        try:
            if self.motion_excep_status.get(id) is not None and \
                self.motion_excep_status[id].get(index) is not None:
                if code in self.motion_excep_status[id][index]:
                    self.motion_excep_status[id][index].remove(code)
                    cache = {'id': id, 'index': index, 'code': code}
                    # self.motion_excep_cache.remove(cache)
                    to_remove = [ex for ex in self.motion_excep_cache
                                if all(ex.get(k) == v for k, v in cache.items())]
                    if to_remove:
                        for ex in to_remove:
                            self.motion_excep_cache.remove(ex)

                    excep_cache = self.motion_excep_cache + self.system_excep_cache
                    self.server.send_event("snapmaker:exception_status", {"exceptions": excep_cache})
                    logging.info(f"klippy exception {id}-{index}-{code} cleared")
        except Exception as e:
            logging.error(f"failed to clear klippy exception: {e}, id:{id}, index:{index}, code:{code}")

    async def _on_klippy_raise_exception(self, id: int,
                                         index: int,
                                         code: int,
                                         message: str,
                                         oneshot: int = 1,
                                         level: int = 3
                                        ) -> None:
        try:
            excep = {'id': id, 'index': index, 'code': code, 'level': level, 'message': message}
            timestamp = time.time()
            excep['timestamp'] = timestamp
            self.server.send_event("snapmaker:exception_notification", excep)

            if not oneshot:
                # record the exception
                self._init_exception_status(self.motion_excep_status, id, index, code)
                should_notify = self._update_exception_cache(
                    self.motion_excep_cache, id, index, code, level, timestamp, message)

                if should_notify:
                    self._notify_exception_status()

            logging.info(f"klippy raise exception: {excep}")
        except Exception as e:
            logging.error(f"invalid klippy exception : {e}, {id}-{index}-{code}: {message}")

    async def _query_exception(self,
                                web_request: WebRequest
                                ) -> Dict[str, Any]:
        excep_cache = self.motion_excep_cache + self.system_excep_cache
        return {"state": "success", "exceptions": excep_cache}

    async def _on_exception_raise(self,
                                web_request: WebRequest
                                ) -> Dict[str, Any]:
        try:
            id = web_request.get_int('id')
            index = web_request.get_int('index')
            code = web_request.get_int('code')
            oneshot = web_request.get_int('oneshot', 1)
            message = web_request.get_str('message', '')
            level = web_request.get_int('level', 3)
            timestamp = time.time()

            excep = {'id': id, 'index': index, 'code': code, 'level': level, 'message': message}
            excep['timestamp'] = timestamp
            self.server.send_event("snapmaker:exception_notification", excep)

            if oneshot is None:
                oneshot = True

            if not oneshot:
                # record the exception
                self._init_exception_status(self.system_excep_status, id, index, code)
                should_notify = self._update_exception_cache(
                    self.system_excep_cache, id, index, code, level, timestamp, message)

                if should_notify:
                    self._notify_exception_status()

            logging.info(f"Exception from MQTT: {excep}")
        except TypeError:
            # response is a generic gcode error
            # excep = {'code': 0, 'message': payload}
            # self.server.send_event("snapmaker:exception", excep)
            logging.error(f"failed to get args form end: \
                            {web_request.get_endpoint()} {web_request.get_args()}")
            return {"state": "error", "message": "invalid arguments"}
        except Exception as e:
            logging.error(f"invalid system exception info: {e}, {web_request.get_args()}")
            return {"state": "error", "message": str(e)}

        return {"state": "success"}

    async def _on_exception_clear(self,
                                web_request: WebRequest
                                ) -> Dict[str, Any]:
        try:
            id = web_request.get_int('id')
            index = web_request.get_int('index')
            code = web_request.get_int('code')
            cleared = False

            # Check system exception status
            if self.system_excep_status.get(id) is not None and \
                self.system_excep_status[id].get(index) is not None:
                if code in self.system_excep_status[id][index]:
                    self.system_excep_status[id][index].remove(code)
                    cache = {'id': id, 'index': index, 'code': code}
                    # self.system_excep_cache.remove(cache)
                    to_remove = [ex for ex in self.system_excep_cache
                                if all(ex.get(k) == v for k, v in cache.items())]
                    if to_remove:
                        for ex in to_remove:
                            self.system_excep_cache.remove(ex)
                    cleared = True
                    logging.info(f"system exception {id}.{index}.{code} cleared")

            # Check motion exception status
            if self.motion_excep_status.get(id) is not None and \
                self.motion_excep_status[id].get(index) is not None:
                if code in self.motion_excep_status[id][index]:
                    self.motion_excep_status[id][index].remove(code)
                    cache = {'id': id, 'index': index, 'code': code}
                    # self.motion_excep_cache.remove(cache)
                    to_remove = [ex for ex in self.motion_excep_cache
                                if all(ex.get(k) == v for k, v in cache.items())]
                    if to_remove:
                        for ex in to_remove:
                            self.motion_excep_cache.remove(ex)
                    cleared = True
                    logging.info(f"motion exception {id}.{index}.{code} cleared")

            if cleared:
                excep_cache = self.motion_excep_cache + self.system_excep_cache
                self.server.send_event("snapmaker:exception_status", {"exceptions": excep_cache})

        except Exception as e:
            logging.error(f"failed to clear system exception: {e}, {web_request.get_args()}")
            return {"state": "error", "message": str(e)}

        return {"state": "success"}

    def _set_motion_exception(self, exceptions: Dict[str, Any]) -> None:
        self.motion_excep_status = {}
        self.motion_excep_cache = []
        latest_excep = exceptions.get('exceptions', [])
        logging.info(f'klippy exceptions: {latest_excep}')
        if latest_excep is not None:
            for excep in latest_excep:
                id = excep['id']
                index = excep['index']
                code = excep['code']
                level = excep['level']
                message = excep['message']
                timestamp = excep.get('timestamp', time.time())
                if self.motion_excep_status.get(id) is None:
                    self.motion_excep_status[id] = {}
                if self.motion_excep_status[id].get(index) is None:
                    self.motion_excep_status[id][index] = []

                if code not in self.motion_excep_status[id][index]:
                    self.motion_excep_status[id][index].append(code)

                self._update_exception_cache(self.motion_excep_cache, id, index, code, level, timestamp, message)

                logging.info(f"Exception from Klippy: {excep}")
        excep_cache = self.motion_excep_cache + self.system_excep_cache
        self.server.send_event("snapmaker:exception_status", {"exceptions": excep_cache})

    def _init_exception_status(self,
                             status_dict: Dict[int, Dict[int, List[int]]],
                             id: int,
                             index: int,
                             code: int) -> None:
        """Initialize exception status dictionary structure if needed and add code."""
        if status_dict.get(id) is None:
            status_dict[id] = {}
        if status_dict[id].get(index) is None:
            status_dict[id][index] = []
        if code not in status_dict[id][index]:
            status_dict[id][index].append(code)

    def _update_exception_cache(self,
                              cache_list: List[Dict[str, Any]],
                              id: int,
                              index: int,
                              code: int,
                              level: int,
                              timestamp: float,
                              message: str) -> bool:
        """Update exception cache and return if it's a new exception."""
        duplicate, updated = False, False
        cache = {'id': id, 'index': index, 'code': code, 'level': level, 'message': message, 'timestamp': timestamp}
        for e in cache_list:
            try:
                if (e['id'] == id and e['index'] == index and e['code'] == code):
                    if e['level'] == level and e['message'] == message and e['timestamp'] == timestamp:
                        duplicate = True
                    else:
                        e.update({'level': level, 'message': message, 'timestamp': timestamp})
                        updated = True
                    break
            except KeyError:
                continue

        if not duplicate and not updated:
            cache_list.append(cache)

        return not duplicate

    def _notify_exception_status(self) -> None:
        """Send current exception status to all clients."""
        excep_cache = self.motion_excep_cache + self.system_excep_cache
        self.server.send_event("snapmaker:exception_status", {"exceptions": excep_cache})

    async def _on_klippy_ready(self) -> None:
        result: Dict[str, Dict[str, Any]]
        logging.info(f"query klippy exceptions")
        result = await self.klippy_apis.query_objects(
            {"exception_manager": None}, {}
        )
        logging.info(f"klippy exceptions: {result}")
        if 'exception_manager' not in result:
            return
        self._set_motion_exception(result['exception_manager'])

    async def _on_klippy_shutdown(self) -> None:
        result: Dict[str, Dict[str, Any]]
        logging.info(f"query klippy exceptions")
        result = await self.klippy_apis.query_objects(
            {"exception_manager": None}, {}
        )
        logging.info(f"klippy exceptions: {result}")
        if 'exception_manager' not in result:
            return
        self._set_motion_exception(result['exception_manager'])
        # self.motion_excep_status = {self.MUDOLE_ID_MOTION: {0: [self.CODE_MOTION_SHUTDOWN]}}
        # cache = {'id': self.MUDOLE_ID_MOTION, 'index': 0, 'code': self.CODE_MOTION_SHUTDOWN}
        # self.motion_excep_cache = [cache]
        # # publish to clients
        # excep_cache = self.motion_excep_cache + self.system_excep_cache
        # self.server.send_event("snapmaker:exception_status", {"exceptions": excep_cache})

    async def _on_klippy_disconnected(self) -> None:
        pass
        # self.motion_excep_status = {self.MUDOLE_ID_MOTION: {0: [self.CODE_MOTION_DISCONNECTED]}}
        # cache = {'id': self.MUDOLE_ID_MOTION, 'index': 0, 'code': self.CODE_MOTION_DISCONNECTED}
        # self.motion_excep_cache = [cache]
        # # publish to clients
        # excep_cache = self.motion_excep_cache + self.system_excep_cache
        # self.server.send_event("snapmaker:exception_status", {"exceptions": excep_cache})

def load_component(config: ConfigHelper) -> ExceptionManager:
    return ExceptionManager(config)
