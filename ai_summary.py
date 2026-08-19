#!/usr/bin/env python3
"""
GitHub Actions "AI failure analysis" Log Analyzer

Extracts logs from finished GitHub workflows, isolates the "AI failure analysis"
step and parses the "Run statistics" block printed by the triage bot.

Jobs where the step was skipped, or where the bot bailed out because it was
provided with an unknown substrate, are ignored.

Every shell tool call made by the bot is recorded verbatim, together with the
context growth it caused, into a tool_calls-<timestamp>.txt file for later
analysis.
"""

import argparse
import json
import os
import re
import statistics
import sys
from collections import Counter
from datetime import datetime, timedelta, timezone
from typing import Optional, Tuple

import requests
from dotenv import load_dotenv
from github import Github
from github.GithubException import GithubException

STEP_NAME = "AI failure analysis"
STEP_START_MARKER = "Running AI failure analysis"
UNKNOWN_SUBSTRATE_MARKER = "no knowledge for substrate"
SKIP_MARKER = "OPENROUTER_API_KEY not set; skipping AI failure analysis"

ARTIFACT_PREFIX = "generated/happy-bot/"
RESULTS_DIR = "results"

UUID_PATTERN = re.compile(
    r"^\s*(?:JOB_)?UUID:\s*([0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}"
    r"-[0-9a-fA-F]{4}-[0-9a-fA-F]{12})\s*$"
)

TIMESTAMP_PATTERN = re.compile(r"^(\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}\.\d+Z)\s?(.*)$")
SHELL_CALL_PATTERN = re.compile(r"^\[tool_call\]\s*shell\((.*)\)\s*$")
TOOL_CALL_PATTERN = re.compile(r"^\[tool_call\]\s*([A-Za-z_][A-Za-z0-9_]*)\(")
TOOL_RESULT_PATTERN = re.compile(r"^\[tool_result\]\s*([A-Za-z_][A-Za-z0-9_]*):\s*(.*)$")
CONTEXT_PATTERN = re.compile(
    r"^\[context\][^:]*:\s*([\d,]+)\s*msgs,\s*~([\d,]+)\s*tok est\s*\(([\d,]+)\s*chars\)"
)


def parse_arguments() -> argparse.Namespace:
    """Parse command line arguments."""
    parser = argparse.ArgumentParser(
        description="Extract 'AI failure analysis' run statistics from workflow logs"
    )
    parser.add_argument(
        "days_or_date",
        type=str,
        help="Number of days to analyze (excluding current day), 0 for today, or specific date (YYYY-MM-DD)",
    )
    parser.add_argument(
        "--verbose",
        action="store_true",
        help="Also print the statistics block of each individual job",
    )
    parser.add_argument(
        "--output",
        metavar="FILE",
        help="Where to write the recorded shell calls "
        "(default: tool_calls-<timestamp>.txt in the current directory)",
    )
    parser.add_argument(
        "--results-dir",
        metavar="DIR",
        default=RESULTS_DIR,
        help=f"Directory for the downloaded happy-bot files (default: {RESULTS_DIR})",
    )
    return parser.parse_args()


def load_environment() -> Tuple[str, str, str]:
    """Load GitHub token, repository and swift container URL from the environment."""
    load_dotenv()

    github_token = os.getenv("GITHUB_TOKEN")
    repository_path = os.getenv("GITHUB_REPOSITORY")
    swift_container_url = os.getenv("SWIFT_CONTAINER_URL")

    if not github_token:
        raise ValueError("GITHUB_TOKEN not found in environment variables")
    if not repository_path:
        raise ValueError("GITHUB_REPOSITORY not found in environment variables")
    if not swift_container_url:
        raise ValueError("SWIFT_CONTAINER_URL not found in environment variables")

    # Listing needs the container's trailing slash, or the gateway redirects
    # and drops the prefix query.
    if not swift_container_url.endswith("/"):
        swift_container_url += "/"

    return github_token, repository_path, swift_container_url


