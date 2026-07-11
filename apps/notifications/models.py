from django.db import models


class OTPCode(models.Model):
    class Purpose(models.TextChoices):
        REGISTRATION = "registration", "Registration"
        PASSWORD_RESET = "password_reset", "Password reset"

    phone_number = models.CharField(max_length=20, db_index=True)
    purpose = models.CharField(max_length=20, choices=Purpose.choices)
    code = models.CharField(max_length=6)
    created_at = models.DateTimeField(auto_now_add=True)
    expires_at = models.DateTimeField()
    attempts = models.PositiveIntegerField(default=0)
    verified_at = models.DateTimeField(null=True, blank=True)

    def __str__(self):
        return f"OTPCode<{self.phone_number}, {self.purpose}>"
