from django.conf import settings
from django.db import models, transaction

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


class Address(models.Model):
    """Task 40a (SPEC_PHASE2.md Feature 10). A reusable delivery address a
    customer/distributor can save once and pick again at checkout.
    Deliberately a separate, independent model -- apps.orders.models.Order
    still snapshots address/area/landmark/delivery_zone at creation time
    (Task 17a) and must keep doing so unchanged; this model is only ever a
    pre-fill SOURCE for that snapshot, never something an already-placed
    Order reads from later. Field shape mirrors Order's own address fields
    exactly (same Ghana landmark-based addressing convention), and reuses
    Order.DeliveryZone rather than duplicating the choice set -- imported
    inside the class body (not at module level) to keep this file's own
    import direction obviously one-way (accounts -> orders, never the
    reverse) and easy to audit for the cycle risk that direction would
    otherwise raise."""

    from apps.orders.models import Order as _Order

    user = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="addresses"
    )
    label = models.CharField(
        max_length=50, blank=True, default="", help_text="e.g. Home, Office"
    )
    delivery_zone = models.CharField(max_length=20, choices=_Order.DeliveryZone.choices)
    address = models.CharField(max_length=255)
    area = models.CharField(max_length=255, blank=True, default="")
    landmark = models.CharField(max_length=255, blank=True, default="")
    is_default = models.BooleanField(default=False)
    created_at = models.DateTimeField(auto_now_add=True)

    del _Order

    class Meta:
        ordering = ["-is_default", "-created_at"]

    def __str__(self):
        return f"{self.label or self.address} ({self.user})"

    def save(self, *args, **kwargs):
        # code-review-and-quality (CodeRabbit, PR #73), then a follow-up
        # security-auditor pass: the original bare exclude().update() had
        # no locking at all, so two concurrent requests changing an
        # *existing* default could both read "not yet cleared" and both
        # end up writing a default. A first fix chained select_for_update()
        # directly onto the .update() call -- but that's a genuine no-op in
        # Django: .update() compiles straight to a bulk UPDATE and never
        # evaluates (fetches) the queryset at all, so FOR UPDATE was never
        # actually issued (caught by the security-auditor pass, not
        # CodeRabbit's own automated review). Fixed properly here: list(...)
        # forces the fetch, which is what actually acquires the row lock,
        # before the separate .update() call runs against those now-locked
        # rows. This closes the realistic race (a user already has a
        # default and is switching it). It does NOT close the rarer edge
        # case of two concurrent *first-time* default saves for a user with
        # none yet -- list(...) on an empty queryset locks nothing, and a
        # real DB-level guarantee would need a partial
        # UniqueConstraint(condition=Q(is_default=True)), which MySQL (this
        # project's CI/production database) doesn't support. Accepted as a
        # documented gap, not silently unaddressed -- matches
        # SPEC_PHASE2.md's own reservation of full concurrency rigor for
        # Discount Codes/Escrow, not this low-stakes UX convenience.
        if self.is_default:
            with transaction.atomic():
                list(
                    Address.objects.select_for_update()
                    .filter(user=self.user, is_default=True)
                    .exclude(pk=self.pk)
                )
                Address.objects.filter(user=self.user, is_default=True).exclude(
                    pk=self.pk
                ).update(is_default=False)
                super().save(*args, **kwargs)
        else:
            super().save(*args, **kwargs)


class AdminProfile(models.Model):
    """Wrong-password lockout tracking for admin/staff logins (Task 6) — same
    pattern as Distributor's lockout fields (Task 5), using the same
    django-constance thresholds (MAX_FAILED_LOGIN_ATTEMPTS,
    ACCOUNT_LOCKOUT_DURATION_MINUTES) since SPEC.md applies these rules across
    all account types. Created lazily on first failed/successful admin login
    (see apps/accounts/signals.py) rather than at user-creation time, so it
    works for any is_staff user without needing explicit seeding."""

    user = models.OneToOneField(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name="admin_profile",
    )
    failed_login_attempts = models.PositiveIntegerField(default=0)
    locked_until = models.DateTimeField(null=True, blank=True)

    def __str__(self):
        return f"AdminProfile<{self.user}>"
