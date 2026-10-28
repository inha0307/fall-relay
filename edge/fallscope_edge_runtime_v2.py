#!/usr/bin/env python3
"""Former name of fall_relay_edge_runtime.py, kept for bundles and service units that still start it by this name.

The runtime was developed as FallScope; the file was renamed for the 1.0.0 release. This shim re-exports the
module and runs the same main(). New installations should start fall_relay_edge_runtime.py directly.
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from fall_relay_edge_runtime import *  # noqa: E402,F401,F403
from fall_relay_edge_runtime import main  # noqa: E402

if __name__ == "__main__":
    main()