def get_date_range(days_or_date: str) -> Tuple[datetime, datetime]:
    """Calculate the date range for analysis.

    Args:
        days_or_date: Either number of days (0 for today, N for previous N days)
                     or specific date in YYYY-MM-DD format

    Returns:
        Tuple of (start_date, end_date) in UTC
    """
    try:
        specific_date = datetime.strptime(days_or_date, "%Y-%m-%d")
        specific_date = specific_date.replace(tzinfo=timezone.utc)
        start_date = specific_date.replace(hour=0, minute=0, second=0, microsecond=0)
        end_date = specific_date.replace(
            hour=23, minute=59, second=59, microsecond=999999
        )
        return start_date, end_date
    except ValueError:
        pass

    try:
        days = int(days_or_date)
    except ValueError:
        raise ValueError(
            f"Invalid argument: '{days_or_date}'. Must be a number or date in YYYY-MM-DD format"
        )

    now = datetime.now(timezone.utc)
    if days == 0:
        start_date = now.replace(hour=0, minute=0, second=0, microsecond=0)
        end_date = now
    else:
        end_date = now.replace(hour=0, minute=0, second=0, microsecond=0)
        start_date = end_date - timedelta(days=days)
    return start_date, end_date


def strip_timestamp(line: str) -> str:
    """Remove the GitHub Actions timestamp prefix from a log line."""
    match = TIMESTAMP_PATTERN.match(line)
    return match.group(2) if match else line


def extract_step_lines(log_text: str) -> list:
    """Return the log lines (timestamp stripped) belonging to the AI step."""
    lines = []
    inside = False

    for raw_line in log_text.splitlines():
        message = strip_timestamp(raw_line)

        if not inside:
            # The command echo inside the ##[group] block is not the step output.
            if message.strip() == STEP_START_MARKER:
                inside = True
                lines.append(message)
            continue

        # The step output ends when the next step's group starts.
        if message.startswith("##[group]"):
            break
        lines.append(message)

    return lines


def parse_run_statistics(step_lines: list) -> Optional[dict]:
    """Parse the 'Run statistics' block emitted at the end of the step."""
    try:
        start = next(
            index
            for index, line in enumerate(step_lines)
            if line.strip() == "Run statistics"
        )
    except StopIteration:
        return None

    stats = {"tool_calls_breakdown": {}}

    for line in step_lines[start + 1 :]:
        stripped = line.strip()
        if not stripped:
            continue

        if match := re.match(r"^Terminal state\s*:\s*(.+)$", stripped):
            stats["terminal_state"] = match.group(1).strip()
        elif match := re.match(r"^Duration\s*:\s*([\d.]+)s", stripped):
            stats["duration"] = float(match.group(1))
        elif match := re.match(
            r"^LLM requests\s*:\s*(\d+)\s*\(retries:\s*(\d+)\)", stripped
        ):
            stats["llm_requests"] = int(match.group(1))
            stats["retries"] = int(match.group(2))
        elif match := re.match(r"^Model / provider\s*:\s*(.+)\s+/\s+(.+)$", stripped):
            stats["model"] = match.group(1).strip()
            stats["provider"] = match.group(2).strip()
        elif match := re.match(
            r"^Tokens\s*:\s*(\d+) total \(prompt (\d+), completion (\d+)\)", stripped
        ):
            stats["tokens_total"] = int(match.group(1))
            stats["tokens_prompt"] = int(match.group(2))
            stats["tokens_completion"] = int(match.group(3))
        elif match := re.match(r"^reasoning (\d+), cached (\d+)", stripped):
            stats["tokens_reasoning"] = int(match.group(1))
            stats["tokens_cached"] = int(match.group(2))
        elif match := re.match(r"^Throughput\s*:\s*([\d.]+) tok/s", stripped):
            stats["throughput"] = float(match.group(1))
        elif match := re.match(r"^Cost\s*:\s*\$([\d.]+)", stripped):
            stats["cost"] = float(match.group(1))
        elif match := re.match(r"^Tool calls\s*:\s*(\d+)$", stripped):
            stats["tool_calls"] = int(match.group(1))
        elif match := re.match(r"^-\s*(\S+?)\s*:\s*(\d+)$", stripped):
            stats["tool_calls_breakdown"][match.group(1)] = int(match.group(2))
        elif match := re.match(r"^Shell failures\s*:\s*(\d+)$", stripped):
            stats["shell_failures"] = int(match.group(1))
        elif match := re.match(r"^Follow-up rounds\s*:\s*(\d+)$", stripped):
            stats["followup_rounds"] = int(match.group(1))

    return stats if len(stats) > 1 else None


