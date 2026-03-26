# Snapmaker Client Manager
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
import subprocess
from queue import SimpleQueue
from ..loghelper import LocalQueueHandler
from ..common import RequestType, JobEvent, KlippyState, UserInfo, WebRequest, TransportType
from ..utils import json_wrapper as jsonw
from urllib.parse import urlparse
from urllib.parse import unquote
import glob, json

from typing import (
    TYPE_CHECKING,
    Awaitable,
    Optional,
    Dict,
    List,
    Union,
    Any,
    Callable,
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
    from .history import History
    from .mqtt import MQTTClient, SubscriptionHandle
class ClientManager:
    LINK_MODE_CLOUD = 0
    LINK_MODE_LAN = 1

    DEFAULT_CODE = 12345678

    SYNC_USER_RECORDS_INTERVAL = 30  # seconds

    PIN_CODE_EXPIRE = 180
    PIN_CODE_INVALID_AFTER_REQ = 5

    CA_BEGIN_DATE = "20251104"
    CA_AVAILABLE_DAYS = "5475"  # 15 years

    def __init__(self, config: ConfigHelper) -> None:
        self.server = config.get_server()
        self.mqtt: MQTTClient = None
        self.machine: Machine = None
        self.mqtt_lan_access_hdl: SubscriptionHandle = None
        self.pin_code: int = self.DEFAULT_CODE
        self.pin_code_expire: float = time.time()
        self.agent_state: str = "disconnected"
        self.userinfo_lock = asyncio.Lock()
        self.userid: str = ""
        self.username: str = ""
        self.last_id: int = 0
        self.tls_port: int = 8883
        self.oem_config = dict()
        self.doing_factory_reset:bool = False
        self.disconnecting_lan_clients:bool = False
        self.ca_tool = "/home/lava/bin/ca_tool.py"

        app_args = self.server.get_app_args()
        self.datapath: pathlib.Path = pathlib.Path(app_args["data_path"])
        self.client_cfg_path = self.datapath.joinpath("mqtt").joinpath("client.json")
        self.mqtt_users_conf = self.datapath.joinpath("mqtt").joinpath("users.conf")
        self.certs_dir = self.datapath.joinpath("certs")
        self.acl_tls = self.datapath.joinpath("mqtt").joinpath("acl_tls.conf")
        self.mosquitto_db = self.datapath.joinpath("mqtt").joinpath("mosquitto.db")
        self._load_oem_config()

        self._refresh_user_records_timestamp = 0

        self.history: History = None

        # show oem configuration
        logging.info(f"oem config: {self.oem_config}")
        # show file size of mosquitto.db
        if os.path.exists(self.mosquitto_db):
            db_size = os.path.getsize(self.mosquitto_db)
            logging.info(f"mosquitto size: {db_size} bytes")

        self.server.register_notification("snapmaker:client_access")
        self.server.register_notification("snapmaker:user_access")
        self.server.register_notification("snapmaker:update_mdns_info")

        # API to disconnect all LAN client for  screen
        self.server.register_endpoint("/server/client_manager/disconnect_all",
                                        RequestType.POST,
                                        self._handle_disconnection_all,
                                        transports=TransportType.WEBSOCKET)
        # API thtop get access code for screen
        self.server.register_endpoint("/server/client_manager/info",
                                        RequestType.POST,
                                        self._handle_get_info,
                                        transports=TransportType.WEBSOCKET)
        # API to refresh access code for screen
        self.server.register_endpoint("/server/client_manager/refresh_access_code",
                                        RequestType.POST,
                                        self._handle_refresh_access_code,
                                        transports=TransportType.WEBSOCKET)
        # API to refresh pin code for screen
        self.server.register_endpoint("/server/client_manager/refresh_pin_code",
                                        RequestType.POST,
                                        self._handle_refresh_pin_code,
                                        transports=TransportType.WEBSOCKET)
        # API to logout user account
        self.server.register_endpoint("/server/client_manager/logout",
                                        RequestType.POST,
                                        self._handle_logout,
                                        transports=TransportType.WEBSOCKET | TransportType.MQTT)
        # API to logout user account
        self.server.register_endpoint("/server/client_manager/set_link_mode",
                                        RequestType.POST,
                                        self._handle_set_link_mode,
                                        transports=TransportType.WEBSOCKET)
        # API to approve client connection for screen
        self.server.register_endpoint("/server/client_manager/approve",
                                        RequestType.POST,
                                        self._handle_approve_connection,
                                        transports=TransportType.WEBSOCKET | TransportType.MQTT)
        # API to approve client connection for screen
        self.server.register_endpoint("/server/client_manager/get_authentication",
                                        RequestType.POST,
                                        self._handle_get_authentication,
                                        transports=TransportType.MQTT)
        # API to set binded user info for cloud server
        self.server.register_endpoint("/server/client_manager/set_userinfo",
                                        RequestType.POST,
                                        self._handle_set_userinfo,
                                        transports=TransportType.MQTT)
        # API to set binded user info for cloud server
        self.server.register_endpoint("/server/client_manager/set_region",
                                        RequestType.POST,
                                        self._handle_set_region,
                                        transports=TransportType.WEBSOCKET | TransportType.MQTT)

        self.server.register_endpoint("/server/client_manager/request_lan_auth",
                                        RequestType.POST,
                                        self._handle_request_lan_auth,
                                        transports=TransportType.MQTT)
        self.server.register_endpoint("/server/client_manager/confirm_lan_status",
                                        RequestType.POST,
                                        self._handle_confirm_lan_status,
                                        transports=TransportType.MQTT)
        self.server.register_endpoint("/server/client_manager/request_pin_code",
                                        RequestType.POST,
                                        self._handle_request_pin_code,
                                        transports=TransportType.MQTT)
        self.server.register_endpoint("/server/client_manager/sync_agent_config",
                                        RequestType.POST,
                                        self._handle_sync_agent_config,
                                        transports=TransportType.MQTT)

    async def component_init(self) -> None:
        self.mqtt = self.server.lookup_component("mqtt", None)
        if self.mqtt is None:
            logging.info("smclient: MQTT doesn't exist")
            return

        self.machine = self.server.lookup_component("machine", None)
        if self.machine is None:
            logging.info("smclient: Machine doesn't exist")
            return

        self.history = self.server.lookup_component("history", None)
        if self.history is None:
            logging.info("smclient: History doesn't exist")
            return

        # subscribe to access code topic
        self.mqtt_lan_access_hdl = self.mqtt.subscribe_topic(
                                    f"{self.oem_config['access_code']}/config/request",
                                    self._handle_lan_config_request,
                                    qos=1)

        self.mqtt_wan_config_hdl = self.mqtt.subscribe_topic(
                                    "cloud/config/request",
                                    self._handle_wan_config_request,
                                    qos=1)
        if self.oem_config.get('userid', "") != "":
            # to clean userinfo in old version config
            # delete userid and username from oem config
            try:
                del self.oem_config['userid']
                del self.oem_config['username']
                with open(self.client_cfg_path, "w") as f:
                    json.dump(self.oem_config, f, indent='\t')
                logging.info(f"clean old config, now config: {self.oem_config}")
            except Exception as e:
                logging.error(f"Failed to clean old config: {e}")

    def _notify_client_logout(self, message) -> None:
        """Notify MQTT clients about the authentication request."""
        # Notify MQTT clients about the authentication request
        self.mqtt.publish_notification("notify_logout", message)
    def _notify_client_link_mode(self, message) -> None:
        """Notify MQTT clients about the link mode change."""
        # Notify MQTT clients about the link mode change
        self.mqtt.publish_notification("notify_link_mode_update", message)

    async def handle_factory_reset(self):
        self.doing_factory_reset = True
        async with self.userinfo_lock:
            self.oem_config['logging_out_userid'] = self.userid
            self.userid = ""
            self.username = ""
        self.oem_config['link_mode'] = self.LINK_MODE_CLOUD
        self.oem_config['access_code'] = self.DEFAULT_CODE
        self.oem_config['region'] = ""
        if 'clients' in self.oem_config:
            del self.oem_config['clients']
        with open(self.client_cfg_path, "w") as f:
            json.dump(self.oem_config, f, indent='\t')
        logging.info("factory reset: reset oem config")
        # notify Agent to reset
        jrpc_id = random.randint(0, 0x7fffffff)
        retry = 3
        while retry > 0:
            retry -= 1
            try:
                # send new pin to authentication server and request it update to cloud server
                req_msg = {
                    'jsonrpc': "2.0",
                    'method': 'mqtt_agent.factory_reset',
                    'id': jrpc_id
                }
                resp = await self.mqtt.publish_topic_with_response("mqtt_agent/request",
                                                            "mqtt_agent/response",
                                                            req_msg,
                                                            0,
                                                            False,
                                                            0.5)
                if resp is None:
                    logging.info("factory reset: no response from agent")
                    continue

                obj: Dict[str, Any] = jsonw.loads(resp)
                if obj.get('result', None) is None:
                    logging.info("factory reset: invalid response from agent")
                    continue

                if obj.get('id', 0) != jrpc_id:
                    logging.error(f"Invalid jrpc id in response: {obj}")
                    continue

                result = obj['result']
                if not isinstance(result, dict) and result.get('state', 'error') != 'success':
                    logging.error(f"reset agent state: {result}")
                    continue
                logging.info("factory reset: agent reset successfully")
                break
            except Exception as e:
                resp = None
                logging.error(f"reset agent exception: {e}, resp: {resp}")
                continue

    def get_access_code(self) -> int:
        return self.oem_config['access_code']

    def get_link_mode(self) -> int:
        return self.oem_config['link_mode']

    def get_userid(self) -> str:
        return self.userid

    def get_region(self) -> str:
        return self.oem_config['region']

    async def _handle_sync_agent_config(self,
                                    web_request: WebRequest
                                    ) -> Dict[str, Any]:
        """ Handle request to sync configuration.
            When the agent passes an empty userid, it means no user is currently logged in,
            possibly just logged out or has been powered on without login.
            In this case, clear the local login status regardless of the local situation.
            When the agent passes a non-empty userid, it means a user is currently logged in,
            possibly just logged in or restored login status after power on.
            At this time, the local might have been in logged out state since power on,
            then directly update the local state.
            If the local has just logged out a user, logging_out_userid is non-empty,
            indicating a user has just been logged out. If the incoming userid is the same as logging_out_userid,
            it means the user has not been logged out successfully yet, so do not update the local state,
            wait until the user is logged out successfully.
            If the incoming userid is different from logging_out_userid,
            it means the just logged out user has been logged out successfully,
            then update the local state to the new logged in user.
        """
        if self.doing_factory_reset:
            logging.info("we are doing factory reset, won't sync config")
            return {"state": "error"}
        self.agent_state = web_request.get_str("agent_state", "disconnected")
        agent_userid = web_request.get_str("userid", "")
        username = web_request.get_str("username", "")
        region = web_request.get_str("region", "")
        link_mode = web_request.get_str("link_mode", "")
        device_name = web_request.get_str("name", "")
        update_mdns = False
        sync_config = False
        async with self.userinfo_lock:
            logging_out_userid = self.oem_config.get('logging_out_userid', "")
            local_userid = self.userid

        if agent_userid != local_userid:
            update_mdns = True
            logging.info(f"mqtt agent state: {self.agent_state}, userid: {agent_userid}, username: {username}, "
                            f"cur userid: {local_userid}, logging_out_userid: {logging_out_userid}")
        if len(agent_userid) == 0:
            save = False
            async with self.userinfo_lock:
                if self.userid != "" or self.username != "":
                    save = True
                self.userid = ""
                self.username = ""
                if len(logging_out_userid) > 0:
                    save = True
                    update_mdns = False
                    logging_out_userid = ""
                    self.oem_config['logging_out_userid'] = ""
            if save:
                with open(self.client_cfg_path, "w") as f:
                    json.dump(self.oem_config, f, indent='\t')
        # if new userid is provided, check if logged out
        else:
            if logging_out_userid == agent_userid:
                # user is logging out, don't accept login
                # sync logging_out_userid to agent below
                async with self.userinfo_lock:
                    # just make sure local state is logged out
                    self.userid = ""
                    self.username = ""
                logging.debug(f"User {agent_userid} is not logged out, won't accept login")
                update_mdns = False
                sync_config = True
            else:
                # new user
                save = False
                async with self.userinfo_lock:
                    if self.userid != agent_userid or self.username != username:
                        save = True
                    self.userid = agent_userid
                    self.username = username
                    if len(logging_out_userid) > 0:
                        save = True
                        logging.info(f"old userid:{logging_out_userid} has been logged out, \
                            new userid:{agent_userid} is logged in")
                        logging_out_userid = ""
                        self.oem_config['logging_out_userid'] = ""
                if save:
                    with open(self.client_cfg_path, "w") as f:
                        json.dump(self.oem_config, f, indent='\t')
        if update_mdns:
            logging.info(f"notify mdns update, userid: {agent_userid}")
            self.server.send_event("snapmaker:update_mdns_info", {'userid': agent_userid})

        result = {
            "state": "success",
            "device_name": self.machine.get_device_name(),
            "logging_out_userid": self.oem_config['logging_out_userid']
        }

        if device_name != self.machine.get_device_name():
            logging.info(f"device name: local: {self.machine.get_device_name()}, agent: {device_name}")
            sync_config = True

        if region != self.oem_config['region']:
            logging.info(f"region: local: {self.oem_config['region']}, agent: {region}")
            sync_config = True

        link_mode_local = 'wan' if self.oem_config['link_mode'] == self.LINK_MODE_CLOUD else 'lan'
        if link_mode != link_mode_local:
            logging.info(f"link mode: local: {self.oem_config['link_mode']}, agent: {link_mode}")
            sync_config = True

        if sync_config:
            await self._sync_config_to_agent(logging_out_userid)

        return result

    async def _sync_config_to_agent(self, logging_out_userid="") -> None:
        link_mode = self.oem_config['link_mode']
        retry = 3
        jrpc_id = random.randint(0, 0x7fffffff)
        resp = None
        while retry > 0:
            retry -= 1
            params = None
            try:
                # send new pin to authentication server and request it update to cloud server
                req_msg = {
                    'jsonrpc': "2.0",
                    'method': 'mqtt_agent.sync_config',
                    'id': jrpc_id,
                    'params': {
                        'link_mode': 'wan' if link_mode == self.LINK_MODE_CLOUD else 'lan',
                        'region': self.oem_config['region'],
                        'name': self.machine.get_device_name(),
                        'total_print_time': 0
                    }
                }
                job_totals = self.history.get_job_totals()
                req_msg['params']['total_print_time'] = int(job_totals['total_print_time'])
                if len(logging_out_userid) > 0:
                    logging.debug(f"sync config with logging_out_userid: {logging_out_userid}")
                    req_msg['params']['logging_out_userid'] = logging_out_userid
                resp = await self.mqtt.publish_topic_with_response("mqtt_agent/request/moonraker_cfg",
                                                            "mqtt_agent/response/moonraker_cfg",
                                                            req_msg,
                                                            1,
                                                            False,
                                                            2)
                if resp is None:
                    await asyncio.sleep(1)
                    continue

                obj: Dict[str, Any] = jsonw.loads(resp)
                if obj.get('result', None) is None:
                    await asyncio.sleep(1)
                    continue

                if obj.get('id', 0) != jrpc_id:
                    logging.error(f"Invalid jrpc id in response: {obj}")
                    continue

                params = obj['result']
                if not isinstance(params, dict) and params.get('state', 'error') != 'success':
                    # sleep 1s and retry
                    logging.error(f"sync config state: {params}")
                    await asyncio.sleep(1)
                    continue
                break
            except Exception as e:
                resp = None
                logging.error(f"sync config exception: {e}, resp: {resp}")
                continue
    async def _sync_link_mode_to_agent(self) -> None:
        link_mode = self.oem_config['link_mode']
        retry = 3
        jrpc_id = random.randint(0, 0x7fffffff)
        resp = None
        while retry > 0:
            retry -= 1
            params = None
            try:
                # send new pin to authentication server and request it update to cloud server
                req_msg = {
                    'jsonrpc': "2.0",
                    'method': 'mqtt_agent.set_link_mode',
                    'id': jrpc_id,
                    'params': {
                        'link_mode': 'wan' if link_mode == self.LINK_MODE_CLOUD else 'lan',
                    }
                }
                resp = await self.mqtt.publish_topic_with_response("mqtt_agent/request/moonraker_link",
                                                            "mqtt_agent/response/moonraker_link",
                                                            req_msg,
                                                            1,
                                                            False,
                                                            2)
                if resp is None:
                    await asyncio.sleep(1)
                    continue

                obj: Dict[str, Any] = jsonw.loads(resp)
                if obj.get('result', None) is None:
                    await asyncio.sleep(1)
                    continue

                if obj.get('id', 0) != jrpc_id:
                    logging.error(f"Invalid jrpc id in response: {obj}")
                    continue

                params = obj['result']
                if not isinstance(params, dict) and params.get('state', 'error') != 'success':
                    # sleep 1s and retry
                    logging.error(f"sync link mode state: {params}")
                    await asyncio.sleep(1)
                    continue
                break
            except Exception as e:
                resp = None
                logging.error(f"sync link mode exception: {e}, resp: {resp}")
                continue

    async def _sync_region_to_agent(self) -> None:
        region = self.oem_config['region']
        try:
            retry = 3
            jrpc_id = random.randint(0, 0x7fffffff)
            while retry > 0:
                retry -= 1
                params = None
                try:
                    # send new pin to authentication server and request it update to cloud server
                    req_msg = {
                        'jsonrpc': "2.0",
                        'method': 'mqtt_agent.set_region',
                        'id': jrpc_id,
                        'params': {
                            'region': region,
                        }
                    }
                    resp = await self.mqtt.publish_topic_with_response("mqtt_agent/request/moonraker_region",
                                                                "mqtt_agent/response/moonraker_region",
                                                                req_msg,
                                                                1,
                                                                False,
                                                                2)
                    if resp is None:
                        await asyncio.sleep(1)
                        continue

                    obj: Dict[str, Any] = jsonw.loads(resp)
                    if obj.get('result', None) is None:
                        await asyncio.sleep(1)
                        continue

                    if obj.get('id', 0) != jrpc_id:
                        logging.error(f"Invalid jrpc id in response: {obj}")
                        continue

                    params = obj['result']
                    if not isinstance(params, dict) and params.get('state', 'error') != 'success':
                        # sleep 1s and retry
                        logging.error(f"sync region state: {params}")
                        await asyncio.sleep(1)
                        continue
                    break
                except Exception as e:
                    resp = None
                    logging.error(f"sync region exception: {e}")
                    continue
        except Exception as e:
            logging.error(f"sync region exception: {e}")
            return

    def _create_mqtt_account(self, username, password, password_file):
        """Create MQTT account using mosquitto_passwd.

        Args:
            username (str): MQTT username (same as certificate name)
            password (str): Password for the account
            password_file (str): Path to password file

        Returns:
            bool: True if account created successfully
        """
        import subprocess
        try:
            # Create password file if not exists
            if not os.path.exists(password_file):
                open(password_file, 'a').close()
                os.chmod(password_file, 0o600)

            # Create account
            result = subprocess.run(
                ['mosquitto_passwd', '-b', password_file, username, password],
                check=True
            )
            return True
        except subprocess.CalledProcessError as e:
            print(f"Failed to create MQTT account: {e}")
            return False

    async def _generate_certificate(self, username, cert_type="client") -> None:
        # re-generate certificate
        cmd = [
            sys.executable,
            self.ca_tool,
            str(self.certs_dir),
            self.CA_BEGIN_DATE,
            self.CA_AVAILABLE_DAYS,
            cert_type,
            f"mqtt_{username}",
        ]
        proc = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE
        )
        stdout, stderr = await proc.communicate()
        if proc.returncode != 0:
            logging.error(f"Failed to generate certificate: {stderr.decode()}")
            raise self.server.error(
                f"Failed to generate certificate: {stderr.decode()}")

        logging.info(f"Generated certificate for {username}, type: {cert_type}")

    async def _remove_local_mqtt_certs(self) -> None:
        try:
            # remove all crt, then screen should restart /etc/init.d/S49mqtt_broker
            # and restart /etc/init.d/S50mosquitto
            self._certs = glob.glob(f"{self.certs_dir}/mqtt_*")
            for f in self._certs:
                if 'mqtt' in f or 'cli' in f:
                    logging.debug(f"remove {f}")
                    os.remove(f)

            # cleanup the users.conf
            if os.path.exists(self.mqtt_users_conf):
                with open(self.mqtt_users_conf, 'w') as file:
                    pass
            # re-generate certificate
            await self._generate_certificate("ca", "ca")
            await self._generate_certificate("server", "server")
            await self._generate_certificate("cli0")
        except Exception as e:
            logging.error(f"Failed to disconnect all clients: {e}")

    async def _handle_disconnection_all(self,
                                        web_request: WebRequest
                                        ) -> Dict[str, Any]:
        self.disconnecting_lan_clients = True
        # cleanup the oem_config
        if "clients" in self.oem_config:
            self.oem_config.pop("clients")
            with open(self.client_cfg_path, "w", encoding="utf-8") as f:
                json.dump(self.oem_config, f, indent='\t')
        await self._remove_local_mqtt_certs()
        # screen should restart /etc/init.d/S50mosquitto to disconnect original client
        self.disconnecting_lan_clients = False
        # publish notificatioin to clients
        sn_path = os.path.join(self.datapath, ".lava.sn")
        with open(sn_path, "r") as f:
            sn_content = f.read().strip().rstrip().upper()
        device_name = self.machine.get_device_name()
        logging.info("disconnect all clients")
        self.mqtt.publish_notification("notify_disconnecting_lan_clients", {
            "sn": sn_content,
            "device_name": device_name
        })
        return {
            "state": "success"
        }

    async def _handle_get_info(self,
                                web_request: WebRequest
                                ) -> Dict[str, Any]:
        clients = self.oem_config.get("clients", {})
        client_count = len(clients)
        # logging.info(f"get client info, client count: {client_count}")
        return {
            "state": "success",
            "access_code": str(self.oem_config['access_code']),
            "link_mode": self.oem_config['link_mode'],
            "userid": self.userid,
            "username": self.username,
            "region": self.oem_config['region'],
            "client_count": client_count
        }

    async def _handle_refresh_access_code(self,
                                    web_request: WebRequest
                                    ) -> Dict[str, Any]:
        if self.oem_config['link_mode'] == 0:
            return {
                "state": "error",
                "message": "link mode is not lan"
            }
        # for now not allow refresh access code
        return {
            "state": "success",
            "access_code": str(self.oem_config['access_code'])
        }

    async def _refresh_pin_code(self) -> Optional[str]:
        logging.info("request to refresh pin code")
        retry = 6
        params = {}
        while retry > 0:
            retry -= 1
            params = None
            jrpc_id = random.randint(0, 0x7fffffff)
            try:
                # send new pin to authentication server and request it update to cloud server
                req_msg = {
                    'jsonrpc': "2.0",
                    'method': 'mqtt_agent.refresh_auth_code',
                    'id': jrpc_id
                }
                resp = await self.mqtt.publish_topic_with_response("mqtt_agent/request/moonraker_pin_code",
                                                    "mqtt_agent/response/moonraker_pin_code",
                                                    req_msg,
                                                    qos=1,
                                                    timeout=10)
                if resp is None:
                    await asyncio.sleep(2)
                    continue

                obj: Dict[str, Any] = jsonw.loads(resp)
                if obj.get('result') is None:
                    await asyncio.sleep(2)
                    continue

                if obj.get('id', 0) != jrpc_id:
                    logging.error(f"invalid JRPC id, resp id: {obj.get('id')} != req id: {jrpc_id}")
                    await asyncio.sleep(2)
                    continue

                params = obj['result']
                logging.debug("update pin code result: {}".format(params))
                if params.get('state') != 'success' or params.get('pin_code') is None:
                    # sleep 2s and retry
                    await asyncio.sleep(2)
                    continue
                break
            except Exception as e:
                resp = None
                logging.error(f"Failed to request agent to update pin code: {e}")
                continue
        if retry > 0 and params is not None and \
                params.get('pin_code') is not None:
            # update pin code
            self.pin_code = params['pin_code']
            self.pin_code_expire = time.time() + self.PIN_CODE_EXPIRE
            logging.info(f"pin code updated: {self.pin_code}")
            return str(self.pin_code)
        else:
            logging.error("Failed to update pin code")
            return None

    async def _handle_refresh_pin_code(self,
                                    web_request: WebRequest
                                    ) -> Dict[str, Any]:
        if self.oem_config['link_mode'] != self.LINK_MODE_CLOUD:
            logging.error("Device is in LAN mode, cannot refresh pin code")
            return {
                "state": "error",
                "message": "device is in LAN mode"
            }
        retry = 10
        while self.agent_state != "connected" and retry > 0:
            retry -= 1
            logging.info(f"agent not connected to cloud, retry: {retry}")
            await asyncio.sleep(2)
            if self.oem_config['link_mode'] != self.LINK_MODE_CLOUD:
                return {
                    "state": "disconnected",
                    "message": "agent is disconnected"
                }
        pin_code = await self._refresh_pin_code()
        if pin_code is None:
            return {
                "state": "failed",
                "message": "Failed to refresh pin code"
            }
        return {
            "state": "success",
            "pin_code": str(pin_code)
        }

    async def _handle_set_link_mode(self,
                                    web_request: WebRequest
                                    ) -> Dict[str, Any]:
        if self.doing_factory_reset:
            logging.info("we are doing factory reset, won't change link")
            return {"state": "error"}
        try:
            # set link mode
            link_mode = web_request.get_int("link_mode", 0)
            logging.info("set link mode to : {}".format("lan" if link_mode > 0 else "wan"))
            if link_mode != self.oem_config['link_mode']:
                mode = 'lan' if link_mode > 0 else 'wan'
                self.oem_config['link_mode'] = link_mode
                async with self.userinfo_lock:
                    if mode == 'lan':
                        if len(self.userid) != 0:
                            self.oem_config['logging_out_userid'] = self.userid
                            self.username = ""
                            self.userid = ""
                        else:
                            logging.info("no user logged currently")
                        userid = ""
                    else:
                        userid = self.userid
                with open(self.client_cfg_path, "w", encoding="utf-8") as f:
                    json.dump(self.oem_config, f, indent='\t')
                self._notify_client_link_mode({'link_mode': mode})
                # notify MQTT clients the link mode has been changed
                self.server.send_event("snapmaker:update_mdns_info", {'link_mode': mode, 'userid': userid})
                # sync link mode to agent, if change to LAN, agent will logout current user
                await self._sync_link_mode_to_agent()
            return {
                "state": "success",
                "link_mode": self.oem_config['link_mode']
            }
        except Exception as e:
            logging.error(f"Failed to set link mode: {e}")
            return {
                "state": "error",
                "message": str(e)
            }
    async def _logout_user(self, userid, username, block=True) -> bool:
        if len(userid) == 0:
            logging.info("No user is logged in, nothing to logout")
            return True
        logging.info(f"logout user uuid: {userid}")
        self._notify_client_logout({"userid": str(userid)})

        if block:
            timeout = 10
            retry = 3
            qos = 2
        else:
            timeout = 1
            retry = 3
            qos = 1

        req_msg = {
            'jsonrpc': "2.0",
            'method': 'mqtt_agent.logout',
            'id': random.randint(0, 0x7fffffff),
            'params': {
                'userid': str(userid),
                'username': str(username)
            }
        }
        while retry > 0:
            retry -= 1
            # send logout request to authentication server and request it update to cloud server
            try:
                resp = await self.mqtt.publish_topic_with_response("mqtt_agent/request/moonraker_logout",
                                                    "mqtt_agent/response/moonraker_logout",
                                                    req_msg,
                                                    qos=qos,
                                                    timeout=timeout)
                if resp is None:
                    time.sleep(1)
                    continue
                obj: Dict[str, Any] = jsonw.loads(resp)
                if obj.get('result') is None:
                    time.sleep(1)
                    continue
                result = obj['result']
                state = result.get('state')
                if state != 'success':
                    logging.error("logout failed: {}".format(result))
                    time.sleep(2)
                    continue
                break
            except Exception as e:
                logging.error(f"Failed to request agent to logout: {e}")
                resp = None

        if retry <= 0:
            return False
        return True

    async def _handle_logout(self,
                            web_request: WebRequest
                            ) -> Dict[str, Any]:
        async with self.userinfo_lock:
            if len(self.userid) != 0:
                self.oem_config['logging_out_userid'] = self.userid
                userid = self.userid
                username = self.username
                self.userid = ""
                self.username = ""
            else:
                userid = ""
                username = ""
                logging.info("no user logged currently")

        with open(self.client_cfg_path, "w", encoding="utf-8") as f:
            json.dump(self.oem_config, f, indent='\t')

        ret = await self._logout_user(userid, username, True)
        self.server.send_event("snapmaker:update_mdns_info", {'userid': ""})
        if ret is False:
            return {
                "state": "error",
                "userid": userid,
                "username": username,
                "message": "network error, failed to logout user"
            }

        return   {
            "state": "success",
            "userid": userid,
            "username": username
        }

    def _load_oem_config(self) -> None:
        # Set default values, default WAN mode
        defaults = {
            'access_code': self.DEFAULT_CODE,
            'logging_out_userid': "",
            'link_mode': self.LINK_MODE_CLOUD,  # default to cloud mode
            'region': "" # default region is empty, which means device won't connect to cloud server
        }

        if not os.path.exists(self.client_cfg_path):
            logging.info(f"Creating new clients file: {self.client_cfg_path}")
            self.oem_config.update(defaults)
            with open(self.client_cfg_path, "w", encoding="utf-8") as f:
                json.dump(self.oem_config, f, indent='\t')
            # make sure to remove local MQTT users conf
            self._remove_local_mqtt_certs()
            return

        retry = 3
        while retry >= 0:
            retry -= 1
            try:
                with open(self.client_cfg_path, "r") as f:
                    self.oem_config = json.load(f)
                    logging.info(f"Loaded clients config")
                    break
            except json.JSONDecodeError as e:
                logging.error(f"Invalid JSON in {self.client_cfg_path}, err: {e}")
        if retry < 0:
            logging.info(f"Creating new clients file due to JSON error: {self.client_cfg_path}, resetting to defaults")
            self.oem_config.update(defaults)
            with open(self.client_cfg_path, "w", encoding="utf-8") as f:
                json.dump(self.oem_config, f, indent='\t')
            # make sure to remove local MQTT users conf
            self._remove_local_mqtt_certs()
            return

        # Update missing keys with defaults
        logging.info(f"check clients file: {self.client_cfg_path}")
        need_write = False
        for key, default_val in defaults.items():
            if key not in self.oem_config:
                logging.info(f"Missing key {key}, setting default value: {default_val}")
                self.oem_config[key] = default_val
                need_write = True

        if self.oem_config['access_code'] != self.DEFAULT_CODE:
            self.oem_config['access_code'] = self.DEFAULT_CODE
            need_write = True

        if need_write:
            logging.info(f"Updating clients file: {self.client_cfg_path}")
            with open(self.client_cfg_path, "w", encoding="utf-8") as f:
                json.dump(self.oem_config, f, indent='\t')

    async def _ack_authentication_request(self, result: Dict[str, Any], link_mode=LINK_MODE_LAN) -> None:
        payload = {
            "jsonrpc": "2.0",
            "method": "",
            "params": [result]
        }
        # Acknowledge the access request
        if self.mqtt is not None:
            if link_mode == self.LINK_MODE_CLOUD:
                topic = "cloud/config/notification"
                payload["method"] = "notify_cloud_auth"
            else:
                payload["method"] = "notify_lan_auth"
                topic = f"{self.oem_config['access_code']}/config/notification"
            logging.info(f"ack topic[{topic}] : {payload}")
            await self.mqtt.publish_topic(topic,
                                payload,
                                1,
                                False)

    def _check_existing_client(self, clientid: str) -> Optional[Dict[str, Any]]:
        """Check if clientid exists in oem_config and return its info if found."""
        if "clients" not in self.oem_config:
            return None
        return self.oem_config["clients"].get(clientid)

    def _get_client_info(self, clientid: str) -> Dict[str, Any]:
        """Get client info including certs, username and password."""
        try:
            sn_path = os.path.join(self.datapath, ".lava.sn")
            with open(sn_path, "r") as f:
                sn_content = f.read().strip().rstrip().upper()

            client_data = self.oem_config["clients"][clientid]

            # Read certificate files from stored paths
            with open(client_data["ca_path"], "r") as f:
                ca_content = f.read()
            with open(client_data["cert_path"], "r") as f:
                cert_content = f.read()
            with open(client_data["key_path"], "r") as f:
                key_content = f.read()

            client_info = {
                "state": "success",
                "clientid": clientid,
                "sn": sn_content,
                "ca": ca_content,
                "cert": cert_content,
                "key": key_content,
                "port": self.tls_port
            }
            return client_info
        except Exception as e:
            logging.error(f"Failed to get client info for {clientid}: {e}, remove clientid")
            # remove clientid
            self.oem_config["clients"].pop(clientid, None)
            return {
                "state": "error",
                "message": f"Failed to get client info: {str(e)}"
            }

    def _update_acl_entry_file(self, net_mode:int, access_code: int) -> None:
        """Update /etc/mosquitto/acl_entry.conf with new access code permissions."""
        acl_entry_path = "/etc/mosquitto/acl_entry.conf"
        try:
            # Clear the file and write new access code permissions
            with open(acl_entry_path, "w") as f:
                if net_mode == self.LINK_MODE_CLOUD:
                    f.write("topic write cloud/config/request\n")
                    f.write("topic read cloud/config/response\n")
                else:
                    # Write topic permissions for the new access code
                    f.write(f"topic write {access_code}/config/request\n")
                    f.write(f"topic read {access_code}/config/response\n")

            logging.info(f"Updated ACL entry file with access code: {access_code}")
        except Exception as e:
            logging.error(f"Failed to update ACL entry file: {e}")

    async def _handle_lan_config_request(self,
                                    data: bytes
                                    ) -> None:
        rpc: JsonRPC = self.server.lookup_component("jsonrpc")
        response = await rpc.dispatch(data, self.mqtt)
        if response is not None:
            await self.mqtt.publish_topic(f"{self.oem_config['access_code']}/config/response", response,
                                    1)
        return

    async def _handle_request_lan_auth(self,
                                    web_request: WebRequest
                                    ) -> Dict[str, Any]:
        clientid = web_request.get_str("clientid", None)
        app_id = web_request.get_str("app_id", "")
        if clientid is None or not isinstance(clientid, str):
            logging.error("Invalid clientid: %s", clientid)
            return {
                "state": "error",
                "clientid": clientid,
                "app_id": app_id,
                "message": "invalid clientid type: {}".format(type(clientid))
            }

        logging.info(f"LAN auth request from clientid: {clientid}, app_id: {app_id}")

        if self.oem_config['link_mode'] == self.LINK_MODE_CLOUD:
            return {
                "state": "error",
                "clientid": clientid,
                "app_id": app_id,
                "message": "link mode is not LAN"
            }

        if self.disconnecting_lan_clients:
            logging.info("Currently disconnecting all LAN clients, reject new auth request")
            return {
                "state": "error",
                "clientid": clientid,
                "app_id": app_id,
                "message": "currently disconnecting all LAN clients"
            }

        # Record clientid in oem_config
        if "clients" not in self.oem_config:
            self.oem_config["clients"] = {}
            with open(self.client_cfg_path, "w", encoding="utf-8") as f:
                json.dump(self.oem_config, f, indent='\t')

        # Check if client already has credentials
        existing_client = self._check_existing_client(clientid)
        if existing_client and all(k in existing_client for k in ["ca_path", "cert_path", "key_path", "index"]):
            logging.info("Access request from existing client")
            client_info = self._get_client_info(clientid)
            if client_info['state'] == 'success':
                client_info["app_id"] = app_id
                return client_info
            else:
                # re-authorize
                logging.error(f"Failed to get client info, need to re-authorize")

        logging.info(f"notify screen new client access request")
        # notify the screen
        self.server.send_event("snapmaker:client_access", {
            "id": "0",
            "clientid": clientid,
            "app_id": app_id
        })

        return {
            "state": "authorizing",
            "clientid": clientid,
            "app_id": app_id,
            "message": "waiting user authorization"
        }

    async def _handle_wan_config_request(self,
                                    data: bytes
                                    ) -> None:
        rpc: JsonRPC = self.server.lookup_component("jsonrpc")
        response = await rpc.dispatch(data, self.mqtt)
        if response is not None:
            await self.mqtt.publish_topic("cloud/config/response", response,
                                    1)
        return

    async def _handle_request_pin_code(self,
                                    web_request: WebRequest
                                    ) -> Dict[str, Any]:
        userid = web_request.get_str("userid", None)
        username = web_request.get_str("nickname", None)
        app_id = web_request.get_str("app_id", "")
        try:
            if userid is None or not isinstance(userid, str):
                logging.error("Invalid access request: %s", web_request.get_args())
                return {
                    "state": "error",
                    "message": "invalid userid field: {}".format(type(userid))
                }

            if username is None or not isinstance(username, str):
                logging.error("Invalid access request: %s", web_request.get_args())
                return {
                    "state": "error",
                    "message": "invalid nickname field: {}".format(type(username))
                }
            logging.info(f"pincode request from user: {username}, id: {userid}")

            if self.oem_config['link_mode'] != self.LINK_MODE_CLOUD:
                logging.error("Device is in LAN mode, cannot request pin code")
                return {
                    "state": "error",
                    "userid": userid,
                    "app_id": app_id,
                    "nickname": username,
                    "message": "link mode is not cloud"
                }

            if self.agent_state != "connected":
                logging.error("mqtt agent is not connected to cloud, cannot request pin code")
                return {
                    "state": "disconnected",
                    "userid": userid,
                    "app_id": app_id,
                    "nickname": username,
                    "message": "Server disconnected, please check network connection."
                }

            logging.info(f"pincode request from user: {username}, id: {userid}")

            if len(self.userid) == 0:
                logging.info(f"new user {username}, uuid: {userid} request pin code")
                # notify screen new user arrived
                self.server.send_event("snapmaker:user_access", {
                    "id": "0",
                    "userid": userid,
                    "app_id": app_id,
                    "username": username
                })
                return {
                    "state": "authorizing",
                    "userid": userid,
                    "nickname": username,
                    "app_id": app_id,
                    "message": "waiting user authorization",
                }
            else:
                if self.userid != userid:
                    logging.error(f"Device has been binded to another user {self.userid}, cannot change to user: {userid}")
                    return {
                        "state": "error",
                        "userid": userid,
                        "app_id": app_id,
                        "nickname": username,
                        "message": f"Device has been binded to another user {self.userid}"
                    }
                else:
                    logging.info(f"pincode request from existing user: {self.userid}")
                    self.server.send_event("snapmaker:user_access", {
                        "id": "0",
                        "userid": userid,
                        "username": username,
                        "app_id": app_id,
                    })
                    return {
                        "state": "authorizing",
                        "userid": userid,
                        "nickname": username,
                        "app_id": app_id,
                        "message": "waiting user authorization",
                    }
        except Exception as e:
            logging.error(f"Failed to handle access request: {e}")
            return {
                "state": "error",
                "userid": userid,
                "nickname": username,
                "app_id": app_id,
                "message": f"Failed to handle access request: {str(e)}"
            }

    async def _handle_get_authentication(self,
                                        web_request: WebRequest
                                        ) -> Dict[str, Any]:
        return await self._handle_approve_connection(WebRequest(
            web_request.get_endpoint(),
            {"clientid": web_request.get_str("clientid", None), "trusted": True, "approve": 1},
            web_request.get_request_type(),
            web_request.transport
        ))

    async def _handle_set_userinfo(self,
                                        web_request: WebRequest
                                        ) -> Dict[str, Any]:
        try:
            auther: Dict = web_request.get("auther", {})
            userid = auther.get("id", None)
            username = auther.get("nickname", None)
            logging.info(f"set userinfo: id: {userid}, name: {username}")

            return {
                "state": "success",
                "userid": self.userid,
                "username": self.username
            }
        except Exception as e:
            logging.error(f"Failed to handle set userinfo: {e}")
            return {
                "state": "error",
                "message": str(e)
            }

    async def _handle_set_region(self,
                                        web_request: WebRequest
                                        ) -> Dict[str, Any]:
        if self.doing_factory_reset:
            logging.info("we are doing factory reset, won't change region")
            return {"state": "error"}

        try:
            region = web_request.get_str("region", "cn")
            if region not in ["cn", "int", "eu", "us"]:
                logging.error(f"Invalid region: {region}")
                return {
                    "state": "error",
                    "message": "Invalid region, must be one of 'cn', 'us', 'eu'"
                }
            logging.info(f"set region to: {region}")
            if self.oem_config['region'] != region:
                # make pin code invalid
                self.pin_code_expire = time.time() - 1
                # clean up current user info
                async with self.userinfo_lock:
                    if len(self.userid) != 0:
                        if self.oem_config['region'] != "":
                            # logout current user only when current region is not empty
                            # while system boot up after factory reset, region is empty
                            self.oem_config['logging_out_userid'] = self.userid
                        self.username = ""
                        self.userid = ""
                    else:
                        logging.info("no user logged currently")
                self.oem_config['region'] = region
                with open(self.client_cfg_path, "w", encoding="utf-8") as f:
                    json.dump(self.oem_config, f, indent='\t')
                await self._sync_region_to_agent()
                self.server.send_event("snapmaker:update_mdns_info", {'region': region, 'userid': ""})
            return {
                "state": "success",
                "region": self.oem_config['region']
            }
        except Exception as e:
            logging.error(f"Failed to handle set region: {e}")
            return {
                "state": "error",
                "message": str(e)
            }

    async def _approve_client(self, clientid: str, trusted, app_id: str) -> Dict[str, Any]:
        # Check if client already has credentials
        existing_client = self._check_existing_client(clientid)
        if existing_client and all(k in existing_client for k in ["ca_path", "cert_path", "key_path", "index"]):
            client_info = self._get_client_info(clientid)
            # request from app through cloud
            if trusted:
                logging.info("Access request from trusted client, just return client info: %s", clientid)
                client_info["app_id"] = app_id
                return client_info
            if client_info["state"] == "error":
                logging.error(f"Failed to get client info, need to re-authorize")
                await self._ack_authentication_request({
                    "state": "denied",
                    "clientid": clientid,
                    "app_id": app_id,
                    "message": "Need to re-authorize"
                })
            else:
                client_info["state"] = "approve"
                client_info["app_id"] = app_id
                await self._ack_authentication_request(client_info)
            return {
                "state": "success"
            }
        else:
            logging.info(f"Client  does not have valid credentials, generating new certificate")

        # Read existing users
        max_id = 0
        for client in self.oem_config.get("clients", {}):
            if "index" in self.oem_config["clients"][client]:
                try:
                    index = int(self.oem_config["clients"][client]["index"])
                    if index > max_id:
                        max_id = index
                except ValueError:
                    logging.error(f"Invalid index for client: {self.oem_config['clients'][client]['index']}")

        # Generate new clientid if not provided
        username = f"cli{max_id + 1}"

        # Read generated certificate
        ca_path = os.path.join(self.certs_dir, f"mqtt_ca.crt")
        cert_path = os.path.join(self.certs_dir, f"mqtt_{username}.crt")
        key_path = os.path.join(self.certs_dir, f"mqtt_{username}.key")
        sn_path = os.path.join(self.datapath, f".lava.sn")

        if os.path.exists(cert_path):
            # Remove old certificate files
            logging.info(f"Removing old certificate files for {username}")
            os.remove(cert_path)
        if os.path.exists(key_path):
            os.remove(key_path)

        # Generate certificate
        await self._generate_certificate(username)

        # read the certificate content
        with open(cert_path, "r") as f:
            cert_content = f.read()
        with open(key_path, "r") as f:
            key_content = f.read()
        with open(ca_path, "r") as f:
            ca_content = f.read()
        with open(sn_path, "r") as f:
            sn_content = f.read()
            sn_content = sn_content.strip().rstrip().upper()

        # Store client info in oem_config (only paths, not content)
        if "clients" not in self.oem_config:
            self.oem_config["clients"] = {}
        self.oem_config["clients"][clientid] = {
            "ca_path": ca_path,
            "cert_path": cert_path,
            "key_path": key_path,
            "index": max_id + 1
        }
        with open(self.client_cfg_path, "w", encoding="utf-8") as f:
            json.dump(self.oem_config, f, indent='\t')

        client_info = {
                "state": "success",
                "clientid": clientid,
                "sn": sn_content,
                "ca": ca_content,
                "cert": cert_content,
                "key": key_content,
                "port": self.tls_port,
                "app_id": app_id
            }
        # request form App through cloud, just return the client info
        if trusted:
            return client_info

        # request from Orca, send client_info to Orca and event to screen
        client_info["state"] = "approve"
        await self._ack_authentication_request(client_info)
        return {
            "state": "success"
        }

    async def _approve_cloud_user(self, userid: str, username: str, app_id: str) -> Dict[str, Any]:
        logging.info(f"approve cloud user: {username}, uuid: {userid}, app_id: {app_id}")

        if self.agent_state != "connected":
            logging.error("mqtt agent is not connected to cloud, cannot approve user")
            await self._ack_authentication_request({
                "state": "disconnected",
                "userid": userid,
                "app_id": app_id,
                "nickname": username,
                "message": "Server disconnected, please check network connection."
            }, self.LINK_MODE_CLOUD)
            return {
                "state": "error",
                "message": "device is offline, please check network connection"
            }

        if self.pin_code_expire < time.time():
            # request mqtt agent to refresh pin code
            pin_code = await self._refresh_pin_code()
            if pin_code is None:
                await self._ack_authentication_request({
                    "state": "denied",
                    "userid": userid,
                    "app_id": app_id,
                    "nickname": username,
                    "message": "Failed to refresh pin code"
                }, self.LINK_MODE_CLOUD)
                # make current pincode be invalid after 5s
                # then pincode will be refreshed on next request
                self.pin_code_expire = time.time() + self.PIN_CODE_INVALID_AFTER_REQ
                return {
                    "state": "error",
                    "message": "Failed to refresh pin code"
                }

        # send new pin to authentication server and request it update to cloud server
        await self._ack_authentication_request({
            "state": "approve",
            "userid": userid,
            "nickname": username,
            "app_id": app_id,
            "pin_code": self.pin_code
        }, self.LINK_MODE_CLOUD)
        self.pin_code_expire = time.time() + self.PIN_CODE_INVALID_AFTER_REQ
        return {
            "state": "success"
        }

    async def _handle_approve_connection(self,
                                        web_request: WebRequest
                                        ) -> Dict[str, Any]:
        try:
            id_str = web_request.get_str("id", str(self.last_id))
            id = int(id_str)
            clientid = web_request.get_str("clientid", None)
            userid = web_request.get_str("userid", None)
            username = web_request.get_str("username", None)
            trusted = web_request.get_boolean("trusted", False)
            approve = web_request.get_int("approve", 0)
            app_id = web_request.get_str("app_id", "")
            logging.info(f"approve connection: clientid: {clientid}, userid: {userid}, trusted: {trusted}, approve: {approve}, app_id: {app_id}")
            if clientid is None and userid == "none":
                return {
                    "state": "error",
                    "message": "must specify clientid or userid"
                }

            # access from request
            if not approve:
                if clientid != None:
                    # while client apply 2 authentication continuously,
                    # we should check if the clientid already exists
                    existing_client = self._check_existing_client(clientid)
                    if existing_client and all(k in existing_client for k in ["ca_path", "cert_path", "key_path", "index"]):
                        return {
                            "state": "success"
                        }
                    await self._ack_authentication_request({
                        "state": "denied",
                        "clientid": clientid,
                        "app_id": app_id
                    })
                elif userid != None:
                    await self._ack_authentication_request({
                        "state": "denied",
                        "userid": userid,
                        "app_id": app_id,
                        "nickname": username,
                    }, self.LINK_MODE_CLOUD)
                else:
                    logging.error("Invalid approve request: must specify clientid or userid")
                return {
                    "state": "success"
                }

            if clientid is None and userid != None:
                return await self._approve_cloud_user(userid, username, app_id)

            return await self._approve_client(clientid, trusted, app_id)

        except Exception as e:
            logging.error(f"Error handling approve connection: {e}")
            return {
                "state": "error",
                "message": str(e)
            }

    async def _handle_confirm_lan_status(self,
                                    web_request: WebRequest
                                    ) -> Dict[str, Any]:
        clientid = web_request.get_str("clientid", None)
        app_id = web_request.get_str("app_id", "")
        logging.info(f"confirm lan status for client: {clientid}")
        # check if clientid exists in oem_config
        if clientid is None or not isinstance(clientid, str):
            logging.error("Invalid clientid: %s", clientid)
            return {
                "state": "error",
                "clientid": "none",
                "app_id": app_id,
                "message": f"invalid client field: {web_request.get_args()}"
            }

        if self.disconnecting_lan_clients:
            logging.info("Currently disconnecting LAN clients, returning error")
            return {
                "state": "error",
                "clientid": clientid,
                "app_id": app_id,
                "message": "currently disconnecting LAN clients"
            }

        # check link mode, if not in LAN mode, return error
        if self.oem_config['link_mode'] != self.LINK_MODE_LAN:
            logging.info("Link mode is not LAN, cannot connect to server")
            return {
                "state": "error",
                "clientid": clientid,
                "app_id": app_id,
                "message": "link mode is not LAN"
            }

        existing_client = self._check_existing_client(clientid)
        if existing_client is None:
            logging.info(f"Client does not exist in oem_config, returning unauthorized")
            return {
                "state": "unauthorized",
                "clientid": clientid,
                "app_id": app_id,
                "message": f"client is unauthorized"
            }

        # get client info
        client_info = self._get_client_info(clientid)
        if client_info.get("state") == "error":
            logging.error(f"Failed to get client info, returning unauthorized")
            return {
                "state": "unauthorized",
                "clientid": clientid,
                "app_id": app_id,
                "message": "client is unauthorized"
            }
        else:
            client_info["app_id"] = app_id
            return client_info

def load_component(config: ConfigHelper) -> ClientManager:
    return ClientManager(config)
