#!/usr/bin/env python
"""Print raw phone state to diagnose teleop inputs (no robot involved)."""
import time
from lerobot.teleoperators.phone import Phone, PhoneConfig
from lerobot.teleoperators.phone.config_phone import PhoneOS


def main() -> None:
    teleop = Phone(PhoneConfig(phone_os=PhoneOS.IOS))
    teleop.connect()  # prompts for B1 calibration

    print("\nStreaming phone state. Press & hold B1, move phone, watch values. Ctrl-C to quit.\n")
    frames = 0
    t_window = time.perf_counter()
    try:
        while True:
            t0 = time.perf_counter()
            obs = teleop.get_action()  # blocking: paced by HEBI feedback rate
            dt_read_ms = (time.perf_counter() - t0) * 1e3
            frames += 1
            # Rolling FPS over ~1 s
            elapsed = time.perf_counter() - t_window
            if elapsed >= 1.0:
                fps = frames / elapsed
                frames = 0
                t_window = time.perf_counter()
            else:
                fps = None

            if not obs:
                continue
            pos = obs["phone.pos"]
            enabled = obs["phone.enabled"]
            raw = obs["phone.raw_inputs"]
            b1 = raw.get("b1")
            a3 = raw.get("a3")
            b1_s = "-" if b1 is None else str(b1)
            a3_s = "-" if a3 is None else f"{float(a3):+.2f}"
            fps_s = f"{fps:5.1f}" if fps is not None else "  ...  "
            print(
                f"fps={fps_s}  read={dt_read_ms:5.1f}ms  "
                f"enabled={enabled!s:5}  b1={b1_s}  a3={a3_s}  "
                f"pos=[{pos[0]:+.3f},{pos[1]:+.3f},{pos[2]:+.3f}]",
                flush=True,
            )
    except KeyboardInterrupt:
        print("\nDone.")
    finally:
        if teleop.is_connected:
            teleop.disconnect()


if __name__ == "__main__":
    main()
