"""python -m strategy_validation validate --strategy ID --dataset FILE ... (see strategy_validation/cli.py). Research only; never orders."""
import sys

from strategy_validation.cli import main

if __name__ == "__main__":
    sys.exit(main())
