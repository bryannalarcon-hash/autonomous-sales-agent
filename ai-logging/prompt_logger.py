#!/usr/bin/env python3
"""Append prompts and final replies to a conversation log automatically via Claude Code hooks.

Modes (argv[1]):
  prompt  -> UserPromptSubmit hook. Ignores noise inputs (background subagent
             task-notifications). For a real prompt, finalizes the previous
             turn's reply (now fully flushed), then logs the new prompt into both
             the "# PROMPTs ONLY" and "# full conversation" sections.
  reply   -> Stop hook. Overwrites the current prompt's reply with the latest
             *final* assistant message. Because a multi-step task can span several
             turns (e.g. async subagent completions stream in as separate turns),
             the reply is overwritten each turn so the LAST thing said — the
             synthesized summary after combing through subagent reports — wins.

Robustness:
  - Subagent output is on sidechains (isSidechain) and is skipped.
  - Background-agent completion notices arrive as prompts starting with
    "<task-notification>"; is_noise() drops them so they never become entries.
  - The final reply is the last assistant text AFTER the last tool_use (a preamble
    precedes a tool call, so it never qualifies) — this dodges the Stop-hook race
    where the hook fires before the final message is flushed.
  - Trivial local-command echoes ("No response requested.") are ignored so they
    can't clobber a real reply.

Log file: personalized per role as ai-logging/claude-<role>-conversation.md
(planner / designer / coder / …, mirroring the project's own structure). The hook
sets PROMPT_LOG_FILE to that path (the "pointer"). If PROMPT_LOG_FILE is unset, the
script falls back to the single claude-*-conversation.md next to it, else
conversation.md.
"""
import glob
import json
import os
import re
import sys
import time
from datetime import datetime

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))


def _resolve_log():
    """Personalized per-role log next to this script (the project's ai-logging/ dir).

    Priority: PROMPT_LOG_FILE (the pointer the hook sets) > the single
    claude-<role>-conversation.md present > conversation.md.
    """
    env = os.environ.get("PROMPT_LOG_FILE")
    if env:
        return env
    matches = sorted(glob.glob(os.path.join(SCRIPT_DIR, "claude-*-conversation.md")))
    if len(matches) == 1:
        return matches[0]
    return os.path.join(SCRIPT_DIR, "conversation.md")


LOG_FILE = _resolve_log()

CONV_HEADER = "# full conversation"
SEPARATOR = "\n---\n\n"
# Drop anything that isn't a real user-typed prompt: background subagent completions,
# autonomous-loop ticks, the /goal Stop-hook activation notice, and harness resume nudges.
NOISE_PREFIXES = (
    "<task-notification",
    "# Autonomous loop",
    "A session-scoped Stop hook is now active",
    "Continue from where you left off",
)
TRIVIAL_REPLIES = {"no response requested."}  # local-command echoes to ignore


def now_ts():
    return datetime.now().astimezone().strftime("%Y-%m-%d %H:%M:%S %Z")


def read_log():
    with open(LOG_FILE, "r", encoding="utf-8") as f:
        return f.read()


def write_log(text):
    with open(LOG_FILE, "w", encoding="utf-8") as f:
        f.write(text)


def is_noise(text):
    s = (text or "").lstrip()
    return any(s.startswith(p) for p in NOISE_PREFIXES)


def split_sections(content):
    matches = list(re.finditer(r"^# full conversation[ \t]*$", content, re.MULTILINE))
    if not matches:
        return content.rstrip() + "\n", CONV_HEADER + "\n"
    idx = matches[-1].start()
    top = re.sub(r"\n-{3,}\s*$", "\n", content[:idx].rstrip()) + "\n"
    return top, content[idx:]


def next_prompt_number(content):
    nums = [int(m) for m in re.findall(r"^### Prompt #(\d+)", content, re.MULTILINE)]
    return (max(nums) + 1) if nums else 1


def max_reply_number(content):
    nums = [int(m) for m in re.findall(r"^\*\*Reply #(\d+):\*\*", content, re.MULTILINE)]
    return max(nums) if nums else 0


def extract_final_reply(transcript_path):
    """Last main-agent assistant text emitted AFTER the last tool_use.

    Skips subagent (sidechain) output and trivial local-command echoes. Returns ""
    if the newest text still precedes a later tool_use (final reply not flushed).
    """
    if not transcript_path or not os.path.exists(transcript_path):
        return ""
    last_text, last_text_i, last_tool_i, i = "", -1, -1, 0
    with open(transcript_path, "r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                obj = json.loads(line)
            except json.JSONDecodeError:
                continue
            if obj.get("isSidechain"):
                continue
            msg = obj.get("message") or {}
            if msg.get("role") != "assistant":
                continue
            content = msg.get("content")
            text, has_tool = "", False
            if isinstance(content, str):
                text = content.strip()
            elif isinstance(content, list):
                text = "".join(
                    b.get("text", "")
                    for b in content
                    if isinstance(b, dict) and b.get("type") == "text"
                ).strip()
                has_tool = any(
                    isinstance(b, dict) and b.get("type") == "tool_use" for b in content
                )
            i += 1
            if text and text.lower() not in TRIVIAL_REPLIES:
                last_text, last_text_i = text, i
            if has_tool:
                last_tool_i = i
    return last_text if last_text and last_text_i > last_tool_i else ""


def set_reply(content, number, reply):
    """Insert or overwrite '**Reply #number:**' (always the last block in the file)."""
    marker = "**Reply #%d:**" % number
    body = reply.rstrip()
    idx = content.rfind("\n" + marker)
    head = content[:idx] if idx != -1 else content
    write_log(head.rstrip() + "\n\n" + marker + "\n" + body + "\n")


def handle_prompt(data):
    prompt = (data.get("prompt") or "").rstrip()
    if not prompt or is_noise(prompt):
        return
    # Finalize the previous turn's reply (fully flushed by now).
    content = read_log()
    prev = next_prompt_number(content) - 1
    if prev >= 1:
        r = extract_final_reply(data.get("transcript_path"))
        if r:
            set_reply(content, prev, r)
            content = read_log()
    # Append the new prompt to both sections.
    n = next_prompt_number(content)
    entry = "### Prompt #%d — %s\n%s\n" % (n, now_ts(), prompt)
    top, bottom = split_sections(content)
    top = top.rstrip() + "\n\n" + entry
    bottom = bottom.rstrip() + "\n\n" + entry
    write_log(top.rstrip() + "\n" + SEPARATOR + bottom.rstrip() + "\n")


def handle_reply(data):
    if (next_prompt_number(read_log()) - 1) < 1:
        return
    # Poll briefly: the Stop hook can fire before the final message is flushed.
    reply = ""
    for _ in range(12):
        reply = extract_final_reply(data.get("transcript_path"))
        if reply:
            break
        time.sleep(0.25)
    if not reply:
        return  # next prompt's finalize step will capture it
    content = read_log()
    set_reply(content, next_prompt_number(content) - 1, reply)


def main():
    mode = sys.argv[1] if len(sys.argv) > 1 else ""
    try:
        data = json.load(sys.stdin)
    except (json.JSONDecodeError, ValueError):
        data = {}
    if mode == "prompt":
        handle_prompt(data)
    elif mode == "reply":
        handle_reply(data)
    sys.exit(0)


if __name__ == "__main__":
    main()
