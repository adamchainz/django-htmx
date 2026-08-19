from __future__ import annotations

from decimal import Decimal

from django.db import migrations

MENU_BURGERS = [
    # (name, price, patties, vegetarian)
    ("The Classic Smash", "8.50", 1, False),
    ("Double Trouble", "11.90", 2, False),
    ("The Full Django", "14.20", 3, False),
    ("Halloumi Hero", "9.80", 1, True),
    ("Beet It", "8.90", 1, True),
]

MENU_HOT_DOGS = [
    # (name, price, length_cm, vegetarian)
    ("Plain Jane", "4.50", 15, False),
    ("Chicago Classic", "7.20", 20, False),
    ("Chili Cheese Blaster", "8.10", 20, False),
    ("Footlong Fiesta", "9.60", 30, False),
    ("Tofu Pup", "6.40", 15, True),
]


def add_menu(apps, schema_editor):
    Burger = apps.get_model("diner", "Burger")
    Burger.objects.bulk_create(
        Burger(name=name, price=Decimal(price), patties=patties, vegetarian=vegetarian)
        for name, price, patties, vegetarian in MENU_BURGERS
    )
    HotDog = apps.get_model("diner", "HotDog")
    HotDog.objects.bulk_create(
        HotDog(
            name=name, price=Decimal(price), length_cm=length_cm, vegetarian=vegetarian
        )
        for name, price, length_cm, vegetarian in MENU_HOT_DOGS
    )


def remove_menu(apps, schema_editor):
    apps.get_model("diner", "Burger").objects.all().delete()
    apps.get_model("diner", "HotDog").objects.all().delete()


class Migration(migrations.Migration):
    dependencies = [
        ("diner", "0001_initial"),
    ]

    operations = [
        migrations.RunPython(add_menu, remove_menu),
    ]
