from django import forms

# Matches apps.accounts.forms's own established shared-widget-class
# convention (INPUT_CLASSES), not a bespoke style for this one form.
INPUT_CLASSES = (
    "w-full px-4 py-3 bg-white border border-outline-variant rounded-lg "
    "font-body-md text-body-md focus:ring-2 focus:ring-secondary "
    "focus:border-secondary outline-none transition-all"
)


class ContactForm(forms.Form):
    # CodeRabbit finding (PR #58): the error text under each field had no
    # id, and the field itself had no aria-describedby/aria-invalid, so a
    # screen-reader user submitting an invalid field got no indication of
    # which field failed or why. aria-describedby is static -- each field's
    # error <p> always renders in the template (empty when there's no
    # error), so the id always resolves to something, per the standard
    # aria-describedby-can-point-at-empty-content pattern. aria-invalid is
    # set dynamically in clean() below, once real per-field error state
    # exists.
    name = forms.CharField(
        max_length=120,
        widget=forms.TextInput(
            attrs={
                "class": INPUT_CLASSES,
                "placeholder": "Your name",
                "aria-describedby": "id_name-error",
            }
        ),
    )
    email = forms.EmailField(
        max_length=254,  # RFC 5321's own address-length limit
        widget=forms.EmailInput(
            attrs={
                "class": INPUT_CLASSES,
                "placeholder": "you@example.com",
                "aria-describedby": "id_email-error",
            }
        ),
    )
    subject = forms.CharField(
        max_length=200,
        widget=forms.TextInput(
            attrs={
                "class": INPUT_CLASSES,
                "placeholder": "How can we help?",
                "aria-describedby": "id_subject-error",
            }
        ),
    )
    message = forms.CharField(
        max_length=5000,
        widget=forms.Textarea(
            attrs={
                "class": INPUT_CLASSES,
                "rows": 6,
                "placeholder": "Tell us more...",
                "aria-describedby": "id_message-error",
            }
        ),
    )

    def clean(self):
        # Runs after every field's own clean_<field>(), so self.errors
        # already reflects field-level validation failures at this point --
        # safe to mark exactly those widgets aria-invalid for the re-render
        # that follows a failed POST. Never runs for a fresh, unbound form
        # (a GET request never calls full_clean()), so a first-load field
        # is never marked invalid before the user has submitted anything.
        cleaned_data = super().clean()
        for field_name in self.errors:
            if field_name in self.fields:
                self.fields[field_name].widget.attrs["aria-invalid"] = "true"
        return cleaned_data

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
