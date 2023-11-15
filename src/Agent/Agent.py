import datetime
import json
import asyncio
import threading
import queue
from ..Request import Request
from ..WebSocket import WebSocketServer
from ..utils import RuntimeCtx, StorageDelegate, PluginManager, AliasManager, IdGenerator, to_json_desc, find_json, check_version, load_json

class Agent(object):
    def __init__(
        self,
        *,
        agent_id: str=None,
        auto_save: bool=False,
        parent_agent_runtime_ctx: object,
        global_storage: object,
        global_websocket_server: object,
        parent_plugin_manager: object,
        parent_settings: object,
    ):
        # Integrate
        self.global_storage = global_storage
        self.global_websocket_server = global_websocket_server
        self.plugin_manager = PluginManager(parent = parent_plugin_manager)
        self.settings = RuntimeCtx(parent = parent_settings)
        self.alias_manager = AliasManager(self)
        self.agent_runtime_ctx = RuntimeCtx(parent = parent_agent_runtime_ctx)
        self.request_runtime_ctx = RuntimeCtx()
        self.request = Request(
            parent_plugin_manager = self.plugin_manager,
            parent_request_runtime_ctx = self.request_runtime_ctx,
            parent_settings = self.settings,
        )
        # Agent Id
        if agent_id == None:
            self.agent_id = IdGenerator("agent").create()
        else:
            self.agent_id = agent_id
        # Agent Storage
        self.agent_storage = StorageDelegate(
            db_name = self.agent_id,
            plugin_manager = self.plugin_manager,
            settings = self.settings,
        )
        # Load Saved agent_runtime_ctx
        self.agent_runtime_ctx.update_by_dict(self.agent_storage.table("agent_runtime_ctx").get())
        # Set Agent Auto Save Setting
        self.agent_runtime_ctx.set("agent_auto_save", auto_save)
        # Version Check In Debug Model
        if self.settings.get_trace_back("is_debug"):
            check_version_record = self.global_storage.get("agently", "check_version_record")
            today = str(datetime.date.today())
            if check_version_record != today:
                check_version(self.global_storage, today)
        # Agent Request Early, Prefix & Suffix
        self.agent_request_early = []
        self.agent_request_prefix = []
        self.agent_request_suffix = []
        # Register Default Request Alias to Agent
        self.request._register_default_alias(self.alias_manager)
        # Install Agent Components
        self.refresh_plugins()

    def refresh_plugins(self):
        # Agent Components
        agent_components = self.plugin_manager.get("agent_component")
        component_toggles = self.settings.get_trace_back("component_toggles")
        for agent_component_name, AgentComponentClass in agent_components.items():
            # Skip component_toggles Those Be Toggled Off
            if agent_component_name in component_toggles and component_toggles[agent_component_name] == False:
                    continue
            # Attach Component
            agent_component_instance = AgentComponentClass(agent = self)
            setattr(self, agent_component_name, agent_component_instance)
            component_export = agent_component_instance.export()
            # Register export_early, export_prefix, export_suffix
            if "early" in component_export:
                if isinstance(component_export["early"], list):
                    self.agent_request_early.extend(component_export["early"])
                elif callable(component_export["early"]):
                    self.agent_request_early.append(component_export["early"])
            if "prefix" in component_export:
                if isinstance(component_export["prefix"], list):
                    self.agent_request_prefix.extend(component_export["prefix"])
                elif callable(component_export["prefix"]):
                    self.agent_request_prefix.append(component_export["prefix"])
            if "suffix" in component_export:
                if isinstance(component_export["suffix"], list):
                    self.agent_request_suffix.extend(component_export["suffix"])
                elif callable(component_export["suffix"]):
                    self.agent_request_suffix.append(component_export["suffix"])
            # Register Alias
            if "alias" in component_export:
                for alias_name, alias_info in component_export["alias"].items():
                    self.alias_manager.register(
                        alias_name,
                        alias_info["func"],
                        return_value = alias_info["return_value"] if "return_value" in alias_info else False,
                    )

    def toggle_auto_save(self, is_enabled: bool):
        self.agent_runtime_ctx.set("agent_auto_save", is_enabled)
        return self

    def save(self):
        self.agent_storage.table("agent_runtime_ctx").update_by_dict(self.agent_runtime_ctx.get()).save()
        return self

    def set_settings(self, settings_key: str, settings_value: any):
        self.settings.set(settings_key, settings_value)
        return self

    async def start_async(self, request_type: str=None):
        is_debug = self.settings.get_trace_back("is_debug")
        # Auto Save Agent runtime_ctx
        if self.agent_runtime_ctx.get("agent_auto_save") ==  True:
            self.save()
        # Call Early Func before Prefix Stage (in case of sometimes need to call other alias)
        for early_func in self.agent_request_early:
            early_data = await early_func() if asyncio.iscoroutinefunction(early_func) else early_func()
            if early_data != None:
                if isinstance(early_data, tuple) and isinstance(early_data[0], str) and early_data[1] != None:
                    self.request.request_runtime_ctx.update(early_data[0], early_data[1])
                elif isinstance(early_data, dict):
                    for key, value in early_data.items():
                        if value != None:
                            self.request.request_runtime_ctx.delta(f"prompt.{ key }", value)
                else:
                    raise Exception("[Agent Component] Early stage return data error: only accept None or Dict({'<request slot name>': <data append to slot>, ... } or Tuple('request slot name', <data append to slot>)")
        # Call Prefix Funcs to Prepare Prefix Data(From agent_runtime_ctx To request_runtime_ctx)
        for prefix_func in self.agent_request_prefix:
            prefix_data = await prefix_func() if asyncio.iscoroutinefunction(prefix_func) else prefix_func()
            if prefix_data != None:
                if isinstance(prefix_data, tuple) and isinstance(prefix_data[0], str) and prefix_data[1] != None:
                    self.request.request_runtime_ctx.update(prefix_data[0], prefix_data[1])
                elif isinstance(prefix_data, dict):
                    for key, value in prefix_data.items():
                        if value != None:
                            self.request.request_runtime_ctx.delta(f"prompt.{ key }", value)
                else:
                    raise Exception("[Agent Component] Prefix return data error: only accept None or Dict({'<request slot name>': <data append to slot>, ... } or Tuple('request slot name', <data append to slot>)")

        # Request
        event_generator = await self.request.get_event_generator(request_type)
    
        # Call Suffix Func to Handle Response Events
        if is_debug:
            print("[Realtime Response]\n")
        async def call_request_suffix(response):
            for suffix_func in self.agent_request_suffix:
                if asyncio.iscoroutinefunction(suffix_func):
                    await suffix_func(response["event"], response["data"]) 
                else:
                    suffix_func(response["event"], response["data"])

        async def handle_response(response):
            if response["event"] == "response:delta" and is_debug:
                print(response["data"], end="")
            if response["event"] == "response:done":
                if self.request.response_cache["reply"] == None:
                    self.request.response_cache["reply"] = response["data"]
                if is_debug:
                    print("\n--------------------------\n")
                    print("[Final Reply]\n", self.request.response_cache["reply"], "\n--------------------------\n")
            await call_request_suffix(response)

        if "__aiter__" in dir(event_generator):
            async for response in event_generator:
                await handle_response(response)
        else:
            for response in event_generator:
                await handle_response(response)

        await call_request_suffix({ "event": "response:finally", "data": self.request.response_cache })

        # Fix JSON if Required
        if self.request.response_cache["type"] == "JSON":
            self.request.response_cache["reply"] = load_json(
                self.request.response_cache["reply"],
                self.request.response_cache["prompt"]["input"],
                self.request.response_cache["prompt"]["output"],
                self.request,
                is_debug = is_debug,
            )
            '''
            try:
                self.request.response_cache["reply"] = json.loads(find_json(self.request.response_cache["reply"]))
                if is_debug:
                    print("[Parse JSON to Dict] Done")
                    print("\n--------------------------\n")
            except json.JSONDecodeError as e:
                try:
                    fixed_result = self.request\
                        .input({
                            "target": self.request.response_cache["prompt"]["input"],
                            "format": to_json_desc(self.request.response_cache["prompt"]["output"]),
                            "origin JSON String": self.request.response_cache["reply"] ,
                            "error": e.msg,
                            "position": e.pos,
                        })\
                        .output('Fixed JSON String can be parsed by Python only without explanation and decoration.')\
                        .start()
                    self.request.response_cache["reply"] = json.loads(find_json(fixed_result))
                    if is_debug:
                        print("[Parse JSON to Dict] Done")
                        print("\n--------------------------\n")
                except Exception as e:
                    raise Exception(f"[Agent Request] Error still occured when try to fix JSON decode error: { str(e) }")
            '''

        self.request_runtime_ctx.empty()
        return self.request.response_cache["reply"]

    def start(self, request_type: str=None):
        reply_queue = queue.Queue()
        def start_in_theard():
            asyncio.set_event_loop(asyncio.new_event_loop())
            reply = asyncio.get_event_loop().run_until_complete(self.start_async(request_type))
            reply_queue.put_nowait(reply)
        theard = threading.Thread(target=start_in_theard)
        theard.start()
        theard.join()        
        reply = reply_queue.get_nowait()
        return reply

    def start_websocket_server(self, port:int=15365):
        is_debug = self.settings.get_trace_back("is_debug")

        def alias_handler(data: any, response: callable):
            try:
                if isinstance(data["params"], dict):
                    getattr(self, data["name"])(**data["params"])
                elif isinstance(data["params"], (list, tuple, set)): 
                    getattr(self, data["name"])(*data["params"])
                else:
                    getattr(self, data["name"])(data["params"])
                response("alias_done")
            except Exception as e:
                if is_debug:
                    print("[Agent WebSocket Server] Error: ", str(e))
        self.global_websocket_server.add_event_handler(self.agent_id, "alias", alias_handler)

        def start_handler(data: any, response: callable):
            def write_message(event: str, data: any):
                try:
                    response(event, data)
                except Exception as e:
                    if is_debug:
                        print("[Agent WebSocket Server] Error", str(e))
            self\
                .on_delta(
                    lambda data: write_message("delta", data)
                )\
                .on_done(
                    lambda data: write_message("done", data)
                )\
                .start()
        self.global_websocket_server.add_event_handler(self.agent_id, "start", start_handler)

        if is_debug:
            print(f"[WebSocket Server] Event listeners of agent '{ self.agent_id }' are on.")

        if self.global_websocket_server.status == 0:
            self.global_websocket_server.start(port)
            if is_debug:
                print(f"[WebSocket Server] WebSocket server started at { self.global_websocket_server.host }:{ self.global_websocket_server.port }.")
        else:
            if is_debug and port != self.global_websocket_server.port:
                print(f"[WebSocket Server] WebSocket server has already started at { self.global_websocket_server.host }:{ self.global_websocket_server.port }")

    def stop_websocket_server(self):
        self.global_websocket_server.remove_event_handler(self.agent_id)
        if self.settings.get_trace_back("is_debug"):
            print(f"[WebSocket Server] Agent '{ self.agent_id } websocket server stoped.'")