def extract_shell_calls(step_lines: list) -> list:
    """Record every shell tool call verbatim with the context growth it caused.

    The bot prints a ``[context]`` line after each batch of tool calls, so the
    token delta of a batch is reported for every call it contains, along with
    the batch size, leaving the attribution to whoever analyses the file.
    """
    records = []
    pending = []
    results = []
    previous = None

    for line in step_lines:
        stripped = line.strip()

        call_match = TOOL_CALL_PATTERN.match(stripped)
        if call_match:
            tool = call_match.group(1)
            command = None

            if tool == "shell":
                shell_match = SHELL_CALL_PATTERN.match(stripped)
                if shell_match:
                    try:
                        arguments = json.loads(shell_match.group(1))
                        command = arguments.get("command")
                    except (json.JSONDecodeError, AttributeError):
                        command = shell_match.group(1)

            pending.append({"tool": tool, "command": command})
            continue

        result_match = TOOL_RESULT_PATTERN.match(stripped)
        if result_match:
            results.append(result_match.group(2).strip())
            continue

        context_match = CONTEXT_PATTERN.match(stripped)
        if not context_match:
            continue

        messages = int(context_match.group(1).replace(",", ""))
        tokens = int(context_match.group(2).replace(",", ""))
        chars = int(context_match.group(3).replace(",", ""))

        for position, call in enumerate(pending):
            if call["tool"] == "shell" and call["command"] is not None:
                records.append(
                    {
                        "command": call["command"],
                        "result": results[position] if position < len(results) else "",
                        "batch_size": len(pending),
                        "batch_tools": [item["tool"] for item in pending],
                        "tokens_before": previous["tokens"] if previous else None,
                        "tokens_after": tokens,
                        "batch_delta": (
                            tokens - previous["tokens"] if previous else None
                        ),
                        "chars_after": chars,
                        "chars_delta": chars - previous["chars"] if previous else None,
                        "messages_after": messages,
                    }
                )

        pending = []
        results = []
        previous = {"tokens": tokens, "chars": chars}

    return records


def extract_weebl_uuid(log_text: str) -> Optional[str]:
    """Return the weebl job UUID announced in the workflow log."""
    for raw_line in log_text.splitlines():
        match = UUID_PATTERN.match(strip_timestamp(raw_line))
        if match:
            return match.group(1).lower()
    return None


def list_artifacts(uuid: str, container_url: str) -> list:
    """List the happy-bot object names stored in swift for a weebl UUID."""
    prefix = f"{uuid}/{ARTIFACT_PREFIX}"

    try:
        response = requests.get(container_url, params={"prefix": prefix}, timeout=120)
    except Exception as exception:
        print(f"\nError listing artifacts for {uuid}: {exception}")
        return []

    if response.status_code != 200:
        print(f"\nFailed to list artifacts for {uuid}: HTTP {response.status_code}")
        return []

    return [name.strip() for name in response.text.splitlines() if name.strip()]


def download_artifacts(uuid: str, results_dir: str, container_url: str) -> dict:
    """Download the happy-bot artifacts of a run into results/<uuid>/."""
    objects = list_artifacts(uuid, container_url)
    if not objects:
        return {"downloaded": 0, "failed": 0, "files": []}

    target_dir = os.path.join(results_dir, uuid)
    os.makedirs(target_dir, exist_ok=True)

    downloaded = 0
    failed = 0
    files = []

    for name in objects:
        filename = name.rsplit("/", 1)[-1]
        if not filename:
            continue

        try:
            response = requests.get(f"{container_url}{name}", timeout=120)
        except Exception as exception:
            print(f"\nError downloading {name}: {exception}")
            failed += 1
            continue

        if response.status_code != 200:
            print(f"\nFailed to download {name}: HTTP {response.status_code}")
            failed += 1
            continue

        with open(os.path.join(target_dir, filename), "wb") as handle:
            handle.write(response.content)
        downloaded += 1
        files.append(filename)

    return {"downloaded": downloaded, "failed": failed, "files": files}


