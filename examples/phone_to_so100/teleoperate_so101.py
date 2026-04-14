#!/usr/bin/env python

# Copyright 2025 The HuggingFace Inc. team. All rights reserved.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0

"""
SO101 phone teleop. Adapted from examples/phone_to_so100/teleoperate.py.

Prerequisites:
  - Robot calibrated and saved as ~/.cache/huggingface/lerobot/calibration/robots/so_follower/so101_follower_1.json
  - iOS HEBI Mobile I/O app running, Family=HEBI, Name=mobileIO, foreground, same LAN
  - URDF at ./so101_urdf/so101_new_calib.urdf (downloaded alongside this script)

Phone controls:
  - Hold B1 to engage motion; release to freeze robot and reposition phone freely.
  - Re-press B1 to re-latch the reference pose at the current phone pose.
  - A1 analog slider: gripper open/close velocity.
"""

import logging
import time
from pathlib import Path

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")

from lerobot.model.kinematics import RobotKinematics  # noqa: E402
from lerobot.processor import (
    RobotProcessorPipeline,
    robot_action_observation_to_transition,
    transition_to_robot_action,
)
from lerobot.robots.so_follower import SO101Follower, SOFollowerRobotConfig
from lerobot.robots.so_follower.robot_kinematic_processor import (
    EEBoundsAndSafety,
    EEReferenceAndDelta,
    GripperVelocityToJoint,
    InverseKinematicsEEToJoints,
)
from lerobot.teleoperators.phone import Phone, PhoneConfig
from lerobot.teleoperators.phone.config_phone import PhoneOS
from lerobot.teleoperators.phone.phone_processor import MapPhoneActionToRobotAction
from lerobot.types import RobotAction, RobotObservation
from lerobot.utils.robot_utils import precise_sleep
from lerobot.utils.visualization_utils import init_rerun, log_rerun_data

FPS = 60  # control loop rate; lower end-to-end lag. Drop if robot serial IO can't keep up.

URDF_PATH = Path(__file__).parent / "so101_urdf" / "so101_new_calib.urdf"
ROBOT_PORT = "/dev/so101_follower_1"
ROBOT_ID = "so101_follower_1"

# Reconnect policy for the phone teleop stream.
PHONE_RETRY_INTERVAL_S = 3.0  # sleep between reconnection attempts
PHONE_STALE_TIMEOUT_S = 2.0   # feedback older than this triggers reconnect

# Centered joint pose in degrees (calibration midpoint). Adjust if your "0°" pose is unsafe.
HOME_POSE_DEG: dict[str, float] = {
    "shoulder_pan": 0.0,
    "shoulder_lift": 0.0,
    "elbow_flex": 0.0,
    "wrist_flex": 0.0,
    "wrist_roll": 0.0,
    "gripper": 0.0,
}
HOME_RAMP_S = 3.0  # smoothly drive to HOME_POSE over this many seconds


def connect_phone_blocking(teleop_device, retry_interval_s: float = PHONE_RETRY_INTERVAL_S) -> None:
    """Keep attempting ``teleop_device.connect()`` until it succeeds.

    Only ``RuntimeError`` messages mentioning 'Mobile I/O not found' (the HEBI
    app isn't running / discoverable) are treated as retryable. Anything else
    re-raises. Ctrl-C propagates and aborts the wait.
    """
    attempt = 0
    while True:
        try:
            teleop_device.connect()
            return
        except RuntimeError as e:
            if "Mobile I/O not found" not in str(e):
                raise
            attempt += 1
            print(
                f"[reconnect #{attempt}] HEBI Mobile I/O not found. "
                f"Open the app on the phone. Retrying in {retry_interval_s:.0f}s... "
                f"(Ctrl-C to abort)"
            )
            time.sleep(retry_interval_s)


def safe_disconnect_phone(teleop_device) -> None:
    """Best-effort phone teardown: swallow all errors so reconnect can proceed."""
    if not teleop_device.is_connected:
        return
    try:
        teleop_device.disconnect()
    except Exception:
        logging.getLogger(__name__).exception("Phone disconnect raised; continuing")


def move_to_home(robot, target_deg: dict[str, float], duration_s: float, fps: int = FPS) -> None:
    """Linearly interpolate joints from current pose to target over duration_s."""
    obs = robot.get_observation()
    start = {k: float(obs[f"{k}.pos"]) for k in target_deg.keys() if f"{k}.pos" in obs}
    if not start:
        print("Warning: could not read current joint positions, skipping home move.")
        return
    n_steps = max(int(duration_s * fps), 1)
    print(f"Moving to home pose over {duration_s:.1f}s...")
    for i in range(1, n_steps + 1):
        alpha = i / n_steps
        action = {f"{k}.pos": start[k] + alpha * (target_deg[k] - start[k]) for k in start}
        robot.send_action(action)
        precise_sleep(1.0 / fps)
    print("At home pose.")


