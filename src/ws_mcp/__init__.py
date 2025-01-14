import argparse
import asyncio
import json
import logging
import os
import shlex
import sys

import jsonschema
import websockets
from pathlib import Path

from asyncio import create_subprocess_exec, subprocess, Queue
from typing import Optional, Dict, List, Any, Union

from websockets.legacy.server import WebSocketServerProtocol

import time

class MessagePublisher:
    """Handles publishing messages to WebSocket clients."""
    def __init__(self):
        self.message_queue: "Queue[Dict[str, Any]]" = Queue()
        self.websocket: Optional[WebSocketServerProtocol] = None
        self.running = False
        self._task: Optional[asyncio.Task] = None

    def set_websocket(self, websocket: WebSocketServerProtocol):
        """Set the WebSocket connection to publish messages to."""
        self.websocket = websocket
        
    async def start(self):
        """Start the message publishing task."""
        if self._task is None:
            self.running = True
            self._task = asyncio.create_task(self._publish_messages())
            
    async def stop(self):
        """Stop the message publishing task."""
        self.running = False
        if self._task:
            await self.message_queue.put(None)  # Sentinel to stop the loop
            await self._task
            self._task = None
            
    async def publish(self, message: Dict[str, Any]):
        """Queue a message for publishing."""
        await self.message_queue.put(message)
        
    async def _publish_messages(self):
        """Main loop for publishing messages to WebSocket."""
        while self.running:
            try:
                message = await self.message_queue.get()
                if message is None:  # Stop sentinel
                    break
                    
                if self.websocket and self.websocket.state == websockets.protocol.State.OPEN:
                    await self.websocket.send(json.dumps(message))
                    
            except Exception as e:
                logger.error(f"Error publishing message: {e}")
                continue

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

INITIALIZE_REQUEST_SCHEMA = {
    "type": "object",
    "required": ["jsonrpc", "method", "params", "id"],
    "properties": {
        "jsonrpc": {"type": "string", "enum": ["2.0"]},
        "method": {"type": "string", "enum": ["initialize"]},
        "params": {
            "type": "object",
            "required": ["protocolVersion", "clientInfo", "capabilities"],
            "properties": {
                "protocolVersion": {"type": "string"},
                "clientInfo": {
                    "type": "object",
                    "required": ["name", "version"],
                    "properties": {
                        "name": {"type": "string"},
                        "version": {"type": "string"}
                    }
                },
                "capabilities": {"type": "object"}
            }
        },
        "id": {"type": ["string", "number"]}
    }
}

