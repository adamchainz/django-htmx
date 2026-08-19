Example Application: Django’s Diner
===================================

An example project serving an authenticated MCP endpoint with tools powered by the Django ORM.
The diner’s database holds ``Burger``, ``HotDog``, and ``Order`` models, exposed through three tools defined in ``diner/mcp.py``:

* ``search_menu``—query the menu, with optional filters, e.g. ``vegetarian``, ``max_price``.
* ``menu_stats``—counts and price range.
* ``place_order``—create an ``Order`` row for a menu item.

Setup
-----

Run, in this directory:

.. code-block:: sh

    uv run manage.py migrate
    uv run manage.py runserver

Migrating creates an SQLite database seeded with the menu.
The MCP endpoint is then at http://127.0.0.1:8000/mcp, authenticated with the bearer token in the ``MCPZ_TOKEN`` setting, which for this example project is hard-coded to ``easy-peasy-example-token``.

Smoke-test it with curl:

.. code-block:: sh

    curl -s http://127.0.0.1:8000/mcp \
      -H "Authorization: Bearer easy-peasy-example-token" \
      -H "Content-Type: application/json" \
      -H "MCP-Protocol-Version: 2026-07-28" \
      -H "Mcp-Method: tools/list" \
      -d '{
            "jsonrpc": "2.0", "id": 1, "method": "tools/list",
            "params": {
              "_meta": {
                "io.modelcontextprotocol/protocolVersion": "2026-07-28",
                "io.modelcontextprotocol/clientCapabilities": {}
              }
            }
          }'

Talking to it with an LLM
-------------------------

Any MCP client that speaks protocol revision 2026-07-28 can connect—the server does not implement earlier revisions, so clients that only speak the older ``initialize``-handshake protocol cannot.

`Claude Code <https://claude.com/claude-code>`__ makes a one-command demonstration.
Save this as ``diner-mcp.json``:

.. code-block:: json

    {
      "mcpServers": {
        "diner": {
          "type": "http",
          "url": "http://127.0.0.1:8000/mcp",
          "headers": {
            "Authorization": "Bearer easy-peasy-example-token"
          }
        }
      }
    }

…and, with the dev server running, ask away:

.. code-block:: sh

    claude -p "What vegetarian items does the diner sell, and what do they cost?" \
      --mcp-config diner-mcp.json --strict-mcp-config --allowedTools "mcp__diner__*"

The model calls ``search_menu`` and answers from the database:

.. code-block:: text

    The diner has 3 vegetarian items:

    Burgers:
    - Beet It — $8.90
    - Halloumi Hero — $9.80

    Hot Dogs:
    - Tofu Pup — $6.40

Write tools work too—this creates real ``Order`` rows and reports the IDs and totals back:

.. code-block:: sh

    claude -p "Order two of the diner's biggest burger (most patties) plus one Tofu Pup. Tell me the order IDs and grand total." \
      --mcp-config diner-mcp.json --strict-mcp-config --allowedTools "mcp__diner__*"

Check the result with ``uv run manage.py shell``, or ask a follow-up question against ``menu_stats``.

Talking to it from Python
-------------------------

The official `MCP Python SDK <https://pypi.org/project/mcp/>`__ (version 2+) connects like so:

.. code-block:: python

    import asyncio

    import httpx2
    from mcp.client.client import Client
    from mcp.client.streamable_http import streamable_http_client


    async def main():
        async with httpx2.AsyncClient(
            headers={"Authorization": "Bearer easy-peasy-example-token"}
        ) as http_client:
            transport = streamable_http_client(
                "http://127.0.0.1:8000/mcp", http_client=http_client
            )
            async with Client(transport) as client:
                tools = await client.list_tools()
                print([tool.name for tool in tools.tools])
                result = await client.call_tool("search_menu", {"max_price": 6.50})
                print(result.structured_content)


    asyncio.run(main())

This is also the route for wiring the diner up to other models’ tool-calling APIs: fetch the tool definitions over MCP, hand them to the model, and execute the model’s tool calls through ``client.call_tool()``.

Fully local with LLM and Ollama
-------------------------------

To try the diner with a model running on your own machine, pair `LLM <https://llm.datasette.io/>`__ with `Ollama <https://ollama.com/>`__.

First, with Ollama installed and running, pull a model that supports tool calling—its capabilities, listed by ``ollama show <model>``, must include ``tools``.
``qwen3:4b`` is a capable small default (about 2.5 GB):

.. code-block:: sh

    ollama pull qwen3:4b

LLM connects to the diner through its |functions option|__, using the ``diner_functions.py`` file in this directory: a dependency-free bridge exposing each of the diner’s tools as a Python function that makes one MCP ``tools/call`` request.
With the dev server running, run, in this directory:

.. code-block:: sh

    uvx --with llm-ollama llm -m qwen3:4b \
      --functions diner_functions.py \
      "What vegetarian items does the diner sell, and what do they cost?"

.. |functions option| replace:: ``--functions`` option
__ https://llm.datasette.io/en/stable/tools.html

Add ``--td`` (tools debug) to watch the tool calls and results flow by.
Small models can be hit-and-miss at deciding when to call tools—if yours waffles, try a larger one, or make the instruction explicit: “use the search_menu tool”.

``diner_functions.py`` is also a template for wiring any django-mcpz endpoint into LLM: copy it, point ``ENDPOINT`` and ``TOKEN`` at your server, and write one thin function per tool.

.. note::

    Why not LLM’s MCP plugin?
    As of version 0.4, ``llm-tools-mcp`` speaks the retired ``initialize``-handshake protocol revisions and cannot send the ``Authorization`` header, so it cannot connect to a django-mcpz endpoint.

Things to take apart
--------------------

* ``diner/mcp.py``—the endpoint and tools, using typed msgspec Struct schemas with descriptions and constraints, ``annotations={"readOnlyHint": True}`` on the read-only tools, and ``ToolError`` for the unknown-item case, so the model can self-correct.
* ``diner_functions.py``—the diner’s tools as LLM ``--functions``, showing the raw shape of MCP ``tools/call`` requests along the way.
* ``diner/models.py`` and ``diner/migrations/0002_menu.py``—the models and the seeded menu.
* ``example/settings.py``—the ``MCPZ_TOKEN`` setting; in a real project, load it from the environment or another secret store.
