"""When is a posted log ready to recap?

Links usually get posted while live logging is still running, and WCL computes parses a few minutes
after each upload. A log is ready once it has stopped growing and WCL has processed every part.
"""

from dataclasses import dataclass


@dataclass(frozen=True)
class Readiness:
    ready: bool
    reason: str


def check_ready(
    status: dict,
    now: int,
    quiet_seconds: int,
    last_end_time: int | None,
    last_change_at: int | None,
) -> Readiness:
    segments = status.get("segments") or 0
    exported = status.get("exportedSegments") or 0
    end_time = int(status.get("endTime") or 0)  # ms, time of the last event in the log

    if segments == 0:
        return Readiness(False, "nothing uploaded yet")
    if exported < segments:
        return Readiness(False, f"WCL still processing ({exported}/{segments} parts)")

    # Quiet by the log's own clock (the normal after-raid case: ready on the first check) ...
    if end_time and now - end_time // 1000 >= quiet_seconds:
        return Readiness(True, "log ended a while ago")
    # ... or by ours, in case the uploader's clock is off and the log's timestamps run ahead.
    if last_end_time == end_time and last_change_at and now - last_change_at >= quiet_seconds:
        return Readiness(True, "log stopped growing")
    return Readiness(False, "log may still be live")
