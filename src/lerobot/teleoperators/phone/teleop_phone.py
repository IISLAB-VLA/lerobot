#!/usr/bin/env python

# Copyright 2024 The HuggingFace Inc. team. All rights reserved.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

# Docs:
# hebi: https://docs.hebi.us/tools.html#mobile-io
# teleop: https://github.com/SpesRobotics/teleop

import logging
import threading
import time

import hebi
import numpy as np
from teleop import Teleop

from lerobot.utils.decorators import check_if_already_connected, check_if_not_connected
from lerobot.utils.rotation import Rotation

from ..teleoperator import Teleoperator
from .config_phone import PhoneConfig, PhoneOS

logger = logging.getLogger(__name__)


class BasePhone:
    _enabled: bool = False
    _calib_pos: np.ndarray | None = None
    _calib_rot_inv: Rotation | None = None

    def _reapply_position_calibration(self, pos: np.ndarray) -> None:
        self._calib_pos = pos.copy()

    @property
    def is_calibrated(self) -> bool:
        return (self._calib_pos is not None) and (self._calib_rot_inv is not None)

    @property
    def action_features(self) -> dict[str, type]:
        return {
            "phone.pos": np.ndarray,  # shape (3,)
            "phone.rot": Rotation,  # scipy.spatial.transform.Rotation
            "phone.raw_inputs": dict,  # analogs/buttons or webXR meta
            "phone.enabled": bool,
        }

    @property
    def feedback_features(self) -> dict[str, type]:
        # No haptic or other feedback implemented yet
        pass

    def configure(self) -> None:
        # No additional configuration required for phone teleop
        pass

    def send_feedback(self, feedback: dict[str, float]) -> None:
        # We could add haptic feedback (vibrations) here, but it's not implemented yet
        raise NotImplementedError


