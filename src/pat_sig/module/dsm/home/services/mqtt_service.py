import json
import logging
import time
from threading import Lock, Thread

import paho.mqtt.client as mqtt
from django.conf import settings

logger = logging.getLogger(__name__)

# Seconds paho waits between (re)connect attempts. Fixed, not backed off: six
# signs on the overlay, and pat-sig-mqtt-watchdog counts the
# "MQTT reconnecting in 5 seconds" lines at this cadence.
RECONNECT_DELAY_S = 5
# How often the supervisor checks that paho's network thread is running.
SUPERVISE_S = 30
# How long a flush waits for one already running before leaving it the rest.
OUTBOX_LOCK_WAIT_S = 30


class MqttService:
    """The sign's one MQTT client.

    Paho's own network thread (loop_start) makes the first connection and every
    reconnection, retrying every RECONNECT_DELAY_S for as long as it runs. The
    service used to run a second reconnect thread of its own beside it; the two
    raced, and the loser could stop paho's thread for good while the sign still
    believed it was connected: deaf to alerts, every report failing with rc=4,
    and nothing in the log (PISN004, 2026-09-12 to 09-18; security register
    I2). Now there is one reconnect mechanism, "connected" is paho's own state,
    and a supervisor only brings the network thread back if it ever dies.
    """

    _instance = None
    _client = None
    _started = False
    _stopping = False
    _supervisor = None
    _outbox_lock = Lock()

    def __new__(cls):
        if cls._instance is None:
            cls._instance = super().__new__(cls)
        return cls._instance

    def __init__(self):
        if self._client is None:
            self._client = mqtt.Client()
            # A callback that raises is logged by paho instead of ending its
            # network thread.
            self._client.suppress_exceptions = True
            self._client.reconnect_delay_set(
                min_delay=RECONNECT_DELAY_S, max_delay=RECONNECT_DELAY_S
            )
            self._client.on_connect = self._on_connect
            self._client.on_connect_fail = self._on_connect_fail
            self._client.on_message = self._on_message
            self._client.on_disconnect = self._on_disconnect

            tls_enabled = getattr(settings, "MQTT_TLS_ENABLED", False)
            if tls_enabled:
                # Pass only the TLS material that is configured; an empty/None
                # ca_certs falls back to the system trust store, and certfile/
                # keyfile enable mutual-TLS client auth when both are set.
                tls_kwargs = {}
                ca_certs = getattr(settings, "MQTT_TLS_CA_CERTS", None)
                certfile = getattr(settings, "MQTT_TLS_CERTFILE", None)
                keyfile = getattr(settings, "MQTT_TLS_KEYFILE", None)
                if ca_certs:
                    tls_kwargs["ca_certs"] = ca_certs
                if certfile:
                    tls_kwargs["certfile"] = certfile
                if keyfile:
                    tls_kwargs["keyfile"] = keyfile
                self._client.tls_set(**tls_kwargs)

    @property
    def _connected(self):
        """Paho's own connection state, never a flag of ours that can go stale."""
        return self._client.is_connected()

    def _on_connect(self, client, userdata, flags, rc):
        if rc == 0:
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
            # Refused (certificate, not authorised, ...): paho tries again.
            logger.error(f"MQTT connection failed with code: {rc}")

    def _on_connect_fail(self, client, userdata):
        # The broker could not be reached; paho tries again after the delay.
        logger.info(f"MQTT reconnecting in {RECONNECT_DELAY_S} seconds...")

    def _on_disconnect(self, client, userdata, rc):
        logger.warning(f"MQTT disconnected with code: {rc}")
        if not self._stopping:
            logger.info(f"MQTT reconnecting in {RECONNECT_DELAY_S} seconds...")

    def _supervise(self):
        """Bring paho's network thread back if it has died.

        While that thread runs, paho reconnects on its own. If it ever stops,
        nothing else notices: is_connected() keeps saying True because no one
        is left to see the socket die. Restarting the loop is the in-process
        equivalent of restarting pat-sig, which the 2026-07-10 hand-patch did
        by hand, without ever calling reconnect() beside paho.
        """
        while not self._stopping:
            time.sleep(SUPERVISE_S)
            if self._stopping:
                return
            try:
                thread = getattr(self._client, "_thread", None)
                if thread is not None and thread.is_alive():
                    continue
                logger.error("MQTT network thread is not running; restarting it")
                self._client.loop_stop()
                self._client.loop_start()
            except Exception as e:
                logger.error(f"MQTT supervisor could not restart the loop: {e}")

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
        if self._started:
            logger.info("MQTT already started")
            return
        broker = getattr(settings, "MQTT_BROKER", "localhost")
        port = getattr(settings, "MQTT_PORT", 1883)
        keepalive = getattr(settings, "MQTT_KEEPALIVE", 60)
        logger.info(f"Connecting to MQTT broker: {broker}:{port}")
        # connect_async: the first connection is made by paho's network thread
        # too, and retried like any other, so a broker that is down (or a name
        # that does not resolve) at boot needs nothing extra.
        self._stopping = False
        self._client.connect_async(broker, port, keepalive)
        self._client.loop_start()
        self._started = True
        self._supervisor = Thread(
            target=self._supervise, name="mqtt-supervisor", daemon=True
        )
        self._supervisor.start()

    def disconnect(self):
        if self._client:
            self._stopping = True
            self._client.disconnect()
            self._client.loop_stop()
            self._started = False
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

        One flush at a time (security register I5). It is called from paho's
        thread on connect, from the scheduler tick and after every new report;
        two flushes working from the same list publish each row twice and can
        send an old report after a newer one. A caller waits for the running
        flush, then sends whatever is still queued.
        """
        if not self._connected:
            return
        if not self._outbox_lock.acquire(timeout=OUTBOX_LOCK_WAIT_S):
            logger.warning("Outbox flush still running; the next one sends the rest")
            return
        try:
            self._flush_outbox()
        finally:
            self._outbox_lock.release()

    def _flush_outbox(self):
        from home.models import OutboxReport

        try:
            rows = list(OutboxReport.objects.all().order_by("created_at", "id"))
        except Exception as e:
            logger.error(f"Failed to read outbox: {e}")
            return

        for row in rows:
            result = self._client.publish(row.topic, row.payload, qos=1)
            try:
                if result.rc == mqtt.MQTT_ERR_SUCCESS:
                    logger.info(f"Outbox flushed -> {row.topic}: {row.payload}")
                    row.delete()
                    continue
                row.attempts += 1
                row.save(update_fields=["attempts"])
                logger.warning(
                    f"Outbox flush failed (rc={result.rc}); will retry #{row.pk}"
                )
            except Exception as e:
                # A locked database: stop here, in order. A row that was sent
                # but not deleted goes again next time, with the same `at`, and
                # the backend drops the repeat.
                logger.error(f"Outbox update failed on #{row.pk}: {e}")
            break

    def is_connected(self):
        return self._client.is_connected()


mqtt_service = MqttService()

