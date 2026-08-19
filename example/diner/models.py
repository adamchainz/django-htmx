from __future__ import annotations

from django.db import models


class Burger(models.Model):
    name = models.CharField(max_length=100, unique=True)
    price = models.DecimalField(max_digits=5, decimal_places=2)
    patties = models.PositiveSmallIntegerField(default=1)
    vegetarian = models.BooleanField(default=False)

    class Meta:
        ordering = ["name"]

    def __str__(self) -> str:
        return self.name


class HotDog(models.Model):
    name = models.CharField(max_length=100, unique=True)
    price = models.DecimalField(max_digits=5, decimal_places=2)
    length_cm = models.PositiveSmallIntegerField()
    vegetarian = models.BooleanField(default=False)

    class Meta:
        ordering = ["name"]

    def __str__(self) -> str:
        return self.name


class Order(models.Model):
    item_name = models.CharField(max_length=100)
    quantity = models.PositiveSmallIntegerField()
    total = models.DecimalField(max_digits=7, decimal_places=2)
    placed_at = models.DateTimeField(auto_now_add=True)

    def __str__(self) -> str:
        return f"{self.quantity}× {self.item_name}"