class McpServer:
    """Represents a single MCP server process and its state."""
    def __init__(self, command: str, env: Optional[Dict[str, str]] = None):
        self.command = command
        self.env = env or {}
        self.process: Optional[subprocess.Process] = None
        self.pending_requests: Dict[str, asyncio.Future] = {}
        self.tools: Dict[str, Any] = {}  # Map of tool names to their schemas
        self.initialized = False
        self.message_publisher: Optional[MessagePublisher] = None

    async def start_process(self):
        """Start the MCP server process"""
        args = shlex.split(self.command)
        process_env = {**os.environ, **self.env}

        self.process = await create_subprocess_exec(
            *args,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            env=process_env
        )

        if not self.process.stdin or not self.process.stdout:
            raise RuntimeError(f"Failed to create process pipes for command: {self.command}")

        logger.info(f"Started MCP process: {self.command}")
        return self.process

    def register_tools(self, tools: Dict[str, Any]):
        """Register tools provided by this server"""
        self.tools = tools
        self.initialized = True

    async def send_request(self, request: Dict[str, Any]) -> Dict[str, Any]:
        """Send a request to this server and wait for response"""
        if not self.process or not self.process.stdin:
            raise RuntimeError("Process not started or stdin not available")

        request_id = request.get("id")
        if request_id:
            self.pending_requests[request_id] = asyncio.Future()

        message_bytes = f"{json.dumps(request)}\n".encode()
        self.process.stdin.write(message_bytes)
        await self.process.stdin.drain()
        logger.info(f"Sent to {self.command}: {request}")

        if request_id:
            try:
                response = await asyncio.wait_for(
                    self.pending_requests[request_id],
                    timeout=10.0
                )
                return response
            except asyncio.TimeoutError:
                return {
                    "jsonrpc": "2.0",
                    "id": request_id,
                    "error": {
                        "code": -32000,
                        "message": "Request timed out"
                    }
                }
            finally:
                self.pending_requests.pop(request_id, None)

        return {}

    def set_message_publisher(self, publisher: MessagePublisher):
        """Set the message publisher for this server."""
        self.message_publisher = publisher

    async def handle_stdout(self):
        """Handle server process output"""
        while True:
            if not self.process or not self.process.stdout:
                break

            try:
                line = await self.process.stdout.readline()
                if not line:
                    logger.info(f"Process stdout closed for command: {self.command}")
                    break

                line_str = line.decode().strip()
                if not line_str:
                    logger.info(f"handle_stdout {self.command} got no-decode-able line")
                    continue

                logger.info(f"handle_stdout {self.command} got line {line_str}")

                try:
                    message = json.loads(line_str)
                    if "id" in message and message["id"] in self.pending_requests:
                        future = self.pending_requests.pop(message["id"])
                        future.set_result(message)

                    if self.message_publisher:
                        await self.message_publisher.publish(message)
                except json.JSONDecodeError as e:
                    logger.error(f"Failed to parse JSON from {self.command}: {line_str}")
                    logger.error(f"JSON error: {e}")

            except Exception as e:
                logger.error(f"Error handling process output for {self.command}: {e}")
                continue

    async def handle_stderr(self):
        """Handle stderr output from the process"""
        while True:
            if not self.process or not self.process.stderr:
                break

            try:
                line = await self.process.stderr.readline()
                if not line:
                    break
                logger.info(f"Process stderr ({self.command}): {line.decode().strip()}")
            except Exception as e:
                logger.error(f"Error handling stderr ({self.command}): {e}")
                break

    async def cleanup(self):
        """Clean up server resources"""
        if self.process:
            try:
                self.process.terminate()
                await asyncio.wait_for(self.process.wait(), timeout=5.0)
            except asyncio.TimeoutError:
                self.process.kill()
            self.process = None

        for future in self.pending_requests.values():
            if not future.done():
                future.cancel()
        self.pending_requests.clear()


