Middleware
==========

.. currentmodule:: django_mcpz.middleware

``MCPMiddleware`` attaches ``request.mcp``, an instance of :class:`MCPDetails`, to every request, giving convenient access to the MCP request metadata headers that the `Streamable HTTP transport <https://modelcontextprotocol.io/specification/2026-07-28/basic/transports/streamable-http#request-metadata>`__ mirrors from the JSON-RPC body.
This is useful in other middleware, logging, or rate-limiting code that wants to inspect MCP traffic without parsing request bodies—the same job the headers exist to do for load balancers and gateways.

The middleware supports both sync and async modes.

Note that :doc:`endpoint views <endpoints>` do not require the middleware: they validate the headers against the request body themselves.

.. class:: MCPDetails

    .. method:: __bool__()

        ``True`` if the request has the ``MCP-Protocol-Version`` header, which MCP clients send on every request POST.
        (Notification POSTs have no defined header requirements, so they may not be detected.)
        This allows the pattern:

        .. code-block:: python

            if request.mcp:
                ...

    .. attribute:: protocol_version
        :type: str | None

        The ``MCP-Protocol-Version`` header, or ``None``.

    .. attribute:: method
        :type: str | None

        The JSON-RPC method name, from the ``Mcp-Method`` header, e.g. ``"tools/call"``, or ``None``.

    .. attribute:: name
        :type: str | None

        The tool name, resource URI, or prompt name, from the ``Mcp-Name`` header, or ``None``.
        Values in the specification’s Base64 sentinel format (``=?base64?…?=``) are decoded; a malformed encoded value reads as ``None``.

    .. attribute:: params
        :type: dict[str, str]

        Tool parameter values mirrored into ``Mcp-Param-<name>`` headers via `x-mcp-header <https://modelcontextprotocol.io/specification/2026-07-28/server/tools#x-mcp-header>`__ annotations, keyed by ``<name>``, with Base64 sentinel values decoded.
        Since HTTP header names are case-insensitive, keys appear as Django reports them (title-cased); prefer :meth:`param` for lookups.

    .. method:: param(name)

        Return the value of the ``Mcp-Param-<name>`` header, matched case-insensitively and decoded, or ``None``.