def analyze_job_logs(repo_path: str, job_id: int) -> dict:
    """Download job logs and classify/parse the AI failure analysis step.

    Returns dict with 'status', 'bytes_downloaded', 'stats' and 'substrate' keys.
    Status is one of: no_step, skipped, unknown_substrate, no_stats, ok, error.
    """
    result = {
        "status": "error",
        "bytes_downloaded": 0,
        "stats": None,
        "substrate": None,
        "uuid": None,
    }

    github_token = os.getenv("GITHUB_TOKEN")
    if not github_token:
        print("Cannot download logs: GITHUB_TOKEN not found")
        return result

    headers = {"Authorization": f"token {github_token}"}
    logs_url = f"https://api.github.com/repos/{repo_path}/actions/jobs/{job_id}/logs"

    try:
        response = requests.get(logs_url, headers=headers)
    except Exception as exception:
        print(f"Error downloading logs for job {job_id}: {exception}")
        return result

    if response.status_code != 200:
        print(f"Failed to download logs for job {job_id}: HTTP {response.status_code}")
        return result

    log_text = response.text
    result["bytes_downloaded"] = len(log_text.encode("utf-8"))
    result["uuid"] = extract_weebl_uuid(log_text)

    step_lines = extract_step_lines(log_text)
    if not step_lines:
        # When OPENROUTER_API_KEY is missing the step exits before printing
        # its start marker, so look for the bail-out message directly.
        if any(
            strip_timestamp(line).strip() == SKIP_MARKER
            for line in log_text.splitlines()
        ):
            result["status"] = "skipped"
        else:
            result["status"] = "no_step"
        return result

    for line in step_lines:
        if match := re.match(r"^Substrate:\s*(.+)$", line.strip()):
            result["substrate"] = match.group(1).strip()
            break

    joined = "\n".join(step_lines)
    if SKIP_MARKER in joined:
        result["status"] = "skipped"
        return result
    if UNKNOWN_SUBSTRATE_MARKER in joined:
        result["status"] = "unknown_substrate"
        return result

    stats = parse_run_statistics(step_lines)
    if stats is None:
        result["status"] = "no_stats"
        return result

    result["status"] = "ok"
    result["stats"] = stats
    stats["shell_calls"] = extract_shell_calls(step_lines)
    return result


def has_ai_step(job) -> Optional[str]:
    """Return the conclusion of the AI step if present in the job, else None."""
    for step in job.steps or []:
        if step.name and step.name.strip() == STEP_NAME:
            return step.conclusion
    return None


def print_job_details(run, job, substrate: str, stats: dict) -> None:
    """Print the parsed statistics for a single job."""
    print(f"\n{'=' * 70}")
    print(f"Run {run.id} / Job {job.id} - {run.name}")
    print(f"  URL             : {job.html_url}")
    print(f"  Job conclusion  : {job.conclusion}")
    if substrate:
        print(f"  Substrate       : {substrate}")
    if job.completed_at:
        print(f"  Completed at    : {job.completed_at.isoformat()}")
    print("Run statistics")
    print(f"  Terminal state  : {stats.get('terminal_state', 'n/a')}")
    print(f"  Duration        : {stats.get('duration', 0):.1f}s")
    print(
        f"  LLM requests    : {stats.get('llm_requests', 0)} "
        f"(retries: {stats.get('retries', 0)})"
    )
    print(
        f"  Model / provider: {stats.get('model', 'n/a')} / {stats.get('provider', 'n/a')}"
    )
    print(
        f"  Tokens          : {stats.get('tokens_total', 0)} total "
        f"(prompt {stats.get('tokens_prompt', 0)}, "
        f"completion {stats.get('tokens_completion', 0)})"
    )
    print(
        f"                    reasoning {stats.get('tokens_reasoning', 0)}, "
        f"cached {stats.get('tokens_cached', 0)}"
    )
    print(f"  Throughput      : {stats.get('throughput', 0):.1f} tok/s")
    print(f"  Cost            : ${stats.get('cost', 0):.6f}")
    print(f"  Tool calls      : {stats.get('tool_calls', 0)}")
    for tool, count in sorted(stats.get("tool_calls_breakdown", {}).items()):
        print(f"      - {tool:<18}: {count}")
    print(f"  Shell failures  : {stats.get('shell_failures', 0)}")
    print(f"  Follow-up rounds: {stats.get('followup_rounds', 0)}")