class IOSPhone(BasePhone, Teleoperator):
    name = "ios_phone"

    def __init__(self, config: PhoneConfig):
        super().__init__(config)
        self.config = config
        self._group = None
        # Shared state written by the HEBI SDK feedback-handler thread and read by
        # the control loop. Sticky: callback only overwrites IO keys present in
        # each packet, so held buttons remain registered across pose-only frames.
        self._ios_lock = threading.Lock()
        self._latest_ar_pos: np.ndarray | None = None
        self._latest_ar_quat_wxyz: np.ndarray | None = None
        self._latest_io: dict[str, float | int] = {}
        self._latest_fb_ts: float = 0.0
        self._feedback_handler = None

    @property
    def is_connected(self) -> bool:
        return self._group is not None

    @check_if_already_connected
    def connect(self) -> None:
        logger.info("Connecting to IPhone, make sure to open the HEBI Mobile I/O app.")
        lookup = hebi.Lookup()
        time.sleep(2.0)
        group = lookup.get_group_from_names(["HEBI"], ["mobileIO"])
        if group is None:
            raise RuntimeError("Mobile I/O not found — check name/family settings in the app.")
        self._group = group

        # Defensive reset so re-connect starts from a clean cache.
        with self._ios_lock:
            self._latest_ar_pos = None
            self._latest_ar_quat_wxyz = None
            self._latest_io = {}
            self._latest_fb_ts = 0.0

        # Register handler before starting the background pull so no packet is
        # dropped. Pull rate is tunable via PhoneConfig.feedback_frequency_hz.
        try:
            self._feedback_handler = self._ios_feedback_callback
            self._group.add_feedback_handler(self._feedback_handler)
            self._group.feedback_frequency = float(self.config.feedback_frequency_hz)
        except Exception:
            self._feedback_handler = None
            self._group = None
            raise

        # Warmup: let the callback observe at least one IO packet — including
        # the user's currently-held button states — before calibrate() blocks.
        time.sleep(float(self.config.connect_warmup_s))
        logger.info(f"{self} connected to HEBI group with {group.size} module(s).")

        self.calibrate()

    def calibrate(self) -> None:
        print(
            "Hold the phone so that: top edge points forward in same direction as the robot (robot +x) and screen points up (robot +z)"
        )
        print("Press and hold B1 in the HEBI Mobile I/O app to capture this pose...\n")
        position, rotation = self._wait_for_capture_trigger()
        self._calib_pos = position.copy()
        self._calib_rot_inv = rotation.inv()
        self._enabled = False
        print("Calibration done\n")

    def _ios_feedback_callback(self, group_feedback) -> None:
        """Runs on the HEBI SDK feedback thread. Keep short, never raise.

        HEBI only sends IO bits on state-change packets, so we update the shared
        cache with a sticky merge: channels not present in this packet keep
        their previously latched values. Pose is always overwritten when ARKit
        data is present. Heavy math (scipy Rotation, camera offset) is left to
        readers so this callback stays under a few microseconds.
        """
        try:
            pose = group_feedback[0]
            ar_pos = getattr(pose, "ar_position", None)
            ar_quat = getattr(pose, "ar_orientation", None)

            new_io: dict[str, float | int] = {}
            io = getattr(pose, "io", None)
            if io is not None:
                bank_a, bank_b = io.a, io.b
                if bank_a is not None:
                    for ch in range(1, 9):
                        if bank_a.has_float(ch):
                            new_io[f"a{ch}"] = float(bank_a.get_float(ch))
                if bank_b is not None:
                    for ch in range(1, 9):
                        if bank_b.has_int(ch):
                            new_io[f"b{ch}"] = int(bank_b.get_int(ch))
                        elif hasattr(bank_b, "has_bool") and bank_b.has_bool(ch):
                            new_io[f"b{ch}"] = int(bank_b.get_bool(ch))

            pos_copy = None if ar_pos is None else np.asarray(ar_pos, dtype=float).copy()
            quat_copy = None if ar_quat is None else np.asarray(ar_quat, dtype=float).copy()
            now = time.monotonic()

            with self._ios_lock:
                if pos_copy is not None:
                    self._latest_ar_pos = pos_copy
                if quat_copy is not None:
                    self._latest_ar_quat_wxyz = quat_copy
                if new_io:
                    self._latest_io.update(new_io)
                self._latest_fb_ts = now
        except Exception:
            logger.exception("IOSPhone feedback callback failed")

    def _wait_for_capture_trigger(self) -> tuple[np.ndarray, Rotation]:
        """
        Blocks until B1 is observed as pressed, then returns the current pose.

        Reads the sticky IO cache populated by ``_ios_feedback_callback``. Works
        even if the user was already holding B1 before calibration started:
        ``connect()`` warms the cache for 0.5 s so that the held-B1 state packet
        is observed before this loop begins.
        """
        while True:
            with self._ios_lock:
                b1 = int(self._latest_io.get("b1", 0))
            if b1 == 1:
                has_pose, position, rotation, _ = self._read_current_pose()
                if has_pose:
                    return position, rotation
            time.sleep(0.01)

    def _read_current_pose(self) -> tuple[bool, np.ndarray | None, Rotation | None, None]:
        """
        Reads the latest 6-DoF pose cached by the HEBI feedback callback.

        The 4th tuple slot (historically the raw HEBI feedback object) is now always
        None: IO state lives in ``self._latest_io`` and callers must read it from there.

        Returns:
            (has_pose, pos, rot, None)
        """
        with self._ios_lock:
            if self._latest_ar_pos is None or self._latest_ar_quat_wxyz is None:
                return False, None, None, None
            ar_pos = self._latest_ar_pos
            ar_quat = self._latest_ar_quat_wxyz
        # HEBI provides orientation in w, x, y, z; scipy expects x, y, z, w.
        quat_xyzw = np.concatenate((ar_quat[1:], ar_quat[:1]))
        rot = Rotation.from_quat(quat_xyzw)
        pos = ar_pos - rot.apply(self.config.camera_offset)
        return True, pos, rot, None

    @check_if_not_connected
    def get_action(self) -> dict:
        # Snapshot pose + IO together under a single lock so the returned pair
        # is always consistent. Heavy math runs outside the lock.
        with self._ios_lock:
            if self._latest_ar_pos is None or self._latest_ar_quat_wxyz is None:
                return {}
            ar_pos = self._latest_ar_pos
            ar_quat = self._latest_ar_quat_wxyz
            raw_inputs = dict(self._latest_io)

        if not self.is_calibrated:
            return {}

        quat_xyzw = np.concatenate((ar_quat[1:], ar_quat[:1]))  # wxyz -> xyzw
        raw_rotation = Rotation.from_quat(quat_xyzw)
        raw_position = ar_pos - raw_rotation.apply(self.config.camera_offset)

        enable = bool(raw_inputs.get("b1", 0))
        # Rising edge: re-latch the reference pose when B1 is pressed.
        if enable and not self._enabled:
            self._reapply_position_calibration(raw_position)

        pos_cal = self._calib_rot_inv.apply(raw_position - self._calib_pos)
        rot_cal = self._calib_rot_inv * raw_rotation
        self._enabled = enable

        return {
            "phone.pos": pos_cal,
            "phone.rot": rot_cal,
            "phone.raw_inputs": raw_inputs,
            "phone.enabled": self._enabled,
        }

    def feedback_age_s(self) -> float:
        """Seconds since the last HEBI feedback packet. ``inf`` if none yet.

        Useful for detecting mid-session disconnects (HEBI app quit, WiFi loss):
        a value well above ``1.0 / feedback_frequency_hz`` means the stream has
        stalled.
        """
        with self._ios_lock:
            ts = self._latest_fb_ts
        if ts <= 0.0:
            return float("inf")
        return time.monotonic() - ts

    @check_if_not_connected
    def disconnect(self) -> None:
        # Stop the background pull first so no late callback fires against a
        # half-torn-down group reference.
        try:
            self._group.feedback_frequency = 0.0
        except Exception:
            logger.exception("Failed to stop HEBI feedback pull")
        try:
            self._group.clear_feedback_handlers()
        except Exception:
            logger.exception("Failed to clear HEBI feedback handlers")

        self._feedback_handler = None
        self._group = None
        with self._ios_lock:
            self._latest_ar_pos = None
            self._latest_ar_quat_wxyz = None
            self._latest_io = {}
            self._latest_fb_ts = 0.0


