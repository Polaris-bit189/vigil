"""支持 `python -m vigil <命令>`。"""

import sys

from .cli import main

if __name__ == "__main__":
    sys.exit(main())
