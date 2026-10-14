
"""
agent.py — Maintains the "WHAT I'M UP TO" section of a GitHub profile README.

Required environment variables:
    GITHUB_REPOSITORY  owner/repository (provided by GitHub Actions)
    GH_TOKEN           GitHub token with public repository/user read access
    GROQ_API_KEY       Groq API key

Optional:
    GROQ_MODEL         Defaults to llama-3.3-70b-versatile
    GITHUB_OUTPUT      GitHub Actions output file
"""

import json
import os
import re
import sys
import urllib.error
import urllib.request


GITHUB_REPOSITORY = os.environ.get("GITHUB_REPOSITORY", "")
GITHUB_USERNAME = GITHUB_REPOSITORY.split("/", 1)[0] if "/" in GITHUB_REPOSITORY else ""

GITHUB_TOKEN = os.environ.get("GH_TOKEN", "")
GROQ_API_KEY = os.environ.get("GROQ_API_KEY", "")
GROQ_MODEL = os.environ.get("GROQ_MODEL", "llama-3.3-70b-versatile")

README_PATH = "README.md"
START_MARK = "<!-- AGENT-START -->"
END_MARK = "<!-- AGENT-END -->"


def validate_environment():
    """Fail early with a useful error when configuration is missing."""
    missing = []

    if not GITHUB_USERNAME:
        missing.append("GITHUB_REPOSITORY (expected owner/repository)")
    if not GITHUB_TOKEN:
        missing.append("GH_TOKEN")
    if not GROQ_API_KEY:
        missing.append("GROQ_API_KEY")

    if missing:
        raise RuntimeError(
            "Missing or invalid environment configuration: "
            + ", ".join(missing)
        )


def http_json(url, headers, data=None):
    """Make an HTTP request and return its JSON response."""
    request = urllib.request.Request(
        url,
        data=data,
        headers=headers,
        method="POST" if data is not None else "GET",
    )

    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            return json.loads(response.read().decode("utf-8"))

    except urllib.error.HTTPError as exc:
        details = exc.read().decode("utf-8", errors="replace")
        raise RuntimeError(
            f"HTTP {exc.code} calling {url}: {details[:1000]}"
        ) from exc

    except urllib.error.URLError as exc:
        raise RuntimeError(
            f"Network error calling {url}: {exc.reason}"
        ) from exc

    except json.JSONDecodeError as exc:
        raise RuntimeError(
            f"Invalid JSON response from {url}"
        ) from exc


def gh_api(path):
    """Call the GitHub REST API."""
    return http_json(
        f"https://api.github.com{path}",
        headers={
            "Authorization": f"Bearer {GITHUB_TOKEN}",
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
            "User-Agent": "github-readme-activity-agent",
        },
    )


def gather_activity():
    """Collect recent public pushes and recently pushed repositories."""
    events = gh_api(
        f"/users/{GITHUB_USERNAME}/events/public?per_page=30"
    )
    repos = gh_api(
        f"/users/{GITHUB_USERNAME}/repos"
        "?sort=pushed&per_page=10&type=owner"
    )

    if not isinstance(events, list) or not isinstance(repos, list):
        raise RuntimeError("GitHub returned an unexpected API response.")

    recent_pushes = []

    for event in events:
        if event.get("type") != "PushEvent":
            continue

        payload = event.get("payload") or {}

        recent_pushes.append({
            "repo": (event.get("repo") or {}).get("name", "unknown"),
            "commits": [
                commit.get("message", "")
                for commit in payload.get("commits", [])[:3]
                if isinstance(commit, dict) and commit.get("message")
            ],
            "at": event.get("created_at"),
        })

    recent_repos = [
        {
            "name": repo.get("name"),
            "description": repo.get("description"),
            "language": repo.get("language"),
            "pushed_at": repo.get("pushed_at"),
            "stars": repo.get("stargazers_count", 0),
        }
        for repo in repos
        if isinstance(repo, dict)
    ]

    return {
        "recent_pushes": recent_pushes[:15],
        "recent_repos": recent_repos,
    }


def get_current_section(readme_text):
    """Extract the marked section and reject missing or invalid markers."""
    start = readme_text.find(START_MARK)

    if start == -1:
        raise ValueError(f"README is missing {START_MARK}")

    content_start = start + len(START_MARK)
    end = readme_text.find(END_MARK, content_start)

    if end == -1:
        raise ValueError(f"README is missing {END_MARK}")

    if readme_text.find(START_MARK, content_start) != -1:
        raise ValueError("README contains duplicate or nested start markers.")

    if readme_text.find(END_MARK, end + len(END_MARK)) != -1:
        raise ValueError("README contains multiple end markers.")

    return readme_text[content_start:end].strip()


