from __future__ import annotations

from typing import Annotated, Any

import msgspec
from django.http import HttpRequest

from diner.models import Burger, HotDog, Order
from django_mcpz.endpoints import MCPEndpoint, ToolError

endpoint = MCPEndpoint(
    name="djangos-diner",
    version="1.0.0",
    title="Django’s Diner",
    instructions=(
        "Django’s Diner sells burgers and hot dogs. Search the menu with"
        " search_menu, check totals with menu_stats, and place orders with"
        " place_order, using exact item names from the menu."
    ),
    ttl_ms=300_000,
)


class SearchMenuParams(msgspec.Struct):
    query: Annotated[
        str | None,
        msgspec.Meta(description="Case-insensitive substring match on item names."),
    ] = None
    vegetarian: Annotated[
        bool | None,
        msgspec.Meta(description="Only (non-)vegetarian items."),
    ] = None
    max_price: (
        Annotated[
            float,
            msgspec.Meta(description="Only items costing at most this much.", ge=0.0),
        ]
        | None
    ) = None


@endpoint.tool(
    description="Search the menu of burgers and hot dogs.",
    input_schema=SearchMenuParams,
    annotations={"readOnlyHint": True},
)
def search_menu(request: HttpRequest, params: SearchMenuParams) -> dict[str, Any]:
    burgers = Burger.objects.all()
    hot_dogs = HotDog.objects.all()
    if params.query is not None:
        burgers = burgers.filter(name__icontains=params.query)
        hot_dogs = hot_dogs.filter(name__icontains=params.query)
    if params.vegetarian is not None:
        burgers = burgers.filter(vegetarian=params.vegetarian)
        hot_dogs = hot_dogs.filter(vegetarian=params.vegetarian)
    if params.max_price is not None:
        burgers = burgers.filter(price__lte=params.max_price)
        hot_dogs = hot_dogs.filter(price__lte=params.max_price)
    return {
        "burgers": [
            {
                "name": burger.name,
                "price": float(burger.price),
                "patties": burger.patties,
                "vegetarian": burger.vegetarian,
            }
            for burger in burgers
        ],
        "hot_dogs": [
            {
                "name": hot_dog.name,
                "price": float(hot_dog.price),
                "length_cm": hot_dog.length_cm,
                "vegetarian": hot_dog.vegetarian,
            }
            for hot_dog in hot_dogs
        ],
    }


@endpoint.tool(
    description="Counts and price range of the menu, and orders placed so far.",
    input_schema={"type": "object", "additionalProperties": False},
    annotations={"readOnlyHint": True},
)
def menu_stats(request: HttpRequest, arguments: dict[str, Any]) -> dict[str, Any]:
    prices = [
        *Burger.objects.values_list("price", flat=True),
        *HotDog.objects.values_list("price", flat=True),
    ]
    return {
        "burgers": Burger.objects.count(),
        "hot_dogs": HotDog.objects.count(),
        "cheapest": float(min(prices)) if prices else None,
        "priciest": float(max(prices)) if prices else None,
        "orders_placed": Order.objects.count(),
    }


class PlaceOrderParams(msgspec.Struct):
    item: Annotated[
        str,
        msgspec.Meta(description="Exact item name, as returned by search_menu."),
    ]
    quantity: Annotated[int, msgspec.Meta(ge=1, le=100)] = 1


@endpoint.tool(
    description="Place an order for a menu item.",
    input_schema=PlaceOrderParams,
)
def place_order(request: HttpRequest, params: PlaceOrderParams) -> dict[str, Any]:
    item = (
        Burger.objects.filter(name=params.item).first()
        or HotDog.objects.filter(name=params.item).first()
    )
    if item is None:
        raise ToolError(
            f"No menu item named {params.item!r}. Use search_menu to find"
            " exact item names."
        )
    order = Order.objects.create(
        item_name=item.name,
        quantity=params.quantity,
        total=item.price * params.quantity,
    )
    return {
        "order_id": order.id,
        "item": order.item_name,
        "quantity": order.quantity,
        "total": float(order.total),
    }
