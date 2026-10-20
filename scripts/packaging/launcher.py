"""Entry script for the PyInstaller bundle.

Does exactly what ``python -m offline_doc_tools`` does (see ``offline_doc_tools/__main__.py``).
PyInstaller needs a plain script as its entry point; ``offline_doc_tools/app.py`` itself cannot
be one because it uses relative imports.
"""

from offline_doc_tools.app import main

raise SystemExit(main())
