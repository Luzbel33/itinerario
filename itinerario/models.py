from dataclasses import dataclass
from enum import StrEnum
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, StringConstraints

Short = Annotated[str, StringConstraints(strip_whitespace=True, max_length=250)]
Body = Annotated[str, StringConstraints(strip_whitespace=True, max_length=1100)]
Coordinate = tuple[float, float]


class Depth(StrEnum):
    QUICK = "rápido"
    STANDARD = "detallado"
    DEEP = "profundo"
    EXTENSIVE = "extenso"


@dataclass(frozen=True)
class ResearchBudget:
    findings: int
    places: int
    research_tokens: int
    report_tokens: int
    web_calls: int
    social_calls: int
    thread_calls: int = 0
    local_calls: int = 0
    contrast_calls: int = 0
    targeted_calls: int = 0
    quota_units: int = 1

    @property
    def searches(self) -> int:
        return (self.web_calls + self.social_calls + self.thread_calls + self.local_calls
                + self.contrast_calls + self.targeted_calls)


BUDGETS = {
    Depth.QUICK: ResearchBudget(4, 4, 2200, 2700, web_calls=1, social_calls=1),
    Depth.STANDARD: ResearchBudget(9, 8, 4600, 5200, web_calls=3, social_calls=2),
    Depth.DEEP: ResearchBudget(
        15, 12, 7300, 8800, web_calls=3, social_calls=2, thread_calls=2, contrast_calls=2, targeted_calls=2,
    ),
    Depth.EXTENSIVE: ResearchBudget(
        20, 15, 8700, 15000, web_calls=5, social_calls=3, thread_calls=3, local_calls=4, contrast_calls=3,
        targeted_calls=2, quota_units=2,
    ),
}


class Model(BaseModel):
    model_config = ConfigDict(extra="forbid")


class Intent(Model):
    action: Literal["chat", "clarify", "report"]
    answer: Body
    objective: Body
    depth: Depth
    delivery: Literal["telegram", "pdf"]
    map_kind: Literal["none", "pins", "route"]
    area: Short
    country_code: Short
    job_search: bool
    instagram: Literal["priorizar", "opcional", "omitir"]
    instagram_query: Short
    origin: Short
    finish: Short
    return_to_origin: bool
    transport: Literal["drive", "walk", "bicycle"]
    sector_size: int
    sector_count: int
    sort_by: Literal["time", "distance"]


class Source(Model):
    id: int
    title: Short
    url: str
    accessed: Short
    channel: Literal["web", "social"] = "web"


class Finding(Model):
    title: Short
    detail: Body
    kind: Literal["documentado", "inferencia", "pendiente"]
    date: Short
    sources: list[int] = Field(max_length=8)


class Place(Model):
    name: Short
    category: Short
    address: Short
    locality: Short
    fit: Body
    action: Body
    public_contact: Short
    reference: Short
    date: Short
    sources: list[int] = Field(max_length=8)
    address_sources: list[int] = Field(max_length=5)


class Comparison(Model):
    criterion: Short
    option: Short
    observation: Body
    sources: list[int] = Field(max_length=8)


class Report(Model):
    title: Short
    executive_summary: Body
    findings: list[Finding] = Field(max_length=20)
    places: list[Place] = Field(max_length=15)
    comparisons: list[Comparison] = Field(max_length=20)
    next_steps: list[Body] = Field(max_length=10)
    limitations: list[Body] = Field(max_length=12)
    methodology: Body


class Point(Model):
    label: Short
    address: Short
    coordinates: Coordinate
    district: str = ""
    place_index: int | None = None


class Leg(Model):
    meters: float = Field(ge=0, allow_inf_nan=False)
    seconds: float = Field(ge=0, allow_inf_nan=False)
    geometry: list[Coordinate] = Field(min_length=2)


class Geography(Model):
    points: list[Point] = Field(default_factory=list)
    legs: list[Leg] = Field(default_factory=list)
    excluded: list[str] = Field(default_factory=list)
    notes: list[str] = Field(default_factory=list)
    baseline_meters: float | None = None
    baseline_seconds: float | None = None
    mode: Literal["drive", "walk", "bicycle"] = "drive"
    objective: Literal["time", "distance"] = "time"
    sector_size: int = 5
    sector_count: int = 0


class MapView(Model):
    title: str
    sector: int = 0
    point_indices: list[int]
    leg_indices: list[int]


class Snapshot(Model):
    created: str
    objective: str
    depth: Depth
    report: Report
    sources: list[Source]
    geography: Geography
