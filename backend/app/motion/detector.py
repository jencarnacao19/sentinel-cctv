"""Frame-by-frame motion detection.

Pipeline::

    frame -> grayscale -> blur -> background subtraction -> threshold
          -> morphology -> contours -> score

Deliberately free of any model. MOG2 learns what the scene normally looks like
and reports what does not fit, which is all that is needed to mark a timeline.

Two things separate this from a naive frame-difference:

* **A learned background.** Frame differencing fires on every lighting flicker
  and on the compression noise cheap cameras produce constantly. MOG2 models
  each pixel as a mixture of Gaussians and absorbs that noise into "normal".
* **Global-change rejection.** When almost the whole frame changes at once it is
  an exposure change, a light switching on, or an infrared cut filter flipping --
  not something moving. Reporting those as motion is the single largest source
  of false events on an outdoor camera.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, field, replace

import cv2
import numpy as np

from app.motion.zones import MotionZone, ZoneKind

#: Frames are analysed at this size regardless of the source resolution.
#: Motion large enough to care about is still obvious here, and the cost is a
#: fiftieth of analysing 2K.
ANALYSIS_WIDTH = 640
ANALYSIS_HEIGHT = 360

#: Above this fraction of changed pixels the frame is treated as a global
#: lighting change rather than movement.
GLOBAL_CHANGE_FRACTION = 0.55

#: Frames fed to MOG2 before its output is trusted. Until then the model still
#: thinks an empty scene is unusual and would report the whole frame as motion.
WARMUP_FRAMES = 15


@dataclass(frozen=True, slots=True)
class MotionSettings:
    """Detector tuning, derived from the 0-100 slider plus optional overrides."""

    sensitivity: int = 50
    min_area_fraction: float = 0.0015
    """Smallest contour, as a fraction of the frame, that counts. The default
    ignores an insect on the lens but catches a person at the far end of a
    driveway."""
    threshold: int = 32
    var_threshold: float = 16.0
    """MOG2's actual pixel-model sensitivity. The subtractor emits an almost
    binary mask, so changing only the later OpenCV threshold did not materially
    tune detection on real cameras."""
    learning_rate: float = 0.005
    blur_kernel: int = 5

    @classmethod
    def from_sensitivity(cls, sensitivity: int, **overrides: float | int) -> MotionSettings:
        """Map the single slider onto the values that actually matter.

        Higher sensitivity means a lower MOG2 variance threshold and a smaller
        minimum area, so faint or distant movement registers.
        """
        level = max(0, min(100, sensitivity))
        settings = cls(
            sensitivity=level,
            # 60 at sensitivity 0 down to 12 at sensitivity 100.
            threshold=round(60 - (level / 100) * 48),
            # 28 at sensitivity 0, 16 at the default 50, 4 at 100.
            var_threshold=28 - (level / 100) * 24,
            # 0.5% of the frame down to 0.02%.
            min_area_fraction=0.005 - (level / 100) * 0.0048,
        )
        # dataclasses.replace, not **__dict__: this is a slots dataclass and has
        # no __dict__ at all.
        return replace(settings, **overrides) if overrides else settings  # type: ignore[arg-type]


@dataclass(frozen=True, slots=True)
class ZoneResult:
    """Motion inside one detection zone, in the same units as the frame score."""

    score: float
    regions: list[tuple[int, int, int, int]] = field(default_factory=list)


@dataclass(frozen=True, slots=True)
class MotionResult:
    """What one frame produced."""

    score: float
    """0.0-1.0. The fraction of the frame occupied by qualifying contours."""
    changed_fraction: float
    """Raw fraction of pixels the subtractor flagged, before contour filtering."""
    regions: list[tuple[int, int, int, int]] = field(default_factory=list)
    """Bounding boxes in analysis-frame coordinates."""
    is_global_change: bool = False
    """True when the whole scene changed at once: a light, or auto-exposure."""
    warming_up: bool = False
    zones: dict[str, ZoneResult] = field(default_factory=dict)
    """Detection zones with qualifying motion this frame, by zone id."""

    @property
    def has_motion(self) -> bool:
        return self.score > 0 and not self.is_global_change and not self.warming_up

    def for_zone(self, zone_id: str) -> MotionResult:
        """This frame as seen by one detection zone, for its own event state."""
        hit = self.zones.get(zone_id)
        return MotionResult(
            score=hit.score if hit is not None else 0.0,
            changed_fraction=self.changed_fraction,
            regions=list(hit.regions) if hit is not None else [],
            is_global_change=self.is_global_change,
            warming_up=self.warming_up,
        )


@dataclass(frozen=True, slots=True)
class _CompiledZone:
    id: str
    mask: np.ndarray
    sensitivity: int | None


def build_mask(
    zones: list[list[tuple[float, float]]],
    width: int = ANALYSIS_WIDTH,
    height: int = ANALYSIS_HEIGHT,
) -> np.ndarray | None:
    """Turn normalised ignore-polygons into a binary mask.

    Coordinates are stored normalised (0-1) so a mask drawn on a 2K preview
    still applies after the analysis resolution or the camera changes.
    """
    if not zones:
        return None

    mask = np.full((height, width), 255, dtype=np.uint8)
    drew = False
    for polygon in zones:
        if len(polygon) < 3:
            continue
        points = np.array([(int(x * width), int(y * height)) for x, y in polygon], dtype=np.int32)
        cv2.fillPoly(mask, [points], 0)
        drew = True
    return mask if drew else None


def build_zone_mask(
    points: Sequence[tuple[float, float]],
    width: int = ANALYSIS_WIDTH,
    height: int = ANALYSIS_HEIGHT,
) -> np.ndarray:
    """A binary mask that is set inside one normalised polygon."""
    mask = np.zeros((height, width), dtype=np.uint8)
    polygon = np.array([(int(x * width), int(y * height)) for x, y in points], dtype=np.int32)
    cv2.fillPoly(mask, [polygon], 255)
    return mask


class MotionDetector:
    """Stateful detector for one camera.

    Not thread-safe: each camera's worker owns exactly one instance.
    """

    def __init__(self, settings: MotionSettings | None = None) -> None:
        self.settings = settings or MotionSettings()
        self._mask: np.ndarray | None = None
        self._detect_zones: list[_CompiledZone] = []
        self._kernel = cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (5, 5))
        self._model = self._model_settings()
        self.reset()

    def set_zones(self, zones: Sequence[MotionZone]) -> None:
        """Ignore zones are cut out first; once any detection zone exists,
        motion only counts inside one, each judged at its own sensitivity."""
        enabled = [zone for zone in zones if zone.enabled]
        self._mask = build_mask([list(z.points) for z in enabled if z.kind is ZoneKind.IGNORE])
        self._detect_zones = [
            _CompiledZone(id=z.id, mask=build_zone_mask(z.points), sensitivity=z.sensitivity)
            for z in enabled
            if z.kind is ZoneKind.DETECT
        ]
        self._refresh_model()

    def set_settings(self, settings: MotionSettings) -> None:
        self.settings = settings
        self._refresh_model()

    def reset(self) -> None:
        """Forget the learned background.

        Called after a reconnect: the camera may have moved, or the scene may be
        hours older, and carrying the old model over produces a burst of false
        motion.
        """
        self._subtractor = cv2.createBackgroundSubtractorMOG2(
            history=500,
            # Mahalanobis distance, not a pixel threshold. 16 is OpenCV's
            # default and behaves well on compressed camera output.
            varThreshold=self._model.var_threshold,
            detectShadows=False,
        )
        self._frames_seen = 0

    def _model_settings(self) -> MotionSettings:
        """One background model serves every zone, so it runs at the most
        sensitive level any zone asks for; less sensitive zones then demand a
        larger movement before they count it."""
        zone_levels = [z.sensitivity for z in self._detect_zones if z.sensitivity is not None]
        if not zone_levels or max(zone_levels) <= self.settings.sensitivity:
            return self.settings
        return MotionSettings.from_sensitivity(max(zone_levels))

    def _refresh_model(self) -> None:
        model = self._model_settings()
        rebuild = model.var_threshold != self._model.var_threshold
        self._model = model
        if rebuild:
            self.reset()

    def _zone_min_area_fraction(self, zone: _CompiledZone) -> float:
        if zone.sensitivity is None:
            return self.settings.min_area_fraction
        return MotionSettings.from_sensitivity(zone.sensitivity).min_area_fraction

    @property
    def is_warm(self) -> bool:
        # Strictly greater: the counter is incremented before the check, so the
        # first WARMUP_FRAMES frames all count as warm-up and the one after is
        # the first whose result is trusted.
        return self._frames_seen > WARMUP_FRAMES

    def process(self, frame: np.ndarray) -> MotionResult:
        """Analyse one frame. ``frame`` is BGR or already single-channel."""
        prepared = self._prepare(frame)
        self._frames_seen += 1

        foreground = self._subtractor.apply(prepared, learningRate=self.settings.learning_rate)
        _, binary = cv2.threshold(foreground, self._model.threshold, 255, cv2.THRESH_BINARY)

        if self._mask is not None:
            binary = cv2.bitwise_and(binary, self._mask)

        # Opening removes speckle (sensor noise, rain, compression artefacts);
        # closing rejoins a person split into two blobs by a dark jacket.
        binary = cv2.morphologyEx(binary, cv2.MORPH_OPEN, self._kernel)
        binary = cv2.morphologyEx(binary, cv2.MORPH_CLOSE, self._kernel, iterations=2)

        total_pixels = binary.shape[0] * binary.shape[1]
        changed_fraction = float(cv2.countNonZero(binary)) / total_pixels

        if not self.is_warm:
            return MotionResult(score=0.0, changed_fraction=changed_fraction, warming_up=True)

        if changed_fraction >= GLOBAL_CHANGE_FRACTION:
            # A light came on, the IR filter flipped, or auto-exposure stepped.
            # Relearn immediately so the next frames are judged against the new
            # scene rather than producing motion for the following minute.
            self._subtractor.apply(prepared, learningRate=0.5)
            return MotionResult(score=0.0, changed_fraction=changed_fraction, is_global_change=True)

        if not self._detect_zones:
            motion_area, regions = _qualifying_motion(
                binary, self.settings.min_area_fraction * total_pixels
            )
            return MotionResult(
                score=min(1.0, motion_area / total_pixels),
                changed_fraction=changed_fraction,
                regions=regions,
            )

        zones: dict[str, ZoneResult] = {}
        for zone in self._detect_zones:
            zone_area, zone_regions = _qualifying_motion(
                cv2.bitwise_and(binary, zone.mask),
                self._zone_min_area_fraction(zone) * total_pixels,
            )
            if zone_area > 0:
                zones[zone.id] = ZoneResult(
                    score=min(1.0, zone_area / total_pixels), regions=zone_regions
                )
        # The camera as a whole is in motion while any of its zones is.
        return MotionResult(
            score=max((hit.score for hit in zones.values()), default=0.0),
            changed_fraction=changed_fraction,
            regions=[region for hit in zones.values() for region in hit.regions],
            zones=zones,
        )

    def _prepare(self, frame: np.ndarray) -> np.ndarray:
        if frame.ndim == 3:
            frame = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY)
        if frame.shape[:2] != (ANALYSIS_HEIGHT, ANALYSIS_WIDTH):
            frame = cv2.resize(
                frame, (ANALYSIS_WIDTH, ANALYSIS_HEIGHT), interpolation=cv2.INTER_AREA
            )
        kernel = self.settings.blur_kernel
        if kernel > 1:
            # Must be odd for GaussianBlur.
            kernel = kernel if kernel % 2 else kernel + 1
            frame = cv2.GaussianBlur(frame, (kernel, kernel), 0)
        return frame


def _qualifying_motion(
    binary: np.ndarray, min_area: float
) -> tuple[float, list[tuple[int, int, int, int]]]:
    """Total area and bounding boxes of the contours at least ``min_area`` big."""
    contours, _ = cv2.findContours(binary, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
    regions: list[tuple[int, int, int, int]] = []
    motion_area = 0.0
    for contour in contours:
        area = cv2.contourArea(contour)
        if area < min_area:
            continue
        motion_area += area
        x, y, w, h = cv2.boundingRect(contour)
        regions.append((int(x), int(y), int(w), int(h)))
    return motion_area, regions
