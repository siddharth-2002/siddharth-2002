"""
agent.py — an autonomous agent that maintains the "WHAT I'M UP TO" section
of a GitHub profile README.

What it does each run:
  1. Pulls recent public GitHub activity for the user (events, repos, languages)
  2. Reads the current README.md
  3. Hands both to Claude and asks it to DECIDE whether the section is stale
     and, if so, rewrite it — in its own words, based on real signal, not a
     template fill-in
  4. Claude also writes its own short commit message explaining what changed
     and why (or explicitly says "no update needed" and the workflow skips
     the commit)
  5. Only the text between <!-- AGENT-START --> and <!-- AGENT-END --> is
     ever touched — everything else in the README is left completely alone
"""

import os
import re
import sys
import json
import urllib.request

GITHUB_USERNAME = os.environ["GITHUB_REPOSITORY"].split("/")[0]
GITHUB_TOKEN = os.environ["GH_TOKEN"]
GROQ_API_KEY = os.environ["GROQ_API_KEY"]
GROQ_MODEL = os.environ.get("GROQ_MODEL", "llama-3.3-70b-versatile")

README_PATH = "README.md"
START_MARK = "<!-- AGENT-START -->"
END_MARK = "<!-- AGENT-END -->"


def gh_api(path):
    req = urllib.request.Request(
        f"https://api.github.com{path}",
        headers={
            "Authorization": f"Bearer {GITHUB_TOKEN}",
            "Accept": "application/vnd.github+json",
        },
    )
    with urllib.request.urlopen(req) as resp:
        return json.loads(resp.read())


def gather_activity():
    """Pull lightweight, real signal about what the user has actually been doing."""
    events = gh_api(f"/users/{GITHUB_USERNAME}/events/public")[:30]
    repos = gh_api(f"/users/{GITHUB_USERNAME}/repos?sort=pushed&per_page=10")

    recent_pushes = []
    for e in events:
        if e["type"] == "PushEvent":
            recent_pushes.append({
                "repo": e["repo"]["name"],
                "commits": [c["message"] for c in e["payload"].get("commits", [])][:3],
                "at": e["created_at"],
            })

    recent_repos = [
        {
            "name": r["name"],
            "description": r.get("description"),
            "language": r.get("language"),
            "pushed_at": r["pushed_at"],
            "stars": r["stargazers_count"],
        }
        for r in repos
    ]

    return {"recent_pushes": recent_pushes[:15], "recent_repos": recent_repos}


def get_current_section(readme_text):
    match = re.search(f"{re.escape(START_MARK)}(.*?){re.escape(END_MARK)}", readme_text, re.DOTALL)
    return match.group(1).strip() if match else ""


def ask_agent(activity, current_section):
    system = """You are an autonomous agent that maintains one small section of \
Siddharth Duttagupta's GitHub profile README, titled "WHAT I'M UP TO".

You will be given:
- His recent GitHub activity (pushes, commit messages, repos touched)
- The current text in that section

Your job: decide whether the section should change. Write like a real person \
giving a quick, honest update on what they're building — 1 to 3 short sentences, \
warm but not cheesy, no corporate buzzwords, no emoji spam (one emoji max if it \
fits naturally). Base it only on real signal from the activity data — never \
invent projects or claims that aren't supported by what you were given.

If there's genuinely nothing new since the current text still fairly reflects \
his activity, say so explicitly and don't force a change.

Respond ONLY with valid JSON, no markdown fences, no preamble:
{
  "should_update": true or false,
  "new_text": "the new section text (only meaningful if should_update is true)",
  "commit_message": "a short, specific commit message explaining what changed and why (only meaningful if should_update is true)"
}"""

    user_content = json.dumps({
        "current_section_text": current_section,
        "recent_activity": activity,
    }, indent=2)

    body = json.dumps({
        "model": GROQ_MODEL,
        "max_tokens": 500,
        "response_format": {"type": "json_object"},
        "messages": [
            {"role": "system", "content": system},
            {"role": "user", "content": user_content},
        ],
    }).encode("utf-8")

    req = urllib.request.Request(
        "https://api.groq.com/openai/v1/chat/completions",
        data=body,
        headers={
            "Authorization": f"Bearer {GROQ_API_KEY}",
            "Content-Type": "application/json",
        },
        method="POST",
    )

    with urllib.request.urlopen(req) as resp:
        data = json.loads(resp.read())

    text = data["choices"][0]["message"]["content"]
    return json.loads(text)


def main():
    with open(README_PATH, "r", encoding="utf-8") as f:
        readme = f.read()

    current_section = get_current_section(readme)
    activity = gather_activity()
    decision = ask_agent(activity, current_section)

    if not decision.get("should_update"):
        print("Agent decided no update needed.")
        # signal to the workflow that there's nothing to commit
        with open(os.environ["GITHUB_OUTPUT"], "a") as f:
            f.write("changed=false\n")
        return

    new_section = decision["new_text"].strip()
    new_readme = re.sub(
        f"{re.escape(START_MARK)}.*?{re.escape(END_MARK)}",
        f"{START_MARK}\n{new_section}\n{END_MARK}",
        readme,
        flags=re.DOTALL,
    )

    with open(README_PATH, "w", encoding="utf-8") as f:
        f.write(new_readme)

    commit_msg = decision.get("commit_message", "chore: agent update").strip()
    with open(os.environ["GITHUB_OUTPUT"], "a") as f:
        f.write("changed=true\n")
        f.write(f"commit_message={commit_msg}\n")

    print(f"Updated section. Commit message: {commit_msg}")


if __name__ == "__main__":
    main()
