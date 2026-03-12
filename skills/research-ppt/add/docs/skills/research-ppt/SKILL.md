---
name: research-ppt
description: "Generate research-backed PowerPoint presentations. Automatically handles python-pptx installation with version pinning and falls back to a plain-text report if PPTX generation fails."
---

# Research PPT Skill

Generate a structured PowerPoint presentation from research on any topic. Uses the `research_ppt` tool which is available as a container tool.

## How to Use

When a user asks for a presentation, slide deck, or PPT on any topic:

1. Call `research_ppt` with the topic and optional parameters
2. The tool will research the topic and create a PPTX file (or a TXT fallback)
3. Send the resulting file to the user

## Tool Parameters

- `topic` (required): The research topic or title for the presentation
- `slide_count` (optional, default 8): Number of slides to generate (max 20)
- `output_path` (optional): Where to save the file (default: `/workspace/group/output.pptx`)
- `language` (optional, default "en"): Language for the presentation content

## Self-Healing Behavior

The tool automatically:
- Installs `python-pptx==1.0.2` if not present (version-pinned to prevent drift)
- Falls back to a plain-text `.txt` file if PPTX generation fails for any reason
- Reports which format was used in its return value

## Example Usage

```python
result = research_ppt(topic="Quantum Computing in 2025", slide_count=10)
# result = {"status": "ok", "path": "/workspace/group/output.pptx", "format": "pptx", "slides": 10}
# or fallback:
# result = {"status": "ok", "path": "/workspace/group/output.txt", "format": "text", "slides": 10}
```

After the tool returns, use `send_file` (or the Telegram file-sending IPC) to deliver the file to the user.