class McpWebSocketBridge:
    def __init__(self, commands: List[str], port: int = 3000, env: Optional[Dict[str, str]] = None):
        self.servers: List[McpServer] = [McpServer(cmd, env) for cmd in commands]
        self.port = port
        self.websocket: Optional[WebSocketServerProtocol] = None
        self.tool_to_server: Dict[str, McpServer] = {}  # Maps tool names to servers
        self.message_publisher = MessagePublisher()

    async def start_all_servers(self):
        """Start all MCP server processes"""
        for server in self.servers:
            await server.start_process()

    def get_server_for_tool(self, method: str) -> Optional[McpServer]:
        """Get the server responsible for a given tool/method"""
        return self.tool_to_server.get(method)

    def get_combined_tools(self):
        combined_tools = {}
        for server in self.servers:
            combined_tools.update(server.tools)
        combined_tools

    async def handle_initialize(self, request: Dict[str, Any]) -> Dict[str, Any]:
        """Handle initialize request by forwarding to all servers and combining responses"""
        # Send initialize to all servers and collect their tools
        for server in self.servers:
            response = await server.send_request(request)
            logger.info(f"handle_initialize: initialize {server.command} - {response}")

            time.sleep(2)

            response = await server.send_request({"jsonrpc":"2.0","method":"tools/list","id":2})
            logger.info(f"handle_initialize: tools/list {server.command} - {response}")

            if "result" in response and "tools" in response["result"]:
                server_tools = response["result"]["tools"]
                # Register tools with this server
                server.register_tools(server_tools)
                # Update tool to server mapping
                for tool in server_tools:
                    self.tool_to_server[tool["name"]] = server

        # Return combined response
        logger.info(f"handle_initialize: done with {self.tool_to_server}")
        #return {
        #    "jsonrpc": "2.0",
        #    "id": request["id"],
        #    "result": {
        #        "name": "ws-mcp-multi",
        #        "version": "1.0.0",
        #        "tools": combined_tools
        #    }
        #}
        # TODO
        return {
            "jsonrpc": "2.0",
            "id": request["id"],
            "result": {"protocolVersion": "2024-11-05", "capabilities": {"experimental": {}, "prompts": {"listChanged": False}, "tools": {"listChanged": False}}, "serverInfo": {"name": "ws-mcp-multi", "version": "1.0.0"}},
        }

    async def list_tools(self, id):
        return {
            "jsonrpc": "2.0",
            "id": id,
            "result": {
                "tools": self.get_combined_tools()
            }
        }


    #async def handle_stderr(self):
    #    """Handle stderr output from all processes"""
    #    stderr_tasks = []
    #    for server in self.servers:
    #        async def handle_server_stderr(server: McpServer):
    #            while True:
    #                if not server.process or not server.process.stderr:
    #                    break
    #                try:
    #                    line = await server.process.stderr.readline()
    #                    if not line:
    #                        break
    #                    logger.info(f"Process stderr ({server.command}): {line.decode().strip()}")
    #                except Exception as e:
    #                    logger.error(f"Error handling stderr for {server.command}: {e}")
    #                    break

    #        stderr_tasks.append(asyncio.create_task(handle_server_stderr(server)))

    #    await asyncio.gather(*stderr_tasks)


    async def handle_client(self, websocket: WebSocketServerProtocol):
        """Handle WebSocket client connection"""
        self.websocket = websocket
        self.message_publisher.set_websocket(websocket)
        
        # Set message publisher for all servers
        for server in self.servers:
            server.set_message_publisher(self.message_publisher)
            
        logger.info("WebSocket client connected")

        try:
            async for message in websocket:
                try:
                    data = json.loads(message)
                    logger.debug(f"Received message: {data}")

                    method = data.get("method")

                    # Special handling for initialize
                    if method == "initialize":
                        try:
                            jsonschema.validate(instance=data, schema=INITIALIZE_REQUEST_SCHEMA)
                            response = await self.handle_initialize(data)
                            await websocket.send(json.dumps(response))
                            continue
                        except jsonschema.exceptions.ValidationError as e:
                            error_response = {
                                "jsonrpc": "2.0",
                                "id": data.get("id"),
                                "error": {
                                    "code": -32600,
                                    "message": f"Invalid initialize request: {str(e)}"
                                }
                            }
                            await websocket.send(json.dumps(error_response))
                            continue
                    elif method == "tools/list":
                        tools = await self.list_tools(data.get("id"))
                        logger.info(f"tools/list: {tools}")
                        await websocket.send(json.dumps(tools))
                        continue
                    elif method == "notifications/initialized":
                        continue

                    # Route other requests to appropriate server
                    server = self.get_server_for_tool(method)
                    if not server:
                        error_response = {
                            "jsonrpc": "2.0",
                            "id": data.get("id"),
                            "error": {
                                "code": -32601,
                                "message": f"Method not found: {method}"
                            }
                        }
                        await websocket.send(json.dumps(error_response))
                        continue

                    response = await server.send_request(data)
                    if response:
                        await websocket.send(json.dumps(response))

                except Exception as e:
                    logger.error(f"Error handling message: {e}")
                    if "id" in data:
                        error_response = {
                            "jsonrpc": "2.0",
                            "id": data["id"],
                            "error": {
                                "code": -32000,
                                "message": str(e)
                            }
                        }
                        await websocket.send(json.dumps(error_response))

        except websockets.exceptions.ConnectionClosed:
            logger.info("WebSocket client disconnected")
        finally:
            self.websocket = None

    async def cleanup(self):
        """Clean up all server resources"""
        await self.message_publisher.stop()
        for server in self.servers:
            await server.cleanup()

    async def serve(self):
        """Start the WebSocket server and all MCP processes"""
        try:
            # Start all MCP servers
            await self.start_all_servers()

            # Start the message publisher
            await self.message_publisher.start()

            # Start output handlers for all servers
            stdout_tasks = [
                asyncio.create_task(server.handle_stdout())
                for server in self.servers
            ]
            stderr_tasks = [
                asyncio.create_task(server.handle_stderr())
                for server in self.servers
            ]

            # Start WebSocket server
            async with websockets.serve(self.handle_client, "localhost", self.port):
                logger.info(f"Multi-MCP bridge running on ws://localhost:{self.port}")

                try:
                    #await asyncio.gather(*output_tasks, stderr_task)
                    await asyncio.gather(*stdout_tasks, *stderr_tasks)
                except Exception as e:
                    logger.error(f"Error in message handling: {e}")

                await asyncio.Future()  # run forever

        except Exception as e:
            logger.error(f"Server error: {e}")
        finally:
            await self.cleanup()

