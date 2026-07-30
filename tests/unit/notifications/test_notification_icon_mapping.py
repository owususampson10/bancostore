import pytest

from apps.notifications.models import Notification


@pytest.mark.parametrize(
    "event_type", [choice for choice, _ in Notification.EventType.choices]
)
def test_every_event_type_has_an_icon_and_color_mapping(event_type):
    notification = Notification(event_type=event_type)

    assert notification.icon_name
    assert notification.icon_classes
