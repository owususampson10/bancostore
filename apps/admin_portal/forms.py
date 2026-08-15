from django import forms
from django.core.exceptions import ValidationError
from django.forms import inlineformset_factory

from constance import config

from apps.catalog.models import Category, Product, ProductImage, ProductVariant
from apps.notifications.models import NotificationTemplate
from apps.notifications.rendering import _PLACEHOLDER_RE
from apps.notifications.template_registry import PLACEHOLDERS_BY_KEY
from apps.pages.models import SocialMediaLink
from apps.promotions.models import Banner, DiscountCode

# Shared Tailwind classes so every plain text/number/select/textarea input
# across the Catalog Management forms matches the admin_portal design
# system's real tokens (static/src/main.css) instead of Django's unstyled
# defaults -- one constant so the look stays consistent if it ever changes.
_INPUT_CLASS = (
    "w-full bg-surface border border-outline-variant rounded-lg px-4 py-3 "
    "font-body-md text-body-md focus:ring-1 focus:ring-primary "
    "focus:border-primary outline-none transition-all"
)
_SELECT_CLASS = _INPUT_CLASS + " appearance-none cursor-pointer"


class CategoryImageWidget(forms.ClearableFileInput):
    """Renders only a sr-only file input plus, when editing a category that
    already has an image, a sr-only "clear" checkbox -- no visible
    "Currently: ... Clear:" text at all. Keeps ClearableFileInput's built-in
    clear-on-checkbox ModelForm semantics (form.save() needs no changes),
    matching the same "sr-only input, template builds its own tile UI"
    convention already established for product images
    (build_product_image_formset below)."""

    template_name = "admin_portal/widgets/category_image_input.html"


class CategoryForm(forms.ModelForm):
    """Slug is deliberately not a form field -- Category.save() derives it
    from name automatically (apps/catalog/models.py), matching this
    codebase's existing convention for every slugged model."""

    class Meta:
        model = Category
        fields = ["name", "image"]
        widgets = {
            "image": CategoryImageWidget(
                attrs={
                    "class": "sr-only",
                    "x-ref": "fileInput",
                    "@change": "onFileChange($event)",
                }
            ),
        }


class BannerForm(forms.ModelForm):
    """Task 42. link_type/product/category are all rendered as hidden
    inputs driven by a themed Alpine listbox/radio group in the template
    (matching ProductForm.category's established "no native <select>"
    convention below); start_date/end_date are likewise HiddenInput,
    rendered by the shared admin_portal/_date_filter_field.html themed
    calendar popover (already used twice on order_management_queue.html)
    instead of a native <input type="date"> -- clean() below is the real
    server-side enforcement that the right target field/date pair was
    actually filled in, regardless of what the client-side JS happened to
    show/hide."""

    class Meta:
        model = Banner
        fields = [
            "image",
            "link_type",
            "product",
            "category",
            "url",
            "start_date",
            "end_date",
            "order",
        ]
        widgets = {
            "image": CategoryImageWidget(
                attrs={
                    "class": "sr-only",
                    "x-ref": "fileInput",
                    "@change": "onFileChange($event)",
                }
            ),
            "link_type": forms.HiddenInput(),
            "product": forms.HiddenInput(),
            "category": forms.HiddenInput(),
            "url": forms.URLInput(
                attrs={"class": _INPUT_CLASS, "placeholder": "https://..."}
            ),
            "start_date": forms.HiddenInput(),
            "end_date": forms.HiddenInput(),
            "order": forms.NumberInput(attrs={"class": _INPUT_CLASS, "min": "0"}),
        }

    def clean(self):
        cleaned_data = super().clean()
        link_type = cleaned_data.get("link_type")
        if link_type == Banner.LinkType.PRODUCT and not cleaned_data.get("product"):
            raise ValidationError("Choose a product for this banner to link to.")
        if link_type == Banner.LinkType.CATEGORY and not cleaned_data.get("category"):
            raise ValidationError("Choose a category for this banner to link to.")
        if link_type == Banner.LinkType.PAGE and not cleaned_data.get("url"):
            raise ValidationError("Enter a URL for this banner to link to.")

        start_date = cleaned_data.get("start_date")
        end_date = cleaned_data.get("end_date")
        if start_date and end_date and start_date > end_date:
            raise ValidationError("The end date must be on or after the start date.")

        return cleaned_data