def write_shell_calls(collected: list, path: str) -> int:
    """Write every recorded shell call verbatim to a text file."""
    total = 0

    with open(path, "w") as handle:
        handle.write(
            "# Shell tool calls made by the 'AI failure analysis' step.\n"
            "#\n"
            "# Each record holds the command verbatim between the <<<COMMAND and\n"
            "# COMMAND>>> markers. Context figures come from the bot's [context]\n"
            "# line printed after each batch of tool calls: batch_delta is the token\n"
            "# growth of the whole batch, shared by the batch_size calls it contains.\n"
            "# The first batch of a run has no delta, as no earlier context line\n"
            "# exists to compare against.\n\n"
        )

        for stats in collected:
            calls = stats.get("shell_calls", [])
            handle.write(f"{'=' * 78}\n")
            handle.write(f"JOB {stats['job_id']} RUN {stats['run_id']}\n")
            handle.write(f"URL {stats['job_url']}\n")
            handle.write(f"WEEBL_UUID {stats.get('uuid') or 'unknown'}\n")
            handle.write(f"SUBSTRATE {stats.get('substrate') or 'unknown'}\n")
            handle.write(f"COMPLETED_AT {stats.get('completed_at') or 'unknown'}\n")
            handle.write(
                f"RUN_DURATION {stats.get('duration', 0):.1f}s "
                f"COST ${stats.get('cost', 0):.6f} "
                f"LLM_REQUESTS {stats.get('llm_requests', 0)} "
                f"SHELL_CALLS_RECORDED {len(calls)}\n"
            )
            handle.write(f"{'=' * 78}\n\n")

            for index, call in enumerate(calls, 1):
                total += 1
                before = call["tokens_before"]
                delta = call["batch_delta"]
                handle.write(
                    f"--- call {index}/{len(calls)}"
                    f" | result: {call['result'] or 'n/a'}"
                    f" | context {before if before is not None else 'n/a'}"
                    f" -> {call['tokens_after']} tok"
                    f" | batch_delta {delta if delta is not None else 'n/a'} tok"
                    f" | chars_delta {call['chars_delta'] if call['chars_delta'] is not None else 'n/a'}"
                    f" | batch_size {call['batch_size']}"
                    f" | batch_tools {','.join(call['batch_tools'])}"
                    f" | messages {call['messages_after']}\n"
                )
                handle.write("<<<COMMAND\n")
                handle.write(call["command"])
                if not call["command"].endswith("\n"):
                    handle.write("\n")
                handle.write("COMMAND>>>\n\n")

    return total


def print_metric(label: str, values: list, unit: str = "", precision: int = 1) -> None:
    """Print min/max/avg/median for a list of numeric values."""
    if not values:
        print(f"  {label:<18}: no data")
        return

    def fmt(value: float) -> str:
        return f"{value:,.{precision}f}{unit}"

    print(
        f"  {label:<18}: min {fmt(min(values))} | max {fmt(max(values))} | "
        f"avg {fmt(statistics.mean(values))} | median {fmt(statistics.median(values))}"
    )


