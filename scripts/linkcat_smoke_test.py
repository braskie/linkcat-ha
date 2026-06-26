r"""Local smoke test for Linkcat login/scraping.

Usage (PowerShell):
  $env:LINKCAT_USERNAME="your_barcode"
  $env:LINKCAT_PASSWORD="your_pin"
  .\.venv\Scripts\python.exe .\scripts\linkcat_smoke_test.py

Optional:
  .\.venv\Scripts\python.exe .\scripts\linkcat_smoke_test.py --fetch
"""

from __future__ import annotations

import argparse
import asyncio
import importlib
import os
import sys
import types
from pathlib import Path


def _prepare_import_path() -> None:
    """Load linkcat_client without importing Home Assistant runtime modules."""
    repo_root = Path(__file__).resolve().parents[1]
    custom_components_dir = repo_root / "custom_components"
    linkcat_dir = custom_components_dir / "linkcat"

    if not custom_components_dir.exists() or not linkcat_dir.exists():
        raise RuntimeError("Could not locate custom_components/linkcat in this repository.")

    if "custom_components" not in sys.modules:
        pkg = types.ModuleType("custom_components")
        pkg.__path__ = [str(custom_components_dir)]
        sys.modules["custom_components"] = pkg

    if "custom_components.linkcat" not in sys.modules:
        subpkg = types.ModuleType("custom_components.linkcat")
        subpkg.__path__ = [str(linkcat_dir)]
        sys.modules["custom_components.linkcat"] = subpkg


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Smoke test Linkcat login from local VS Code environment")
    parser.add_argument(
        "--fetch",
        action="store_true",
        help="After credential validation, fetch and print checkout/hold counts",
    )
    parser.add_argument(
        "--dump-html-dir",
        type=str,
        default="",
        help="Optional directory where raw Linkcat HTML pages should be saved",
    )
    return parser.parse_args()


async def _run(fetch: bool, dump_html_dir: str) -> int:
    username = os.getenv("LINKCAT_USERNAME", "").strip()
    password = os.getenv("LINKCAT_PASSWORD", "").strip()

    if not username or not password:
        print("RESULT=MISSING_ENV")
        print("Set LINKCAT_USERNAME and LINKCAT_PASSWORD before running.")
        return 2

    if dump_html_dir:
        os.environ["LINKCAT_DEBUG_HTML_DIR"] = dump_html_dir
        print(f"HTML_DUMP_DIR={dump_html_dir}")

    _prepare_import_path()
    module = importlib.import_module("custom_components.linkcat.linkcat_client")

    LinkcatClient = module.LinkcatClient
    LinkcatAuthError = module.LinkcatAuthError
    LinkcatConnectionError = module.LinkcatConnectionError

    client = LinkcatClient(username=username, password=password)

    try:
        await client.validate_credentials()
        print("RESULT=AUTH_OK")

        if fetch:
            data = await client.fetch_account_data()
            print(f"CHECKOUTS={data.checkout_count}")
            print(f"HOLDS={data.hold_count}")
            print(f"READY_HOLDS={data.ready_hold_count}")
    except LinkcatAuthError as exc:
        print("RESULT=AUTH_ERROR")
        print(str(exc))
        return 1
    except LinkcatConnectionError as exc:
        print("RESULT=CONNECTION_ERROR")
        print(str(exc))
        return 1
    except Exception as exc:  # pragma: no cover - debug guard for local test use
        print("RESULT=UNEXPECTED_ERROR")
        print(type(exc).__name__)
        print(str(exc))
        return 1

    return 0


def main() -> int:
    args = _parse_args()
    return asyncio.run(_run(fetch=args.fetch, dump_html_dir=args.dump_html_dir))


if __name__ == "__main__":
    raise SystemExit(main())
