"""
MCP 隧道服务器 —— 让 Claude Code 能自主创建内网穿透隧道
"""
import asyncio
import json
import subprocess
import tempfile
import os
import httpx
from pathlib import Path
from typing import Any

from mcp.server import Server, NotificationOptions
from mcp.server.models import InitializationOptions
import mcp.server.stdio
import mcp.types as types

# ============ 配置 ============
FRP_SERVER_ADDR = "your-public-server.com"
FRP_SERVER_PORT = 7000
FRP_AUTH_TOKEN = "your-token"
# =============================

server = Server("tunnel-server")
active_tunnels: dict[str, subprocess.Popen] = {}


def _write_frpc_toml(local_port: int, remote_port: int, tunnel_name: str) -> str:
    """生成 frpc 配置文件"""
    toml = f"""serverAddr = "{FRP_SERVER_ADDR}"
serverPort = {FRP_SERVER_PORT}
auth.token = "{FRP_AUTH_TOKEN}"

[[proxies]]
name = "{tunnel_name}"
type = "tcp"
localIP = "127.0.0.1"
localPort = {local_port}
remotePort = {remote_port}
"""
    path = os.path.join(tempfile.gettempdir(), f"frpc_{tunnel_name}.toml")
    Path(path).write_text(toml)
    return path


@server.list_tools()
async def handle_list_tools() -> list[types.Tool]:
    return [
        types.Tool(
            name="expose_port",
            description="将本地端口暴露到公网（创建TCP隧道）",
            inputSchema={
                "type": "object",
                "properties": {
                    "local_port": {"type": "integer", "description": "本地服务端口"},
                    "remote_port": {"type": "integer", "description": "公网映射端口"},
                    "name": {"type": "string", "description": "隧道名称"},
                },
                "required": ["local_port", "remote_port"],
            },
        ),
        types.Tool(
            name="proxy_request",
            description="通过隧道代理请求内网服务",
            inputSchema={
                "type": "object",
                "properties": {
                    "url": {"type": "string", "description": "内网URL"},
                    "method": {"type": "string", "description": "HTTP方法"},
                    "headers": {"type": "object", "description": "请求头"},
                    "body": {"type": "string", "description": "请求体"},
                },
                "required": ["url", "method"],
            },
        ),
        types.Tool(
            name="close_tunnel",
            description="关闭指定的隧道",
            inputSchema={
                "type": "object",
                "properties": {
                    "name": {"type": "string", "description": "隧道名称"},
                },
                "required": ["name"],
            },
        ),
        types.Tool(
            name="list_tunnels",
            description="列出所有活跃隧道",
            inputSchema={"type": "object", "properties": {}},
        ),
    ]


@server.call_tool()
async def handle_call_tool(
    name: str, arguments: dict[str, Any] | None
) -> list[types.TextContent]:
    args = arguments or {}

    if name == "expose_port":
        local_port = args["local_port"]
        remote_port = args["remote_port"]
        tunnel_name = args.get("name", f"tunnel_{local_port}_{remote_port}")

        if tunnel_name in active_tunnels:
            return [types.TextContent(type="text", text=f"隧道 {tunnel_name} 已存在")]

        config_path = _write_frpc_toml(local_port, remote_port, tunnel_name)
        proc = subprocess.Popen(
            ["frpc", "-c", config_path],
            stdout=subprocess.PIPE, stderr=subprocess.PIPE
        )
        active_tunnels[tunnel_name] = proc

        return [types.TextContent(
            type="text",
            text=f"✅ 隧道 {tunnel_name} 已创建: 本地 {local_port} → 公网端口 {remote_port}\n"
                 f"公网地址: {FRP_SERVER_ADDR}:{remote_port}"
        )]

    elif name == "proxy_request":
        url = args["url"]
        method = args.get("method", "GET").upper()
        headers = args.get("headers", {})
        body = args.get("body")

        async with httpx.AsyncClient(timeout=30) as client:
            resp = await client.request(method, url, headers=headers, content=body)

        return [types.TextContent(
            type="text",
            text=f"状态: {resp.status_code}\n"
                 f"响应头: {dict(resp.headers)}\n\n"
                 f"响应体:\n{resp.text[:5000]}"
        )]

    elif name == "close_tunnel":
        tunnel_name = args["name"]
        proc = active_tunnels.pop(tunnel_name, None)
        if proc:
            proc.terminate()
            return [types.TextContent(type="text", text=f"已关闭隧道 {tunnel_name}")]
        return [types.TextContent(type="text", text=f"未找到隧道 {tunnel_name}")]

    elif name == "list_tunnels":
        if not active_tunnels:
            return [types.TextContent(type="text", text="当前无活跃隧道")]
        info = "\n".join(
            f"  - {name} (PID {proc.pid})" for name, proc in active_tunnels.items()
        )
        return [types.TextContent(type="text", text=f"活跃隧道:\n{info}")]

    raise ValueError(f"Unknown tool: {name}")


async def main():
    async with mcp.server.stdio.stdio_server() as (read_stream, write_stream):
        await server.run(
            read_stream, write_stream,
            InitializationOptions(
                server_name="tunnel-server",
                server_version="0.1.0",
            ),
        )

if __name__ == "__main__":
    asyncio.run(main())
