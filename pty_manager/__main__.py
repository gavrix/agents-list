"""Entry point for pty-manager."""

from __future__ import annotations

import sys
from typing import List


def print_usage():
    print("""PTY Manager - Manage AI agent sessions across terminals

Usage:
  python3 -m pty_manager <command> [args]

Commands:
  start                         Start the PTY manager server
  terminal                      Run as terminal client (displays active session)
  manager                       Interactive session manager TUI
  ls, list                      List active sessions
  spawn <name> -- <command>     Spawn a new session
  attach <id|name>              Attach to a session (Ctrl+B d to detach)
  kill <id|name>                Kill a session
  shutdown                      Shutdown the server

Interactive Mode:
  # Terminal 1: Start the server
  python3 -m pty_manager start

  # Terminal 2: Run as display terminal
  python3 -m pty_manager terminal

  # Terminal 3: Run interactive manager
  python3 -m pty_manager manager
  # Use arrow keys to navigate, Enter to switch, 'n' to spawn, 'd' to delete

CLI Mode:
  python3 -m pty_manager spawn shell -- /bin/bash
  python3 -m pty_manager ls
  python3 -m pty_manager attach shell
""")


def main(args: List[str] = None) -> int:
    if args is None:
        args = sys.argv[1:]

    if not args:
        print_usage()
        return 1

    command = args[0].lower()

    if command in ("start", "server"):
        from pty_manager.server import Server
        server = Server()
        server.start()
        return 0

    elif command in ("ls", "list"):
        from pty_manager.client import Client
        return Client().cmd_list()

    elif command == "spawn":
        if len(args) < 2:
            print("Usage: python3 -m pty_manager spawn <name> -- <command>")
            return 1

        # Parse: spawn <name> -- <command...>
        rest = args[1:]
        if "--" not in rest:
            print("Usage: python3 -m pty_manager spawn <name> -- <command>")
            print("Example: python3 -m pty_manager spawn shell -- /bin/bash")
            return 1

        sep_idx = rest.index("--")
        if sep_idx == 0:
            print("Error: name is required")
            return 1

        name = rest[0]
        command_args = rest[sep_idx + 1:]

        if not command_args:
            print("Error: command is required")
            return 1

        from pty_manager.client import Client
        return Client().cmd_spawn(name, command_args)

    elif command == "attach":
        if len(args) < 2:
            print("Usage: python3 -m pty_manager attach <id|name>")
            return 1

        target = args[1]
        from pty_manager.client import Client
        return Client().cmd_attach(target)

    elif command == "kill":
        if len(args) < 2:
            print("Usage: python3 -m pty_manager kill <id|name>")
            return 1

        target = args[1]
        from pty_manager.client import Client
        return Client().cmd_kill(target)

    elif command == "shutdown":
        from pty_manager.client import Client
        return Client().cmd_shutdown()

    elif command == "terminal":
        from pty_manager.client import Client
        return Client().cmd_terminal()

    elif command == "manager":
        from pty_manager.client import Client
        return Client().cmd_manager()

    elif command in ("help", "-h", "--help"):
        print_usage()
        return 0

    else:
        print(f"Unknown command: {command}")
        print_usage()
        return 1


if __name__ == "__main__":
    sys.exit(main())
