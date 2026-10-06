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

The installer refreshes apt package lists and installs Python 3, PyGObject, GTK 3, and VTE. It uses `sudo` when needed and can be run again to ensure the dependencies are installed. Agent CLIs are installed separately.

SWARM uses `/usr/bin/python3` and requires VTE 0.68 or newer. No virtual environment is needed.

The first terminal starts in your home folder. To choose another folder or custom executable paths:

```sh
./swarm --directory /path/to/project --codex /absolute/path/to/codex
./swarm --hermes /absolute/path/to/hermes
```

If your shell setup adds these commands to `PATH`, launch SWARM from that configured shell or provide `--codex` / `--hermes` explicitly. `SWARM_CODEX` and `SWARM_HERMES` also set the executable paths.

## Terminals and agents

Choose **Actions > Linked Agents** to turn Codex and Hermes on or off. Both start enabled. Changes are saved immediately in `$XDG_CONFIG_HOME/swarm/linked-agents.json` (normally `~/.config/swarm/linked-agents.json`) and apply across windows and future launches. Changing a switch leaves existing tabs running.

Click **Open Swarm** in the titlebar, or choose **Swarm > Open Swarm**, to open one tab per enabled harness in the active terminal's folder. With both enabled, you get a Codex tab and a Hermes tab; with Hermes off, you get only Codex. Each click opens a fresh set of tabs. If both are off, SWARM opens Linked Agents so you can enable one. If an enabled command is missing, SWARM reports it before opening any tabs.

The initial terminal lets you `cd` to your project before opening a swarm. Once all swarm agents have started, that terminal closes automatically if its shell has no running jobs. Extra terminals you open yourself stay open, and a failed swarm launch keeps the initial terminal available.

Codex tabs launch with `codex --yolo`. This disables Codex command approvals and its sandbox; the agent runs with your user account's permissions. Manually launched Codex keeps the options you supplied. Hermes tabs launch with `hermes --yolo`, which bypasses its dangerous-command approval prompts; it otherwise uses its own configuration and sign-in. **Actions > Restart Exited Agent** restarts the tab's original harness.

Press **Ctrl+T** to open another agent using the selected agent tab's harness and folder: Hermes from a Hermes tab, or Codex from a Codex tab. From a regular terminal or with no agent selected, it opens Codex in the current folder, so you can use `cd` first. **Ctrl+Shift+T** does the same. Linked Agents switches control which harnesses **Open Swarm** opens; the shortcut follows the selected tab even if that harness's switch is off.

**Session > New Terminal** opens another shell in the current folder. Use `cd` to change folders. New windows have their own tabs and broadcasts. Closing SWARM does not save or restore sessions.

Interactive Codex launched in a shell is detected automatically, including `resume` and `fork`. Its tab becomes an agent while Codex is in the foreground. Returning to the shell, suspending it, or moving it to the background removes it from broadcasts. Background processes and noninteractive commands such as `login`, `exec`, and `app-server` are excluded.

The tab spinner runs while a Codex or Hermes agent is working. The bottom-left status shows one combined agent total and running count, for example **4 agents · 2 running**. The running count follows the tab spinners. Hermes activity comes from its terminal title in the Ink interface or its live composer indicator in the classic interface; unsupported or unavailable activity stays quiet. Codex agents opened through **Ctrl+T** or **Open Swarm** move to the far left when they become idle, newest first, without changing your selected tab. You can still drag tabs around; an agent moves automatically again only when it next becomes ready.

**Swarm > Interrupt All Agents** sends Escape to each running agent in the current window, including detected manual Codex sessions, to request that it stop its current work. Agent tabs stay open. Pending broadcast deliveries and their completion alerts are cancelled; text already pasted may remain in the prompt. Shell and sign-in tabs are unaffected. Hermes uses **Ctrl+C** in its terminal to interrupt work; the Escape action does not stop Hermes work.

**Swarm > Kill All Agents** stops and closes all agent tabs in the current window, including exited agents. If any agents are still working, a warning shows how many and lets you cancel. Shell and sign-in tabs stay open.

## Broadcasts

All broadcasts submit through terminal input. Finish CLI onboarding and leave prompts empty before sending. Each harness decides how input is handled while busy. Hermes tabs participate in Global and Custom Broadcast. Sleeper Broadcast and completion notifications remain limited to Codex. The notification option is disabled when a global broadcast includes Hermes.

| Menu | Recipients |
| --- | --- |
| Global Broadcast | All running agents in this window, including detected manual sessions. |
| Sleeper Broadcast | Codex agents opened through Ctrl+T or Open Swarm that are ready when you send. Busy agents, approval prompts, and unavailable status are skipped. |
| Custom Broadcast | Only the running agents you select, including selected busy or manual sessions. |

Global Broadcast offers **Notify me when these agents finish**, off by default. It works with Codex agents opened through **Open Swarm**, **Ctrl+T**, and Codex launched manually in a terminal. It sends one desktop notification after all recipients have shown working activity and become idle. The alert follows each agent's activity indicator; an agent that never reports activity cannot signal completion. Closing or replacing a recipient, a failed delivery, or another broadcast to any of those agents cancels the pending alert. Notification display follows your desktop settings.

Custom Broadcast shows agent names, folders, and current activity. Use **Select all**, **Select idle**, or **Clear selection**. Selection is remembered until the window closes; new or restarted agents start unselected. **Select idle** takes a snapshot when clicked. Sleeper Broadcast checks readiness again during delivery.

Manual sessions can show **Status unavailable** and remain selectable for Custom Broadcast. They are excluded from Sleeper Broadcast because a missing spinner does not establish readiness. **Ctrl+T** and **Open Swarm** request an explicit status from Codex without changing your saved configuration.

All three menus are disabled without running agents. Sleeper Broadcast does not wait for busy agents to finish. If readiness changes during delivery, submission can be cancelled after text was pasted; review the affected prompt.

## Account and usage

SWARM reuses Codex's saved sign-in. The Session menu provides ChatGPT and device-code sign-in, plus logout. Logout runs `codex logout` and removes the shared saved login for your operating-system user, including use outside SWARM. Already-running sessions may retain authentication until restarted. Your ChatGPT browser session is separate. These controls and the usage badge apply only to Codex; configure Hermes authentication in Hermes itself.

The titlebar shows your remaining usage, including accounts with only a weekly limit. Click or hover to see limits, reset times, and remaining usage resets when the CLI reports them. Usage refreshes every minute while signed in and can be refreshed manually. If limits cannot be read, the badge shows no percentage. Reading usage uses Codex's existing authentication and does not send a prompt.

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
| Fullscreen | F11 |

## Source

- `swarm`: launcher; `swarm_app/__main__.py`: command-line options.
- `harnesses.py`: the harness registry. Each harness (Codex, Hermes) is a class
  holding its launch command, activity readers, and capability flags; adding a
  harness means adding one class there, with no call-site edits elsewhere.
- `app.py`: windows, menus, and broadcast controls.
- `session.py`: terminal sessions and message delivery.
- `activity.py` and `codex_detection.py`: activity parsing and Codex foreground detection.
- `custom_broadcast.py`: recipient picker; `usage.py`: account usage queries.
- `linked_agents.py` and `linked_agents_dialog.py`: saved harness switches and their configuration window.
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
