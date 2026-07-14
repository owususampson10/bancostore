from django.contrib import admin
from django.shortcuts import render
from django.utils.html import format_html

from constance import config

from .models import DiditVerification, Distributor
from .services import approve_kyc, reject_kyc


class DiditVerificationInline(admin.StackedInline):
    """Task 11c: shows Didit's result (status, per-check scores, extracted
    data, warnings, images) alongside each Distributor for admin review --
    informational only, the admin still clicks Approve/Reject below."""

    model = DiditVerification
    can_delete = False
    extra = 0
    readonly_fields = (
        "session_id",
        "status",
        "id_verification_status",
        "face_match_status",
        "face_match_score",
        "liveness_status",
        "liveness_score",
        "extracted_full_name",
        "extracted_document_number",
        "extracted_date_of_birth",
        "warnings",
        "image_previews",
    )
    fields = readonly_fields

    @admin.display(description="Images")
    def image_previews(self, obj):
        images = [
            ("ID front", obj.id_front_image),
            ("ID back", obj.id_back_image),
            ("Selfie", obj.selfie_image),
        ]
        tags = [
            format_html(
                '<div style="display:inline-block;margin-right:12px;text-align:center">'
                '<img src="{}" style="max-height:150px;max-width:150px" /><br>{}</div>',
                image.url,
                label,
            )
            for label, image in images
            if image
        ]
        return format_html("".join(tags)) if tags else "No images"

    def has_add_permission(self, request, obj=None):
        return False


@admin.register(Distributor)
class DistributorAdmin(admin.ModelAdmin):
    list_display = ("user", "rank", "kyc_status", "ir_id", "sponsor")
    inlines = [DiditVerificationInline]
    actions = ["approve_selected_kyc", "reject_selected_kyc"]

    @admin.action(description="Approve selected KYC submissions")
    def approve_selected_kyc(self, request, queryset):
        for distributor in queryset:
            approve_kyc(distributor)
        self.message_user(request, f"Approved {queryset.count()} distributor(s).")

    @admin.action(description="Reject selected KYC submissions (with a reason)")
    def reject_selected_kyc(self, request, queryset):
        # Django's documented intermediate-action-page pattern (the same
        # shape as the built-in "delete selected" confirmation): the first
        # pass has no "reason" in POST, so render a form collecting one;
        # the second pass (submitting that form) actually rejects.
        if "reason" in request.POST:
            reason = request.POST["reason"].strip()
            if reason:
                for distributor in queryset:
                    reject_kyc(distributor, reason=reason)
                self.message_user(
                    request, f"Rejected {queryset.count()} distributor(s)."
                )
                return None

        preset_reasons = [
            r.strip() for r in config.KYC_REJECTION_REASONS.split(",") if r.strip()
        ]
        return render(
            request,
            "admin/distributors/reject_kyc_confirmation.html",
            {
                "distributors": queryset,
                "preset_reasons": preset_reasons,
                "action_checkbox_name": admin.helpers.ACTION_CHECKBOX_NAME,
                "action": "reject_selected_kyc",
            },
        )