class DiscountCodeForm(forms.ModelForm):
    """Task 43a. `amount`'s valid range depends on `discount_type` (a
    0-100 percentage vs. an unbounded GHS amount) -- can't be expressed
    as a single field-level validator, so it's enforced here in clean(),
    the same "form is the real cross-field enforcement" shape as
    BannerForm above."""

    class Meta:
        model = DiscountCode
        fields = [
            "code",
            "discount_type",
            "amount",
            "expiry_date",
            "audience",
            "max_uses",
            "limit_one_per_customer",
            "is_active",
        ]
        widgets = {
            "code": forms.TextInput(
                attrs={"class": _INPUT_CLASS, "placeholder": "e.g. SAVE20"}
            ),
            "discount_type": forms.HiddenInput(),
            "amount": forms.NumberInput(
                attrs={"class": _INPUT_CLASS, "step": "0.01", "min": "0"}
            ),
            "expiry_date": forms.HiddenInput(),
            "audience": forms.HiddenInput(),
            "max_uses": forms.NumberInput(attrs={"class": _INPUT_CLASS, "min": "1"}),
            "limit_one_per_customer": forms.CheckboxInput(
                attrs={"class": "sr-only peer"}
            ),
            "is_active": forms.CheckboxInput(attrs={"class": "sr-only peer"}),
        }

    def clean_code(self):
        # Debugging finding: DiscountCode.save() normalizes to uppercase,
        # but Django's ModelForm runs its automatic uniqueness check
        # against the RAW submitted value during _post_clean(), which
        # happens before save() ever executes -- "save20" never collided
        # with an already-stored "SAVE20" at validation time, so a
        # duplicate (differently-cased) code passed validation and only
        # failed later as a raw, unhandled IntegrityError from save()'s
        # own normalization. Normalizing here instead, in a field-level
        # clean_<name> method (which Django processes before its
        # uniqueness check), means the check runs against the same
        # uppercase value save() would have produced anyway.
        return self.cleaned_data["code"].upper()

    def clean(self):
        cleaned_data = super().clean()
        discount_type = cleaned_data.get("discount_type")
        amount = cleaned_data.get("amount")
        if amount is not None:
            if amount <= 0:
                raise ValidationError("The amount must be greater than zero.")
            if discount_type == DiscountCode.DiscountType.PERCENTAGE and amount > 100:
                raise ValidationError("A percentage discount cannot exceed 100.")

        max_uses = cleaned_data.get("max_uses")
        if max_uses is not None and max_uses <= 0:
            raise ValidationError("The usage limit must be at least 1.")

        return cleaned_data


class ProductForm(forms.ModelForm):
    class Meta:
        model = Product
        fields = [
            "name",
            "category",
            "description",
            "price",
            "pv_value",
            "stock",
            "is_active",
            "is_featured",
        ]
        widgets = {
            # x-model drives the live slug preview in the template --
            # Alpine reads/writes this attribute regardless of which
            # code (Django's widget or hand-written HTML) rendered it.
            "name": forms.TextInput(attrs={"class": _INPUT_CLASS, "x-model": "name"}),
            # Rendered as a themed Alpine listbox in the template, not a
            # native <select> -- this hidden input is what the listbox's
            # JS actually writes the chosen category id into (matching
            # the same hidden-input-behind-a-listbox pattern already used
            # by the product-list filter bar's own category dropdown).
            "category": forms.HiddenInput(),
            "description": forms.Textarea(attrs={"class": _INPUT_CLASS, "rows": 4}),
            "price": forms.NumberInput(
                attrs={"class": _INPUT_CLASS + " pl-8", "step": "0.01", "min": "0"}
            ),
            "pv_value": forms.NumberInput(attrs={"class": _INPUT_CLASS, "min": "0"}),
            "stock": forms.NumberInput(attrs={"class": _INPUT_CLASS, "min": "0"}),
            "is_active": forms.CheckboxInput(attrs={"class": "sr-only peer"}),
            "is_featured": forms.CheckboxInput(attrs={"class": "sr-only peer"}),
        }


# A product may have at most 5 images total. No "Add Image" button is
# needed -- every request renders exactly enough blank rows to reach 5
# (build_product_image_formset computes that count from how many images
# already exist), so all upload slots are simply present from the start.
# max_num/validate_max is the real server-side enforcement (a crafted
# POST with more than 5 image forms must still be rejected regardless of
# what the form happened to render).
MAX_PRODUCT_IMAGES = 5


def build_product_image_formset(extra):
    """Called fresh per-request (not a module-level constant) so `extra`
    can reflect how many images the product already has -- see
    apps/admin_portal/views.py's create/edit views."""
    return inlineformset_factory(
        Product,
        ProductImage,
        fields=["image", "is_primary", "order"],
        extra=extra,
        max_num=MAX_PRODUCT_IMAGES,
        validate_max=True,
        can_delete=True,
        widgets={
            # Plain FileInput, not ClearableFileInput -- the template
            # builds its own photo-tile preview and delete control, so
            # ClearableFileInput's default "Currently: ... Clear:" text
            # would be redundant UI fighting the custom one. sr-only
            # keeps it clickable (wrapped in a <label> the template
            # styles as the upload dropzone) without ever showing the
            # native "No file chosen" text.
            "image": forms.FileInput(attrs={"class": "sr-only"}),
            "is_primary": forms.CheckboxInput(attrs={"class": "sr-only"}),
            "order": forms.NumberInput(attrs={"class": "sr-only", "tabindex": "-1"}),
        },
    )


_VARIANT_INPUT_CLASS = (
    "w-full bg-white border border-outline-variant rounded-md px-3 py-2 text-body-md"
)

