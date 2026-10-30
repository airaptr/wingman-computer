#!/usr/bin/env python3
"""Launch a session on its quota's CPUs, leaving VM-overhead CPUs to the guest agent."""
import os
from pathlib import Path
import sys


def quota_affinity(available, cpu_max):
    quota, period = cpu_max.split()
    period = int(period)
    if period <= 0:
        raise ValueError("the CPU quota period must be positive")
    if quota == "max":
        return set(available)
    quota = int(quota)
    if quota <= 0:
        raise ValueError("the CPU quota must be positive")
    count = (quota + period - 1) // period
    return set(sorted(available)[:count])


def main():
    if len(sys.argv) < 2:
        sys.exit("wingman: a session command is required")
    maximum = Path("/sys/fs/cgroup/cpu.max")
    if maximum.is_file():
        try:
            affinity = quota_affinity(os.sched_getaffinity(0), maximum.read_text())
            os.sched_setaffinity(0, affinity)
        except (OSError, ValueError) as error:
            sys.exit(f"wingman: cannot apply the session CPU quota: {error}")
    os.execvp(sys.argv[1], sys.argv[1:])


if __name__ == "__main__":
    main()
