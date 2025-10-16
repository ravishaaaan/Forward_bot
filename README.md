Setup and run

1. Create and activate a virtual environment (recommended Python 3.8-3.11):

   python3 -m venv .venv
   source .venv/bin/activate

2. Install dependencies:

   pip install -r requirements.txt

Notes and troubleshooting

- I installed `python-telegram-bot==13.15` in this workspace's `.venv` using the system Python (3.13). That package and some of its vendored dependencies may not be fully compatible with Python 3.13; you may see import warnings or errors.
- If you experience import errors related to bundled vendored modules (for example, missing `telegram.vendor.ptb_urllib3.urllib3.packages.six.moves`), install a supported Python version (3.8–3.11) and recreate the venv with that interpreter.

Quick verification

After activating the venv:

   python -c "import telegram; print('telegram', telegram.__version__)"

If that errors, try recreating the venv with Python 3.10 or 3.11.
