"""Read-only garden selections and timestamped environmental samples."""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from typing import TYPE_CHECKING, Literal

from .exceptions import VivosunResponseError

if TYPE_CHECKING:
    from collections.abc import Mapping, Sequence

    from .gardens import Garden
    from .models import DeviceInfo

Role = Literal["inside", "outside", "micro"]
METRICS = ("temperature", "humidity", "vpd")
KEYS = {
    "inside": ("inTemp", "inHumi", "inVpd"),
    "outside": ("outTemp", "outHumi", "outVpd"),
}
PHYSICAL_KEYS = (
    *KEYS["inside"], *KEYS["outside"],
    "pTemp", "pHumi", "pVpd", "bTemp", "bHumi", "bVpd",
)
SOURCE_KEYS = (
    *KEYS.values(),
    ("pTemp", "pHumi", "pVpd"),
    ("bTemp", "bHumi", "bVpd"),
)


@dataclass(frozen=True, slots=True)
class Source:
    """Selected panel source, never evidence of per-sample routing."""

    device_id: str
    client_id: str
    channel: str
    code: str
    keys: tuple[str, str, str]
    channel_name: str

    @property
    def identity(self) -> str:
        """Hide hardware identifiers while keeping deterministic source identity."""
        payload = json.dumps([self.device_id, self.client_id, self.channel, self.code], separators=(",", ":"))
        return hashlib.sha256(payload.encode()).hexdigest()


@dataclass(frozen=True, slots=True)
class Selection:
    """One successful panel read; invalid groups are not empty mappings."""

    inside: Source | None
    outside: Source | None
    micro: tuple[Source, ...]
    invalid: frozenset[Role]
    observed: float


@dataclass(frozen=True, slots=True)
class Sample:
    """One newest timestamped row, without inferred source attribution."""

    time: int
    values: dict[str, int | None]

    def value(self, key: str, now: float, *, after: float | None = None) -> float | None:
        """Apply freshness and metric validity independently."""
        if self.time < now - 300 or self.time > now + 60 or (after is not None and self.time <= after):
            return None
        value = self.values.get(key)
        return None if value is None else value / 100


def parse_sample(data: Mapping[str, object]) -> Sample | None:
    """Choose the newest unique row without filling missing metrics from older rows."""
    rows = data.get("iotDataLogList")
    if not isinstance(rows, list):
        raise VivosunResponseError("Invalid environmental log")
    if not rows:
        return None
    if any(not isinstance(row, dict) or type(row.get("time")) is not int for row in rows):
        raise VivosunResponseError("Invalid environmental log timestamp")
    newest = max(row["time"] for row in rows)
    latest = [row for row in rows if row["time"] == newest]
    if len(latest) != 1:
        raise VivosunResponseError("Ambiguous environmental log timestamp")
    row = latest[0]
    values: dict[str, int | None] = {}
    for key in PHYSICAL_KEYS:
        raw = row.get(key)
        values[key] = raw if type(raw) is int and raw != -6666 else None
    return Sample(newest, values)


def parse_selection(
    data: Mapping[str, object], garden: Garden, devices: Sequence[DeviceInfo], observed: float,
) -> Selection:
    """Resolve selected climate cards by inventory and known metric contracts."""
    groups = data.get("deviceCardGroupList")
    if not isinstance(groups, list):
        raise VivosunResponseError("Invalid garden panel")
    found: dict[Role, list[Source]] = {"inside": [], "outside": [], "micro": []}
    invalid: set[Role] = set()
    tags: dict[str, tuple[Role, str]] = {
        "in_humiture": ("inside", "main_env"),
        "out_humiture": ("outside", "main_env"),
        "other_humiture": ("micro", "additional_env"),
    }
    members = {member.device_id: member.client_id for member in garden.members}
    for group in groups:
        if not isinstance(group, dict) or not isinstance(group.get("deviceCardList"), list):
            raise VivosunResponseError("Invalid garden panel group")
        for card in group["deviceCardList"]:
            if not isinstance(card, dict):
                raise VivosunResponseError("Invalid garden panel card")
            tag = card.get("dataTag")
            if not isinstance(tag, str) or tag not in tags:
                continue
            role, expected_group = tags[tag]
            if card.get("group") != expected_group or card.get("dataGroupType") != "CLIMATE":
                invalid.add(role)
                continue
            fields = [card.get(key) for key in ("extraId", "clientId", "dataGroupGid", "code")]
            if all(value == "" for value in fields) and card.get("deviceDataList") == []:
                continue
            if not all(isinstance(value, str) and value for value in fields) or card.get("extraMasterId") != "":
                invalid.add(role)
                continue
            device_id, client_id, channel, code = (str(value) for value in fields)
            matches = [d for d in devices if d.device_id == device_id and d.client_id == client_id]
            if (len(matches) != 1 or device_id not in members
                    or (members[device_id] is not None and members[device_id] != client_id)):
                invalid.add(role)
                continue
            descriptors = card.get("deviceDataList")
            if not isinstance(descriptors, list):
                invalid.add(role)
                continue
            pairs = [(item.get("dataType"), item.get("dataKey")) for item in descriptors if isinstance(item, dict)]
            keys = next(
                (keys for keys in SOURCE_KEYS if len(descriptors) == len(pairs) == 3
                 and set(pairs) == set(zip(("TEMP", "HUMI", "VPD"), keys, strict=True))),
                None,
            )
            if keys is None:
                invalid.add(role)
                continue
            channel_name = card.get("dataGroupName")
            found[role].append(Source(device_id, client_id, channel, code, keys,
                                      channel_name if isinstance(channel_name, str) else ""))
    for role in ("inside", "outside"):
        if len(found[role]) > 1:
            invalid.add(role)
    micro = found["micro"]
    if (len({(source.device_id, source.client_id, source.channel) for source in micro}) != len(micro)
            or len({source.code for source in micro}) != len(micro)):
        invalid.add("micro")
    return Selection(
        found["inside"][0] if len(found["inside"]) == 1 and "inside" not in invalid else None,
        found["outside"][0] if len(found["outside"]) == 1 and "outside" not in invalid else None,
        tuple(micro) if "micro" not in invalid else (), frozenset(invalid), observed,
    )
