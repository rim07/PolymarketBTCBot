"""Manual kill switch: presence of the flag file halts all new order placement.

Usage:
  python kill_switch.py on    # halt trading
  python kill_switch.py off   # resume trading
  python kill_switch.py       # print status
"""
import sys

import config


def is_engaged() -> bool:
    return config.KILL_SWITCH_FLAG_PATH.exists()


def engage(reason: str = "manual") -> None:
    config.KILL_SWITCH_FLAG_PATH.write_text(reason, encoding="utf-8")


def disengage() -> None:
    config.KILL_SWITCH_FLAG_PATH.unlink(missing_ok=True)


if __name__ == "__main__":
    arg = sys.argv[1].lower() if len(sys.argv) > 1 else ""
    if arg == "on":
        engage("manual cli")
        print("Kill switch ENGAGED — no new orders will be placed.")
    elif arg == "off":
        disengage()
        print("Kill switch DISENGAGED — trading may resume.")
    else:
        print("Kill switch ENGAGED" if is_engaged() else "Kill switch OFF (trading allowed)")
