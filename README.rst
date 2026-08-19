===========
django-mcpz
===========

.. image:: https://img.shields.io/readthedocs/django-mcpz?style=for-the-badge
   :target: https://django-mcpz.readthedocs.io/en/latest/

.. image:: https://img.shields.io/github/actions/workflow/status/adamchainz/django-mcpz/main.yml.svg?branch=main&style=for-the-badge
   :target: https://github.com/adamchainz/django-mcpz/actions?workflow=CI

.. image:: https://img.shields.io/badge/Coverage-100%25-success?style=for-the-badge
   :target: https://github.com/adamchainz/django-mcpz/actions?workflow=CI

.. image:: https://img.shields.io/pypi/v/django-mcpz.svg?style=for-the-badge
   :target: https://pypi.org/project/django-mcpz/

.. image:: https://img.shields.io/badge/code%20style-black-000000.svg?style=for-the-badge
   :target: https://github.com/psf/black

.. image:: https://img.shields.io/badge/pre--commit-enabled-brightgreen?logo=pre-commit&logoColor=white&style=for-the-badge
   :target: https://github.com/pre-commit/pre-commit
   :alt: pre-commit

----

Easy peasy MCP.
Define `Model Context Protocol (MCP) <https://modelcontextprotocol.io/>`__ endpoints in your Django project and attach tool functions to them, so AI assistants can call into your application.

django-mcpz implements MCP protocol revision 2026-07-28 (“MCP 2”), which made the protocol stateless: no initialization handshake, no sessions, no server-sent event streams.
That means a plain, synchronous Django view is a fully compliant MCP server—WSGI works great, no ASGI required.
JSON serialization and deserialization use `msgspec <https://msgspec.dev/>`__, via `django-msgspec <https://django-msgspec.readthedocs.io/>`__, for speed.

A quick taste—define an endpoint with a tool in, say, ``myapp/mcp.py``:

.. code-block:: python

    from typing import Literal

    import msgspec

    from django_mcpz.endpoints import MCPEndpoint

    endpoint = MCPEndpoint(name="shop", version="1.0.0")


    class CountOrdersParams(msgspec.Struct):
        status: Literal["pending", "shipped", "cancelled"] | None = None


    class CountOrdersResult(msgspec.Struct):
        count: int


    @endpoint.tool(
        description="Count orders, optionally filtered by status.",
        input_schema=CountOrdersParams,
        output_schema=CountOrdersResult,
    )
    def count_orders(request, params):
        qs = Order.objects.all()
        if params.status is not None:
            qs = qs.filter(status=params.status)
        return CountOrdersResult(count=qs.count())

The Struct types generate the tool’s JSON Schemas, and arguments are validated before the tool runs—plain ``dict`` schemas work too.

…route it in ``urls.py``:

.. code-block:: python

    from myapp.mcp import endpoint

    urlpatterns = [
        path("mcp", endpoint.as_view()),
    ]

…and set the bearer token that clients must send, since endpoints are authenticated by default:

.. code-block:: python

    MCPZ_TOKEN = "..."  # a long random string

Point any MCP client at the endpoint, or smoke-test it with curl:

.. code-block:: sh

    curl -s http://localhost:8000/mcp \
      -H "Authorization: Bearer $MCPZ_TOKEN" \
      -H "Content-Type: application/json" \
      -H "MCP-Protocol-Version: 2026-07-28" \
      -H "Mcp-Method: tools/call" \
      -H "Mcp-Name: count_orders" \
      -d '{
            "jsonrpc": "2.0",
            "id": 1,
            "method": "tools/call",
            "params": {
              "name": "count_orders",
              "arguments": {"status": "shipped"},
              "_meta": {
                "io.modelcontextprotocol/protocolVersion": "2026-07-28",
                "io.modelcontextprotocol/clientCapabilities": {}
              }
            }
          }'

…which responds:

.. code-block:: json

    {
      "jsonrpc": "2.0",
      "id": 1,
      "result": {
        "resultType": "complete",
        "isError": false,
        "content": [{"type": "text", "text": "{\"count\":42}"}],
        "structuredContent": {"count": 42},
        "_meta": {
          "io.modelcontextprotocol/serverInfo": {"name": "shop", "version": "1.0.0"}
        }
      }
    }

Documentation
-------------

Please see https://django-mcpz.readthedocs.io/.
