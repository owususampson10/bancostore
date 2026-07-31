from django import forms

# Matches apps.accounts.forms's own established shared-widget-class
# convention (INPUT_CLASSES), not a bespoke style for this one form.
INPUT_CLASSES = (
    "w-full px-4 py-3 bg-white border border-outline-variant rounded-lg "
    "font-body-md text-body-md focus:ring-2 focus:ring-secondary "
    "focus:border-secondary outline-none transition-all"
)


class ContactForm(forms.Form):
    name = forms.CharField(
        max_length=120,
        widget=forms.TextInput(
            attrs={"class": INPUT_CLASSES, "placeholder": "Your name"}
        ),
    )
    email = forms.EmailField(
        max_length=254,  # RFC 5321's own address-length limit
        widget=forms.EmailInput(
            attrs={"class": INPUT_CLASSES, "placeholder": "you@example.com"}
        ),
    )
    subject = forms.CharField(
        max_length=200,
        widget=forms.TextInput(
            attrs={"class": INPUT_CLASSES, "placeholder": "How can we help?"}
        ),
    )
    message = forms.CharField(
        max_length=5000,
        widget=forms.Textarea(
            attrs={
                "class": INPUT_CLASSES,
                "rows": 6,
                "placeholder": "Tell us more...",
            }
        ),
    )

    def _reject_header_injection(self, field_name):
        # Doubt-driven-development finding: `subject` flows directly
        # into the outgoing email's Subject header; `name` currently
        # only reaches the plain-text body, not a header, but is kept
        # under this same check as defense-in-depth in case a future
        # change ever adds it to a From-adjacent display name. Django's
        # own SafeMIMEText raises BadHeaderError for a raw \r/\n in a
        # header value -- rejecting here, at form validation, means a
        # crafted submission gets a normal "fix this field" error
        # instead of an unhandled 500.
        value = self.cleaned_data.get(field_name, "")
        if "\r" in value or "\n" in value:
            raise forms.ValidationError("This field cannot contain line breaks.")
        return value

    def clean_name(self):
        return self._reject_header_injection("name")

    def clean_subject(self):
        return self._reject_header_injection("subject")