def print_summary(collected: list, counters: Counter, total_bytes: int) -> None:
    """Print the aggregated summary over all collected runs."""
    print(f"\n\n{'=' * 70}")
    print("AI FAILURE ANALYSIS SUMMARY")
    print(f"{'=' * 70}")

    print(f"  Jobs with the step        : {counters['with_step']}")
    print(f"  Ignored (step skipped)    : {counters['skipped']}")
    print(f"  Ignored (unknown substrate): {counters['unknown_substrate']}")
    print(f"  Ignored (no statistics)   : {counters['no_stats']}")
    print(f"  Failed to read logs       : {counters['error']}")
    print(f"  Analyzed runs (with stats): {len(collected)}")

    if not collected:
        print("\nNo 'Run statistics' blocks found in the selected period.")
        print(
            f"\nTotal data downloaded: {total_bytes:,} bytes "
            f"({total_bytes / 1024 / 1024:.2f} MB)"
        )
        return

    costs = [s["cost"] for s in collected if "cost" in s]
    durations = [s["duration"] for s in collected if "duration" in s]
    requests_counts = [s["llm_requests"] for s in collected if "llm_requests" in s]
    tokens = [s["tokens_total"] for s in collected if "tokens_total" in s]
    prompt_tokens = [s["tokens_prompt"] for s in collected if "tokens_prompt" in s]
    completion_tokens = [
        s["tokens_completion"] for s in collected if "tokens_completion" in s
    ]
    cached_tokens = [s["tokens_cached"] for s in collected if "tokens_cached" in s]
    throughputs = [s["throughput"] for s in collected if "throughput" in s]
    tool_calls = [s["tool_calls"] for s in collected if "tool_calls" in s]
    shell_failures = [s["shell_failures"] for s in collected if "shell_failures" in s]
    retries = [s["retries"] for s in collected if "retries" in s]
    followups = [s["followup_rounds"] for s in collected if "followup_rounds" in s]

    print(f"\n{'-' * 70}")
    print("Per-run metrics (min / max / avg / median)")
    print(f"{'-' * 70}")
    print_metric("Cost ($)", costs, precision=6)
    print_metric("Duration (s)", durations, precision=1)
    print_metric("LLM requests", requests_counts, precision=1)
    print_metric("Tokens total", tokens, precision=0)
    print_metric("Tokens prompt", prompt_tokens, precision=0)
    print_metric("Tokens completion", completion_tokens, precision=0)
    print_metric("Throughput (tok/s)", throughputs, precision=1)
    print_metric("Tool calls", tool_calls, precision=1)
    print_metric("Shell failures", shell_failures, precision=1)
    print_metric("LLM retries", retries, precision=1)
    print_metric("Follow-up rounds", followups, precision=1)

    print(f"\n{'-' * 70}")
    print("Totals")
    print(f"{'-' * 70}")
    print(f"  Total cost        : ${sum(costs):.6f}")
    print(f"  Total duration    : {sum(durations):,.1f}s ({sum(durations) / 3600:.2f}h)")
    print(f"  Total LLM requests: {sum(requests_counts):,}")
    print(f"  Total tokens      : {sum(tokens):,}")
    if sum(prompt_tokens) and cached_tokens:
        cache_ratio = sum(cached_tokens) / sum(prompt_tokens) * 100
        print(f"  Prompt cache hits : {sum(cached_tokens):,} ({cache_ratio:.1f}%)")
    print(f"  Total tool calls  : {sum(tool_calls):,}")
    print(f"  Total shell fails : {sum(shell_failures):,}")
    if costs and tokens:
        print(f"  Cost per 1M tokens: ${sum(costs) / sum(tokens) * 1_000_000:.4f}")
    if costs and requests_counts and sum(requests_counts):
        print(f"  Cost per request  : ${sum(costs) / sum(requests_counts):.6f}")

    terminal_states = Counter(s.get("terminal_state", "unknown") for s in collected)
    print(f"\n{'-' * 70}")
    print("Terminal states")
    print(f"{'-' * 70}")
    for state, count in terminal_states.most_common():
        print(f"  {state:<18}: {count} ({count / len(collected) * 100:.1f}%)")

    models = Counter(
        f"{s.get('model', 'unknown')} / {s.get('provider', 'unknown')}"
        for s in collected
    )
    print(f"\n{'-' * 70}")
    print("Model / provider usage")
    print(f"{'-' * 70}")
    for model, count in models.most_common():
        print(f"  {model}: {count}")

    tools = Counter()
    for stats in collected:
        tools.update(stats.get("tool_calls_breakdown", {}))
    if tools:
        print(f"\n{'-' * 70}")
        print("Tool call breakdown (all runs)")
        print(f"{'-' * 70}")
        for tool, count in tools.most_common():
            print(f"  {tool:<20}: {count}")

    most_expensive = max(collected, key=lambda s: s.get("cost", 0))
    longest = max(collected, key=lambda s: s.get("duration", 0))
    print(f"\n{'-' * 70}")
    print("Outliers")
    print(f"{'-' * 70}")
    print(
        f"  Most expensive run: ${most_expensive.get('cost', 0):.6f} - "
        f"{most_expensive.get('job_url', 'n/a')}"
    )
    print(
        f"  Longest run       : {longest.get('duration', 0):.1f}s - "
        f"{longest.get('job_url', 'n/a')}"
    )

    print(
        f"\nTotal data downloaded: {total_bytes:,} bytes "
        f"({total_bytes / 1024 / 1024:.2f} MB)"
    )


