from django.core.management.base import BaseCommand
from example.core.models import PersonFactory


class Command(BaseCommand):
    help = 'Generate fixture data'

    def handle(self, *args, **options):
        PersonFactory.create_batch(200)
