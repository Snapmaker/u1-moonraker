# SnapmakerCloud Support
#
# Copyright (C) 2024 Scott Huang <shili.huang@snapmaker.com>
#
# This file may be distributed under the terms of the GNU GPLv3 license.

from __future__ import annotations
import os, time, sys
import asyncio
import logging
import queue, threading
import pathlib, random
import hashlib, shutil
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
    from .httpx_client import HttpxClient
    from .power import PrinterPower
    from .announcements import Announcements
    from .webcam import WebcamManager, WebCam
    from .klippy_connection import KlippyConnection
    from .mqtt import MQTTClient
import base64

COMPONENT_VERSION = "0.0.1"

class SnapmakerCloud:
    DEVICE_STA_OFFLINE  = 0
    DEVICE_STA_IDLE     = 1
    DEVICE_STA_PRINTING = 2
    DEVICE_STA_ERROR    = 3

    MAIN_STATE_PRINTING = 1
    # ACTION CODE
    AC_PRINT_PAUSED = 129
    AC_PRINT_EXCEPTION_PAUSED = 137
    AC_PRINT_EXCEPTION_CANCELED = 138
    AC_PRINT_CANCELED = 139
    AC_PRINT_COMPLETE = 140
    def __init__(self, config: ConfigHelper) -> None:
        self.server = config.get_server()
        # self._logger = ProtoLogger(config)
        self.eventloop = self.server.get_event_loop()
        self.job_state: JobState
        self.job_state = self.server.lookup_component("job_state")
        self.klippy_apis: KlippyAPI
        self.klippy_apis = self.server.lookup_component("klippy_apis")
        self.cache = ReportCache()
        self.print_handler = PrintHandler(self)
        self.last_received_temps: Dict[str, float] = {}
        self.last_err_log_time: float = 0.
        self.last_cpu_update_time: float = 0.
        self.intervals: Dict[str, float] = {
            "job": 1.,
            "temps": 1.,
            "temps_target": .25,
            "cpu": 10.,
            "ai": 0.,
            "ping": 20.,
        }
        self.printer_status: Dict[str, Dict[str, Any]] = {}
        self.heaters: Dict[str, str] = {}
        self.missed_job_events: List[Dict[str, Any]] = []
        self.announce_mutex = asyncio.Lock()
        self.connection_task: Optional[asyncio.Task] = None
        self.reconnect_delay: float = 1.
        self.reconnect_token: Optional[str] = None
        self._last_sp_ping: float = 0.
        self._print_request_event: asyncio.Event = asyncio.Event()
        self.next_temp_update_time: float = 0.
        self._last_ping_received: float = 0.
        self.gcode_terminal_enabled: bool = False
        self.connected: bool = False
        self.mqtt: MQTTClient = None
        self.fm: FileManager = self.server.lookup_component("file_manager")
        dp = self.server.get_app_arg("data_path", None)
        if dp:
            sn_path = pathlib.Path(dp).joinpath(".lava.sn")
            if sn_path.is_file():
                self._sn = sn_path.read_text().strip().upper()
            else:
                logging.error("failed to get sn file")

        # Register State Events
        self.server.register_event_handler(
            "server:klippy_started", self._on_klippy_startup)
        self.server.register_event_handler(
            "server:klippy_ready", self._on_klippy_ready)
        self.server.register_event_handler(
            "server:klippy_shutdown", self._on_klippy_shutdown)
        self.server.register_event_handler(
            "server:klippy_disconnect", self._on_klippy_disconnected)
        self.server.register_event_handler(
            "job_state:state_changed", self._on_job_state_changed)
        self.server.register_event_handler(
            "klippy_apis:pause_requested", self._on_pause_requested)
        self.server.register_event_handler(
            "klippy_apis:resume_requested", self._on_resume_requested)
        self.server.register_event_handler(
            "klippy_apis:cancel_requested", self._on_cancel_requested)

        self.server.register_endpoint(
            "/server/files/get_status", RequestType.POST, self._handle_get_status,
            transports=(TransportType.all() & ~TransportType.HTTP)
        )
        self.server.register_endpoint(
            "/server/files/pull", RequestType.POST, self._handle_pull_file,
            transports=(TransportType.all() & ~TransportType.HTTP)
        )
        self.server.register_endpoint(
            "/server/files/start_print", RequestType.POST, self._handle_start_cloud_print,
            transports=(TransportType.all() & ~TransportType.HTTP)
        )
        self.server.register_endpoint(
            "/server/files/cancel_pull", RequestType.POST, self._handle_cancel_pull_file,
            transports=(TransportType.all() & ~TransportType.HTTP)
        )
        self.server.register_endpoint(
            "/server/files/list_page", RequestType.GET, self._handle_filelist_page_request,
            transports=(TransportType.all() & ~TransportType.HTTP)
        )
        self.server.register_endpoint(
            "/server/files/thumbnails_base64", RequestType.GET, self._handle_list_thumbs_base64,
            transports=(TransportType.all() & ~TransportType.HTTP)
        )
        self.server.register_endpoint(
            "/server/files/get_userdata_space", RequestType.POST, self._handle_get_userdata_space
        )
        self.server.register_notification("smcloud:file_pull_progress")
        self.server.register_notification("smcloud:devie_info_update")

        app_args = self.server.get_app_args()
        self.datapath: pathlib.Path = pathlib.Path(app_args["data_path"])

        self.server.register_endpoint("/server/gcode/enable_output",
                                      RequestType.POST,
                                      self._handle_gcode_enable_output,
                                      transports=TransportType.MQTT
                                      )

        self.server.register_endpoint("/server/gcode/disable_output",
                                      RequestType.POST,
                                      self._handle_gcode_disable_output,
                                      transports=TransportType.MQTT
                                      )

        self.server.register_event_handler(
                                    "smcloud:devie_info_update",
                                    self._on_device_info_update
                                    )
    async def component_init(self) -> None:
        self.mqtt = self.server.lookup_component("mqtt", None)
        if self.mqtt is None:
            logging.info("smcloud: MQTT doesn't exist")
            return
        self.mqtt.register_notification("smcloud:file_pull_progress")
        self.mqtt.register_notification("file_manager:filelist_changed")
        self.mqtt.register_notification("job_queue:job_queue_changed")
        self.mqtt.register_notification("history:history_changed")
        # self.mqtt.subscribe_topic("/auth/response/moonraker", self._process_mqtt_response)
        # mqtt.register_notification("server:gcode_response")
        logging.info("smcloud: has registered MQTT notification!")
        self.fm.get_directory
        self.fm.register_data_folder('camera')

    async def _on_device_info_update(self, info: Dict[str, Any]) -> None:
        if 'device_name' in info:
            await self.update_device_status(-1, info['device_name'])
    async def _handle_get_status(self,
                                   web_request: WebRequest
                                   ) -> Dict[str, Any]:
        return await self.print_handler.get_status()

    async def _handle_start_cloud_print(self,
                                   web_request: WebRequest
                                   ) -> Dict[str, Any]:
        """Handle start print request."""
        url: str = web_request.get_str("url")
        checksum = web_request.get_str("checksum", None)
        file_size = web_request.get_int("size", 0)
        file_type = web_request.get_str("type", None)
        logging.info(f"cloud print: url: file_size:{file_size}, checksum: {checksum}, type: {file_type}")
        free_space, _ = self.fm.get_user_space()
        if file_size > free_space:
            logging.error(f"not enough space for file {file_size} > {free_space}")
            return {'state': 'error', 'message': 'not enough space for file'}
        commands = web_request.get_list("commands", [])

        return await self.print_handler.handle_cloud_print(url, commands, checksum, file_type)

    async def _handle_pull_file(self,
                                web_request: WebRequest
                                ) -> Dict[str, Any]:
        try:
            url: str = web_request.get_str("url")
            auto_start = web_request.get_boolean("auto_start", False)
            checksum = web_request.get_str("checksum", None)
            filetype = web_request.get_str("type", None)
            # default file size: 500MB
            filesize = web_request.get_int("size", 0x1F400000)
            free_space, total_space = self.fm.get_user_space()
            print_plate = web_request.get_int("print_plate", 1)
            logging.info(f"free_space: {free_space}, total_space: {total_space}")
            if free_space <= 0 or filesize > free_space:
                logging.error(f"not enough space for file {filesize} > {free_space}")
                return {'state': 'error', 'message': 'not enough space for file'}

            if auto_start:
                if not await self.print_handler.check_can_print():
                    return {"state": "busy", "message": "printer is busy"}
            logging.info(f"pull file: start:{auto_start}, checksum: {checksum}, \
                            type: {filetype}, size: {filesize}, print_plate: {print_plate}")
            return await self.print_handler.download_file(url, auto_start, checksum, filetype, print_plate)
        except Exception as e:
            logging.error(f"{e}")
            return {
                "state": "error",
                "message": "exception: {}".format(e)
            }

    async def _handle_cancel_pull_file(self,
                                        web_request: WebRequest
                                        ) -> Dict[str, Any]:
        return await self.print_handler.cancel()

    async def _handle_filelist_page_request(self,
                                       web_request: WebRequest
                                       ) -> Dict[str, Any]:
        try:
            root = web_request.get_str('root', None)
            files_per_page = web_request.get_int('files_per_page', None)
            page_number = web_request.get_int('page_number', None)
            # specify storage type: all/local/usb
            storage = web_request.get_str('storage', 'all')
            if files_per_page is None or page_number is None or root is None:
                return {
                    'state': 'error',
                    'message': 'invalid parameters {}'.format(web_request.get_args())
                }

            flist = self.fm.get_file_list(root, list_format=True, storage_type=storage)
            flist = cast(List[Dict[str, Any]], flist)

            # Calculate pagination
            total_files = len(flist)
            start_idx = page_number * files_per_page
            end_idx = start_idx + files_per_page
            if start_idx >= len(flist):
                paginated_list = []
            else:
                paginated_list = flist[start_idx:end_idx]
            return {
                'state': 'success',
                'root': root,
                'storage': storage,
                'total': total_files,
                'page_number': page_number,
                'files_per_page': files_per_page,
                'files': paginated_list
            }
        except Exception as e:
            logging.error(f"filelist_page_request error: {e}")
            return {
                'state': 'error',
                'message': str(e)
            }

    async def _handle_list_thumbs_base64(
        self, web_request: WebRequest
    ) -> Dict[str, Any]:
        try:
            requested_file: str = web_request.get_str("path", "")
            if requested_file == "":
                return {
                    'state': 'error',
                    'message': 'invalid parameters {}'.format(web_request.get_args())
                }
            metadata: Optional[Dict[str, Any]]
            metadata = self.fm.gcode_metadata.get(requested_file, None)
            if metadata is None:
                return {
                    'state': 'error',
                    'message': 'file is not a available gcode'
                }
            if "thumbnails" not in metadata:
                return {
                    'state': 'error',
                    'message': 'no thumbnail for file'
                }
            gc_path = self.fm.file_paths.get('gcodes', "")
            full_path = os.path.join(gc_path, requested_file)
            if not os.path.isfile(full_path):
                return {
                    'state': 'error',
                    'message': 'file not found'
                }

            thumb = self._parse_thumbnails(full_path)

            if not thumb:
                return {
                    'state': 'error',
                    'message': 'failed to parse thumbnail'
                }
            thumb['state'] = 'success'
            thumb['path'] = requested_file
            return thumb
        except Exception as e:
            return {
                'state': 'error',
                'message': str(e)
            }


    def _regex_find_ints(self, pattern: str, data: str) -> List[int]:
        pattern = pattern.replace(r"(%D)", r"([0-9]+)")
        matches = re.findall(pattern, data)
        if matches:
            # return the maximum height value found
            try:
                return [int(h) for h in matches]
            except Exception:
                pass
        return []

    def _parse_thumbnails(self, file_path: str) -> Optional[Dict[str, Any]]:
        header_data = None
        with open(file_path, 'r') as f:
            # read the 100kB, which should be enough to
            # identify the thumbnail
            header_data = f.read(102400)
        if header_data is None:
            return header_data
        for data in [header_data]:
            thumb_matches: List[str] = re.findall(
                r"; thumbnail begin[;/\+=\w\s]+?; thumbnail end", data)
            if thumb_matches:
                break
        else:
            return None
        parsed_matches: List[Dict[str, Any]] = []
        for match in thumb_matches:
            lines = re.split(r"\r?\n", match.replace('; ', ''))
            info = self._regex_find_ints(r"(%D)", lines[0])
            data = "".join(lines[1:-1])
            if len(info) != 3:
                logging.info(
                    f"MetadataError: Error parsing thumbnail"
                    f" header: {lines[0]}")
                continue
            if len(data) != info[2]:
                logging.info(
                    f"MetadataError: Thumbnail Size Mismatch: "
                    f"detected {info[2]}, actual {len(data)}")
                continue
            parsed_matches.append({
                'width': info[0], 'height': info[1],
                'size': len(data),
                'data': data})
        if len(parsed_matches) == 0:
            return None

        # Always find the largest thumbnail that is not too large
        largest_match = parsed_matches[0]
        for item in parsed_matches:
            if largest_match['width'] == 300:
                break
            if item['width'] > largest_match['width'] or item['height'] > largest_match['height']:
                largest_match = item

        return largest_match

    def _public_gcode_output(self, *args):
        self.mqtt.publish_notification("notify_gcode_response", list(args)[0])

    async def _handle_gcode_enable_output(self,
                                        web_request: WebRequest
                                        ) -> Dict[str, Any]:
        logging.info(f'client enable gcode output')
        self.server.register_event_handler("server:gcode_response", self._public_gcode_output)
        return {'state': 'success'}

    async def _handle_gcode_disable_output(self,
                                        web_request: WebRequest
                                        ) -> Dict[str, Any]:
        logging.info(f'client disable gcode output')
        self.server.unregister_event_handler("server:gcode_response", self._public_gcode_output)
        return {'state': 'success'}

    async def _on_klippy_ready(self) -> None:
        last_stats: Dict[str, Any] = self.job_state.get_last_stats()
        if last_stats["state"] == "printing":
            self._on_print_started(last_stats, last_stats, False)
            await self.update_device_status(self.DEVICE_STA_PRINTING)
        else:
            self._update_state("operational")
            await self.update_device_status(self.DEVICE_STA_IDLE)

    async def _on_klippy_startup(self, state: KlippyState) -> None:
        if state != KlippyState.READY:
            self._update_state("error")
            await self.update_device_status(self.DEVICE_STA_ERROR)
        else:
            await self.update_device_status(self.DEVICE_STA_IDLE)

    async def _on_klippy_shutdown(self) -> None:
        try:
            if self.cache.is_printing():
                result: Dict[str, Dict[str, Any]]
                result = await self.klippy_apis.query_objects(
                    {"exception_manager": None}, {}
                )
                if 'exception_manager' in result:
                    if 'exceptions' in result['exception_manager']:
                        if isinstance(result['exception_manager']['exceptions'], list) and \
                            len(result['exception_manager']['exceptions']) > 0:
                            exception = result['exception_manager']['exceptions'][0]
                        else:
                            exception = None
                filename = self.cache.job_info.get('filename', '')
                self._send_job_event("shutdown", {}, {'exception': exception, 'filename': filename})
        except Exception as e:
            logging.exception(f"Failed to send_job_event {e}")
        self._update_state("error")
        await self.update_device_status(self.DEVICE_STA_ERROR)

    async def _on_klippy_disconnected(self) -> None:
        self._update_state("offline")
        # self.send_sp("connection", {"new": "disconnected"})
        self.cache.reset_print_state()
        self.printer_status = {}
        await self.update_device_status(self.DEVICE_STA_ERROR)

    def _on_job_state_changed(self, job_event: JobEvent, *args) -> None:
        callback: Optional[Callable] = getattr(self, f"_on_print_{job_event}", None)
        if callback is not None:
            callback(*args)
        else:
            logging.info(f"No defined callback for Job Event: {job_event}")

    def _on_print_started(
        self,
        prev_stats: Dict[str, Any],
        new_stats: Dict[str, Any],
        need_start_event: bool = True
    ) -> None:
        # inlcludes started and resumed events
        self._update_state("printing")
        filename = new_stats["filename"]
        job_info: Dict[str, Any] = {"filename": filename}
        metadata = self.fm.get_file_metadata(filename)
        filament: Optional[float] = metadata.get("filament_total")
        if filament is not None:
            job_info["filament"] = round(filament)
        est_time = metadata.get("estimated_time")
        if est_time is not None:
            job_info["time"] = est_time
        self.cache.metadata = metadata
        self.cache.job_info.update(job_info)
        if need_start_event:
            job_info["started"] = True
    def _reset_file(self) -> None:
        # cur_job = self.cache.job_info.get("filename", "")
        self.print_handler.last_started = ""

    def _on_print_paused(self, *args) -> None:
        # self.send_sp("job_info", {"paused": True})
        # get params from args
        self._update_state("paused")
        self._send_job_event('paused', *args)

    def _on_print_resumed(self, *args) -> None:
        self._update_state("printing")
        self._send_job_event('resumed', *args)

    def _on_print_cancelled(self, *args) -> None:
        self._reset_file()
        self._send_job_event('cancelled', *args)
        self._update_state_from_klippy()
        self.cache.job_info = {}

    def _on_print_error(self, *args) -> None:
        self._reset_file()
        self._send_job_event('error', *args)
        self._update_state_from_klippy()
        self.cache.job_info = {}

    def _on_print_complete(self, *args) -> None:
        self._reset_file()
        self._send_job_event('complete', *args)
        self._update_state_from_klippy()
        self.cache.job_info = {}

    def _on_print_standby(self, *args) -> None:
        self._update_state_from_klippy()
        self.cache.job_info = {}

    def _on_pause_requested(self) -> None:
        self._print_request_event.set()
        if self.cache.state == "printing":
            self._update_state("pausing")

    def _on_resume_requested(self) -> None:
        self._print_request_event.set()
        if self.cache.state == "paused":
            self._update_state("resuming")

    def _on_cancel_requested(self) -> None:
        self._print_request_event.set()
        if self.cache.state in ["printing", "paused", "pausing"]:
            self._update_state("cancelling")

    def _update_state_from_klippy(self) -> None:
        kconn: KlippyConnection = self.server.lookup_component("klippy_connection")
        klippy_state = kconn.state
        if klippy_state == KlippyState.READY:
            sp_state = "operational"
        elif klippy_state in [KlippyState.ERROR, KlippyState.SHUTDOWN]:
            sp_state = "error"
        else:
            sp_state = "offline"
        self._update_state(sp_state)

    def _update_state(self, new_state: str) -> None:
        if self.cache.state == new_state:
            return
        self.cache.state = new_state
    def _send_job_event(self,
                        state: str,
                        prev_stats: Dict[str, Any]={},
                        new_stats: Dict[str, Any]={}) -> None:
        normal_code = "-N-N-N-N"
        exception = new_stats.get("exception", {})
        logging.info(f"state: {state}, exception: {exception}")
        id = exception.get('id', None)
        index = exception.get('index', None)
        level = exception.get('level', None)
        code = exception.get('code', None)
        main_state = str(self.MAIN_STATE_PRINTING)
        if state == "shutdown":
            job_state = ''.join([main_state, '-', str(self.AC_PRINT_EXCEPTION_CANCELED)])
            if id is None or index is None or \
                level is None or code is None:
                job_state = ''.join([job_state, '-', normal_code])
            else:
                job_state = ''.join([job_state, '-', str(level), '-', str(id), '-', str(index), '-', str(code)])
        else:
            if id is None or index is None or \
                level is None or code is None:
                if state == "paused":
                    job_state = ''.join([main_state, '-', str(self.AC_PRINT_PAUSED), normal_code])
                elif state == "cancelled":
                    job_state = ''.join([main_state, '-', str(self.AC_PRINT_CANCELED), normal_code])
                elif state == "complete":
                    job_state = ''.join([main_state, '-', str(self.AC_PRINT_COMPLETE), normal_code])
                else:
                    logging.info(f"won't send event for normal state: {state}")
                    return
            else:
                if state == "paused":
                    job_state = ''.join([main_state, '-', str(self.AC_PRINT_EXCEPTION_PAUSED)])
                elif state == "cancelled" or state == "error":
                    job_state = ''.join([main_state, '-', str(self.AC_PRINT_EXCEPTION_CANCELED)])
                else:
                    logging.info(f"won't send event for except state: {state}")
                    return
                # show exception info:
                logging.debug(f"exception: {id} {index} {level} {code}")
                job_state = ''.join([job_state, '-', str(level), '-', str(id), '-', str(index), '-', str(code)])

        notification = {
            "state": job_state,
            "filename": new_stats.get("filename", ""),
            "timestamp": time.time()
        }
        logging.info(f"state: {state}, job notification: {notification}")
        self.mqtt.publish_notification("notify_device_state_changed", notification)

    def close(self) -> None:
        logging.info("snapmaker cloud exited")
        self.print_handler.cancel()

    async def _handle_get_userdata_space(
        self, web_request: WebRequest
    ) -> Dict[str, Any]:
        """Get the user space in MiB for the user data path."""
        free_space, total_space = self.fm.get_user_space()
        # convert to MiB
        total_space = total_space // (1024 * 1024)
        free_space = free_space // (1024 * 1024)
        logging.info(f"userdata space: {free_space} MiB / {total_space} MiB")
        return {
            "state": "success",
            "free_space": free_space if free_space > 0 else 0,
            "total_space": total_space if total_space > 0 else 0,
            "units": "MiB"
        }

    async def update_device_status(self, status: int = -1, device_name=None) -> None:
        if self.mqtt is None:
            logging.warning("MQTT client not initialized, cannot update device status")
            return
        if device_name is None:
            machine: Machine = self.server.lookup_component("machine", None)
            device_name = machine.get_device_name()
        logging.info(f"Updating device status {status}, name: {device_name}")
        retry: int = 3
        jrpc_id = random.randint(0, 0x7fffffff)
        while retry > 0:
            retry -= 1
            try:
                # publish the device status to the MQTT agent
                req_msg = {
                    'jsonrpc': "2.0",
                    'method': 'mqtt_agent.update_device_status',
                    'id': jrpc_id,
                    'params': {
                        'name': device_name
                    }
                }
                if status >= 0:
                    req_msg['params']['online'] = status
                resp = await self.mqtt.publish_topic_with_response(
                        "mqtt_agent/request/moonraker",
                        "mqtt_agent/response/moonraker",
                        req_msg,
                        qos=0,
                        timeout=10)

                if resp is None:
                    await asyncio.sleep(1)
                    continue

                obj: Dict[str, Any] = jsonw.loads(resp)
                if obj.get('result', None) is None:
                    await asyncio.sleep(1)
                    continue

                if obj.get('id', 0) != jrpc_id:
                    logging.error(f"dev sta: Invalid jrpc id in response: {obj}")
                    await asyncio.sleep(1)
                    continue

                params = obj['result']
                if not isinstance(params, dict) and params.get('state', 'error') != 'success':
                    # sleep 1s and retry
                    logging.error(f"dev sta: return state: {params}")
                    await asyncio.sleep(1)
                    continue
                return
            except Exception as e:
                logging.error(f"Failed to update device status: {e}")
            await asyncio.sleep(1)
        logging.error("Max retries reached, failed to update device status")

