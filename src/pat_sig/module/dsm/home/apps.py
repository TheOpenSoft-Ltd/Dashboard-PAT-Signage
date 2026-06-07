import os

from django.apps import AppConfig


class HomeConfig(AppConfig):
    default_auto_field = "django.db.models.BigAutoField"  # type: ignore
    name = "home"

    def ready(self):
        # Read-only CLI commands (e.g. `pat-sig list`) boot Django just to query
        # the ORM and set this flag so we don't spin up the MQTT client or the
        # scheduler for a one-shot read.
        if os.environ.get("PAT_SIG_NO_SERVICES") == "1":
            return

        from home.services.mqtt_service import mqtt_service  # type: ignore
        from home.services.scheduler_service import scheduler_service  # type: ignore
        from home.signals import mqtt_message_received
        from home.handlers import handle_mqtt_message

        mqtt_service.connect()
        scheduler_service.start()
        mqtt_message_received.connect(handle_mqtt_message)
