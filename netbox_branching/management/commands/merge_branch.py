import json
import subprocess
import tempfile

from django.core.exceptions import ObjectDoesNotExist, ValidationError
from django.core.management.base import BaseCommand, CommandError
from django.db import DEFAULT_DB_ALIAS

from netbox_branching.models import Branch


def get_input_from_editor(editor, initial_data=''):
    """
    Present data to the user for modification using a system editor.
    """
    with tempfile.NamedTemporaryFile(suffix=".json", mode="w+") as temp_file:
        temp_file.write(initial_data)
        temp_file.flush()

        subprocess.call([editor, temp_file.name])

        temp_file.seek(0)
        return temp_file.read()


class Command(BaseCommand):
    help = "Interactively merge a branch"

    def add_arguments(self, parser):
        parser.add_argument('branch', help="Name of the branch to merge")
        parser.add_argument('--editor', default='editor', help="Command line editor")
        parser.add_argument("--commit", action='store_true', dest='commit', help="Commit changes when finished")

    def handle(self, *args, **options):
        self.editor = options['editor']

        try:
            branch = Branch.objects.get(name=options['branch'])
        except ObjectDoesNotExist:
            raise CommandError(f"Branch not found: {options['branch']}")

        self.stdout.write(f"Merging branch {branch}")

        branch.merge(user=None, commit=options['commit'], merge_func=self._apply_change)

    def _apply_change(self, change, logger):
        """
        Apply an ObjectChange
        """
        try:
            change.apply(using=DEFAULT_DB_ALIAS, logger=logger)
        except ValidationError as e:
            self.stdout.write(f"Validation has failed for this object.")
            original_data = json.dumps(change.postchange_data, indent=4)
            self.stdout.write(original_data)
            for k, v in e.message_dict.items():
                self.stdout.write(f"  {k}: {v}")

            # Prompt user for action
            action = self.get_user_selection([
                "Modify the change record data",
                "Exit",
            ])
            if action != 1:
                raise e

            # Prompt user to manipulate data
            new_data = get_input_from_editor(self.editor, initial_data=original_data)
            change.postchange_data = json.loads(new_data)

            # Invalidate cached data
            if 'postchange_data_clean' in change.__dict__:
                del change.__dict__['postchange_data_clean']

            self._apply_change(change, logger)

    def get_user_selection(self, options):
        """
        Prompt the user to select one of several options.
        """
        self.stdout.write("Select an option:")
        for i, option in enumerate(options, start=1):
            self.stdout.write(f"{i}. {option}")

        while True:
            try:
                choice = int(input("> "))
                if choice in range(1, len(options) + 1):
                    return choice
                raise ValueError()
            except ValueError:
                self.stdout.write("Invalid choice.")
