"""Baked into the runtime image (`relay-sandbox-runtime`); never user-controlled — the code it
execs is. Loads `/work/inputs/inputs.json` as a pre-populated `inputs` global, then execs
`/work/inputs/code.py` with the working directory set to `/work/outputs` (a tmpfs mount — files
written there are retrieved by the sandbox service after the container exits, see
docs/system-design.md section 10.7 and ../runner.py).
"""

import json
import os
import sys
import traceback

_INPUTS_DIR = "/work/inputs"
_OUTPUTS_DIR = "/work/outputs"


def main() -> None:
    with open(os.path.join(_INPUTS_DIR, "inputs.json")) as f:
        inputs = json.load(f)
    with open(os.path.join(_INPUTS_DIR, "code.py")) as f:
        code = f.read()

    os.chdir(_OUTPUTS_DIR)
    try:
        exec(compile(code, "<user_code>", "exec"), {"inputs": inputs, "__name__": "__main__"})
    except Exception:
        traceback.print_exc()
        sys.exit(1)


if __name__ == "__main__":
    main()
