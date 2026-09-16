from django import forms

from phonenumber_field.formfields import PhoneNumberField

from .models import Order

# Matches apps/accounts/forms.py's own INPUT_CLASSES convention.
INPUT_CLASSES = (
    "w-full p-4 rounded-lg border border-outline-variant bg-surface-bright "
    "focus:border-primary focus:ring-1 focus:ring-primary outline-none transition-all"
)


class CheckoutForm(forms.Form):
    """Task 17c. doubt-driven-development (design review before
    implementation): clean() must enforce the same cross-field rule as
    Order's own order_delivery_address_required_iff_home_delivery
    CheckConstraint, or an otherwise-valid form submission hits an
    unhandled IntegrityError at Order.objects.create() instead of a
    clean form error."""

    full_name = forms.CharField(
        max_length=255,
        widget=forms.TextInput(
            attrs={"class": INPUT_CLASSES, "placeholder": "John Doe"}
        ),
    )
    phone_number = PhoneNumberField(
        widget=forms.TextInput(
            attrs={"class": INPUT_CLASSES, "placeholder": "+233 XX XXX XXXX"}
        )
    )
    # Task 59: required, unlike the optional email field on the two
    # distributor payment flows. A customer gets an email receipt AND an
    # SMS one on a confirmed order (apps/orders/services.py's
    # _send_confirmation_notifications), and the email half is impossible
    # without a real address. Order.email itself stays blank=True at the
    # model level -- orders placed before this task legitimately have no
    # email, and a schema change would invalidate them.
    email = forms.EmailField(
        widget=forms.EmailInput(
            attrs={"class": INPUT_CLASSES, "placeholder": "john@example.com"}
        ),
    )
    delivery_method = forms.ChoiceField(
        choices=Order.DeliveryMethod.choices,
        widget=forms.RadioSelect,
    )
    # Not required at the field level -- required only for home delivery,
    # enforced in clean() below (mirrors the model's own conditional
    # requirement, not a blanket one).
    delivery_zone = forms.ChoiceField(
        choices=[("", "Select a delivery zone")] + list(Order.DeliveryZone.choices),
        required=False,
        widget=forms.Select(attrs={"class": INPUT_CLASSES}),
    )
    address = forms.CharField(
        max_length=255,
        required=False,
        widget=forms.TextInput(
            attrs={
                "class": INPUT_CLASSES,
                "placeholder": "Digital Address or Street Name",
            }
        ),
    )
    area = forms.CharField(
        max_length=255,
        required=False,
        widget=forms.TextInput(
            attrs={"class": INPUT_CLASSES, "placeholder": "e.g. East Legon"}
        ),
    )
    landmark = forms.CharField(
        max_length=255,
        required=False,
        widget=forms.TextInput(
            attrs={"class": INPUT_CLASSES, "placeholder": "e.g. Near the Shell Station"}
        ),
    )
    # Task 43b. Real enforcement (does this code exist/still work/apply to
    # this customer) happens server-side in
    # apps.promotions.services.redeem_discount_code, called from
    # create_pending_order -- this field is just UX, matching this
    # codebase's established "form is UX, service is the real boundary"
    # convention (see DiscountCodeForm's own docstring, apps/admin_portal/
    # forms.py). checkout_view surfaces a rejection via form.add_error.
    discount_code = forms.CharField(
        max_length=32,
        required=False,
        widget=forms.TextInput(
            attrs={"class": INPUT_CLASSES, "placeholder": "Enter code"}
        ),
    )

    def clean(self):
        cleaned_data = super().clean()
        delivery_method = cleaned_data.get("delivery_method")

        if delivery_method == Order.DeliveryMethod.PICKUP:
            # Pickup carries no address -- discard anything submitted for
            # these fields rather than erroring, matching the model
            # constraint's own "pickup implies blank" shape.
            cleaned_data["delivery_zone"] = ""
            cleaned_data["address"] = ""
            cleaned_data["area"] = ""
        elif delivery_method == Order.DeliveryMethod.HOME_DELIVERY:
            for field in ("delivery_zone", "address", "area"):
                if not cleaned_data.get(field):
                    self.add_error(field, "This field is required for home delivery.")

        return cleaned_data