class AndroidPhone(BasePhone, Teleoperator):
    name = "android_phone"

    def __init__(self, config: PhoneConfig):
        super().__init__(config)
        self.config = config
        self._teleop = None
        self._teleop_thread = None
        self._latest_pose = None
        self._latest_message = None
        self._android_lock = threading.Lock()

    @property
    def is_connected(self) -> bool:
        return self._teleop is not None

    @check_if_already_connected
    def connect(self) -> None:
        logger.info("Starting teleop stream for Android...")
        self._teleop = Teleop()
        self._teleop.subscribe(self._android_callback)
        self._teleop_thread = threading.Thread(target=self._teleop.run, daemon=True)
        self._teleop_thread.start()
        logger.info(f"{self} connected, teleop stream started.")

        self.calibrate()

    def calibrate(self) -> None:
        print(
            "Hold the phone so that: top edge points forward in same direction as the robot (robot +x) and screen points up (robot +z)"
        )
        print("Touch and move on the WebXR page to capture this pose...\n")

        pos, rot = self._wait_for_capture_trigger()
        self._calib_pos = pos.copy()
        self._calib_rot_inv = rot.inv()
        self._enabled = False
        print("Calibration done\n")

    def _wait_for_capture_trigger(self) -> tuple[np.ndarray, Rotation]:
        """
        Blocks execution until the calibration trigger is detected from the Android device.

        This method enters a loop, continuously checking the latest message received from the WebXR
        session. It waits for the user to touch and move their finger on the screen, which generates
        a `move` event. Once this event is detected, the loop breaks and returns the phone's current
        pose.

        Returns:
            A tuple containing the position (np.ndarray) and rotation (Rotation) of the phone at the
            moment the trigger was activated.
        """
        while True:
            with self._android_lock:
                msg = self._latest_message or {}

            if bool(msg.get("move", False)):
                ok, pos, rot, _pose = self._read_current_pose()
                if ok:
                    return pos, rot

            time.sleep(0.01)

    def _read_current_pose(self) -> tuple[bool, np.ndarray | None, Rotation | None, object | None]:
        """
        Reads the latest 6-DoF pose received from the Android device's WebXR session.

        This method accesses the most recent pose data stored by the `_android_callback`. It uses a
        thread lock to safely read the shared `_latest_pose` variable. The pose, a 4x4 matrix, is
        then decomposed into position and rotation, and the configured camera offset is applied.

        Returns:
            A tuple containing:
            - A boolean indicating if a valid pose was available.
            - The 3D position as a NumPy array, or None if no pose has been received yet.
            - The orientation as a `Rotation` object, or None if no pose has been received.
            - The raw 4x4 pose matrix as received from the teleop stream.
        """
        with self._android_lock:
            if self._latest_pose is None:
                return False, None, None, None
            p = self._latest_pose.copy()
            pose = self._latest_pose
        rot = Rotation.from_matrix(p[:3, :3])
        pos = p[:3, 3] - rot.apply(self.config.camera_offset)
        return True, pos, rot, pose

    def _android_callback(self, pose: np.ndarray, message: dict) -> None:
        """
        Callback function to handle incoming data from the Android teleop stream.

        This method is executed by the `teleop` package's subscriber thread whenever a new
        pose and message are received from the WebXR session on the Android phone. It updates
        the internal state (`_latest_pose` and `_latest_message`) with the new data.
        A thread lock is used to ensure that these shared variables are updated atomically,
        preventing race conditions with the main thread that reads them.

        Args:
            pose: A 4x4 NumPy array representing the phone's transformation matrix.
            message: A dictionary containing additional data, such as button presses or touch events.
        """
        with self._android_lock:
            self._latest_pose = pose
            self._latest_message = message

    @check_if_not_connected
    def get_action(self) -> dict:
        ok, raw_pos, raw_rot, pose = self._read_current_pose()
        if not ok or not self.is_calibrated:
            return {}

        # Collect raw inputs (B1 / analogs on iOS, move/scale on Android)
        raw_inputs: dict[str, float | int | bool] = {}
        msg = self._latest_message or {}
        raw_inputs["move"] = bool(msg.get("move", False))
        raw_inputs["scale"] = float(msg.get("scale", 1.0))
        raw_inputs["reservedButtonA"] = bool(msg.get("reservedButtonA", False))
        raw_inputs["reservedButtonB"] = bool(msg.get("reservedButtonB", False))

        enable = bool(raw_inputs.get("move", False))

        # Rising edge then re-capture calibration immediately from current raw pose
        if enable and not self._enabled:
            self._reapply_position_calibration(raw_pos)

        # Apply calibration
        pos_cal = self._calib_rot_inv.apply(raw_pos - self._calib_pos)
        rot_cal = self._calib_rot_inv * raw_rot

        self._enabled = enable

        return {
            "phone.pos": pos_cal,
            "phone.rot": rot_cal,
            "phone.raw_inputs": raw_inputs,
            "phone.enabled": self._enabled,
        }

    @check_if_not_connected
    def disconnect(self) -> None:
        self._teleop = None
        if self._teleop_thread and self._teleop_thread.is_alive():
            self._teleop_thread.join(timeout=1.0)
            self._teleop_thread = None
            self._latest_pose = None


