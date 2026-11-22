"""MCP (Model Context Protocol): extra tools from MCP servers chosen by the user.

The user adds servers (a program such as ``uvx mcp-server-time``, or an
address); their tools become assistant tools that ask for approval before
each call, unless the user exempts them. Secrets stay in the keyring.
"""

from kreafinity.mcp.client import McpError
from kreafinity.mcp.config import McpConfigError, ServerConfig
from kreafinity.mcp.manager import McpManager, McpServer

__all__ = ["McpConfigError", "McpError", "McpManager", "McpServer", "ServerConfig"]
