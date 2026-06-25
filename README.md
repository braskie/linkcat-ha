# Linkcat Home Assistant Integration

A HACS-compatible Home Assistant custom integration that logs into Linkcat and scrapes account data for checkouts and holds.

## Features

- Config flow with Linkcat username and password
- Periodic scraping of account data
- Default poll interval: 24 hours
- Sensors for:
  - Number of checked out items
  - Number of total holds
  - Number of ready holds
  - Per-item attributes for checkouts/holds including title, author, and image URL (when available)

## Installation (HACS)

1. In HACS, add this repository as a custom repository (type: Integration).
2. Install **Linkcat Library**.
3. Restart Home Assistant.
4. Go to **Settings -> Devices & Services -> Add Integration**.
5. Search for **Linkcat Library** and enter your Linkcat credentials.
6. Optional: Open the integration options and set **Poll interval (hours)**.

## Notes

- This integration uses Playwright for browser automation.
- Linkcat has no public API; data is collected by logging in and scraping pages.
- If Linkcat changes its HTML, selector updates may be required.

## Troubleshooting

- If setup fails with a Playwright browser error, install Chromium for Playwright in your Home Assistant Python environment:

```bash
playwright install chromium
```

- If your Home Assistant environment cannot install browser binaries, run Home Assistant where Playwright browser dependencies are available (for example, a Python venv or container image with Playwright browsers installed).

## Development

Place the integration under `custom_components/linkcat` in your Home Assistant config directory.
