import logging

from constance.signals import config_updated
from simple_history.models import HistoricalRecords

logger = logging.getLogger(__name__)


def _current_user():
    """Reuses simple_history's own HistoryRequestMiddleware thread-local
    (already installed for Category/Product/Distributor/Order/
    WithdrawalRequest's HistoricalRecords() tracking) rather than
    inventing a second "who is making this request" mechanism -- the
    same middleware, the same actor-resolution path, for every audit
    trail in this project. Returns None outside a request (a management
    command, a shell session, a test with no client) -- exactly how
    HistoricalRecords() itself degrades in the same situation."""
    request = getattr(HistoricalRecords.context, "request", None)
    if request is not None and request.user.is_authenticated:
        return request.user
    return None


def _is_lazy_default_materialization(key, old_value, new_value):
    """A real bug found via root-cause investigation (a fresh Constance
    table -- every pytest test, or a hypothetical brand-new deploy --
    produced a spurious audit row for EVERY setting on the very first
    save, not just the one an admin actually changed).

    Root cause: constance.base.Config.__getattr__ lazily materializes a
    setting's declared default into storage the first time it's ever
    READ, not just set -- `if result is None: result = default;
    setattr(self, key, default)`. ConstanceForm.save()'s own `current =
    getattr(config, name)` comparison step (constance/forms.py) reads
    EVERY field this way before comparing it to the submitted value,
    so on a table with no rows yet, this fires config_updated once for
    every single setting (old_value=None, new_value=that setting's own
    declared default) as a pure storage-warming side effect -- before
    ConstanceForm.save() goes on to fire a second, genuine signal for
    whichever field the admin actually changed.

    Detected here by old_value being None (a fresh key) AND new_value
    exactly matching that key's own CONSTANCE_CONFIG default -- the
    lazy-materialization signature. The one case this also (harmlessly)
    skips: an admin's genuine first-ever deliberate change to a setting
    that happens to equal its own default, which produces no observable
    behavior change anyway."""
    from .config import CONSTANCE_CONFIG

    if old_value is not None:
        return False
    options = CONSTANCE_CONFIG.get(key)
    return options is not None and options[0] == new_value


def record_setting_change(sender, key, old_value, new_value, **kwargs):
    """Task 47d. Connected to constance.signals.config_updated in
    apps.platform_settings.apps.PlatformSettingsConfig.ready(). Skips
    constance's own lazy-default-materialization noise -- see
    _is_lazy_default_materialization's own docstring -- so every row
    that lands here represents a real admin-submitted change.

    Wrapped so an audit-log failure can never break a real settings
    save -- the settings value is already written by the time this
    signal fires (constance's DatabaseBackend.set() sends config_updated
    AFTER persisting the new value), so there's nothing to roll back
    here even if this raises; letting it propagate would just turn a
    successful save into a 500 for no reason, matching this codebase's
    established "notification/audit failures must not break the primary
    action" convention (_send_confirmation_notifications and friends)."""
    if _is_lazy_default_materialization(key, old_value, new_value):
        return

    from .models import PlatformSettingChange

    try:
        PlatformSettingChange.objects.create(
            key=key,
            old_value=str(old_value),
            new_value=str(new_value),
            changed_by=_current_user(),
        )
    except Exception:
        logger.exception(
            "record_setting_change: failed to audit-log a change to %s", key
        )


config_updated.connect(record_setting_change, dispatch_uid="platform_settings_audit")
