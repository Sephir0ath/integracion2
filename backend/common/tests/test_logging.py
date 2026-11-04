"""Tests of the structured logging."""

import json
import logging

from common.logging import get_request_id, log_event, setup_logging


def test_log_event_writes_one_json_object_per_line(capsys):
    setup_logging("servicio-prueba")
    logger = logging.getLogger("test-logging")

    log_event(logger, "evento_de_prueba", saga_id="saga-1", total=3)

    line = capsys.readouterr().out.strip().splitlines()[-1]
    payload = json.loads(line)
    assert payload["message"] == "evento_de_prueba"
    assert payload["saga_id"] == "saga-1"
    assert payload["total"] == 3
    assert payload["level"] == "INFO"
    assert payload["service"] == "servicio-prueba"


def test_request_id_is_bound_to_the_context(capsys):
    setup_logging("servicio-prueba")
    logger = logging.getLogger("test-logging")

    assert get_request_id() is None
    log_event(logger, "evento_sin_id")
    assert "request_id" not in json.loads(capsys.readouterr().out.strip().splitlines()[-1])
