# SWARM

SWARM is a terminal app for running Codex and Hermes agents on Debian. Each agent gets its own tab. You can use normal shell tabs alongside them and send messages to several agents at once.

Written in Python 3 with GTK 3 and VTE.

## Run

You need Debian 12 or newer with a graphical desktop. Install the harnesses you want to use separately: [Codex CLI](https://learn.chatgpt.com/docs/developer-commands?surface=cli) and/or [Hermes Agent](https://hermes-agent.nousresearch.com/docs/user-guide/cli). Make sure their commands, `codex` and `hermes`, are on your `PATH`.

From the SWARM source folder, install the desktop dependencies and launch it:

```sh
./install.sh
./swarm
```

The installer refreshes apt package lists and installs Python 3, PyGObject, PyYAML, GTK 3, and VTE. It uses `sudo` when needed and can be run again to ensure the dependencies are installed. Agent CLIs are installed separately.

SWARM uses `/usr/bin/python3` and requires VTE 0.68 or newer. No virtual environment is needed.

The first terminal starts in your home folder. To choose another folder or custom executable paths:

```sh
./swarm --directory /path/to/project --codex /absolute/path/to/codex
./swarm --hermes /absolute/path/to/hermes
```

If your shell setup adds these commands to `PATH`, launch SWARM from that configured shell or provide `--codex` / `--hermes` explicitly. `SWARM_CODEX` and `SWARM_HERMES` also set the executable paths.

## Terminals and agents

On startup, **Startup Swarm** shows three editable agent rows with these defaults:

| Agent | Startup command |
| --- | --- |
| Codex | `codex --yolo` |
| GLM-5.3 Flash Thinking · Hermes | `hermes chat --provider zai --model glm-5.3-flash --reasoning max --yolo` |
| DeepSeek V4.1 Flash · Hermes | `hermes chat --provider deepseek --model deepseek-flash --reasoning max --yolo` |

Edit names and commands, enable or disable rows, remove agents, or use **+ Add agent** for additional models or harnesses. Click **Save** to apply the form. Check **Don't show again at startup** to skip the dialog on future launches. Reopen it at any time through **Swarm > Adjust Startup Swarm**. Commands and this preference are saved in `$XDG_CONFIG_HOME/swarm/linked-agents.json` (normally `~/.config/swarm/linked-agents.json`). Older harness switches carry over to the default rows. Existing tabs keep their original launch command.

Click **Open Swarm** in the titlebar, or choose **Swarm > Open Swarm**, to open one tab per enabled row, in order, in the active terminal's folder. Each click opens a fresh set of tabs. If no rows are enabled, SWARM opens Startup Swarm. All executables are checked before any tabs are opened. Commands accept quoted arguments and executable paths with spaces; relative executable paths use the current folder. Commands run directly; for shell expansion or pipelines, use an explicit shell command such as `bash -lc 'your command'`. The names `codex` and `hermes` honor SWARM's `--codex` and `--hermes` executable overrides.

The initial terminal lets you `cd` to your project before opening a swarm. Once all swarm agents have started, that terminal closes automatically if its shell has no running jobs. Extra terminals you open yourself stay open, and a failed swarm launch keeps the initial terminal available.

Codex tabs launch with `codex --yolo`. This disables Codex command approvals and its sandbox; the agent runs with your user account's permissions. Manually launched Codex keeps the options you supplied. The GLM tab launches with `hermes chat --provider zai --model glm-5.3-flash --reasoning max --yolo`. DeepSeek launches with `hermes chat --provider deepseek --model deepseek-flash --reasoning max --yolo`, selecting the latest V4.1 Flash model with maximum reasoning effort. [DeepSeek’s release notes](https://api-docs.deepseek.com/updates/) report stronger coding-agent results than V4 Pro on several benchmarks. Both Hermes tabs bypass command approval prompts with `--yolo` and use the applicable SWARM or Hermes credentials and Hermes tools; these per-session options leave its saved default model in place. **Actions > Restart Exited Agent** restarts the tab's original harness.

Press **Ctrl+T** to open another agent using the selected agent tab's original command and folder, including custom models and harnesses. From a regular terminal or with no agent selected, it opens Codex in the current folder, so you can use `cd` first. **Ctrl+Shift+T** does the same. The shortcut and **Restart Exited Agent** keep the tab's original command even if its startup row has since been edited, disabled, or removed.

**Session > New Terminal** opens another shell in the current folder. Use `cd` to change folders. New windows have their own tabs and broadcasts. Closing SWARM does not save or restore sessions.

Interactive Codex launched in a shell is detected automatically, including `resume` and `fork`. Its tab becomes an agent while Codex is in the foreground. Returning to the shell, suspending it, or moving it to the background removes it from broadcasts. Background processes and noninteractive commands such as `login`, `exec`, and `app-server` are excluded.

The tab spinner runs while a Codex or Hermes agent is working. The bottom-left status shows one combined agent total and running count, for example **4 agents · 2 running**. The running count follows the tab spinners. Hermes activity comes from its terminal title in the Ink interface or its live composer indicator in the classic interface; unsupported or unavailable activity stays quiet. Codex agents opened through **Ctrl+T** or **Open Swarm** move to the far left after their Ready status stays stable for one second, newest first, without changing your selected tab. When one starts working again, it moves behind the idle agents. You can drag tabs within either group; running tabs stay outside the finished group. Custom harnesses have unavailable activity and do not qualify for automatic idle sorting or Sleeper Broadcast.

Click a tab and use **Left/Right** to navigate the tab strip. **Enter**, **Escape**, or clicking the terminal returns focus to terminal input. Typing also focuses the terminal. **Ctrl+PageUp/PageDown** switches tabs from terminal input. Right-click selected terminal text and choose **Copy**, or use **Ctrl+Shift+C**.

**Swarm > Interrupt All Agents** sends Escape to each running agent in the current window, including detected manual Codex sessions, to request that it stop its current work. Agent tabs stay open. Pending broadcast deliveries and their completion alerts are cancelled; text already pasted may remain in the prompt. Shell and sign-in tabs are unaffected. Hermes uses **Ctrl+C** in its terminal to interrupt work; the Escape action does not stop Hermes work.

**Swarm > Kill All Agents** stops and closes all agent tabs in the current window, including exited agents. If any agents are still working, a warning shows how many and lets you cancel. Shell and sign-in tabs stay open.

## Broadcasts

Text broadcasts submit through terminal input. Global, Sleeper, and Custom Broadcast accept multiple clipboard images: copy an image and press **Ctrl+V** in the editor, then repeat for each additional image. **Attach Clipboard Image** also appends an image. Each image has a numbered preview and its own remove button; **Clear all** removes the set. Images keep their paste order and can be sent with or without text. Sending waits until all pending clipboard reads finish.

Codex receives every image as an attachment. The installed Hermes CLI accepts one image path per message, so Hermes receives the first image as an attachment and additional images as ordered local file references with a request to inspect each file. This sends the complete set in one message; Hermes needs its image tools to inspect those additional files. Other harnesses receive accompanying text only and are excluded from image-only sends. The selected model must support image input. Attachments are saved as private PNG files in `$XDG_CACHE_HOME/swarm/broadcast-images` (normally `~/.cache/swarm/broadcast-images`) and retained for queued input and conversation history. Changing the clipboard after sending does not change the attachments.

Finish CLI onboarding and leave prompts empty before sending. Each harness decides how text input is handled while busy. Hermes and custom harnesses with compatible terminal input participate in Global and Custom Broadcast. Sleeper Broadcast and completion notifications remain limited to Codex. The notification option is disabled when a global broadcast includes other harnesses.

| Menu | Recipients |
| --- | --- |
| Global Broadcast | All running agents in this window, including detected manual sessions. |
| Sleeper Broadcast | Codex agents opened through Ctrl+T or Open Swarm that are ready when you send. Busy agents, approval prompts, and unavailable status are skipped. |
| Custom Broadcast | Only the running agents you select, including selected busy or manual sessions. |

Global Broadcast offers **Notify me when these agents finish**, off by default. It works with Codex agents opened through **Open Swarm**, **Ctrl+T**, and Codex launched manually in a terminal. It sends one desktop notification after all recipients have shown working activity and become idle. The alert follows each agent's activity indicator; an agent that never reports activity cannot signal completion. Closing or replacing a recipient, a failed delivery, or another broadcast to any of those agents cancels the pending alert. Notification display follows your desktop settings.

Custom Broadcast shows agent names, folders, and current activity. Use **Select all**, **Select idle**, or **Clear selection**. Selection is remembered until the window closes; new or restarted agents start unselected. **Select idle** takes a snapshot when clicked. Sleeper Broadcast checks readiness again during delivery.

Manual sessions can show **Status unavailable** and remain selectable for Custom Broadcast. They are excluded from Sleeper Broadcast because a missing spinner does not establish readiness. **Ctrl+T** and **Open Swarm** request an explicit status from Codex without changing your saved configuration.

All three menus are disabled without running agents. Sleeper Broadcast does not wait for busy agents to finish. If readiness changes during delivery, submission can be cancelled after text was pasted; review the affected prompt.

## API keys

Open **Actions > Add Key…**, paste a key, and choose **Save Key**. SWARM recognizes distinctive OpenAI, Anthropic, OpenRouter, Gemini, Groq, and xAI key prefixes, plus provider environment assignments such as `DEEPSEEK_API_KEY=…`. Detection happens locally. Generic `sk-…` keys do not identify their provider reliably: select **DeepSeek** (or the appropriate provider), or select its startup agent under **Use for**.

Keys can apply to all matching startup agents or to one specific row in **Swarm > Adjust Startup Swarm**. An agent-specific key takes precedence over the provider default. Other supported choices include Z.ai/GLM and Mistral; **Other / custom** lets you supply the environment variable expected by a selected harness. Use the saved-key list to replace or remove keys. New or restarted agents use the new key; existing processes keep their current credentials.

Keys are stored as plaintext in `$XDG_CONFIG_HOME/swarm/credentials.json` (normally `~/.config/swarm/credentials.json`), with owner-only file permissions. They are passed through the child environment and private launch configuration, never process arguments. Hermes launches use the selected key without overwriting the main Hermes profile. A saved OpenAI key selects API authentication for SWARM-launched Codex sessions while preserving the existing ChatGPT login. Without a SWARM key, the harness keeps using its existing authentication.

Assigning a SWARM key to Hermes currently requires a command without `--profile`. Named Hermes profiles and explicit local/custom Codex accounts show **N/A** when SWARM cannot identify the account to track.

## Account and usage

SWARM reuses Codex's saved sign-in. The Session menu provides ChatGPT and device-code sign-in, plus logout. Logout runs `codex logout` and removes the shared saved login for your operating-system user, including use outside SWARM. Already-running sessions may retain authentication until restarted. Your ChatGPT browser session is separate. These sign-in controls apply only to Codex; API keys are managed separately through **Actions > Add Key…**.

The top-left trackers follow the enabled startup agents. Adding a fourth agent adds a badge; changing, disabling, or removing a row updates its tracker. Saving or removing a key refreshes usage for the selected credentials. Extra badges show the agent name and can be scrolled horizontally when space is limited. Agents using the same provider account/key share its usage. Provider detection uses the startup command or your explicit key assignment.

SWARM supports Codex ChatGPT limits, Z.ai Coding Plan quota, DeepSeek credit, and [OpenRouter per-key spending caps](https://openrouter.ai/docs/api/api-reference/api-keys/get-current-api-key). OpenRouter keys without a cap show **N/A** with available spending details. Other providers, custom harnesses, and OpenAI API billing show **N/A**; selecting a provider alone cannot supply a remaining percentage. Click a badge for details and refresh controls. Usage checks do not send model prompts.

The titlebar shows your remaining Codex usage, including accounts with only a weekly limit. Click or hover to see limits, reset times, and remaining usage resets when the CLI reports them. Usage refreshes every minute while signed in and can be refreshed manually. If limits cannot be read, the badge shows no percentage. Reading usage uses Codex's existing authentication and does not send a prompt.

For the default GLM agent, an **orange percentage** shows Z.ai Coding Plan usage left. It uses the applicable SWARM key, falling back to Hermes's saved Z.ai key, and refreshes every minute independently of Codex sign-in. The percentage follows the shortest model usage window (normally five hours); hover or click to see both that window and the weekly limit, with reset times when reported. Z.ai shares this subscription quota across GLM models. Search and other tool quotas are excluded. Click the badge or its **Refresh** button for an immediate update. Missing credentials or failed requests show **—%** instead of an outdated number.

A **blue percentage to the right of GLM** tracks DeepSeek prepaid API credit. DeepSeek's [balance API](https://api-docs.deepseek.com/api/get-user-balance/) reports current money, without a subscription quota or lifetime deposit total. SWARM shows the current balance as a percentage of the **highest balance it has observed**, separately for each API key and currency. It starts at 100% on the first successful check. Spending reduces it; a balance above the previous high updates the baseline. A top-up below that high increases the percentage without resetting it to 100%. Hover or click to see the actual USD/CNY balance and tracked baseline. This is a credit indicator, not a daily/weekly allowance. It refreshes every minute and clears to **—%** if a request fails. Balance checks do not send model prompts.

Add a DeepSeek key through **Actions > Add Key…**. Existing `DEEPSEEK_API_KEY` settings in `~/.hermes/.env` (or your `HERMES_HOME/.env`) remain a fallback until a SWARM key is saved. Balance baselines are stored under `$XDG_STATE_HOME/swarm/deepseek-balance.json` (normally `~/.local/state/swarm/deepseek-balance.json`), identified by a hash of the key. Keep this file to preserve the baseline across launches. Older Hermes versions may display the compatibility alias `deepseek-v4-flash`; DeepSeek currently routes that alias to V4.1 Flash.

The old `deepseek.txt` watcher is disabled by default. For compatibility, explicitly setting `SWARM_DEEPSEEK_KEY_FILE=/absolute/path/to/deepseek.txt` enables it at startup and every two seconds. That opt-in watcher updates Hermes's `.env`; a key saved through SWARM still takes precedence. Local credential files such as `deepseek.txt` are ignored by Git.

## Optional installation

Install under `~/.local` and add an application-menu entry:

```sh
./scripts/install-local.sh
```

Run it again to update. To remove that copy:

```sh
./scripts/uninstall-local.sh
```

These scripts accept `PREFIX=/absolute/path`. Removing SWARM leaves Codex settings and conversations in place.

Build a Debian package with `dpkg-deb`, then install it:

```sh
./scripts/build-deb.sh
sudo apt install ./dist/swarm-terminal_0.1.14_all.deb
```

Building needs no root access. Remove the package with `sudo apt remove swarm-terminal`.

## Shortcuts

| Action | Shortcut |
| --- | --- |
| New agent using the selected harness | Ctrl+T or Ctrl+Shift+T |
| New terminal | Ctrl+Shift+Return |
| New window | Ctrl+Shift+N |
| Global broadcast | Ctrl+Shift+B |
| Close tab | Ctrl+Shift+W |
| Copy / paste | Ctrl+Shift+C / Ctrl+Shift+V |
| Previous / next tab | Ctrl+PageUp / Ctrl+PageDown |
| Previous / next tab after clicking a tab | Left / Right |
| Fullscreen | F11 |

## Source

- `swarm`: launcher; `swarm_app/__main__.py`: command-line options.
- `app.py`: windows, menus, and broadcast controls.
- `clipboard_image.py`: clipboard image attachments for broadcasts.
- `session.py`: terminal sessions and message delivery.
- `activity.py` and `codex_detection.py`: activity and foreground process detection.
- `custom_broadcast.py`: recipient picker; `usage.py`: Codex usage queries; `zai_usage.py`: Z.ai Coding Plan usage queries; `deepseek_usage.py`: DeepSeek prepaid balance tracking using Hermes credentials.
- `deepseek_credentials.py`: automatic `deepseek.txt` synchronization into Hermes credentials.
- `linked_agents.py` and `linked_agents_dialog.py`: saved startup commands and their configuration window.
- `credentials.py` and `credentials_dialog.py`: saved provider/agent keys, local detection, and launch authentication.
- `agent_usage.py` and `provider_usage.py`: dynamic startup-agent badges and provider-specific usage reads.
- `notifications.py`: desktop completion alerts.
- `scripts/` and `packaging/`: installation and Debian packaging.
- `tests/`: terminal fixtures and tests.

Python modules listed above are under `swarm_app/`.

With `xvfb` and `xauth` installed, run the existing tests from the source directory:

```sh
xvfb-run -a /usr/bin/python3 -m unittest discover -s tests -v
```

## License

MIT. See [LICENSE](LICENSE).
