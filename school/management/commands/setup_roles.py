"""
Create the roles "Адміністратор" and "Викладач" (Django groups with permissions).

Usage:
    python manage.py setup_roles
        - only create / update the two groups (safe to run many times)
    python manage.py setup_roles --teacher-logins --password "SomePassword123"
        - also create a login for every active teacher who has no login yet
          (login = surname in Latin letters, for example "kovalenko")
"""
from django.core.management.base import BaseCommand, CommandError
from django.db import transaction

from school.access import create_teacher_login, setup_role_groups
from school.models import Teacher


class Command(BaseCommand):
    help = "Створює ролі «Адміністратор» і «Викладач» та (за бажанням) логіни викладачів"

    def add_arguments(self, parser):
        parser.add_argument("--teacher-logins", action="store_true",
                            help="Створити логіни для викладачів, у яких їх ще немає")
        parser.add_argument("--password", help="Пароль для нових логінів викладачів")

    @transaction.atomic
    def handle(self, *args, **options):
        setup_role_groups()
        self.stdout.write(self.style.SUCCESS("Ролі «Адміністратор» і «Викладач» готові."))

        if not options["teacher_logins"]:
            return
        if not options["password"] or len(options["password"]) < 8:
            raise CommandError("Вкажіть --password довжиною щонайменше 8 символів.")
        for teacher in Teacher.objects.filter(is_active=True):
            user, created = create_teacher_login(teacher, options["password"])
            state = "створено" if created else "вже був"
            self.stdout.write(f"  {teacher}: логін «{user.username}» ({state})")