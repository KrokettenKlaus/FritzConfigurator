import sys

if len(sys.argv) > 1:
    from .cli import main
    sys.exit(main())
else:
    from .gui import main as gui_main
    gui_main()
