from django.contrib.auth import get_user_model
from django.contrib.auth.models import Group
from django.core.management.base import BaseCommand

from apps.distributors.models import Distributor

User = get_user_model()

STUB_PASSWORD = "bancostore-dev-only"

STUB_USERS = {
    "customer": {"username": "stub_customer", "email": "stub_customer@example.test"},
    "distributor": {
        "username": "stub_distributor",
        "email": "stub_distributor@example.test",
    },
    "admin": {
        "username": "stub_admin",
        "email": "stub_admin@example.test",
        "is_staff": True,
    },
}


class Command(BaseCommand):
    help = (
        "Seed the customer/distributor/admin groups and one stub user per role, "
        "for local testing only."
    )

    def handle(self, *args, **options):
        for role, fields in STUB_USERS.items():
            group, _ = Group.objects.get_or_create(name=role)

            username = fields["username"]
            user, created = User.objects.get_or_create(
                username=username,
                defaults={
                    "email": fields["email"],
                    "is_staff": fields.get("is_staff", False),
                },
            )
            if created:
                user.set_password(STUB_PASSWORD)
                user.save()

            user.groups.add(group)

            if role == "distributor":
                Distributor.objects.get_or_create(
                    user=user, defaults={"phone_number": "+233200000000"}
                )

        self.stdout.write(self.style.SUCCESS("Seeded roles and stub users."))
