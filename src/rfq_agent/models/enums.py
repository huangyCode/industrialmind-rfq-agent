from enum import StrEnum


class ShapeClass(StrEnum):
    ROTATIONAL = "rotational"
    PRISMATIC = "prismatic"
    ASSEMBLY = "assembly"


class FeatureType(StrEnum):
    OUTER_DIAMETER = "outer_diameter"
    BORE = "bore"
    HOLE = "hole"
    THREAD = "thread"
    KEYWAY = "keyway"
    GROOVE = "groove"
    CHAMFER = "chamfer"
    POCKET = "pocket"
    SLOT = "slot"
    FACE = "face"
    WALL = "wall"
    GEAR_TEETH = "gear_teeth"
    SPLINE = "spline"


class Severity(StrEnum):
    BLOCKER = "blocker"
    WARNING = "warning"
    INFO = "info"


class MakeOrBuy(StrEnum):
    MAKE = "make"
    BUY = "buy"
    SERVICE = "service"


class Tier(StrEnum):
    FAST_TRACK = "fast_track"
    STANDARD = "standard"
    MANUAL = "manual"


class RFQStatus(StrEnum):
    NEW = "new"
    EXTRACTED = "extracted"
    NEEDS_CLARIFICATION = "needs_clarification"
    IN_REVIEW = "in_review"
    APPROVED = "approved"
    REJECTED = "rejected"
    ERROR = "error"
