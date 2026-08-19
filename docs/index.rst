django-mcpz documentation
=========================

*Easy peasy MCP.*

django-mcpz lets you define `Model Context Protocol (MCP) <https://modelcontextprotocol.io/>`__ endpoints in your Django project and attach tool functions to them, so AI assistants can call into your application.

It implements MCP protocol revision 2026-07-28 (“MCP 2”), which made the protocol stateless: no initialization handshake, no sessions, no server-sent event streams.
That means a plain, synchronous Django view is a fully compliant MCP server—WSGI works great, no ASGI required.
JSON serialization and deserialization use `msgspec <https://msgspec.dev/>`__, via `django-msgspec <https://django-msgspec.readthedocs.io/>`__, for speed.

.. toctree::
   :maxdepth: 2
   :caption: Contents

   installation
   endpoints
   middleware
   example_project
   changelog