def main() -> None:
    robot_config = SOFollowerRobotConfig(
        port=ROBOT_PORT,
        id=ROBOT_ID,
        use_degrees=True,
    )
    teleop_config = PhoneConfig(
        phone_os=PhoneOS.IOS,
        id="iphone_teleop",
        feedback_frequency_hz=200.0,  # higher = lower staleness; 100 is default/safe
        connect_warmup_s=0.3,  # shorter startup; bump back to 0.5 if first-press misses
    )

    robot = SO101Follower(robot_config)
    teleop_device = Phone(teleop_config)

    kinematics_solver = RobotKinematics(
        urdf_path=str(URDF_PATH),
        target_frame_name="gripper_frame_link",
        joint_names=list(robot.bus.motors.keys()),
    )

    phone_to_robot_joints_processor = RobotProcessorPipeline[
        tuple[RobotAction, RobotObservation], RobotAction
    ](
        steps=[
            MapPhoneActionToRobotAction(platform=teleop_config.phone_os),
            EEReferenceAndDelta(
                kinematics=kinematics_solver,
                # 0.1 = phone 1 m → EE 0.1 m. Lower if motion still feels jumpy.
                end_effector_step_sizes={"x": 0.1, "y": 0.1, "z": 0.1},
                motor_names=list(robot.bus.motors.keys()),
                use_latched_reference=True,
            ),
            EEBoundsAndSafety(
                end_effector_bounds={"min": [-1.0, -1.0, -0.1], "max": [1.0, 1.0, 1.0]},
                # Per-step cap. 0.01m * 60fps = 0.6 m/s. Library now clips
                # (saturates) rather than raising when exceeded.
                max_ee_step_m=0.01,
            ),
            GripperVelocityToJoint(speed_factor=20.0),
            InverseKinematicsEEToJoints(
                kinematics=kinematics_solver,
                motor_names=list(robot.bus.motors.keys()),
                initial_guess_current_joints=True,
                # SO101 is 5-DOF (+ gripper) so it cannot hit arbitrary 6-DOF
                # poses. Moderate orientation weight: IK prioritises position
                # but also tries to match phone roll/pitch/yaw best-effort.
                # Lower → smoother at the cost of orientation fidelity; higher
                # → tighter rotation tracking but may flip joints on extreme
                # phone rotations. Tune between 0.05 and 1.0.
                orientation_weight=0.3,
            ),
        ],
        to_transition=robot_action_observation_to_transition,
        to_output=transition_to_robot_action,
    )

    robot.connect()
    move_to_home(robot, HOME_POSE_DEG, HOME_RAMP_S)
    init_rerun(session_name="phone_so101_teleop")

    try:
        while True:  # outer reconnect loop — exits only on KeyboardInterrupt
            connect_phone_blocking(teleop_device)
            if not robot.is_connected:
                raise RuntimeError("Robot is not connected.")

            print("Starting teleop loop. Press & hold B1 in the HEBI app, then move the phone.")
            stalled = False
            while True:  # inner control loop
                t0 = time.perf_counter()
                age = teleop_device.feedback_age_s()
                if age > PHONE_STALE_TIMEOUT_S:
                    print(
                        f"\nPhone feedback stale for {age:.1f}s "
                        f"(> {PHONE_STALE_TIMEOUT_S:.0f}s). App may have quit or WiFi dropped. "
                        f"Reconnecting..."
                    )
                    stalled = True
                    break
                robot_obs = robot.get_observation()
                phone_obs = teleop_device.get_action()
                joint_action = phone_to_robot_joints_processor((phone_obs, robot_obs))
                robot.send_action(joint_action)
                log_rerun_data(observation=phone_obs, action=joint_action)
                precise_sleep(max(1.0 / FPS - (time.perf_counter() - t0), 0.0))

            # Tear down the phone; robot stays connected and holds its last pose.
            safe_disconnect_phone(teleop_device)
            if not stalled:
                break  # inner loop exited for a non-stall reason (unused today)
    except KeyboardInterrupt:
        print("\nStopping teleop.")
    finally:
        safe_disconnect_phone(teleop_device)
        if robot.is_connected:
            robot.disconnect()


if __name__ == "__main__":
    main()
