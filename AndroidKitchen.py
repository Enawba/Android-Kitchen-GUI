"""Android Kitchen - GUI launcher.

    python AndroidKitchen.py [firmware]          # GUI
    python AndroidKitchen.py --cli <firmware> <workspace> [--all] [--full]
"""
import os
import sys

if __name__ == "__main__":
    if len(sys.argv) > 1 and sys.argv[1] == "--cli":
        from kitchen.cli import main
        sys.exit(main(sys.argv[2:]))
    from kitchen.gui import main
    main()
