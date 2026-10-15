import unittest

from app.robot_core.sensing.multi_camera import FramePacket, MultiCameraManager


class MultiCameraManagerTest(unittest.TestCase):
    def test_bounded_packets_rotate_across_all_cameras(self):
        manager = MultiCameraManager({f"cam{i:02d}": lambda: object() for i in range(1, 9)})
        for index, camera_id in enumerate(manager.queues, start=1):
            manager.queues[camera_id].put_latest(
                FramePacket(camera_id, index, 0.0, object())
            )

        first = [packet.camera_id for packet in manager.packets(max_packets=4)]
        second = [packet.camera_id for packet in manager.packets(max_packets=4)]

        self.assertEqual(["cam01", "cam02", "cam03", "cam04"], first)
        self.assertEqual(["cam05", "cam06", "cam07", "cam08"], second)

    def test_empty_camera_does_not_block_other_cameras(self):
        manager = MultiCameraManager({"cam01": lambda: object(), "cam02": lambda: object()})
        manager.queues["cam02"].put_latest(FramePacket("cam02", 1, 0.0, object()))

        packets = list(manager.packets(max_packets=1))

        self.assertEqual(["cam02"], [packet.camera_id for packet in packets])


if __name__ == "__main__":
    unittest.main()
