import unittest

import pins


class Pins(unittest.TestCase):
    def test_one_pin_per_shot_sharpest_in_middle(self):
        g = {"render": [0, 260], "own": [0, 250], "windows": 1}
        sharp = [0.0] * 260
        sharp[60], sharp[200] = 5.0, 9.0  # 60 is in shot 1's middle half, 200 in shot 2's
        self.assertEqual(pins.choose(g, [100], sharp), [60, 200])

    def test_never_next_to_a_cut(self):
        g = {"render": [0, 260], "own": [0, 250], "windows": 1}
        sharp = [0.0] * 260
        sharp[99] = sharp[101] = 100.0
        self.assertTrue(all(abs(p - 100) >= pins.EDGE for p in pins.choose(g, [100], sharp)))

    def test_long_take_one_pin_per_window_outside_overlaps(self):
        g = {"render": [0, 702], "own": [0, 700], "windows": 3}
        sharp = [1.0] * 702
        got = pins.choose(g, [], sharp)
        self.assertEqual(len(got), 3)
        self.assertEqual([pins.window_of(p, 3) for p in got], [0, 1, 2])
        for p in got[1:]:
            self.assertGreaterEqual(p % pins.HOP, pins.OVERLAP + pins.EDGE)

    def test_overlap_frames_are_unusable(self):
        self.assertIsNone(pins.window_of(pins.HOP + 5, 3))
        self.assertEqual(pins.window_of(pins.HOP + 40, 3), 1)

    def test_asked_times_win_and_cap_at_four(self):
        g = {"render": [0, 294], "own": [0, 294], "windows": 1}
        got = pins.choose(g, [], [0.0] * 294, asked=[10, 50, 90, 130, 170, 400])
        self.assertEqual(got, [10, 50, 90, 130])

    def test_asked_time_in_another_group_is_ignored(self):
        g = {"render": [242, 400], "own": [250, 400], "windows": 1}
        self.assertNotIn(100, pins.choose(g, [250], [1.0] * 400, asked=[100]))


if __name__ == "__main__":
    unittest.main()