def parse_dotenv(env_file: Path) -> Dict[str, str]:
    """Parse a .env file and return a dictionary of environment variables."""
    if not env_file.exists():
        raise FileNotFoundError(f"Environment file not found: {env_file}")

    env_vars = {}
    with env_file.open() as f:
        for line in f:
            line = line.strip()
            # Skip empty lines and comments
            if not line or line.startswith('#'):
                continue
            try:
                key, value = line.split('=', 1)
                # Remove quotes if present
                key = key.strip()
                value = value.strip().strip("'").strip('"')
                env_vars[key] = value
            except ValueError:
                logger.warning(f"Skipping invalid line in .env file: {line}")
    return env_vars

def parse_args():
    parser = argparse.ArgumentParser(
        description='Bridge multiple stdio-based MCP servers to WebSocket',
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  %(prog)s --command "uv tool run --from wcgw@latest --python 3.12 wcgw_mcp" --command "node path/to/mcp-server.js" --port 3000
  %(prog)s --command "./server1" --command "./server2" --port 3001 --env API_KEY=xyz123
  %(prog)s --command "./server1" --command "./server2" --env-file .env"""
    )

    parser.add_argument(
        '--command',
        type=str,
        required=True,
        action='append',
        help='Command to start an MCP server (in quotes). Can be specified multiple times.'
    )

    parser.add_argument(
        '--port',
        type=int,
        default=3000,
        help='Port for the WebSocket server (default: 3000)'
    )

    parser.add_argument(
        '--log-level',
        choices=['DEBUG', 'INFO', 'WARNING', 'ERROR', 'CRITICAL'],
        default='INFO',
        help='Set the logging level (default: INFO)'
    )

    parser.add_argument(
        '--env',
        type=str,
        action='append',
        help='Environment variables to pass to the MCP server in KEY=VALUE format. Can be specified multiple times. Overrides .env.'
    )

    parser.add_argument(
        '--env-file',
        type=Path,
        help='Path to a .env file containing environment variables'
    )

    return parser.parse_args()

async def execute():
    args = parse_args()

    # Set log level from arguments
    logging.getLogger().setLevel(args.log_level)

    # Initialize environment variables dictionary
    env = {}

    # Read environment variables from .env file if provided
    if args.env_file:
        try:
            env.update(parse_dotenv(args.env_file))
        except Exception as e:
            logger.error(f"Error reading .env file: {e}")
            sys.exit(1)

    # Add/override with command line environment variables if provided
    if args.env:
        for env_var in args.env:
            try:
                key, value = env_var.split('=', 1)
                env[key] = value
            except ValueError:
                logger.error(f"Invalid environment variable format: {env_var}. Must be KEY=VALUE")
                sys.exit(1)

    # Initialize bridge with multiple commands
    bridge = McpWebSocketBridge(args.command, args.port, env)
    await bridge.serve()

def main():
    asyncio.run(execute())

if __name__ == "__main__":
    main()
