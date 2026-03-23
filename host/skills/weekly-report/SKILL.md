---
name: weekly-report
description: Generate a weekly activity report for a team or agent.
---

# Weekly Report Skill

Generate a comprehensive weekly report summarizing:
- Tasks completed this week
- Issues encountered and resolutions
- Memory entries added or updated
- Bot-to-bot interactions (if any)
- Recommendations for next week

## Usage

Invoke when asked for a weekly summary or report. Query the MemoryBus
shared memory store for recent entries, summarize agent activity, and
format as a structured markdown report.

## Output Format

```markdown
# Weekly Report — {date}

## ✅ Completed
- ...

## ⚠️ Issues
- ...

## 💡 Next Week
- ...
```
