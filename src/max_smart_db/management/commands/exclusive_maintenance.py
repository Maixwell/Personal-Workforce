"""Perform one approved Max Smart migration boundary transition."""

from django.core.management.base import BaseCommand, CommandError

from max_smart_db.maintenance import (
    MaintenanceError,
    run_exclusive_maintenance,
)


class Command(BaseCommand):
    help = "Perform one scanner-verified exclusive migration transition."
    requires_migrations_checks = False
    requires_system_checks = []

    def add_arguments(self, parser):
        parser.add_argument(
            "direction",
            choices=("forward", "reverse"),
            help="Apply or reverse exactly one approved V1 migration.",
        )

    def handle(self, *args, **options):
        try:
            transition = run_exclusive_maintenance(options["direction"])
        except MaintenanceError as error:
            raise CommandError(
                f"Exclusive maintenance refused ({error.code}): {error}"
            ) from error
        self.stdout.write(
            self.style.SUCCESS(
                f"{transition.before} -> {transition.after} "
                f"({transition.migration} {transition.direction})"
            )
        )