def analyze_workflow_runs(
    github_client: Github,
    repo_path: str,
    days_or_date: str,
    verbose: bool,
    swift_container_url: str,
    output: Optional[str] = None,
    results_dir: str = RESULTS_DIR,
) -> dict:
    """Find workflow runs and collect AI failure analysis statistics."""
    try:
        repo = github_client.get_repo(repo_path)
        start_date, end_date = get_date_range(days_or_date)

        print(
            f"Analyzing workflow runs from {start_date.isoformat()} to {end_date.isoformat()}"
        )

        all_workflow_runs = repo.get_workflow_runs(
            status="completed",
            created=f"{start_date.isoformat()}..{end_date.isoformat()}",
        )

        workflow_runs = [
            run
            for run in all_workflow_runs
            if run.name and run.name.startswith("Building on")
        ]

        total_runs = len(workflow_runs)
        print(f"{total_runs} workflow runs found.")
        print(f"\nLooking for the '{STEP_NAME}' step...\n")

        collected = []
        counters = Counter()
        total_bytes = 0

        for run_index, run in enumerate(workflow_runs, 1):
            print(f"\rProcessing run {run_index}/{total_runs}...", end="", flush=True)

            if run.status != "completed":
                continue

            for job in run.jobs():
                if job.conclusion == "cancelled":
                    continue
                if not job.completed_at:
                    continue
                if not start_date <= job.completed_at <= end_date:
                    continue

                step_conclusion = has_ai_step(job)
                if step_conclusion is None:
                    continue

                counters["with_step"] += 1

                if step_conclusion == "skipped":
                    counters["skipped"] += 1
                    continue

                result = analyze_job_logs(repo_path, job.id)
                total_bytes += result["bytes_downloaded"]

                if result["status"] != "ok":
                    counters[result["status"]] += 1
                    continue

                stats = result["stats"]
                stats["job_url"] = job.html_url
                stats["run_id"] = run.id
                stats["job_id"] = job.id
                stats["substrate"] = result["substrate"]
                stats["uuid"] = result["uuid"]
                stats["completed_at"] = (
                    job.completed_at.isoformat() if job.completed_at else None
                )
                collected.append(stats)

                if result["uuid"]:
                    artifacts = download_artifacts(
                        result["uuid"], results_dir, swift_container_url
                    )
                    stats["artifacts"] = artifacts["files"]
                    counters["artifacts_downloaded"] += artifacts["downloaded"]
                    counters["artifacts_failed"] += artifacts["failed"]
                    if artifacts["downloaded"]:
                        counters["runs_with_artifacts"] += 1
                else:
                    stats["artifacts"] = []
                    counters["no_uuid"] += 1

                if verbose:
                    print_job_details(run, job, result["substrate"], stats)

        print(f"\n\nProcessed {counters['with_step']} jobs containing the step.")
        print_summary(collected, counters, total_bytes)

        if collected:
            timestamp = datetime.now().strftime("%Y%m%d-%H%M%S")
            path = output or f"tool_calls-{timestamp}.txt"
            written = write_shell_calls(collected, path)
            print(f"\nRecorded {written:,} shell calls to {path}")
            print(
                f"Downloaded {counters['artifacts_downloaded']} happy-bot files for "
                f"{counters['runs_with_artifacts']} runs into {results_dir}/"
            )
            if counters["no_uuid"]:
                print(f"  Runs without a weebl UUID: {counters['no_uuid']}")
            if counters["artifacts_failed"]:
                print(f"  Failed downloads: {counters['artifacts_failed']}")

        return {"collected": collected, "counters": counters, "total_bytes": total_bytes}

    except GithubException as e:
        if e.status == 401:
            raise ValueError("Authentication failed. Check your GitHub token.")
        elif e.status == 403:
            raise ValueError("API rate limit exceeded or insufficient permissions.")
        else:
            raise ValueError(f"GitHub API error: {e.data}")


def main():
    """Main function."""
    try:
        args = parse_arguments()
        github_token, repo_path, swift_container_url = load_environment()

        github_client = Github(github_token)
        analyze_workflow_runs(
            github_client,
            repo_path,
            args.days_or_date,
            args.verbose,
            swift_container_url,
            args.output,
            args.results_dir,
        )

    except ValueError as e:
        print(f"Error: {e}", file=sys.stderr)
        sys.exit(1)
    except Exception as e:
        print(f"Unexpected error: {e}", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
