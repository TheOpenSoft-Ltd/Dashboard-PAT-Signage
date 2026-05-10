from django.apps import AppConfig


class HomeConfig(AppConfig):
    default_auto_field = "django.db.models.BigAutoField"  # type: ignore
    name = "home"

    def ready(self):
        from home.services.mqtt_service import mqtt_service  # type: ignore
        from home.services.scheduler_service import scheduler_service  # type: ignore
        from home.signals import mqtt_message_received
        from home.handlers import handle_mqtt_message

        mqtt_service.connect()
        scheduler_service.start()
        mqtt_message_received.connect(handle_mqtt_message)