ProductVariantFormSet = inlineformset_factory(
    Product,
    ProductVariant,
    fields=["name", "value"],
    extra=1,
    can_delete=True,
    widgets={
        "name": forms.TextInput(attrs={"class": _VARIANT_INPUT_CLASS}),
        "value": forms.TextInput(attrs={"class": _VARIANT_INPUT_CLASS}),
    },
)


class SocialMediaLinkForm(forms.ModelForm):
    """Every create/update path goes through this form's is_valid() --
    never Model.objects.create(**request.POST) or a bare .save() -- so
    SocialMediaLink.url's URLField scheme validator and icon_color's hex
    RegexValidator are guaranteed to actually run (doubt-driven-development
    finding: full_clean() is NOT called on a bare .save())."""

    class Meta:
        model = SocialMediaLink
        fields = ["name", "url", "platform", "icon_color"]
        widgets = {
            "name": forms.TextInput(
                attrs={"class": _INPUT_CLASS, "placeholder": "e.g. Facebook"}
            ),
            "url": forms.URLInput(
                attrs={
                    "class": _INPUT_CLASS,
                    "placeholder": "https://facebook.com/yourpage",
                }
            ),
            "platform": forms.Select(attrs={"class": _SELECT_CLASS}),
            "icon_color": forms.TextInput(
                attrs={"class": _INPUT_CLASS, "maxlength": "7"}
            ),
        }

    def clean(self):
        cleaned_data = super().clean()
        # Doubt-driven-development finding: nothing otherwise stops an
        # admin from accidentally creating an unbounded number of footer
        # links. Only checked on create (self.instance.pk is falsy for a
        # new, unsaved row) -- editing an existing row must never be
        # blocked by a cap that was already reached.
        #
        # CodeRabbit finding: this cap was a hardcoded Python constant --
        # moved to constance's MAX_SOCIAL_MEDIA_LINKS (default 20) so an
        # admin can raise/lower it without a deploy, matching this
        # project's "business rules live in settings, not code" rule.
        #
        # Code-review finding: this count-then-create has a TOCTOU race
        # under two truly concurrent submissions (both could read count()
        # < the cap before either commits). Accepted, not fixed -- this is
        # an admin-only, cosmetic footer cap, not a financial path, and
        # this codebase already reserves select_for_update-grade locking
        # rigor for money (wallet/PV/commission) code specifically.
        if not self.instance.pk:
            max_links = config.MAX_SOCIAL_MEDIA_LINKS
            if SocialMediaLink.objects.count() >= max_links:
                raise ValidationError(
                    f"You can add up to {max_links} social media links. "
                    "Delete one before adding another."
                )
        return cleaned_data


class NotificationTemplateForm(forms.ModelForm):
    """Task 48a. `key` is deliberately excluded from `fields` -- every
    row is pre-seeded by migration for a fixed Key enum member (see the
    model's own docstring), so editing wording is the only real
    operation; there's no freeform "new key" for an admin to invent.

    clean() enforces two things no field-level validator can, both
    findings from this sub-task's own mandatory security-and-hardening
    pass:
    (1) a body/subject referencing a {{placeholder}} not declared for
        this template's key in PLACEHOLDERS_BY_KEY is rejected outright
        -- catches a typo or a copy-pasted wrong variable name at save
        time, before it can reach a real send as a literal, unreplaced
        '{{typo}}' in a customer-facing message. This is a UX/
        correctness safeguard, not a security boundary by itself: the
        renderer (apps.notifications.rendering.render_template) can
        never expose more than what the caller's own context dict
        explicitly contains, regardless of what an admin writes here.
    (2) `subject` rejects an embedded \\r/\\n -- Django's send_mail()
        already raises BadHeaderError on a multi-line header, but every
        real send site wraps that call in its own best-effort
        try/except and would silently swallow it, quietly breaking that
        notification type at every future send instead of surfacing the
        mistake to the admin who made it right now."""

    class Meta:
        model = NotificationTemplate
        fields = ["subject", "body"]
        widgets = {
            "subject": forms.TextInput(attrs={"class": _INPUT_CLASS}),
            "body": forms.Textarea(attrs={"class": _INPUT_CLASS, "rows": 6}),
        }

    def clean(self):
        cleaned_data = super().clean()
        allowed = set(PLACEHOLDERS_BY_KEY.get(self.instance.key, []))

        for field_name in ("subject", "body"):
            value = cleaned_data.get(field_name) or ""
            used = set(_PLACEHOLDER_RE.findall(value))
            unknown = used - allowed
            if unknown:
                unknown_display = ", ".join(
                    f"{{{{{name}}}}}" for name in sorted(unknown)
                )
                allowed_display = (
                    ", ".join(f"{{{{{name}}}}}" for name in sorted(allowed))
                    if allowed
                    else "none"
                )
                self.add_error(
                    field_name,
                    f"Unknown placeholder(s): {unknown_display}. "
                    f"Available for this template: {allowed_display}.",
                )

        subject = cleaned_data.get("subject") or ""
        if "\n" in subject or "\r" in subject:
            self.add_error("subject", "Subject cannot contain line breaks.")

        return cleaned_data
