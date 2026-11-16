"""Entry point PyInstaller freezes into the Vesper engine (vesper-engine)."""

import multiprocessing
import sys

if __name__ == "__main__":
    # Python starts helper processes (the multiprocessing resource tracker, which tqdm needs while
    # the dictation model loads) by running this same binary with `-c ...`. freeze_support() runs
    # the helper and exits. Without it the helper started a second engine that never quit.
    multiprocessing.freeze_support()
    if "-c" in sys.argv[1:]:  # any other interpreter command is not ours to run as an engine
        sys.exit(f"vesper-engine: unsupported arguments {sys.argv[1:]!r}")

    from vesper.desktop import main

    main()
