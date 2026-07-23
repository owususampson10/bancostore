def is_admin_portal_staff(user) -> bool:
    """Task 22. Mirrors AdminSiteOTPRequiredMixin.has_permission (two_factor/
    admin.py) exactly -- is_staff AND a verified 2FA session, not just
    is_staff -- so these branded screens carry the identical mandatory-2FA
    guarantee as Django Admin itself, not a weaker one."""
    return user.is_staff and user.is_verified()
