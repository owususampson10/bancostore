import os

from celery import Celery

os.environ.setdefault("DJANGO_SETTINGS_MODULE", "bancostore.settings")

app = Celery("bancostore")
app.config_from_object("django.conf:settings", namespace="CELERY")
app.autodiscover_tasks()
