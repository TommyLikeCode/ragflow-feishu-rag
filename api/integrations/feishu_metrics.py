from __future__ import annotations

import json
import os
import threading
import time
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


def _repo_root() -> Path:
    return Path(__file__).resolve().parents[2]


METRICS_FILE = Path(
    os.environ.get("FEISHU_METRICS_FILE", str(_repo_root() / "logs" / "feishu_metrics.json"))
)


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


@dataclass
class FeishuMetricsState:
    received_total: int = 0
    enqueued_total: int = 0
    worker_started_total: int = 0
    processing_success_total: int = 0
    processing_failed_total: int = 0
    send_text_success_total: int = 0
    send_text_failed_total: int = 0
    total_processing_seconds: float = 0.0
    queue_size: int = 0
    worker_running: bool = False
    ws_connected: bool = False
    last_processed_at: str = ""
    last_error: str = ""
    last_update_at: str = ""
    last_received_at: str = ""
    last_enqueued_at: str = ""
    last_worker_started_at: str = ""
    last_send_text_at: str = ""
    last_send_text_error_at: str = ""

    @property
    def avg_processing_seconds(self) -> float:
        if self.processing_success_total <= 0:
            return 0.0
        return self.total_processing_seconds / self.processing_success_total

    def to_metrics_dict(self) -> dict[str, Any]:
        data = asdict(self)
        data["avg_processing_seconds"] = round(self.avg_processing_seconds, 6)
        data["metrics_file"] = str(METRICS_FILE)
        return data

    def to_health_dict(self) -> dict[str, Any]:
        return {
            "ws_connected": self.ws_connected,
            "queue_size": self.queue_size,
            "worker_running": self.worker_running,
            "last_processed_at": self.last_processed_at,
            "last_error": self.last_error,
            "healthy": bool(self.ws_connected and self.worker_running and not self.last_error),
            "metrics_file": str(METRICS_FILE),
        }


_state = FeishuMetricsState()
_lock = threading.RLock()


def _touch_state() -> None:
    _state.last_update_at = _utc_now_iso()
    METRICS_FILE.parent.mkdir(parents=True, exist_ok=True)
    tmp_file = METRICS_FILE.with_suffix(".tmp")
    with tmp_file.open("w", encoding="utf-8") as fp:
        json.dump(_state.to_metrics_dict(), fp, ensure_ascii=False, indent=2)
    os.replace(tmp_file, METRICS_FILE)


def reset_feishu_metrics() -> None:
    with _lock:
        global _state
        _state = FeishuMetricsState()
        _touch_state()


def _set_error(error: Any) -> str:
    if error is None:
        return ""
    text = str(error)
    return text if len(text) <= 500 else text[:500] + "...(truncated)"


def _update(**kwargs: Any) -> None:
    with _lock:
        for key, value in kwargs.items():
            setattr(_state, key, value)
        _touch_state()


def record_ws_received() -> None:
    with _lock:
        _state.received_total += 1
        _state.last_received_at = _utc_now_iso()
        _touch_state()


def record_message_enqueued(queue_size: int) -> None:
    with _lock:
        _state.enqueued_total += 1
        _state.queue_size = queue_size
        _state.last_enqueued_at = _utc_now_iso()
        _touch_state()


def set_queue_size(queue_size: int) -> None:
    _update(queue_size=queue_size)


def set_ws_connected(connected: bool) -> None:
    _update(ws_connected=connected)


def set_worker_running(running: bool) -> None:
    with _lock:
        _state.worker_running = running
        if running:
            _state.worker_started_total += 1
            _state.last_worker_started_at = _utc_now_iso()
        _touch_state()


def record_worker_processing_start(queue_size: int) -> None:
    _update(queue_size=queue_size)


def record_worker_processing_success(duration_seconds: float) -> None:
    with _lock:
        _state.processing_success_total += 1
        _state.total_processing_seconds += max(duration_seconds, 0.0)
        _state.last_processed_at = _utc_now_iso()
        _state.last_error = ""
        _touch_state()


def record_worker_processing_failure(error: Any) -> None:
    with _lock:
        _state.processing_failed_total += 1
        _state.last_processed_at = _utc_now_iso()
        _state.last_error = _set_error(error)
        _touch_state()


def record_send_text_success() -> None:
    with _lock:
        _state.send_text_success_total += 1
        _state.last_send_text_at = _utc_now_iso()
        _state.last_error = ""
        _touch_state()


def record_send_text_failure(error: Any) -> None:
    with _lock:
        _state.send_text_failed_total += 1
        _state.last_send_text_error_at = _utc_now_iso()
        _state.last_error = _set_error(error)
        _touch_state()


def get_feishu_metrics_snapshot() -> dict[str, Any]:
    if METRICS_FILE.exists():
        try:
            with METRICS_FILE.open("r", encoding="utf-8") as fp:
                return json.load(fp)
        except Exception:
            pass
    with _lock:
        return _state.to_metrics_dict()


def get_feishu_health_snapshot() -> dict[str, Any]:
    snapshot = get_feishu_metrics_snapshot()
    return {
        "ws_connected": bool(snapshot.get("ws_connected", False)),
        "queue_size": int(snapshot.get("queue_size", 0) or 0),
        "worker_running": bool(snapshot.get("worker_running", False)),
        "last_processed_at": snapshot.get("last_processed_at", ""),
        "last_error": snapshot.get("last_error", ""),
        "healthy": bool(snapshot.get("ws_connected", False) and snapshot.get("worker_running", False) and not snapshot.get("last_error", "")),
        "metrics_file": str(METRICS_FILE),
    }
