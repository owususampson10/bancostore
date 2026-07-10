from django.conf import settings
from django.db import models


class Distributor(models.Model):
    class KycStatus(models.TextChoices):
        PENDING = "pending", "Pending"
        APPROVED = "approved", "Approved"
        REJECTED = "rejected", "Rejected"

    user = models.OneToOneField(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name="distributor",
    )
    sponsor = models.ForeignKey(
        "self",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="referrals",
    )
    rank = models.CharField(max_length=50, blank=True, default="")
    kyc_status = models.CharField(
        max_length=20, choices=KycStatus.choices, default=KycStatus.PENDING
    )
    ir_id = models.CharField(max_length=50, unique=True, null=True, blank=True)

    def __str__(self):
        return f"Distributor<{self.user}>"
