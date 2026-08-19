"""
Django’s Diner MCP tools, exposed as functions for LLM’s --functions option:

    llm --functions diner_functions.py "What’s on the menu?"

Each function makes one MCP tools/call request to the running example
server, using only the standard library, and returns the text content of
the result—including in-band tool errors, so the model can self-correct.
"""

from __future__ import annotations

import json
import typing
import urllib.error
import urllib.request

ENDPOINT = "http://127.0.0.1:8000/mcp"
TOKEN = "easy-peasy-example-token"
PROTOCOL_VERSION = "2026-07-28"


def _call_tool(name: str, arguments: dict[str, typing.Any]) -> str:
    body = {
        "jsonrpc": "2.0",
        "id": 1,
        "method": "tools/call",
        "params": {
            "name": name,
            "arguments": arguments,
            "_meta": {
                "io.modelcontextprotocol/protocolVersion": PROTOCOL_VERSION,
                "io.modelcontextprotocol/clientCapabilities": {},
            },
        },
    }
    request = urllib.request.Request(
        ENDPOINT,
        data=json.dumps(body).encode(),
        headers={
            "Authorization": f"Bearer {TOKEN}",
            "Content-Type": "application/json",
            "MCP-Protocol-Version": PROTOCOL_VERSION,
            "Mcp-Method": "tools/call",
            "Mcp-Name": name,
        },
    )
    try:
        with urllib.request.urlopen(request) as response:
            message = json.load(response)
    except urllib.error.HTTPError as exc:
        return f"Error: HTTP {exc.code}: {exc.read().decode(errors='replace')}"
    if "error" in message:
        return f"Error: {message['error']['message']}"
    return "".join(
        block["text"]
        for block in message["result"]["content"]
        if block["type"] == "text"
    )


def search_menu(
    query: str = "", vegetarian_only: bool = False, max_price: float = 0
) -> str:
    """
    Search the diner’s menu of burgers and hot dogs.

    query: case-insensitive substring match on item names, or empty for all.
    vegetarian_only: only return vegetarian items.
    max_price: only return items costing at most this much, or 0 for no limit.
    """
    arguments: dict[str, typing.Any] = {}
    if query:
        arguments["query"] = query
    if vegetarian_only:
        arguments["vegetarian"] = True
    if max_price > 0:
        arguments["max_price"] = max_price
    return _call_tool("search_menu", arguments)


def menu_stats() -> str:
    """Counts and price range of the menu, and orders placed so far."""
    return _call_tool("menu_stats", {})


def place_order(item: str, quantity: int = 1) -> str:
    """
    Place an order for a menu item.

    item: exact item name, as returned by search_menu.
    quantity: how many to order.
    """
    return _call_tool("place_order", {"item": item, "quantity": quantity})
