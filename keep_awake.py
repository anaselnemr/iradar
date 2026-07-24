"""Hold Windows awake for the duration of a capture run, then let it sleep again.

Run 127 and run 136 were both killed when the machine idled into modern-standby
mid-run (network drops, the python process is terminated - no crash log, no
Kernel-Power event). This asks Windows to stay awake via SetThreadExecutionState
(the same call `caffeine`/`presentationsettings` use). It is NOT a settings
change: the request lives only while THIS process runs and is cleared on exit,
and the whole thing self-terminates after a hard cap so it can never linger and
keep the laptop awake for days.

Note: this prevents IDLE sleep only. A manual lid-close power action still
sleeps the machine - nothing short of a power-plan change can stop that, and we
don't touch the user's power plan.
"""
import ctypes
import sys
import time

ES_CONTINUOUS = 0x80000000
ES_SYSTEM_REQUIRED = 0x00000001

CAP_SECONDS = 6 * 3600            # hard ceiling; the run should need ~2-3 h
REASSERT_EVERY = 60

kernel32 = ctypes.windll.kernel32


def main():
    elapsed = 0
    try:
        while elapsed < CAP_SECONDS:
            # ES_CONTINUOUS makes the request stick; re-asserting is belt-and-braces
            kernel32.SetThreadExecutionState(ES_CONTINUOUS | ES_SYSTEM_REQUIRED)
            time.sleep(REASSERT_EVERY)
            elapsed += REASSERT_EVERY
    finally:
        kernel32.SetThreadExecutionState(ES_CONTINUOUS)   # release the hold


if __name__ == "__main__":
    sys.exit(main())
