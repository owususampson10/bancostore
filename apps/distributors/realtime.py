def wallet_group_name(distributor_id: int) -> str:
    """Channels group name for a distributor's live wallet-balance updates
    (Task 20d). Shared between consumers.py (group_add) and the wallet
    signal receiver (group_send) so there is exactly one source of truth
    for the naming convention -- a fresh-context review flagged the
    duplicated-string risk of defining this independently in both places.
    """
    return f"wallet_{distributor_id}"


def notification_group_name(distributor_id: int) -> str:
    """Channels group name for a distributor's live notification-bell
    updates (Task 21d). Distinct prefix from wallet_group_name's
    "wallet_{id}" -- verified during doubt-driven-development before this
    was written that the two names can never collide for the same
    distributor.
    """
    return f"notifications_{distributor_id}"
