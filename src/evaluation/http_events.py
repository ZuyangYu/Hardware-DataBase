"""Read the public persisted SSE contract from any control-plane backend."""

from collections.abc import Iterable, Iterator
import json


def iter_sse_events(lines: Iterable[str | bytes]) -> Iterator[dict]:
    event = "message"
    event_id = ""
    data: list[str] = []
    for raw_line in lines:
        line = raw_line.decode("utf-8") if isinstance(raw_line, bytes) else raw_line
        if not line:
            if data:
                yield {"seq": int(event_id or 0), "event": event, "payload": json.loads("\n".join(data))}
            event, event_id, data = "message", "", []
            continue
        if line.startswith(":"):
            continue
        field, _, value = line.partition(":")
        value = value.removeprefix(" ")
        if field == "event":
            event = value
        elif field == "id":
            event_id = value
        elif field == "data":
            data.append(value)
    if data:
        yield {"seq": int(event_id or 0), "event": event, "payload": json.loads("\n".join(data))}
