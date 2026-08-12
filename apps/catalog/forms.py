from django import forms

from .models import Review

INPUT_CLASSES = (
    "w-full p-4 rounded-lg border border-outline-variant bg-surface-bright "
    "focus:border-primary focus:ring-1 focus:ring-primary outline-none transition-all"
)


class ReviewForm(forms.ModelForm):
    """Task 41a. rating gets its own explicit min/max (rather than
    trusting the model's PositiveSmallIntegerField alone) so an
    out-of-range submission is a clean form error, not a 500 from the
    model's own review_rating_between_1_and_5 CheckConstraint."""

    rating = forms.IntegerField(
        min_value=1,
        max_value=5,
        widget=forms.NumberInput(attrs={"class": INPUT_CLASSES, "min": 1, "max": 5}),
    )

    class Meta:
        model = Review
        fields = ["rating", "body"]
        widgets = {
            "body": forms.Textarea(
                attrs={
                    "class": INPUT_CLASSES,
                    "rows": 4,
                    "placeholder": "Share your experience with this product...",
                }
            ),
        }
