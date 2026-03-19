# =============================================================================
# Tests for local worker helper functions (local_worker.py)
# =============================================================================
# Tests _crop_image(), output_result/output_error, and PIPER_RATE_MAP.
# These are all importable without rapidocr or piper-tts (lazy imports).

from PIL import Image

from local_worker import (
    PIPER_RATE_MAP,
    WorkerError,
    WorkerResult,
    _crop_image,
    output_error,
    output_result,
)


class TestCropImage:
    def _make_image(self, width=1280, height=800):
        """Create a test image with known dimensions."""
        return Image.new("RGB", (width, height), color=(128, 128, 128))

    def test_normal_crop(self):
        img = self._make_image()
        cropped = _crop_image(img, {"x1": 100, "y1": 200, "x2": 500, "y2": 600})
        assert cropped.size == (400, 400)

    def test_full_image_region(self):
        img = self._make_image()
        cropped = _crop_image(img, {"x1": 0, "y1": 0, "x2": 1280, "y2": 800})
        assert cropped.size == (1280, 800)

    def test_swapped_coordinates_normalized(self):
        """x1 > x2 or y1 > y2 should be swapped automatically."""
        img = self._make_image()
        cropped = _crop_image(img, {"x1": 500, "y1": 600, "x2": 100, "y2": 200})
        assert cropped.size == (400, 400)

    def test_coordinates_clamped_to_image_bounds(self):
        img = self._make_image(100, 100)
        cropped = _crop_image(img, {"x1": -50, "y1": -50, "x2": 200, "y2": 200})
        assert cropped.size == (100, 100)

    def test_tiny_region_returns_original(self):
        """Regions smaller than 10px in either dimension return the original."""
        img = self._make_image()
        result = _crop_image(img, {"x1": 100, "y1": 100, "x2": 105, "y2": 105})
        # Should return the original image since 5x5 < 10px threshold
        assert result.size == img.size

    def test_missing_keys_use_defaults(self):
        """Missing crop_region keys should default gracefully."""
        img = self._make_image(200, 200)
        cropped = _crop_image(img, {})
        # Without x2/y2, defaults to w/h (200x200) — full image
        assert cropped.size == (200, 200)


class TestPiperRateMap:
    def test_has_all_presets(self):
        expected = {"x-slow", "slow", "medium", "fast", "x-fast"}
        assert set(PIPER_RATE_MAP.keys()) == expected

    def test_medium_is_one(self):
        assert PIPER_RATE_MAP["medium"] == 1.0

    def test_inverse_ordering(self):
        """Piper uses length_scale (inverse): lower = faster, higher = slower."""
        assert PIPER_RATE_MAP["x-slow"] > PIPER_RATE_MAP["slow"]
        assert PIPER_RATE_MAP["slow"] > PIPER_RATE_MAP["medium"]
        assert PIPER_RATE_MAP["medium"] > PIPER_RATE_MAP["fast"]
        assert PIPER_RATE_MAP["fast"] > PIPER_RATE_MAP["x-fast"]

    def test_all_values_positive(self):
        for key, value in PIPER_RATE_MAP.items():
            assert value > 0, f"{key} has non-positive rate: {value}"


class TestLocalWorkerExceptions:
    def test_output_result_raises(self):
        try:
            output_result({"success": True, "text": "detected text"})
            assert False, "Should raise WorkerResult"
        except WorkerResult as r:
            assert r.data["success"] is True

    def test_output_error_raises(self):
        try:
            output_error("File not found")
            assert False, "Should raise WorkerError"
        except WorkerError as e:
            assert e.data["success"] is False
            assert "File not found" in e.data["message"]
