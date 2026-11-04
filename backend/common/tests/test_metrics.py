"""Tests of the metrics helpers."""

from prometheus_client import generate_latest

from common.metrics import REGISTRY, counter, gauge, histogram


def test_the_same_metric_name_returns_the_same_object():
    assert counter("prueba_contador", "doc") is counter("prueba_contador", "doc")
    assert gauge("prueba_gauge", "doc") is gauge("prueba_gauge", "doc")
    assert histogram("prueba_histograma", "doc") is histogram("prueba_histograma", "doc")


def test_the_registry_exposes_the_metric():
    counter("prueba_registry", "doc").inc()
    payload = generate_latest(REGISTRY).decode()
    assert "prueba_registry_total" in payload


def test_labelled_metrics_keep_their_labels():
    metric = counter("prueba_labels", "doc", ["servicio"])
    metric.labels("pagos").inc()
    payload = generate_latest(REGISTRY).decode()
    assert 'prueba_labels_total{servicio="pagos"} 1.0' in payload
