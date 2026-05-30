"""Enable ``python -m exacqman`` to run the CLI.

The web service spawns the CLI via ``[sys.executable, "-m", "exacqman", ...]``
(see ``exacqman.web.services.exacqman_service``), which routes here.
"""

from exacqman.cli import main

if __name__ == "__main__":
    main()
