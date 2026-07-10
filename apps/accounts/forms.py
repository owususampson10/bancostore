from django import forms
from django.contrib.auth.models import Group

from allauth.account.forms import SignupForm
from phonenumber_field.formfields import PhoneNumberField

from .models import CustomerProfile


class CustomerSignupForm(SignupForm):
    """ACCOUNT_USERNAME_REQUIRED=False already makes the base form drop the
    username field itself, so there's no need to remove it here too."""

    full_name = forms.CharField(max_length=150, label="Full name")
    phone_number = PhoneNumberField(label="Phone number")

    def save(self, request):
        user = super().save(request)
        CustomerProfile.objects.create(
            user=user,
            full_name=self.cleaned_data["full_name"],
            phone_number=self.cleaned_data["phone_number"],
        )
        customer_group, _ = Group.objects.get_or_create(name="customer")
        user.groups.add(customer_group)
        return user
