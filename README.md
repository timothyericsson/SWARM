# SWARM

SWARM is a terminal app for running multiple Codex CLI agents on Debian. Each agent gets its own tab. You can use normal shell tabs alongside them and send messages to several agents at once.

Written in Python 3 with GTK 3 and VTE.

## Run

You need Debian 12 or newer with a graphical desktop and [Codex CLI](https://learn.chatgpt.com/docs/developer-commands?surface=cli) installed separately. Make sure `codex` is on your `PATH`.

From the SWARM source folder, install the desktop dependencies and launch it:

```sh
sudo apt install python3 python3-gi gir1.2-gtk-3.0 gir1.2-vte-2.91
./swarm
```

SWARM uses `/usr/bin/python3` and requires VTE 0.68 or newer. No virtual environment is needed.

The first terminal starts in your home folder. To choose another folder or Codex executable:

```sh
./swarm --directory /path/to/project --codex /absolute/path/to/codex
```

If tools such as nvm add Codex to your shell's `PATH`, launch SWARM from that configured shell or provide `--codex` explicitly.

## Terminals and agents

Use `cd` normally, then choose **Swarm > New Agent**. It runs `codex --yolo` in the active shell's folder. This disables Codex command approvals and its sandbox; the agent runs with your user account's permissions. Manually launched Codex keeps the options you supplied.

**Session > New Terminal** opens another shell. **Open Terminal in Folder** lets you choose its folder. New windows have their own tabs and broadcasts. Closing SWARM does not save or restore sessions.

Interactive Codex launched in a shell is detected automatically, including `resume` and `fork`. Its tab becomes an agent while Codex is in the foreground. Returning to the shell, suspending it, or moving it to the background removes it from broadcasts. Background processes and noninteractive commands such as `login`, `exec`, and `app-server` are excluded.

The tab spinner runs while an agent is working. Agents opened through **New Agent** move to the far left when they become idle, newest first, without changing your selected tab. You can still drag tabs around; an agent moves automatically again only when it next becomes ready.

## Broadcasts

All broadcasts submit through terminal input. Finish CLI onboarding and leave prompts empty before sending. Codex decides how input is handled while busy.

| Menu | Recipients |
| --- | --- |
| Global Broadcast | All running agents in this window, including detected manual sessions. |
| Sleeper Broadcast | Agents opened through New Agent that are ready when you send. Busy agents, approval prompts, and unavailable status are skipped. |
| Custom Broadcast | Only the running agents you select, including selected busy or manual sessions. |

Custom Broadcast shows agent names, folders, and current activity. Use **Select all**, **Select idle**, or **Clear selection**. Selection is remembered until the window closes; new or restarted agents start unselected. **Select idle** takes a snapshot when clicked. Sleeper Broadcast checks readiness again during delivery.

Manual sessions can show **Status unavailable** and remain selectable for Custom Broadcast. They are excluded from Sleeper Broadcast because a missing spinner does not establish readiness. **New Agent** requests an explicit status from Codex without changing your saved configuration.

All three menus are disabled without running agents. Sleeper Broadcast does not wait for busy agents to finish. If readiness changes during delivery, submission can be cancelled after text was pasted; review the affected prompt.

## Account and usage

SWARM reuses Codex's saved sign-in. The Session menu provides ChatGPT and device-code sign-in, plus logout. Logout runs `codex logout` and removes the shared saved login for your operating-system user, including use outside SWARM. Already-running sessions may retain authentication until restarted. Your ChatGPT browser session is separate.

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
| New agent | Ctrl+Shift+T |
| New terminal | Ctrl+Shift+Return |
| New window | Ctrl+Shift+N |
| Global broadcast | Ctrl+Shift+B |
| Close tab | Ctrl+Shift+W |
| Copy / paste | Ctrl+Shift+C / Ctrl+Shift+V |
| Previous / next tab | Ctrl+PageUp / Ctrl+PageDown |
| Fullscreen | F11 |

## Source

- `swarm`: launcher; `swarm_app/__main__.py`: command-line options.
- `app.py`: windows, menus, and broadcast controls.
- `session.py`: terminal sessions and message delivery.
- `activity.py` and `codex_detection.py`: activity and foreground process detection.
- `custom_broadcast.py`: recipient picker; `usage.py`: account usage queries.
- `scripts/` and `packaging/`: installation and Debian packaging.
- `tests/`: terminal fixtures and tests.

Python modules listed above are under `swarm_app/`.

With `xvfb` and `xauth` installed, run the existing tests from the source directory:

```sh
xvfb-run -a /usr/bin/python3 -m unittest discover -s tests -v
```

## License

MIT. See [LICENSE](LICENSE).