class ReportCache:
    def __init__(self) -> None:
        self.state = "offline"
        self.metadata: Dict[str, Any] = {}
        self.job_info: Dict[str, Any] = {}
        # Persistent state across connections
        self.firmware_info: Dict[str, Any] = {}
        self.machine_info: Dict[str, Any] = {}
        self.file_url: str = ""
        self.file_size: int = 0
        self.file_type: str = ""
        self.file_checksum: str = ""
        self.configs: List[str] = []

    def is_printing(self) -> bool:
        return self.state == "printing" or self.state == "paused"

    def reset_print_state(self) -> None:
        self.temps = {}
        self.mesh = {}
        self.job_info = {}

class PrintHandler:
    CHECK_INTERVAL = 2
    SHOW_PROGRESS_INTERVAL = 1
    SHOW_PROGRESS = [0, 50, 100]
    def __init__(self, snapmakercloud: SnapmakerCloud) -> None:
        self.snapmakercloud = snapmakercloud
        self.server = snapmakercloud.server
        self.eventloop = self.server.get_event_loop()
        self.cache = snapmakercloud.cache
        self.download_task: Optional[asyncio.Task] = None
        self.download_progress: int = -1
        self.download_file_name: str = ""
        self.download_time: float = 0.0
        self.pending_file: str = ""
        self.last_started: str = ""
        self.auto_start = False
        self.sp_user = UserInfo("SnapmakerCloud", "")
        self.fm: FileManager = self.server.lookup_component("file_manager")
        self.download_state: str = "idle"
        self.state_lock = asyncio.Lock()
        self._check_timer = self.eventloop.register_timer(
            self._check_task
        )

    def _check_task(self, eventtime: float) -> float:
        if self.download_task is None:
            self.download_progress = -1
            self.download_time = 0.0
            return eventtime + self.CHECK_INTERVAL * 2

        if self.download_task.done():
            self.download_progress = -1
            self.download_time = 0.0
            self.download_task = None
            return eventtime + self.CHECK_INTERVAL * 2
        return eventtime + self.CHECK_INTERVAL

    def _notify_download_state(self, state: str, message: str = "") -> None:
        event = {'state': state}
        if len(message) > 0:
            event['message'] = message
        event['path'] = self.download_file_name
        event['auto_start'] = self.auto_start
        logging.info(f"notify_download_event: {event}")
        self.server.send_event("smcloud:file_pull_progress", event)

    async def _reset_download_state(self) -> None:
        async with self.state_lock:
            self.download_state = "idle"
            self.auto_start = False
            self.download_file_name = ""
            self.download_progress = -1
        logging.info("reset_download_state")

    async def get_status(self) -> Dict[str, Any]:
        status = {"state": self.download_state}
        if self.download_state == "downloading":
            status["path"] = self.download_file_name
            status["auto_start"] = self.auto_start
            status["percent"] = self.download_progress if self.download_progress > 0 else 0
        else:
            kconn: KlippyConnection
            kconn = self.server.lookup_component("klippy_connection")
            job_state: JobState = self.server.lookup_component("job_state")
            last_stats = job_state.get_last_stats()
            state: str = last_stats.get('state', "")
            if state in ["printing", "paused"]:
                status["state"] = "busy"
        logging.info(f"file status: {status}")
        return status

    async def download_file(self, url: str, start: bool,
                            checksum: str, filetype: str,
                            print_plate: int) -> Dict[str, Any]:
        url_path = urlparse(url)
        target_file = pathlib.Path(unquote(url_path.path))
        fm: FileManager = self.server.lookup_component("file_manager")
        gc_path = pathlib.Path(fm.get_directory())
        if not gc_path.is_dir():
            logging.warning(f"GCode Path Not Registered: {gc_path}")
            return {"state": "error", "message": "GCode Path not Registered"}

        target_path = gc_path.joinpath(target_file.name)
        if len(target_file.name) == 0:
            logging.warning(f"Invalid URL")
            return {"state": "error", "message": "Invalid URL: {}".format(url)}
        try:
            # check file path is in used or not
            fm._handle_operation_check(str(target_path))
        except Exception as e:
            logging.warning(f"File Operation Check Failed: {e}")
            return {"state": "error", "message": f"{e}"}

        async with self.state_lock:
            if self.download_state != "idle":
                logging.warning(f"we are downloading file, cannot downloading new file")
                return {"state": "downloading", "message": "device is downloading file"}
            self.download_state = "downloading"
            self.download_progress = 0
            self.download_file_name = target_file.name
            self.auto_start = start
            self.download_time = 0

        coro = self._download_sm_file(url, start, checksum, filetype, print_plate)
        self.download_task = self.eventloop.create_task(coro)
        return {"state": "success"}

    async def cancel(self):
        if self.download_task is None:
            return {"state": "error", "message": "we are not pulling any file"}
        elif not self.download_task.done():
            self.download_task.cancel()
            self.download_task = None
            self._notify_download_state("cancel", "user cancel downloading")
            await self._reset_download_state()
            return {"state": "success"}
        else:
            self.download_task = None
            self.download_progress = -1
            self.download_time = 0.0
            self._notify_download_state("cancel", "user cancel downloading")
            await self._reset_download_state()
            return {"state": "error", "message": "pulling task is exited"}

    async def _authorize_download_url(self, url: str) -> str:
        retry = 3
        jrpc_id = random.randint(0, 0x7fffffff)
        resp = None
        while retry > 0:
            retry -= 1
            try:
                # send new pin to authentication server and request it update to cloud server
                req_msg = {
                    'jsonrpc': "2.0",
                    'method': 'mqtt_agent.authorize_file_url',
                    'id': jrpc_id,
                    'params': {
                        'url': url
                    }
                }
                resp = await self.snapmakercloud.mqtt.publish_topic_with_response(
                        "mqtt_agent/request/moonraker_file",
                        "mqtt_agent/response/moonraker_file",
                        req_msg,
                        qos=0,
                        timeout=10)
                if resp is None:
                    await asyncio.sleep(1)
                    continue

                obj: Dict[str, Any] = jsonw.loads(resp)
                if obj.get('result', None) is None:
                    await asyncio.sleep(1)
                    continue

                if obj.get('id', 0) != jrpc_id:
                    logging.error(f"Invalid jrpc id in response: {obj}")
                    await asyncio.sleep(1)
                    continue

                result = obj['result']
                if not isinstance(result, dict) and not isinstance(result.get('url', None), str):
                    # sleep 1s and retry
                    logging.error(f"authorize URL state: {result}")
                    await asyncio.sleep(1)
                    continue
                logging.debug(f"Authorized URL: {result}")
                return result.get('url')
            except Exception as e:
                resp = None
                logging.error(f"authorize URL exception: {e}, resp: {resp}")
                continue

    def _cleanup_3mf_temp_files(self, tmpdir_path: pathlib.Path, plates: List, plates_md5: List, top_thumbs: List) -> None:
        all_files = plates + plates_md5 + top_thumbs
        for file_info in all_files:
            filename = file_info.filename if hasattr(file_info, 'filename') else file_info
            file_path = tmpdir_path / filename
            try:
                if os.path.exists(file_path):
                    os.remove(file_path)
            except Exception as e:
                logging.warning(f"Failed to remove temporary file {file_path}: {e}")

    async def _handle_3mf(self, tmp_path: pathlib.Path,
                            gcode_path: pathlib.Path,
                            name_3mf: pathlib.Path,
                            print_plate: int) -> Optional[str]:
        import zipfile

        encodings = [
            'utf-8',
            'gbk',
            'big5',
            'cp850',
            'cp437',
            'cp932',
            'shift_jis',
            'euc_jp',
            'cp949',
            'euc_kr',
            'iso2022_jp',
            'iso2022_kr',
            'mac_roman',
        ]

        def decode_filename(raw_filename: str) -> str:
            for enc in encodings:
                try:
                    return raw_filename.encode('cp437').decode(enc)
                except Exception:
                    continue
            return raw_filename

        # Use the directory containing the 3mf file as the working directory
        tmpdir_path = tmp_path.parent

        try:
            with zipfile.ZipFile(tmp_path, 'r') as zf:
                content_types_path = None
                plate_1_gcode_path = None
                plate_1_md5_path = None
                project_settings_path = None
                top_1_png_path = None
                plates = []
                plates_md5 = []
                top_thumbs = []

                for info in zf.infolist():
                    decoded_name = decode_filename(info.filename)
                    logging.debug(f"========>> Content file: {decoded_name}")

                    if decoded_name == '[Content_Types].xml':
                        logging.info("[Content_Types].xml found")
                        content_types_path = info.filename
                        continue
                    elif decoded_name.lower().endswith('project_settings.config'):
                        logging.info("Found project_settings.config")
                        project_settings_path = info.filename
                        continue

                    for i in range(1, 36):
                        if decoded_name.lower().endswith(f'plate_{i}.gcode'):
                            logging.info(f"Found plate_{i}.gcode")
                            plates.append(info.filename)
                            continue
                        if decoded_name.lower().endswith(f'plate_{i}.gcode.md5'):
                            logging.info(f"Found plate_{i}.gcode.md5")
                            plates_md5.append(info.filename)
                            continue
                        if decoded_name.lower().endswith(f'top_{i}.png'):
                            logging.info(f"Found top_{i}.png")
                            top_thumbs.append(info.filename)
                            continue

                if content_types_path is None:
                    logging.error("[Content_Types].xml not found in 3mf file")
                    return None

                content_types_content = zf.read(content_types_path).decode('utf-8')
                if '<Default Extension="gcode" ContentType="text/x.gcode"/>' not in content_types_content:
                    logging.error("Invalid content type in 3mf file")
                    return None

                # project_settings.config is a json file, check if it contain field 'print_compatible_printers'
                # and 'print_compatible_printers' is a list, and if there is one element in print_compatible_printers contains string 'Snapmaker U1'
                if project_settings_path is None:
                    logging.error("project_settings.config not found in 3mf file")
                    return None
                project_settings = jsonw.loads(zf.read(project_settings_path).decode('utf-8'))
                print_compatible_u1 = False
                if 'print_compatible_printers' in project_settings \
                        and isinstance(project_settings['print_compatible_printers'], list) \
                        and len(project_settings['print_compatible_printers']) > 0:

                        for printer in project_settings['print_compatible_printers']:
                            logging.info(f"compatible printer: {printer}")
                            if 'Snapmaker U1' in printer:
                                print_compatible_u1 = True

                if not print_compatible_u1:
                    logging.error("Invalid project settings in 3mf file")
                    return None

                if len(plates) == 0:
                    logging.error("No plate gcode files found in 3mf file")
                    return None
                if len(plates_md5) == 0:
                    logging.error("No plate md5 files found in 3mf file")
                    return None
                # Extract all gcode files
                for gcode in plates:
                    zf.extract(gcode, tmpdir_path)
                for md5_file in plates_md5:
                    zf.extract(md5_file, tmpdir_path)
                for thumb in top_thumbs:
                    zf.extract(thumb, tmpdir_path)

                gcode_paths = []
                stem = name_3mf.stem

                # Check if all gcode files exist
                for i in range(1, 36):
                    gcode_pattern = f"plate_{i}.gcode"
                    md5_pattern = f"plate_{i}.gcode.md5"
                    png_pattern = f"top_{i}.png"

                    gcode_match = None
                    md5_match = None
                    png_match = None
                    # Check if gcode file exists
                    for item in plates:
                        if decode_filename(item).lower().endswith(gcode_pattern.lower()):
                            gcode_match = item
                            break
                    # Check if md5 file exists
                    for item in plates_md5:
                        if decode_filename(item).lower().endswith(md5_pattern.lower()):
                            md5_match = item
                            break
                    # Check if png file exists
                    for item in top_thumbs:
                        if decode_filename(item).lower().endswith(png_pattern.lower()):
                            png_match = item
                            break
                    # If gcode file does not exist, skip
                    if gcode_match is None:
                        continue
                    # If md5 file does not exist, report error
                    if md5_match is None:
                        logging.error(f"MD5 file not found for {gcode_pattern}")
                        self._cleanup_3mf_temp_files(tmpdir_path, plates, plates_md5, top_thumbs)
                        return None

                    local_gcode_path = tmpdir_path / decode_filename(gcode_match)
                    local_md5_path = tmpdir_path / decode_filename(md5_match)
                    # Calculate md5
                    calc_md5 = hashlib.md5()
                    with open(local_gcode_path, 'rb') as f:
                        for chunk in iter(lambda: f.read(8192), b''):
                            calc_md5.update(chunk)
                    calc_md5_str = calc_md5.hexdigest().upper()
                    # Read md5
                    with open(local_md5_path, 'r') as f:
                        expected_md5 = f.read().strip().upper()

                    if calc_md5_str != expected_md5:
                        logging.error(f"MD5 mismatch for {gcode_pattern}: calculated {calc_md5_str}, expected {expected_md5}")
                        self._cleanup_3mf_temp_files(tmpdir_path, plates, plates_md5, top_thumbs)
                        return None

                    new_gcode_name = f"{stem}_plate_{i}.gcode"
                    new_gcode_path = gcode_path / new_gcode_name

                    try:
                        self.fm._handle_operation_check(str(new_gcode_path))
                    except Exception as e:
                        logging.error(f"File operation check failed for {new_gcode_path}: {e}")
                        self._cleanup_3mf_temp_files(tmpdir_path, plates, plates_md5, top_thumbs)
                        return None

                    shutil.move(str(local_gcode_path), str(new_gcode_path))
                    gcode_paths.append(new_gcode_path.name)

                    if png_match is not None:
                        local_png_path = tmpdir_path / decode_filename(png_match)
                        new_png_name = f"{stem}_plate_{i}_top.png"
                        new_png_path = gcode_path / new_png_name
                        shutil.move(str(local_png_path), str(new_png_path))

                final_3mf_path = gcode_path / name_3mf.name
                shutil.move(str(tmp_path), str(final_3mf_path))

                self._cleanup_3mf_temp_files(tmpdir_path, plates, plates_md5, top_thumbs)

                for gcode in gcode_paths:
                    if gcode.endswith(f"plate_{print_plate}.gcode"):
                        logging.info(f"Selected gcode file: {gcode}")
                        return gcode
                logging.warning(f"No gcode file found for plate {print_plate}")

                if len(gcode_paths) == 1:
                    return gcode_paths[0]
                else:
                    logging.error("Multiple gcode files found and plate number is not specified")
                    return None

        except Exception as e:
            logging.error(f"Failed to handle 3mf file: {e}")
            return None
    async def _download_sm_file(self, url: str, start: bool, checksum: Optional[str],
                                filetype: str, print_plate:int = 1, commands: Optional[List[str]] = None
                                ) -> None:
        state = "ready"
        message = ""
        tmp_path = ""  # initialize tmp_path to avoid UnboundLocalError
        client: HttpxClient = self.server.lookup_component("httpx_client")
        fm: FileManager = self.server.lookup_component("file_manager")
        gc_path = pathlib.Path(fm.get_directory())
        url_path = urlparse(url)
        dist_file = pathlib.Path(unquote(url_path.path))
        filename = pathlib.PurePath(dist_file.name)
        accept = "text/plain,applicaton/octet-stream"
        auth_url = await self._authorize_download_url(url)
        if len(auth_url) < 10:
            state = "error"
            message = f"invalid URL: {auth_url}"
            self._notify_download_state(state, message)
            await self._reset_download_state()
            return
        else:
            self._on_download_progress(0, 0, 0)
        try:
            logging.debug(f"Downloading URL: {filename}")
            tmp_path = await client.download_file(
                auth_url, accept,
                progress_callback=self._on_download_progress,
                connect_timeout=10.,
                request_timeout=30.,
                attempts=3,
                destination_path=self.fm.gen_temp_upload_path()
            )
        except asyncio.TimeoutError:
            state = "timeout"
            message = "Timeout to download file"
            logging.error(f"timeout to download file: {filename}")
        except Exception as e:
            state = "error"
            message = f"Failed to download file"
            logging.error(f"Failed to download file: {filename}, error: {e}")
        finally:
            self.download_time = 0.0
            if state != "ready":
                self._notify_download_state(state, message)
                await self._reset_download_state()
                # remove the tmp file after download error
                if os.path.exists(tmp_path):
                    os.remove(tmp_path)
                return
        logging.debug("SnapmakerCloud: Download Complete")
        calc = await self._get_file_hash(tmp_path)
        logging.info("calc file checksum: {}".format(calc))
        if checksum is not None:
            if calc is None or calc != checksum:
                logging.error("calc checksum[{}] != recv checksum[{}], remove downfile!".format(calc, checksum))
                self._notify_download_state("check_error", "checksum error")
                await self._reset_download_state()
                if os.path.exists(tmp_path):
                    os.remove(tmp_path)  # remove the tmp file after checksum error
                return
            else:
                logging.info("checksum pass")
        else:
            logging.warning(f"client didn't provide checksum")

        # handle zipped gcode
        if filetype == "zip":
            self._notify_download_state("extracting", "extracting file")
            async with self.state_lock:
                self.download_state = "extracting"
            fpath = await self._extract_gcode_from_zip(tmp_path, gc_path)
            if fpath is None:
                self._notify_download_state("extract_error", "No valid gcode found in zip")
                await self._reset_download_state()
                if os.path.exists(tmp_path):
                    os.remove(tmp_path)  # remove the tmp file after extracting gcode
                return
            args: Dict[str, Any] = {
                "filename": fpath.name,
                "tmp_file_path": str(fpath),
            }
        elif filetype == "3mf":
            self._notify_download_state("extracting", "extracting file")
            gcode = await self._handle_3mf(tmp_path, gc_path, filename, print_plate)
            if gcode is None:
                self._notify_download_state("extract_error", "No valid gcode found in 3mf")
                await self._reset_download_state()
                if os.path.exists(tmp_path):
                    os.remove(tmp_path)
                return
            # to compatible with finalize_upload(), set tmp_file_path to gcode path
            args: Dict[str, Any] = {
                "filename": gcode,
                "tmp_file_path": str(gc_path / gcode)
            }
        else:
            fpath = gc_path.joinpath(filename.name)
            logging.info("target file: {}".format(fpath))
            try:
                # check file path is in used or not
                self.fm._handle_operation_check(str(fpath))
            except Exception as e:
                logging.error(f"File Operation Check Failed: {e}")
                self._notify_download_state("file_in_use", "{}".format(e))
                await self._reset_download_state()
                if os.path.exists(tmp_path):
                    os.remove(tmp_path)  # remove the tmp file after handling gcode
                return
            args: Dict[str, Any] = {
                "filename": fpath.name,
                "tmp_file_path": str(tmp_path),
            }

        if start and commands:
            kapi: KlippyAPI = self.server.lookup_component("klippy_apis")
            logging.info(f"Running commands: {commands}")
            for c in commands:
                await kapi.run_gcode(c)

        try:
            if start:
                if not await self.check_can_print():
                    logging.error("printer not ready to start print but user request start")
                    args["print"] = "false"
                else:
                    args["print"] = "true"
            else:
                args["print"] = "false"
            ret = await fm.finalize_upload(args)
            state = "ready"
            message = ""
        except self.server.error as e:
            state = "save_error"
            message = f"GCode: Finalization Failed: {e}"
            logging.error(f"GCode: Finalization Failed: {e}")
        except Exception as e:
            logging.error(f"raise from finalize_upload: {e}")
            state = "error"
            message = f"GCode: Finalization Failed: {e}"
        finally:
            self._notify_download_state(state, message)
            await self._reset_download_state()
            logging.info(f"final state: {state}, message: {message}")
            if os.path.exists(tmp_path):
                os.remove(tmp_path)  # remove the tmp file after handling gcode
        return

    async def _extract_gcode_from_zip(self, zip_path: pathlib.Path, gc_path: pathlib.Path) -> Optional[pathlib.Path]:
        """
        Extract the first .gcode file from a zip archive to the gc_path directory.
        Supports multilingual filenames by trying multiple encodings (utf-8, gbk, cp932, cp949, cp950, cp437, etc).
        Returns the path to the extracted gcode file, or None if not found.
        """
        import zipfile
        gcode_file_path = None
        # Support encodings for French, German, Korean, North Korean, Japanese, Chinese, etc.
        encodings = [
            'utf-8',           # Universal
            'gbk',             # Simplified Chinese
            'big5',            # Traditional Chinese
            'cp850',           # DOS Western Europe
            'cp437',           # Default for zip
            'cp932',           # Japanese
            'shift_jis',       # Japanese
            'euc_jp',          # Japanese
            'cp949',           # Korean
            'euc_kr',          # Korean
            'iso2022_jp',      # Japanese
            'iso2022_kr',      # Korean (North Korea, South Korea)
            'mac_roman',       # Mac Western Europe
        ]
        try:
            with zipfile.ZipFile(zip_path, 'r') as zf:
                for info in zf.infolist():
                    if info.is_dir():
                        continue
                    raw_filename = info.filename
                    filename = raw_filename
                    # Try multiple encodings for multilingual filename compatibility
                    for enc in encodings:
                        try:
                            filename = raw_filename.encode('cp437').decode(enc)
                            logging.info(f"Decoded filename: {filename} (encoding: {enc})")
                            break
                        except Exception:
                            continue
                    else:
                        # All decoding attempts failed, use the original
                        logging.warning(f"Failed to decode filename: {raw_filename}, using original")
                        filename = raw_filename
                    if filename.lower().endswith('.gcode'):
                        # Check for duplicate file name, add suffix if needed (same as _download_sm_file)
                        base = pathlib.Path(filename).stem
                        suffix = pathlib.Path(filename).suffix
                        target_path = gc_path.joinpath(f"{base}{suffix}")
                        logging.info(f"Extracting gcode to: {target_path}")
                        try:
                            # check file path is in used or not
                            self.fm._handle_operation_check(str(target_path))
                        except self.server.error as e:
                            logging.error(f"{e}: file {target_path} already exists")
                            return None
                        with zf.open(info) as src, open(target_path, 'wb') as dst:
                            shutil.copyfileobj(src, dst)
                        gcode_file_path = target_path
                        break
        except Exception as e:
            logging.error(f"Failed to extract gcode from zip: {e}")
            return None
        return gcode_file_path

    async def start_print(self) -> None:
        if not self.pending_file:
            return
        pending = self.pending_file
        self.pending_file = ""
        kapi: KlippyAPI = self.server.lookup_component("klippy_apis")
        data = {"state": "started"}
        try:
            await kapi.start_print(pending, user=self.sp_user)
        except Exception:
            logging.exception("Print Failed to start")
            data["state"] = "error"
            data["message"] = "Failed to start print"
            data["path"] = pending
        else:
            self.last_started = pending
        self.server.send_event("smcloud:file_pull_progress", data)

    async def check_can_print(self) -> bool:
        kconn: KlippyConnection = self.server.lookup_component("klippy_connection")
        if kconn.state != KlippyState.READY:
            return False
        kapi: KlippyAPI = self.server.lookup_component("klippy_apis")
        try:
            result = await kapi.query_objects({"print_stats": None})
        except Exception:
            # Klippy not connected
            logging.error("Failed to query print_stats from klippy")
            return False
        if 'print_stats' not in result:
            logging.error("No print_stats in klippy response")
            return False
        state: str = result['print_stats']['state']
        if state in ["printing", "paused"]:
            logging.warning("Printer is busy, cannot start new print")
            return False
        return True

    def _on_download_progress(self, percent: int, size: int, recd: int) -> None:
        if percent == self.download_progress:
            return

        if time.time() - self.download_time < self.SHOW_PROGRESS_INTERVAL:
            if percent not in self.SHOW_PROGRESS:
                return
        self.download_time = time.time()
        self.download_progress = percent
        pull_progress = {"state": "downloading", "percent": percent, \
                        "auto_start": self.auto_start, "path": self.download_file_name}
        logging.debug("progress: {}".format(pull_progress))
        self.server.send_event("smcloud:file_pull_progress", pull_progress)

    async def _get_file_hash(self, filename: Optional[pathlib.Path]) -> Optional[str]:
        if filename is None or not filename.is_file():
            return None

        def hash_func(f: pathlib.Path) -> str:
            sha256 = hashlib.sha256()
            # use 10MB for chunk size, cannot read whole file at once
            # it will lead to memory issues
            chunk_size = 10 * 1024 * 1024
            with open(f, 'rb') as file:
                while chunk := file.read(chunk_size):
                    sha256.update(chunk)
            digest = sha256.digest()
            return base64.b64encode(digest).decode("utf-8")

        try:
            event_loop = self.server.get_event_loop()
            return await event_loop.run_in_thread(hash_func, filename)
        except Exception:
            return None

    async def handle_cloud_print(self, url, commands, checksum, file_type) -> Dict[str, Any]:
        job_state: JobState = self.server.lookup_component("job_state")
        last_stats = job_state.get_last_stats()
        state: str = last_stats.get('state', "")
        kconn: KlippyConnection
        kconn = self.server.lookup_component("klippy_connection")
        if not kconn.is_ready() or state in ["printing", "paused"]:
            return {"state": "error", "message": "Klippy not ready or print in progress"}
        if self.download_task is not None and not self.download_task.done():
            return {"state": "error", "message": "A file is already being downloaded"}
        coro = self._download_sm_file(url, True, checksum, file_type, commands=commands)
        self.download_task = self.eventloop.create_task(coro)
        return {"state": "success"}

def load_component(config: ConfigHelper) -> SnapmakerCloud:
    return SnapmakerCloud(config)
