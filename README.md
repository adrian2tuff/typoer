# Typoer for macOS

A small Python utility that captures a prompt with global hotkeys, sends it to the OpenAI Responses API, and types the answer into the currently focused app. It also supports typing clipboard contents and changing the simulated typing speed while it runs.

> **Note:** Typoer uses macOS keyboard monitoring and Accessibility permissions. Review the code before granting permissions. API requests are billed to your OpenAI API account; **Fast mode can cost more than standard processing**.

## Features

- Global prompt capture using macOS Quartz keyboard events
- OpenAI Responses API integration
- **Fast mode enabled by default** using `service_tier="fast"`
- Low reasoning effort by default to favor response speed
- Human-like typing delays and occasional corrections
- Live typing-speed controls
- Clipboard commands

## Requirements

- macOS
- Python 3.10 or newer
- An OpenAI API key with API billing enabled
- Accessibility and Input Monitoring permissions for the app running Typoer

## Installation

Clone the repository and enter its directory:

```bash
git clone https://github.com/adrian2tuff/typoer.git
cd typoer
```

Create and activate a virtual environment:

```bash
python3 -m venv .venv
source .venv/bin/activate
```

Install dependencies:

```bash
python -m pip install -r requirements.txt
```

## Configure your API key

Set your key in the same Terminal session you use to launch Typoer:

```bash
export OPENAI_API_KEY="your-api-key"
```

Do not put your real API key in source code, README files, screenshots, or commits. If you need a key, create one through the OpenAI Platform API keys page.

Optional environment variables:

| Variable | Default | Purpose |
| --- | --- | --- |
| `OPENAI_MODEL` | `gpt-5.6` | Model used for responses |
| `OPENAI_SERVICE_TIER` | `fast` | API processing tier; set to `default` to use standard processing |
| `OPENAI_REASONING_EFFORT` | `low` | Reasoning effort; support depends on the selected model |
| `TYPOER_WPM` | `180` | Initial typing speed |
| `OPENAI_TIMEOUT` | `300` | API request timeout in seconds |
| `OPENAI_SYSTEM_PROMPT` | built-in prompt | Override the default response instructions |

Fast mode provides lower-latency processing on supported models, but it is billed at a premium relative to standard processing. Availability depends on the selected model and API account. See the [Fast mode documentation](https://developers.openai.com/api/docs/guides/fast-mode) for current details.

## Run

With the virtual environment active and `OPENAI_API_KEY` set:

```bash
python typoer_mac.py
```

On first launch, macOS may block global keyboard monitoring or simulated pasting. Open **System Settings → Privacy & Security** and grant the necessary **Accessibility** and **Input Monitoring** permissions to Terminal (or the app you use to launch the script). Quit and relaunch Typoer after changing permissions.

## Hotkeys

| Shortcut | Action |
| --- | --- |
| Control + Option + U | Start capturing a prompt |
| Control + Option + I | Send the captured prompt |
| Escape | Stop the current generated typing |
| Control + Option + ↑ | Increase typing speed by 10 WPM |
| Control + Option + ↓ | Decrease typing speed by 10 WPM (minimum 10 WPM) |
| Control + Option + 0 | Log current typing speed in Terminal |
| Control + C in Terminal | Quit Typoer |

### Special prompts

- `/clip` — use the current clipboard contents as the API prompt.
- `/clipboard` — type the current clipboard contents locally without an API request.
- `/kill` — stop Typoer.

## Troubleshooting

- **Keyboard shortcuts do nothing:** Check Input Monitoring and Accessibility permissions for the app running Python, then restart the script.
- **Pasting fails:** Enable Accessibility for Terminal (or the launcher) in System Settings → Privacy & Security → Accessibility.
- **API key error:** Confirm `OPENAI_API_KEY` is set in the same Terminal session.
- **Unsupported model or service tier:** Set `OPENAI_MODEL` to a model available to your API project, or set `OPENAI_SERVICE_TIER=default` if Fast mode is unavailable.

## Security

- Never commit API keys or other secrets.
- Keep `.venv/`, `.env`, and local editor files out of Git.
- Keyboard monitoring permissions are powerful; grant them only to software you trust.

## License

No license has been specified yet. Unless a license is added, others do not automatically receive permission to reuse, distribute, or modify this project.
