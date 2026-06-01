import json
import logging
import time
from threading import Thread

import paho.mqtt.client as mqtt
from django.conf import settings

logger = logging.getLogger(__name__)


class MqttService:
    _instance = None
    _client = None
    _connected = False
    _reconnect_thread = None

    def __new__(cls):
        if cls._instance is None:
            cls._instance = super().__new__(cls)
        return cls._instance

    def __init__(self):
        if self._client is None:
            self._client = mqtt.Client()
            self._client.on_connect = self._on_connect
            self._client.on_message = self._on_message
            self._client.on_disconnect = self._on_disconnect

            tls_enabled = getattr(settings, "MQTT_TLS_ENABLED", False)
            if tls_enabled:
                self._client.tls_set()

    def _on_connect(self, client, userdata, flags, rc):
        if rc == 0:
            self._connected = True
            logger.info("MQTT connected successfully")
            device_id = getattr(settings, "DEVICE_ID", "")
            if device_id:
                topic = f"pat-sig/{device_id}/data"
                status_topic = f"pat-sig/{device_id}/status"
                alert_topic = f"pat-sig/{device_id}/alert"
            else:
                topic = getattr(settings, "MQTT_TOPIC", "pat-sig/+/data")
                status_topic = "pat-sig/+/status"
                alert_topic = "pat-sig/+/alert"
            client.subscribe(topic)
            client.subscribe(status_topic)
            client.subscribe(alert_topic)
            logger.info(
                f"MQTT subscribed to: {topic}, {status_topic}, {alert_topic}"
            )
            # Resend anything queued while we were offline.
            self.flush_outbox()
        else:
            logger.error(f"MQTT connection failed with code: {rc}")
            self._schedule_reconnect()

    def _on_disconnect(self, client, userdata, rc):
        self._connected = False
        logger.warning(f"MQTT disconnected with code: {rc}")
        self._schedule_reconnect()

    def _schedule_reconnect(self):
        if self._reconnect_thread and self._reconnect_thread.is_alive():
            return
        self._reconnect_thread = Thread(target=self._reconnect_loop, daemon=True)
        self._reconnect_thread.start()

    def _reconnect_loop(self):
        while not self._connected:
            logger.info("MQTT reconnecting in 5 seconds...")
            time.sleep(5)
            try:
                broker = getattr(settings, "MQTT_BROKER", "localhost")
                port = getattr(settings, "MQTT_PORT", 1883)
                keepalive = getattr(settings, "MQTT_KEEPALIVE", 60)
                self._client.connect(broker, port, keepalive)
            except Exception as e:
                logger.error(f"MQTT reconnect failed: {e}")

    def _on_message(self, client, userdata, msg):
        try:
            topic = msg.topic
            payload = msg.payload.decode("utf-8")
            logger.info(f"MQTT message received on {topic}: {payload}")

            parts = topic.split("/")
            dsm_id = parts[1] if len(parts) >= 2 else None

            from home.signals import mqtt_message_received

            mqtt_message_received.send(
                sender=self.__class__, topic=topic, payload=payload, dsm_id=dsm_id
            )
        except Exception as e:
            logger.error(f"Error processing MQTT message: {e}")

    def connect(self):
        if self._connected:
            logger.info("MQTT already connected")
            return
        broker = getattr(settings, "MQTT_BROKER", "localhost")
        port = getattr(settings, "MQTT_PORT", 1883)
        keepalive = getattr(settings, "MQTT_KEEPALIVE", 60)
        try:
            logger.info(f"Connecting to MQTT broker: {broker}:{port}")
            self._client.connect(broker, port, keepalive)
            self._client.loop_start()
        except Exception as e:
            logger.error(f"Failed to connect to MQTT broker: {e}")
            self._schedule_reconnect()

    def disconnect(self):
        if self._client:
            self._client.loop_stop()
            self._client.disconnect()
            self._connected = False
            logger.info("MQTT disconnected")

    def publish(self, topic, payload, qos=0):
        if self._connected:
            result = self._client.publish(topic, payload, qos)
            if result.rc == mqtt.MQTT_ERR_SUCCESS:
                logger.info(f"MQTT published to {topic}: {payload}")
                return True
            else:
                logger.error(f"MQTT publish failed: {result.rc}")
                return False
        else:
            logger.warning("MQTT not connected, cannot publish")
            return False

    def publish_reliable(self, topic, payload):
        """Durable device -> backend report: persist to the outbox first, then
        try to flush. Nothing is lost if the broker/backend is unreachable; the
        row is resent on reconnect (_on_connect) or the next scheduler tick.
        """
        from home.models import OutboxReport

        body = (
            payload
            if isinstance(payload, str)
            else json.dumps(payload, ensure_ascii=False)
        )
        try:
            OutboxReport.objects.create(topic=topic, payload=body)
        except Exception as e:
            logger.error(f"Failed to enqueue outbox report: {e}")
            return
        self.flush_outbox()

    def flush_outbox(self):
        """Publish queued outbox reports in order, deleting each on success.

        Stops at the first failure so ordering is preserved and unsent rows
        survive until the broker/backend is reachable again. Uses QoS 1 so the
        broker acknowledges receipt.
        """
        if not self._connected:
            return
        from home.models import OutboxReport

        try:
            rows = list(OutboxReport.objects.all().order_by("created_at", "id"))
        except Exception as e:
            logger.error(f"Failed to read outbox: {e}")
            return

        for row in rows:
            result = self._client.publish(row.topic, row.payload, qos=1)
            if result.rc == mqtt.MQTT_ERR_SUCCESS:
                logger.info(f"Outbox flushed -> {row.topic}: {row.payload}")
                row.delete()
            else:
                row.attempts += 1
                row.save(update_fields=["attempts"])
                logger.warning(
                    f"Outbox flush failed (rc={result.rc}); will retry #{row.pk}"
                )
                break

    def is_connected(self):
        return self._connected


mqtt_service = MqttService()

