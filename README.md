# Vulcan MCP

A [Model Context Protocol](https://modelcontextprotocol.io) server that lets AI tools build and run code on the Vulcan machine.

Tools: `build`, `run`, `observe`, `send_input`, `stop`, `check_all`, `package`. Programs can be `.c`, `.asm`, a `build.run` manifest or a Vulcan `.exe`. Runs return console output, CPU state and a display PNG. Details: [mcp.md](../vulcanide/docs/mcp.md).

## Setup

Needs Python 3.10+, Java 17+, and the sibling repos (`intel8080`, `c8080`, `vulcanos`, `vulcanide`, `qa`) cloned in one folder.

```powershell
.\setup.ps1
claude mcp add vulcan -- <this folder>\.venv\Scripts\python.exe <this folder>\server.py
```

Other clients: copy the entry from `client.example.json`.
