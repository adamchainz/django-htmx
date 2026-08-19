Endpoints
=========

.. currentmodule:: django_mcpz.endpoints

An MCP endpoint is a single URL that speaks MCP protocol revision 2026-07-28 over the `Streamable HTTP transport <https://modelcontextprotocol.io/specification/2026-07-28/basic/transports/streamable-http>`__.
Since that revision made the protocol stateless, an endpoint is a plain, POST-only, synchronous Django view.

Define an endpoint by creating an :class:`MCPEndpoint` instance, attaching tool functions with its :meth:`~MCPEndpoint.tool` decorator, and routing its view.
For example, in ``myapp/mcp.py``:

.. code-block:: python

    from typing import Literal

    import msgspec

    from django_mcpz.endpoints import MCPEndpoint

    endpoint = MCPEndpoint(
        name="shop",
        version="1.0.0",
        instructions="Query the shop’s order database.",
    )


    class CountOrdersParams(msgspec.Struct):
        status: Literal["pending", "shipped", "cancelled"] | None = None


    class CountOrdersResult(msgspec.Struct):
        count: int


    @endpoint.tool(
        description="Count Order rows, optionally filtered by status.",
        input_schema=CountOrdersParams,
        output_schema=CountOrdersResult,
    )
    def count_orders(request, params):
        qs = Order.objects.all()
        if params.status is not None:
            qs = qs.filter(status=params.status)
        return CountOrdersResult(count=qs.count())

The Struct types generate the tool’s JSON Schemas, and arguments are validated and converted before the tool runs—see :ref:`endpoints-typed-schemas`.
Plain ``dict`` JSON Schemas work too, wherever you need to write the schema exactly.

…and in ``urls.py``:

.. code-block:: python

    from django.urls import path

    from myapp.mcp import endpoint

    urlpatterns = [
        path("mcp", endpoint.as_view()),
    ]

By default, endpoints require requests to carry a bearer token matching the ``MCPZ_TOKEN`` setting—see :ref:`endpoints-authentication`:

.. code-block:: python

    MCPZ_TOKEN = "..."  # a long random string

API
---

