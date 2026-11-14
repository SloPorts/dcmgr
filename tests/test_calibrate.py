"""tools/calibrate/analyze.py: finding flashes in a recording and pairing them with console lines."""
import os
import sys
import unittest

from rig import ROOT

sys.path.insert(0, ROOT)
from dcmgr import calibrate as analyze  # noqa: E402


def recording(flash_at, frame=1 / 30, length=10.0, black=16, white=235):
    """Synthetic (pts, luma) frames: white for 0.5 s from each time in flash_at (recording time)."""
    out = []
    for i in range(int(length / frame)):
        t = round(i * frame, 6)  # computed, not accumulated: no floating-point drift
        out.append((t, white if any(f - 1e-9 <= t < f + 0.5 for f in flash_at) else black))
    return out


class AnalyzeTests(unittest.TestCase):
    def test_onsets_are_first_bright_frames(self):
        frames = recording([2.0, 4.0])
        self.assertEqual([round(x, 3) for x in analyze.onsets(frames)], [2.0, 4.0])

    def test_no_flashes(self):
        with self.assertRaises(ValueError):
            analyze.onsets(recording([]))

    def test_flash_lines_subtract_fill_time(self):
        lines = [{"t": 100.010, "text": "CAL start flashes=2"}, {"t": 102.008, "text": "CAL flash 1 fill_us=8000"},
                 {"t": 104.0, "text": "unrelated"}]
        self.assertEqual(analyze.flash_lines(lines), [(1, 102.008, 102.0)])

    def test_pairing_gives_the_lag(self):
        t0 = 1000.0
        # the DC flashed at recording times 2.0 and 4.0; the capture showed it 0.15 s later
        frames = recording([2.15, 4.15], frame=0.05)
        found = analyze.onsets(frames)
        delays = analyze.pair([(1, t0 + 2.0, t0 + 2.0), (2, t0 + 4.0, t0 + 4.0)], found, t0)
        self.assertEqual([(n, round(d, 3)) for n, d in delays], [(1, 0.15), (2, 0.15)])

    def test_unmatched_flash_is_skipped_and_onsets_used_once(self):
        t0 = 0.0
        found = [2.2, 6.2]
        delays = analyze.pair([(1, 2.0, 2.0), (2, 4.0, 4.0), (3, 6.0, 6.0)], found, t0)
        self.assertEqual([n for n, _ in delays], [1, 3])


class RecommendTests(unittest.TestCase):
    def test_window_too_narrow_for_the_rounding_is_not_reported(self):
        # as on the real rig: both bounds 56.6 ms; the recommendation (60) can't sit inside that
        pts = [i * 0.04 for i in range(200)]
        # flash 1: last black frame 56.6 ms after its line; flash 2: first white frame 56.6 ms after its line
        rec, window = analyze.recommend([2.0234, 4.0234], pts, [2.12, 4.08])
        self.assertEqual(rec, 60)
        self.assertIsNone(window)

    def test_exact_window_when_jitter_is_under_a_frame(self):
        pts = [i * 0.04 for i in range(200)]       # 25 fps
        onsets = [2.12, 4.12]                       # each flash first seen 0.12 s after its line
        rec, window = analyze.recommend([2.0, 4.0], pts, onsets)
        self.assertEqual([round(x) for x in window], [80, 120])
        self.assertEqual(rec, 85)                   # just past 80 ms, rounded up to 5

    def test_jitter_wider_than_a_frame(self):
        pts = [i * 0.04 for i in range(200)]
        onsets = [2.08, 4.16]                       # seen 80 ms and 160 ms after their lines
        rec, window = analyze.recommend([2.0, 4.0], pts, onsets)
        self.assertIsNone(window)                   # (40, 80] and (120, 160] don't overlap
        self.assertEqual(rec, 125)                  # never earlier than the change: just past 120 ms


if __name__ == "__main__":
    unittest.main()