def ask_agent(activity, current_section):
    """Ask Groq to decide whether a meaningful README update is needed."""
    system = """
You maintain the "WHAT I'M UP TO" section of Siddharth Duttagupta's
GitHub profile README.

Use the supplied public GitHub activity and current section to decide
whether a change is justified.

Rules:
- Write 1 to 3 short, natural sentences.
- Be warm, honest, and specific. Avoid corporate buzzwords.
- Use only claims supported by the supplied activity.
- Do not invent projects, progress, skills, or achievements.
- If the current section remains accurate and there is no meaningful
  new information, set should_update to false.
- new_text must contain only the replacement section content, not
  the surrounding HTML markers.
- commit_message must be a short, single-line commit message explaining
  the change.

Return only a valid JSON object with this structure:
{
  "should_update": true,
  "new_text": "Updated section text",
  "commit_message": "docs: refresh current work section"
}
"""

    body = json.dumps({
        "model": GROQ_MODEL,
        "max_tokens": 500,
        "temperature": 0.3,
        "response_format": {"type": "json_object"},
        "messages": [
            {"role": "system", "content": system},
            {
                "role": "user",
                "content": json.dumps({
                    "current_section_text": current_section,
                    "recent_activity": activity,
                }, ensure_ascii=False),
            },
        ],
    }).encode("utf-8")

    response = http_json(
        "https://api.groq.com/openai/v1/chat/completions",
        headers={
            "Authorization": f"Bearer {GROQ_API_KEY}",
            "Content-Type": "application/json",
            "User-Agent": "github-readme-activity-agent",
        },
        data=body,
    )

    try:
        content = response["choices"][0]["message"]["content"]
        decision = json.loads(content)
    except (KeyError, IndexError, TypeError, json.JSONDecodeError) as exc:
        raise RuntimeError(
            "Groq returned an invalid or unexpected response."
        ) from exc

    if not isinstance(decision, dict):
        raise RuntimeError("Groq response must be a JSON object.")

    if not isinstance(decision.get("should_update"), bool):
        raise RuntimeError("Groq response is missing a boolean should_update.")

    if decision["should_update"]:
        new_text = decision.get("new_text")
        commit_message = decision.get("commit_message")

        if not isinstance(new_text, str) or not new_text.strip():
            raise RuntimeError("Groq returned empty replacement text.")

        if START_MARK in new_text or END_MARK in new_text:
            raise RuntimeError(
                "Groq returned HTML section markers inside new_text."
            )

        if not isinstance(commit_message, str) or not commit_message.strip():
            decision["commit_message"] = "docs: update current work section"

    return decision


def replace_section(readme_text, new_section):
    """Replace only the content between the two section markers."""
    start = readme_text.find(START_MARK)
    if start == -1:
        raise ValueError(f"README is missing {START_MARK}")

    content_start = start + len(START_MARK)
    end = readme_text.find(END_MARK, content_start)

    if end == -1:
        raise ValueError(f"README is missing {END_MARK}")

    # Validate that exactly one correctly ordered marker pair exists.
    get_current_section(readme_text)

    replacement = f"\n{new_section.strip()}\n"

    return (
        readme_text[:content_start]
        + replacement
        + readme_text[end:]
    )


def set_workflow_output(changed, commit_message=None):
    """Write safe, single-line outputs for GitHub Actions."""
    output_path = os.environ.get("GITHUB_OUTPUT")

    # Allow local execution without GitHub Actions.
    if not output_path:
        print("GITHUB_OUTPUT is unset; skipping workflow outputs.")
        return

    with open(output_path, "a", encoding="utf-8") as output:
        output.write(f"changed={'true' if changed else 'false'}\n")

        if commit_message is not None:
            # GitHub output values must not introduce extra output lines.
            message = re.sub(r"[\r\n]+", " ", commit_message).strip()
            output.write(f"commit_message={message}\n")


def main():
    validate_environment()

    if not os.path.isfile(README_PATH):
        raise FileNotFoundError(f"README file not found: {README_PATH}")

    with open(README_PATH, "r", encoding="utf-8") as file:
        readme = file.read()

    current_section = get_current_section(readme)
    activity = gather_activity()
    decision = ask_agent(activity, current_section)

    if not decision["should_update"]:
        print("Agent decided no update is needed.")
        set_workflow_output(False)
        return

    new_section = decision["new_text"].strip()
    new_readme = replace_section(readme, new_section)

    if new_readme == readme:
        print("Generated text matches the existing README.")
        set_workflow_output(False)
        return

    # Write only after all API calls and validation succeed.
    with open(README_PATH, "w", encoding="utf-8", newline="") as file:
        file.write(new_readme)

    commit_message = re.sub(
        r"[\r\n]+",
        " ",
        decision.get("commit_message", "docs: update current work section"),
    ).strip()

    set_workflow_output(True, commit_message)
    print(f"Updated README section. Commit message: {commit_message}")


if __name__ == "__main__":
    try:
        main()
    except (RuntimeError, ValueError, OSError) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        sys.exit(1)