.. class:: MCPEndpoint(*, name, version, title=None, instructions=None, ttl_ms=0, cache_scope="private", auth=bearer_token_auth)

    Represents one MCP endpoint and its registry of tools.

    :param name:
        The server name, reported to clients in the ``io.modelcontextprotocol/serverInfo`` metadata of every result and in ``server/discover`` responses.

    :param version:
        The server version, reported alongside ``name``.

    :param title:
        Optional human-readable server name for display purposes.

    :param instructions:
        Optional natural-language guidance for LLMs on how to use this server effectively, returned from ``server/discover``.

    :param ttl_ms:
        Freshness hint, in milliseconds, attached to ``tools/list`` and ``server/discover`` results as ``ttlMs``.
        Clients may cache those results for this long.
        The default of 0 means “immediately stale”, the protocol’s most conservative value.
        If your tool list is static, set this higher, e.g. ``300_000`` (five minutes).

    :param cache_scope:
        Attached to ``tools/list`` and ``server/discover`` results as ``cacheScope``.
        ``"private"``, the default, restricts caching to the requesting client’s authorization context.
        Use ``"public"`` if responses are the same for all callers and shared intermediaries may cache them.

    :param auth:
        Callable implementing authentication, run on each request before the request body is touched.
        It receives the ``HttpRequest`` and should return ``None`` to allow the request, or an ``HttpResponse`` (such as ``HttpResponse(status=401)``) to reject it.

        Defaults to :func:`~django_mcpz.auth.bearer_token_auth`, which requires a bearer token matching the ``MCPZ_TOKEN`` setting.
        Pass ``None`` to disable authentication, if the endpoint is protected some other way, or genuinely public.
        See :ref:`endpoints-authentication`.

    .. method:: tool(*, description, input_schema, name=None, title=None, output_schema=None, annotations=None, icons=None)

        Decorator that registers the decorated function as an MCP tool on this endpoint.

        :param description:
            Human-readable description of the tool’s functionality, for the calling model.

        :param input_schema:
            The tool’s parameters: either a msgspec-supported type, typically a ``msgspec.Struct`` subclass, or a JSON Schema (2020-12) as a plain ``dict``.

            Given a type, django-mcpz generates the JSON Schema from it, and validates and converts arguments to it before each call, rejecting unknown arguments by default—see :ref:`endpoints-typed-schemas`.
            Given a ``dict``, the schema is served as-is and arguments are not validated.
            For a tool with no parameters, use ``{"type": "object", "additionalProperties": False}``.

            Properties may carry the `x-mcp-header <https://modelcontextprotocol.io/specification/2026-07-28/server/tools#x-mcp-header>`__ extension to have clients mirror their values into ``Mcp-Param-<name>`` HTTP headers, for routing by intermediaries.
            django-mcpz validates such annotations at registration time, raising ``ImproperlyConfigured`` for invalid ones, and validates the headers against the request body on each call, as the specification requires.

        :param name:
            The tool name.
            Defaults to the decorated function’s name.

        :param title:
            Optional human-readable tool name for display purposes.

        :param output_schema:
            Optional description of the tool’s structured output: a msgspec-supported type, from which the JSON Schema is generated, or a JSON Schema ``dict``.
            Return values are not validated against it—return a matching value, most easily an instance of the type itself.

        :param annotations:
            Optional ``dict`` of `tool annotations <https://modelcontextprotocol.io/specification/2026-07-28/server/tools#tool>`__ describing tool behaviour, e.g. ``{"readOnlyHint": True}``.

        :param icons:
            Optional list of `icon objects <https://modelcontextprotocol.io/specification/2026-07-28/basic/index#icons>`__ for display in user interfaces.

        Tool functions receive two arguments: the ``HttpRequest``, and the call’s arguments—the converted instance of a type ``input_schema``, or the raw ``arguments`` ``dict`` for a ``dict`` one.
        Django’s request/response cycle applies as usual, so tools can use the ORM, ``request.user`` (if you use session or other middleware-based authentication), and anything else a view can.

        .. warning::

            With a ``dict`` ``input_schema``, django-mcpz does not validate ``arguments``—clients are expected to conform, but a hostile client can send anything JSON-decodable.
            Treat ``arguments`` values like any other user input, or use a typed ``input_schema``, which does validate.

        The return value determines the ``tools/call`` result:

        * a ``str`` becomes a single text content block.
        * ``None`` becomes an empty content list.
        * Any other value becomes the result’s ``structuredContent``, plus its JSON serialization as a text content block for backwards compatibility, per the specification.
          Values are serialized with msgspec, so tools can return anything it supports, including ``dataclasses`` and msgspec ``Struct`` types.

        If your tool declares an ``output_schema``, its return value must conform.

    .. method:: as_view()

        Return the endpoint’s view function, for use in URLconfs.
        The view is CSRF-exempt, since MCP clients are not browsers and authenticate per-request.

.. exception:: ToolError

    Raise in a tool function to report a *tool execution error*: the message is returned in-band, in a result with ``isError: true``, so the calling model can see it and self-correct.

    .. code-block:: python

        from django_mcpz.endpoints import ToolError


        @endpoint.tool(...)
        def get_order(request, arguments):
            try:
                order = Order.objects.get(id=arguments["order_id"])
            except Order.DoesNotExist:
                raise ToolError(f"No order with id {arguments['order_id']}.")
            ...

    Any other exception raised by a tool is logged to the ``django_mcpz`` logger and reported in-band with a generic message, so internal details do not leak to clients.

.. _endpoints-typed-schemas:

Typed schemas
-------------

