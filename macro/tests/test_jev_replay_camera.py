import unittest

from sc2_rl_agent.starcraftenv_test.utils.replay_camera import ReplayCamera


def unit(tag, owner, x, y, base=False, combat=False):
    return dict(tag=tag, owner=owner, pos=(x, y), base=base, combat=combat,
                structure=base, worker=False, build_progress=1)


class ReplayCameraTests(unittest.TestCase):
    def test_threat_cuts_in_immediately_and_camera_holds_after_threat_disappears(self):
        camera = ReplayCamera()
        base = unit(1, 1, 20, 20, base=True)
        self.assertEqual(camera.choose([base], 0)[0], (20, 20))
        enemy = unit(9, 2, 36, 20, combat=True)
        self.assertEqual(camera.choose([base, enemy], 1)[1], 'Base defense')
        self.assertEqual(camera.choose([base], 2)[1], 'Base defense')
        self.assertEqual(camera.choose([base], 8)[1], 'Base economy')

    def test_army_camera_uses_main_cluster_instead_of_midpoint_of_split_groups(self):
        camera = ReplayCamera()
        units = [unit(1, 1, 10, 10, base=True)]
        units += [unit(t, 1, 70 + t % 3, 70, combat=True) for t in range(2, 10)]
        units += [unit(20, 1, 130, 130, combat=True), unit(21, 1, 132, 130, combat=True)]
        position, reason = camera.choose(units, 400)
        self.assertEqual(reason, 'Army movement')
        self.assertLess(position[0], 75)
        self.assertLess(position[1], 75)

    def test_unknown_or_neutral_units_do_not_create_battles(self):
        camera = ReplayCamera()
        units = [unit(1, 1, 20, 20, base=True), unit(2, 16, 21, 20, combat=True)]
        self.assertEqual(camera.choose(units, 0)[1], 'Base economy')


if __name__ == '__main__':
    unittest.main()
