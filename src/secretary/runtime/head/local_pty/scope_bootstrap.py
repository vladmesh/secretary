"""Set the kernel's group OOM contract before dropping scope privileges.

The supervisor is the sole protected process. The head clears the inherited protection before
exec, so a cgroup OOM kills the whole head process tree while the supervisor can journal its exit.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

from ..memory import OOM_STREAM_ENV, open_oom_stream, own_cgroup


def install_oom_contract(cgroup: Path, protection: Path = Path("/proc/self/oom_score_adj")) -> None:
    (cgroup / "memory.oom.group").write_text("1\n", encoding="ascii")
    protection.write_text("-1000\n", encoding="ascii")


def main(argv: list[str] | None = None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    divider = args.index("--")
    privileges, command = args[:divider], args[divider + 1 :]
    cgroup = own_cgroup()
    if cgroup is None or not cgroup.name.startswith("secretary-head-"):
        raise RuntimeError("scope bootstrap is outside a secretary head scope")
    install_oom_contract(cgroup)
    os.environ[OOM_STREAM_ENV] = str(open_oom_stream())
    os.execvp("setpriv", ["setpriv", *privileges, *command])
    return 127


if __name__ == "__main__":  # pragma: no cover - execs the scoped supervisor
    raise SystemExit(main())