Passing a type as ``input_schema`` replaces hand-written JSON Schema ``dict``\s with one typed definition that does three jobs: it generates the schema served from ``tools/list``, validates each call’s arguments, and gives the tool typed attribute access.
Use `msgspec Structs <https://msgspec.dev/en/latest/structs.html>`__:

.. code-block:: python

    from typing import Annotated

    import msgspec


    class SearchParams(msgspec.Struct):
        query: Annotated[str, msgspec.Meta(description="Search terms.", min_length=1)]
        limit: Annotated[int, msgspec.Meta(ge=1, le=100)] = 10


    @endpoint.tool(description="Search products.", input_schema=SearchParams)
    def search(request, params): ...  # params is a validated SearchParams instance

In detail:

* The JSON Schema comes from `msgspec.json.schema() <https://msgspec.dev/en/latest/jsonschema.html>`__.
  Field descriptions and constraints are declared with ``Annotated`` and `msgspec.Meta <https://msgspec.dev/en/latest/constraints.html>`__, and defaults appear in the schema.
  django-mcpz inlines the schema’s top-level ``$ref``, so root properties stay statically reachable; nested Structs remain as ``$defs`` references, and a recursive type keeps its top-level ``$ref``.
* Arguments are validated with ``msgspec.convert()`` before the tool runs.
  Validation failures are reported as in-band tool execution errors (``isError: true``), with msgspec’s message—e.g. ``Invalid arguments: Expected `int`, got `str` - at `$.limit```—so the calling model can self-correct, as the specification recommends for input validation errors.
* Unknown arguments are rejected by default: the generated schema gains ``additionalProperties: false``, which the MCP specification recommends, and calls sending undeclared top-level arguments receive an in-band error.
  Tools are called by language models, for which a silently ignored misspelt argument would be an invisible bug.
  This applies to the top level only—extras inside a nested object follow the nested Struct’s own settings, so declare ``forbid_unknown_fields=True`` on nested Structs for depth.
  For a tool that deliberately accepts open-ended arguments, use a ``dict`` schema.
* ``x-mcp-header`` annotations are declared with ``msgspec.Meta(extra_json_schema={"x-mcp-header": "..."})``, on top-level fields only—a nested Struct’s fields end up behind a ``$ref``, where the specification does not allow the annotation.
* Any type msgspec can generate a schema for works, not just Structs—a ``TypedDict``, for example, if you would rather the tool keep receiving a plain (but now validated) ``dict``.
* A matching ``output_schema`` type pairs well: return an instance and msgspec serializes it directly into ``structuredContent``.

.. _endpoints-authentication:

Authentication
--------------

MCP endpoints expose application internals to network callers, so they are authenticated by default.
The `MCP authorization specification <https://modelcontextprotocol.io/specification/2026-07-28/basic/authorization>`__ defines full OAuth 2.1 resource server behaviour, which is beyond django-mcpz’s scope, but static bearer tokens are a pragmatic alternative, since MCP clients generally support custom headers.
Endpoints cannot reuse Django’s session authentication: MCP clients are not browsers, do not hold session cookies, and per MCP’s statelessness every request must carry its credentials anyway.

The default: a shared token
~~~~~~~~~~~~~~~~~~~~~~~~~~~

.. currentmodule:: django_mcpz.auth

By default, endpoints authenticate with :func:`bearer_token_auth`.

.. function:: bearer_token_auth(request)

    Reject requests, with a 401 response, unless their ``Authorization`` header carries the bearer token in the ``MCPZ_TOKEN`` setting, compared in constant time.

    If the setting is missing or empty, ``ImproperlyConfigured`` is raised—when the endpoint is defined, typically at import time, so misconfiguration fails at startup rather than on the first request.

Set ``MCPZ_TOKEN`` to a long random string, generated with, for example:

.. code-block:: sh

    python -c "import secrets; print(secrets.token_urlsafe(32))"

…and configure your MCP client to send it, e.g. ``Authorization: Bearer <token>``.
Like ``SECRET_KEY``, load it from the environment or another secret store rather than committing it to settings files.

