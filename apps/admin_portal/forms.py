from django import forms
from django.core.exceptions import ValidationError
from django.forms import inlineformset_factory

from apps.catalog.models import Category, Product, ProductImage, ProductVariant
from apps.pages.models import MAX_SOCIAL_MEDIA_LINKS, SocialMediaLink

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
        # Code-review finding: this count-then-create has a TOCTOU race
        # under two truly concurrent submissions (both could read count()
        # < 20 before either commits). Accepted, not fixed -- this is an
        # admin-only, cosmetic footer cap, not a financial path, and this
        # codebase already reserves select_for_update-grade locking
        # rigor for money (wallet/PV/commission) code specifically.
        if not self.instance.pk:
            if SocialMediaLink.objects.count() >= MAX_SOCIAL_MEDIA_LINKS:
                raise ValidationError(
                    f"You can add up to {MAX_SOCIAL_MEDIA_LINKS} social "
                    "media links. Delete one before adding another."
                )
        return cleaned_data
