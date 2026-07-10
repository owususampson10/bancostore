from django.conf import settings
from django.db import models

from phonenumber_field.modelfields import PhoneNumberField


class CustomerProfile(models.Model):
    user = models.OneToOneField(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name="customer_profile",
    )
    full_name = models.CharField(max_length=150)
    phone_number = PhoneNumberField()

    def __str__(self):
        return f"CustomerProfile<{self.user}>"