class Phone(Teleoperator):
    """
    Phone-based teleoperator using ARKit (iOS via HEBI Mobile I/O App) or the teleop Python package (Android via WebXR API).
    For HEBI Mobile I/O we also expose 8 analog (a1-a8) and 8 digital (b1-b8) inputs.

    Press and hold **B1** to enable teleoperation. While enabled, the first B1 press
    captures a reference pose and rotation, when disabled and pressed again the position is reapplied.
    """

    config_class = PhoneConfig
    name = "phone"

    def __init__(self, config: PhoneConfig):
        super().__init__(config)
        self.config = config

        self._phone_impl: Teleoperator

        if self.config.phone_os == PhoneOS.IOS:
            self._phone_impl = IOSPhone(config)
        elif self.config.phone_os == PhoneOS.ANDROID:
            self._phone_impl = AndroidPhone(config)
        else:
            raise ValueError(f"Invalid config phone_os: {self.config.phone_os}")

    @property
    def is_connected(self) -> bool:
        return self._phone_impl.is_connected

    def connect(self) -> None:
        return self._phone_impl.connect()

    def calibrate(self) -> None:
        return self._phone_impl.calibrate()

    @property
    def is_calibrated(self) -> bool:
        return self._phone_impl.is_calibrated

    @property
    def action_features(self) -> dict[str, type]:
        return self._phone_impl.action_features

    @property
    def feedback_features(self) -> dict[str, type]:
        return self._phone_impl.feedback_features

    def configure(self) -> None:
        return self._phone_impl.configure()

    def get_action(self) -> dict:
        return self._phone_impl.get_action()

    def feedback_age_s(self) -> float:
        """Seconds since the last feedback packet (iOS only).

        Returns ``inf`` for backends that don't track timestamps (e.g. Android).
        """
        impl = self._phone_impl
        if hasattr(impl, "feedback_age_s"):
            return impl.feedback_age_s()
        return float("inf")

    def send_feedback(self, feedback: dict[str, float]) -> None:
        return self._phone_impl.send_feedback(feedback)

    def disconnect(self) -> None:
        return self._phone_impl.disconnect()