A single shared token suits a deployment with one trusted client.
For several clients, per-client tokens let you revoke one client without rotating the rest—see :ref:`endpoints-database-tokens` below.

Disabling authentication
~~~~~~~~~~~~~~~~~~~~~~~~

Pass ``auth=None`` if the endpoint is protected some other way—an authenticating reverse proxy, other middleware—or is genuinely public:

.. code-block:: python

    endpoint = MCPEndpoint(name="shop", version="1.0.0", auth=None)

.. _endpoints-database-tokens:

Per-client tokens from the database
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~

For finer-grained control, pass a custom ``auth`` callable that checks tokens against a database table.
Store a hash of each token, not the token itself, so a leaked database dump does not reveal usable credentials—like Django does for passwords.

For example, with this model:

.. code-block:: python

    import hashlib
    import secrets

    from django.db import models


    class MCPToken(models.Model):
        name = models.CharField(max_length=100)
        token_digest = models.CharField(max_length=64, unique=True)
        created_at = models.DateTimeField(auto_now_add=True)

        @classmethod
        def generate(cls, name):
            """Create a token, returning its plain value, shown only once."""
            token = secrets.token_urlsafe(32)
            cls.objects.create(name=name, token_digest=cls.digest(token))
            return token

        @staticmethod
        def digest(token):
            return hashlib.sha256(token.encode()).hexdigest()

…authenticate with:

.. code-block:: python

    from django.http import HttpResponse

    from myapp.models import MCPToken


    def database_token_auth(request):
        header = request.headers.get("Authorization", "")
        rejection = HttpResponse(status=401, headers={"WWW-Authenticate": "Bearer"})
        if not header.startswith("Bearer "):
            return rejection
        token = header.removeprefix("Bearer ")
        try:
            request.mcp_token = MCPToken.objects.get(token_digest=MCPToken.digest(token))
        except MCPToken.DoesNotExist:
            return rejection
        return None


    endpoint = MCPEndpoint(name="shop", version="1.0.0", auth=database_token_auth)

Looking tokens up by hash is not vulnerable to timing attacks the way direct string comparison is, since the attacker-controlled value passes through SHA-256 before touching the database index.
Attaching the matched ``MCPToken`` to the request lets tool functions vary behaviour per client, such as restricting which data each token may query.

Protocol support
----------------

django-mcpz implements the server side of MCP revision 2026-07-28:

* ``server/discover``, reporting supported versions, the ``tools`` capability, and any ``instructions``.
* ``tools/list``, returning all registered tools in registration order (deterministic, per the specification’s caching recommendation), with ``ttlMs`` and ``cacheScope`` hints.
* ``tools/call``, dispatching to your tool functions.
* Per-request protocol version negotiation: unsupported versions receive an ``UnsupportedProtocolVersionError`` (code ``-32022``) listing supported versions.
* Request metadata validation: the required ``_meta`` fields, and the ``MCP-Protocol-Version``, ``Mcp-Method``, ``Mcp-Name``, and ``Mcp-Param-*`` headers are checked against the request body, with mismatches rejected as ``HeaderMismatch`` errors (code ``-32020``), including support for the Base64 sentinel value encoding.
* ``Origin`` header validation against ``ALLOWED_HOSTS``, to prevent DNS rebinding attacks, rejecting invalid origins with HTTP 403.
* Notifications are acknowledged with HTTP 202, and non-POST requests rejected with HTTP 405, as the transport requires.

Not implemented, by design or not yet:

* Per-request SSE streaming responses, which the specification makes optional.
  Responses are always single JSON objects, which suits quick, synchronous tools.
* ``subscriptions/listen`` long-lived notification streams, which need a streaming (and realistically ASGI) response.
* Resources, prompts, completions, and multi round-trip requests (elicitation and sampling).
* Async (ASGI) support and async tools—planned for a future version.
