"""FORK (Calibra ruling R-2026-10-23-f): WHOOP's recovery score keeps no skin temperature.

WHOOP sends ``skin_temp_celsius`` inside each recovery. It is not stored as a series (the write
drops it) and not stored as a component of the recovery score either.
"""

from uuid import uuid4

from app.services.providers.factory import ProviderFactory
from app.services.providers.whoop.data_247 import Whoop247Data

RECOVERY = {
    "cycle_id": 1,
    "sleep_id": "s1",
    "created_at": "2026-10-11T06:00:00Z",
    "score_state": "SCORED",
    "score": {
        "recovery_score": 71,
        "resting_heart_rate": 52,
        "hrv_rmssd_milli": 61.2,
        "spo2_percentage": 96.5,
        "skin_temp_celsius": 33.6,
    },
}


def test_the_recovery_score_keeps_its_other_components_and_no_skin_temperature() -> None:
    data = ProviderFactory().get_provider("whoop").data_247
    assert isinstance(data, Whoop247Data)

    _, score = data.normalize_recovery(RECOVERY, uuid4())

    assert score is not None
    assert set(score.components or {}) == {"resting_heart_rate", "hrv_rmssd_milli", "spo2_percentage"}
