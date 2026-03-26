# Helper for Moonraker to Klippy API calls.
#
# Copyright (C) 2020 Eric Callahan <arksine.code@gmail.com>
#
# This file may be distributed under the terms of the GNU GPLv3 license.

from __future__ import annotations
import logging
from ..utils import Sentinel
from ..common import WebRequest, APITransport, RequestType, TransportType, KlippyState

# Annotation imports
from typing import (
    TYPE_CHECKING,
    Any,
    Union,
    Optional,
    Dict,
    List,
    TypeVar,
    Mapping,
    Callable,
    Coroutine
)
if TYPE_CHECKING:
    from ..confighelper import ConfigHelper
    from ..common import UserInfo
    from .klippy_connection import KlippyConnection as Klippy
    Subscription = Dict[str, Optional[List[Any]]]
    SubCallback = Callable[[Dict[str, Dict[str, Any]], float], Optional[Coroutine]]
    _T = TypeVar("_T")

INFO_ENDPOINT = "info"
ESTOP_ENDPOINT = "emergency_stop"
LIST_EPS_ENDPOINT = "list_endpoints"
GC_OUTPUT_ENDPOINT = "gcode/subscribe_output"
GCODE_ENDPOINT = "gcode/script"
SUBSCRIPTION_ENDPOINT = "objects/subscribe"
STATUS_ENDPOINT = "objects/query"
OBJ_LIST_ENDPOINT = "objects/list"
REG_METHOD_ENDPOINT = "register_remote_method"

