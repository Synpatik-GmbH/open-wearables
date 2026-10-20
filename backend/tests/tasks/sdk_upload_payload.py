"""An Apple SDK batch for the 2.96.6.3 redelivery tests: five heart-rate samples, a workout and a
night."""

import json
from datetime import datetime, timedelta, timezone

_SOURCE = {
    "name": "Test Apple Watch",
    "bundleIdentifier": "com.apple.health",
    "deviceManufacturer": "Apple Inc.",
    "deviceModel": "Watch",
    "productType": "Watch7,5",
    "deviceSoftwareVersion": "10.3.1",
}

# (stage, start, end) in minutes from the night's start.
_STAGES = [
    ("in_bed", 0, 480),
    ("light", 15, 90),
    ("deep", 90, 210),
    ("rem", 210, 300),
    ("awake", 300, 310),
    ("light", 310, 470),
]
_LONG_PAST = datetime(2025, 4, 2, 21, 30, tzinfo=timezone.utc)


def _iso(moment: datetime) -> str:
    return moment.strftime("%Y-%m-%dT%H:%M:%SZ")


def night_payload(night_start: datetime = _LONG_PAST) -> str:
    """By default the night is long past, so it is finished in the same batch. A night that ended
    less than ``sleep_end_gap_minutes`` ago stays open in Redis instead."""
    return json.dumps(
        {
            "provider": "apple",
            "sdkVersion": "1.0.0",
            "syncTimestamp": "2025-04-03T12:00:00Z",
            "data": {
                "records": [
                    {
                        "id": f"0000000{i}-6873-4647-92B2-24F7680014A0",
                        "type": "HKQuantityTypeIdentifierHeartRate",
                        "unit": "count/min",
                        "value": 50 + i,
                        "startDate": f"2025-04-02T22:0{i}:00Z",
                        "endDate": f"2025-04-02T22:0{i}:00Z",
                        "source": _SOURCE,
                    }
                    for i in range(5)
                ],
                "workouts": [
                    {
                        "id": "801B68D7-F4AA-4A23-BD26-A3BA1BA6B08D",
                        "type": "walking",
                        "startDate": "2025-04-02T17:27:00Z",
                        "endDate": "2025-04-02T18:51:24Z",
                        "source": _SOURCE,
                        "values": [{"type": "duration", "unit": "s", "value": 5064}],
                    }
                ],
                "sleep": [
                    {
                        "id": f"E3D5647B-2B0E-43AA-BE3F-9FAD43D3558{i}",
                        "stage": stage,
                        "startDate": _iso(night_start + timedelta(minutes=start)),
                        "endDate": _iso(night_start + timedelta(minutes=end)),
                        "source": _SOURCE,
                    }
                    for i, (stage, start, end) in enumerate(_STAGES)
                ],
            },
        }
    )
