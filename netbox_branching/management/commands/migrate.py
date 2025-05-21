from django.core.exceptions import ObjectDoesNotExist
from django.core.management.base import CommandError, no_translations
from django.core.management.commands.migrate import Command as Command_
from django.db import DEFAULT_DB_ALIAS

from netbox_branching.models import Branch


class Command(Command_):

    def add_arguments(self, parser):
        super().add_arguments(parser)

        # Add support for specifying a branch to migrate
        parser.add_argument(
            "--branch",
            help="Specifies the name of a branch to migrate. Cannot be used with --database.",
        )

    @no_translations
    def handle(self, *args, **options):
        self.verbosity = options["verbosity"]

        # Cannot set branch & database together (--branch overrides --database)
        if options['branch'] is not None and options['database'] != DEFAULT_DB_ALIAS:
            raise CommandError(
                "The --branch and --database arguments are mutually exclusive."
            )

        if options['branch']:
            try:
                branch = Branch.objects.get(name=options['branch'])
            except ObjectDoesNotExist:
                raise CommandError(f"Branch name not found: {options['branch']}")
            if not branch.ready:
                raise CommandError(
                    f"Branch {branch.name} is not ready to migrate (status: {branch.get_status_display()})"
                )
            options['database'] = branch.connection_name
            if self.verbosity >= 1:
                self.stdout.write("Migrating branch: ", self.style.MIGRATE_HEADING, ending='')
                self.stdout.write(branch.name, self.style.MIGRATE_LABEL)

        return super().handle(*args, **options)
