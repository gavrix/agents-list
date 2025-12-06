# PTY Manager

A terminal multiplexer for managing AI agent sessions. Run multiple interactive CLI sessions (like Claude Code, Codex, etc.) and switch between them from a central manager.



https://github.com/user-attachments/assets/14269c8f-4244-4cac-b1b0-1bc07611d75d



## Installation

```bash
git clone <repo-url>
cd pty-manager
pip3 install -e .
```

## Quick Start

You need three terminal panes (e.g., in tmux):

```bash
# Terminal 1: Start the server
pty-manager start

# Terminal 2: Run the display terminal
pty-manager terminal

# Terminal 3: Run the session manager
pty-manager manager
```

In the manager TUI:
- `↑/↓` or `j/k` - Navigate sessions
- `Enter` - Switch to selected session
- `n` - Create new session
- `d` - Delete session
- `q` - Quit manager

## Commands

### Server

```bash
pty-manager start      # Start the PTY manager server
pty-manager shutdown   # Stop the server
```

### Session Management

```bash
pty-manager list                        # List all sessions
pty-manager spawn <name> -- <command>   # Create a new session
pty-manager kill <name>                 # Kill a session
pty-manager attach <name>               # Attach directly to a session
```

### Interactive Mode

```bash
pty-manager terminal   # Run as display terminal (shows active session)
pty-manager manager    # Run interactive session manager TUI
```

## Examples

```bash
# Start a shell session
pty-manager spawn shell -- /bin/bash

# Start Claude Code
pty-manager spawn claude -- claude

# Start Codex
pty-manager spawn codex -- codex

# List sessions
pty-manager list
```

## Architecture

```
┌─────────────────┐     ┌─────────────────┐     ┌─────────────────┐
│     Server      │     │    Terminal     │     │     Manager     │
│                 │     │                 │     │                 │
│  Manages PTY    │◄───►│  Displays the   │     │  TUI for        │
│  sessions and   │     │  active session │     │  switching      │
│  coordinates    │     │                 │     │  sessions       │
│  switching      │◄────────────────────────────►                 │
└─────────────────┘     └─────────────────┘     └─────────────────┘
```

- **Server**: Central daemon that manages PTY sessions and coordinates between clients
- **Terminal**: Displays the currently active session's output
- **Manager**: TUI for listing, creating, and switching between sessions

## Detaching

When attached to a session (via `attach` command), press `Ctrl+B d` to detach.

## Requirements

- Python 3.9+
- Unix-like OS (macOS, Linux)
- No external dependencies (uses only Python stdlib)

## License

MIT