class KlippyAPI(APITransport):
    def __init__(self, config: ConfigHelper) -> None:
        self.server = config.get_server()
        self.klippy: Klippy = self.server.lookup_component("klippy_connection")
        self.eventloop = self.server.get_event_loop()
        app_args = self.server.get_app_args()
        self.version = app_args.get('software_version')
        # Maintain a subscription for all moonraker requests, as
        # we do not want to overwrite them
        self.host_subscription: Subscription = {}
        self.subscription_callbacks: List[SubCallback] = []

        # Register GCode Aliases
        self.server.register_endpoint(
            "/printer/print/pause", RequestType.POST, self._gcode_pause
        )
        self.server.register_endpoint(
            "/printer/print/resume", RequestType.POST, self._gcode_resume
        )
        self.server.register_endpoint(
            "/printer/print/cancel", RequestType.POST, self._gcode_cancel
        )
        self.server.register_endpoint(
            "/printer/print/start", RequestType.POST, self._gcode_start_print
        )
        self.server.register_endpoint(
            "/printer/restart", RequestType.POST, self._gcode_restart
        )
        self.server.register_endpoint(
            "/printer/firmware_restart", RequestType.POST, self._gcode_firmware_restart
        )
        self.server.register_endpoint(
            "/printer/emergency_stop",
            RequestType.POST,
            self._emergency_stop,
            transports=(TransportType.all() & ~TransportType.HTTP)
        )
        self.server.register_endpoint(
            "/printer/control/led",
            RequestType.POST,
            self._control_led,
            transports=(TransportType.all() & ~TransportType.HTTP)
        )
        self.server.register_endpoint(
            "/printer/control/main_fan",
            RequestType.POST,
            self._control_main_fan,
            transports=(TransportType.all() & ~TransportType.HTTP)
        )
        self.server.register_endpoint(
            "/printer/control/generic_fan",
            RequestType.POST,
            self._control_generic_fan,
            transports=(TransportType.all() & ~TransportType.HTTP)
        )
        self.server.register_endpoint(
            "/printer/control/extruder_temp",
            RequestType.POST,
            self._control_extruder_temp,
            transports=(TransportType.all() & ~TransportType.HTTP)
        )
        self.server.register_endpoint(
            "/printer/control/bed_temp",
            RequestType.POST,
            self._control_bed_temp,
            transports=(TransportType.all() & ~TransportType.HTTP)
        )
        self.server.register_endpoint(
            "/printer/control/print_speed",
            RequestType.POST,
            self._control_print_speed,
            transports=(TransportType.all() & ~TransportType.HTTP)
        )
        self.server.register_event_handler(
            "server:klippy_disconnect", self._on_klippy_disconnect
        )

    def _on_klippy_disconnect(self) -> None:
        self.host_subscription.clear()
        self.subscription_callbacks.clear()

    async def _gcode_pause(self, web_request: WebRequest) -> str:
        return await self.pause_print()

    async def _gcode_resume(self, web_request: WebRequest) -> str:
        return await self.resume_print()

    async def _gcode_cancel(self, web_request: WebRequest) -> str:
        return await self.cancel_print()

    async def _gcode_start_print(self, web_request: WebRequest) -> str:
        if not await self._check_can_print():
            return {
                "state": "error",
                "message": "Printer is not ready to start a new print job."
            }
        filename: str = web_request.get_str('filename')
        user = web_request.get_current_user()
        transport = web_request.get_client_connection()
        ip = transport.ip_addr if transport else None
        logging.info(f"Starting print from: {ip if ip else web_request.get_ip_address()}")
        return await self.start_print(filename, user=user)

    async def _gcode_restart(self, web_request: WebRequest) -> str:
        return await self.do_restart("RESTART")

    async def _gcode_firmware_restart(self, web_request: WebRequest) -> str:
        return await self.do_restart("FIRMWARE_RESTART")

    async def _check_can_print(self) -> bool:
        if self.klippy.state != KlippyState.READY:
            return False
        try:
            result = await self.query_objects({"print_stats": None})
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

    async def _send_klippy_request(
        self,
        method: str,
        params: Dict[str, Any],
        default: Any = Sentinel.MISSING,
        transport: Optional[APITransport] = None
    ) -> Any:
        try:
            req = WebRequest(method, params, transport=transport or self)
            result = await self.klippy.request(req)
        except self.server.error:
            if default is Sentinel.MISSING:
                raise
            result = default
        return result

    async def _emergency_stop(self,
                            web_request: WebRequest
                        ) -> Dict[str, Any]:
        sc = web_request.get_client_connection()
        params = web_request.get_args()
        logging.info(f"Emergency Stop, conn uid: {sc.uid}, type: {sc.transport_type}, params: {params}")
        return await self._send_klippy_request(
            "emergency_stop", params, Sentinel.MISSING)
    async def _control_led(self,
                        web_request: WebRequest
                        ) -> Dict[str, Any]:
        name: str = web_request.get_str('name')
        red: int = web_request.get_int('red', 0)
        green: int = web_request.get_int('green', 0)
        blue: int = web_request.get_int('blue', 0)
        white: int = web_request.get_int('white', 0)
        transmit: int = web_request.get_int('transmit', 1)
        index: int = web_request.get_int('transmit', None)
        sync: int = web_request.get_int('sync', 1)
        params = {
            'led': name,
            'red': red,
            'green': green,
            'blue': blue,
            'white': white,
            'transmit': transmit,
            'SYNC': sync
        }
        if index != None:
            params['index'] = index
        return await self._send_klippy_request(
            "control/led", params, Sentinel.MISSING)

    async def _control_main_fan(self,
                        web_request: WebRequest
                        ) -> Dict[str, Any]:
        speed: int = web_request.get_int('speed', 0)
        params = {
            'S': speed,
        }
        return await self._send_klippy_request(
            "control/main_fan", params, Sentinel.MISSING)

    async def _control_generic_fan(self,
                        web_request: WebRequest
                        ) -> Dict[str, Any]:
        name: str = web_request.get_str('name')
        speed: int = web_request.get_int('speed', 0)
        params = {
            'fan': name,
            'S': speed
        }
        return await self._send_klippy_request(
            "control/generic_fan", params, Sentinel.MISSING)

    async def _control_extruder_temp(self,
                        web_request: WebRequest
                        ) -> Dict[str, Any]:
        temp: int = web_request.get_int('temp', 0)
        index: int = web_request.get_int('index', None)
        map_num: int = web_request.get_int('map', 1)
        params = {
            'S': temp,
            'A': map_num
        }
        if index != None:
            params['T'] = index
        return await self._send_klippy_request(
            "control/extruder_temp", params, Sentinel.MISSING)

    async def _control_bed_temp(self,
                        web_request: WebRequest
                        ) -> Dict[str, Any]:
        temp: int = web_request.get_int('temp', 0)
        params = {
            'S': temp
        }
        return await self._send_klippy_request(
            "control/bed_temp", params, Sentinel.MISSING)

    async def _control_print_speed(self,
                        web_request: WebRequest
                        ) -> Dict[str, Any]:
        percentage: int = web_request.get_int('percentage', 0)
        params = {
            'S': percentage
        }
        return await self._send_klippy_request(
            "control/print_speed", params, Sentinel.MISSING)

    async def run_gcode(self,
                        script: str,
                        default: Any = Sentinel.MISSING
                        ) -> str:
        params = {'script': script}
        result = await self._send_klippy_request(
            GCODE_ENDPOINT, params, default)
        return result

    async def start_print(
        self,
        filename: str,
        wait_klippy_started: bool = False,
        user: Optional[UserInfo] = None
    ) -> str:
        # WARNING: Do not call this method from within the following
        # event handlers when "wait_klippy_started" is set to True:
        # klippy_identified, klippy_started, klippy_ready, klippy_disconnect
        # Doing so will result in "wait_started" blocking for the specifed
        # timeout (default 20s) and returning False.
        # XXX - validate that file is on disk
        if filename[0] == '/':
            filename = filename[1:]
        # Escape existing double quotes in the file name
        filename = filename.replace("\"", "\\\"")
        script = f'SDCARD_PRINT_FILE FILENAME="{filename}"'
        if wait_klippy_started:
            await self.klippy.wait_started()
        logging.info(f"Requesting Job Start, filename = {filename}")
        ret = await self.run_gcode(script)
        self.server.send_event("klippy_apis:job_start_complete", user)
        return ret

    async def pause_print(
        self, default: Union[Sentinel, _T] = Sentinel.MISSING
    ) -> Union[_T, str]:
        self.server.send_event("klippy_apis:pause_requested")
        logging.info("Requesting job pause...")
        return await self._send_klippy_request(
            "pause_resume/pause", {}, default)

    async def resume_print(
        self, default: Union[Sentinel, _T] = Sentinel.MISSING
    ) -> Union[_T, str]:
        self.server.send_event("klippy_apis:resume_requested")
        logging.info("Requesting job resume...")
        return await self._send_klippy_request(
            "pause_resume/resume", {}, default)

    async def cancel_print(
        self, default: Union[Sentinel, _T] = Sentinel.MISSING
    ) -> Union[_T, str]:
        self.server.send_event("klippy_apis:cancel_requested")
        logging.info("Requesting job cancel...")
        return await self._send_klippy_request(
            "pause_resume/cancel", {}, default)

    async def do_restart(
        self, gc: str, wait_klippy_started: bool = False
    ) -> str:
        # WARNING: Do not call this method from within the following
        # event handlers when "wait_klippy_started" is set to True:
        # klippy_identified, klippy_started, klippy_ready, klippy_disconnect
        # Doing so will result in "wait_started" blocking for the specifed
        # timeout (default 20s) and returning False.
        if wait_klippy_started:
            await self.klippy.wait_started()
        try:
            result = await self.run_gcode(gc)
        except self.server.error as e:
            if str(e) == "Klippy Disconnected":
                result = "ok"
            else:
                raise
        return result

    async def list_endpoints(self,
                             default: Union[Sentinel, _T] = Sentinel.MISSING
                             ) -> Union[_T, Dict[str, List[str]]]:
        return await self._send_klippy_request(
            LIST_EPS_ENDPOINT, {}, default)

    async def emergency_stop(self) -> str:
        return await self._send_klippy_request(ESTOP_ENDPOINT, {})

    async def get_klippy_info(self,
                              send_id: bool = False,
                              default: Union[Sentinel, _T] = Sentinel.MISSING
                              ) -> Union[_T, Dict[str, Any]]:
        params = {}
        if send_id:
            ver = self.version
            params = {'client_info': {'program': "Moonraker", 'version': ver}}
        return await self._send_klippy_request(INFO_ENDPOINT, params, default)

    async def get_object_list(self,
                              default: Union[Sentinel, _T] = Sentinel.MISSING
                              ) -> Union[_T, List[str]]:
        result = await self._send_klippy_request(
            OBJ_LIST_ENDPOINT, {}, default)
        if isinstance(result, dict) and 'objects' in result:
            return result['objects']
        if default is not Sentinel.MISSING:
            return default
        raise self.server.error("Invalid response received from Klippy", 500)

    async def query_objects(self,
                            objects: Mapping[str, Optional[List[str]]],
                            default: Union[Sentinel, _T] = Sentinel.MISSING
                            ) -> Union[_T, Dict[str, Any]]:
        params = {'objects': objects}
        result = await self._send_klippy_request(
            STATUS_ENDPOINT, params, default)
        if isinstance(result, dict) and "status" in result:
            return result["status"]
        if default is not Sentinel.MISSING:
            return default
        raise self.server.error("Invalid response received from Klippy", 500)

    async def subscribe_objects(
        self,
        objects: Mapping[str, Optional[List[str]]],
        callback: Optional[SubCallback] = None,
        default: Union[Sentinel, _T] = Sentinel.MISSING
    ) -> Union[_T, Dict[str, Any]]:
        # The host transport shares subscriptions amongst all components
        for obj, items in objects.items():
            if obj in self.host_subscription:
                prev = self.host_subscription[obj]
                if items is None or prev is None:
                    self.host_subscription[obj] = None
                else:
                    uitems = list(set(prev) | set(items))
                    self.host_subscription[obj] = uitems
            else:
                self.host_subscription[obj] = items
        params = {"objects": dict(self.host_subscription)}
        result = await self._send_klippy_request(SUBSCRIPTION_ENDPOINT, params, default)
        if isinstance(result, dict) and "status" in result:
            if callback is not None:
                self.subscription_callbacks.append(callback)
            return result["status"]
        if default is not Sentinel.MISSING:
            return default
        raise self.server.error("Invalid response received from Klippy", 500)

    async def subscribe_from_transport(
        self,
        objects: Mapping[str, Optional[List[str]]],
        transport: APITransport,
        default: Union[Sentinel, _T] = Sentinel.MISSING,
    ) -> Union[_T, Dict[str, Any]]:
        params = {"objects": dict(objects)}
        result = await self._send_klippy_request(
            SUBSCRIPTION_ENDPOINT, params, default, transport
        )
        if isinstance(result, dict) and "status" in result:
            return result["status"]
        if default is not Sentinel.MISSING:
            return default
        raise self.server.error("Invalid response received from Klippy", 500)

    async def subscribe_gcode_output(self) -> str:
        template = {'response_template':
                    {'method': "process_gcode_response"}}
        return await self._send_klippy_request(GC_OUTPUT_ENDPOINT, template)

    async def register_method(self, method_name: str) -> str:
        return await self._send_klippy_request(
            REG_METHOD_ENDPOINT,
            {'response_template': {"method": method_name},
             'remote_method': method_name})

    def send_status(
        self, status: Dict[str, Any], eventtime: float
    ) -> None:
        for cb in self.subscription_callbacks:
            self.eventloop.register_callback(cb, status, eventtime)
        self.server.send_event("server:status_update", status)

def load_component(config: ConfigHelper) -> KlippyAPI:
    return KlippyAPI(config)
