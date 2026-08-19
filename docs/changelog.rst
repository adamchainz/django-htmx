=========
Changelog
=========

Unreleased
----------

* Initial release, supporting MCP protocol revision 2026-07-28:

  * ``django_mcpz.endpoints.MCPEndpoint`` for defining MCP endpoints and attaching tool functions to them.
  * ``django_mcpz.middleware.MCPMiddleware`` for ``request.mcp`` access to MCP request metadata headers.
