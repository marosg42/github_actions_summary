#!/usr/bin/env python3
"""
GitHub Actions Workflow Analysis Script

Analyzes GitHub Actions workflow runs for a specified number of days
and generates step execution statistics.

Only a little of human brain and human eyes were involved in the creation of this script.
It is a product of LLM (Claude Code) with a meatbag giving instructions and checking script results.
"""

import argparse
import os
import sys
import shutil
from collections import OrderedDict
from datetime import datetime, timedelta, timezone
from typing import Dict, Tuple
import yaml
import requests

from github import Github
from github.GithubException import GithubException
from dotenv import load_dotenv


def parse_arguments() -> argparse.Namespace:
    """Parse command line arguments."""
    parser = argparse.ArgumentParser(
        description="Analyze GitHub Actions workflow runs for N days"
    )
    parser.add_argument(
        "days", type=int, help="Number of days to analyze (excluding current day)"
    )
    parser.add_argument(
        "--noprogress", action="store_true", help="Disable progress indicator"
    )
    return parser.parse_args()


def load_environment() -> Tuple[str, str]:
    """Load GitHub token and repository from environment variables."""
    load_dotenv()

    github_token = os.getenv("GITHUB_TOKEN")
    repository_path = os.getenv("GITHUB_REPOSITORY")

    if not github_token:
        raise ValueError("GITHUB_TOKEN not found in environment variables")
    if not repository_path:
        raise ValueError("GITHUB_REPOSITORY not found in environment variables")

    return github_token, repository_path


def get_date_range(days: int) -> Tuple[datetime, datetime]:
    """Calculate the date range for analysis (previous N days in UTC)."""
    now = datetime.now(timezone.utc)
    end_date = now.replace(hour=0, minute=0, second=0, microsecond=0)
    start_date = end_date - timedelta(days=days)
    return start_date, end_date


def load_steps_from_file() -> (
    Tuple[Dict[str, int], Dict[str, bool], Dict[str, str], Dict[str, bool]]
):
    """Load steps from local list_of_steps.yaml file."""
    try:
        with open("list_of_steps.yaml", "r") as file:
            data = yaml.safe_load(file)

        steps_list = [step["name"] for step in data["steps"]]
        log_download_steps = {
            step["name"]: step.get("download_logs_on_failure", False)
            for step in data["steps"]
        }
        search_strings = {
            step["name"]: step.get("search_string", "")
            for step in data["steps"]
            if step.get("download_logs_on_failure", False)
        }
        show_url_steps = {
            step["name"]: step.get("show_url", False) for step in data["steps"]
        }

        # Create mapping for step existence checking
        step_mapping = {}
        for i, step_name in enumerate(steps_list):
            step_mapping[step_name] = i

        print(f"Loaded {len(step_mapping)} steps from list_of_steps.yaml")
        return step_mapping, log_download_steps, search_strings, show_url_steps

    except Exception as e:
        print(f"Warning: Could not load list_of_steps.yaml: {e}")
        return {}, {}, {}, {}


def clean_logs_directory() -> None:
    """Clean up any existing log files before starting analysis."""
    logs_dir = "failed_step_logs"
    if os.path.exists(logs_dir):
        shutil.rmtree(logs_dir)
        print(f"Cleaned up existing logs directory: {logs_dir}")


def download_step_logs(
    github_client: Github,
    repo_path: str,
    job,
    step_name: str,
    run_id: int,
    job_id: int,
    search_string: str,
) -> None:
    """Download the last 100 lines before failure for a failed step."""
    try:
        # Create logs directory if it doesn't exist
        logs_dir = "failed_step_logs"
        os.makedirs(logs_dir, exist_ok=True)

        # Get the GitHub token from environment
        github_token = os.getenv("GITHUB_TOKEN")
        if not github_token:
            print(
                f"Cannot download logs for step '{step_name}': GITHUB_TOKEN not found"
            )
            return

        # Use the GitHub API to get job logs
        headers = {"Authorization": f"token {github_token}"}
        logs_url = (
            f"https://api.github.com/repos/{repo_path}/actions/jobs/{job_id}/logs"
        )

        response = requests.get(logs_url, headers=headers)
        if response.status_code == 200:
            # Parse the logs to find the search string and error
            full_logs = response.text
            lines = full_logs.split("\n")

            # Find the search string in the logs
            search_string_idx = None
            for i, line in enumerate(lines):
                if search_string in line:
                    search_string_idx = i
                    break

            if search_string_idx is not None:
                # Find the first "##[error]Process completed with exit code 1." after the search string
                error_idx = None
                for i in range(search_string_idx, len(lines)):
                    if "##[error]Process completed with exit code 1." in lines[i]:
                        error_idx = i
                        break

                if error_idx is not None:
                    # Get 100 lines before the error, starting from after the search string
                    start_idx = max(search_string_idx, error_idx - 100)
                    relevant_lines = lines[start_idx:error_idx]

                    # Filter out lines containing ***
                    filtered_logs = [
                        line for line in relevant_lines if "***" not in line
                    ]

                    # If we have less than 100 lines, try to get more from before the search string
                    if len(filtered_logs) < 100 and start_idx > 0:
                        additional_start = max(
                            0, start_idx - (100 - len(filtered_logs))
                        )
                        additional_lines = lines[additional_start:start_idx]
                        additional_filtered = [
                            line for line in additional_lines if "***" not in line
                        ]
                        filtered_logs = additional_filtered + filtered_logs

                    # Take last 100 lines
                    last_100_lines = (
                        filtered_logs[-100:]
                        if len(filtered_logs) > 100
                        else filtered_logs
                    )

                    # Generate filename with timestamp and run/job IDs
                    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
                    safe_step_name = step_name.replace(" ", "_").replace("/", "_")
                    filename = (
                        f"{safe_step_name}_run{run_id}_job{job_id}_{timestamp}.log"
                    )
                    filepath = os.path.join(logs_dir, filename)

                    # Write logs to file
                    with open(filepath, "w") as f:
                        f.write(f"# Failed Step Logs: {step_name}\n")
                        f.write(f"# Run ID: {run_id}\n")
                        f.write(f"# Job ID: {job_id}\n")
                        f.write(f"# Search String: {search_string}\n")
                        f.write(f"# Downloaded: {datetime.now().isoformat()}\n")
                        f.write(
                            f"# Last {len(last_100_lines)} lines before failure:\n\n"
                        )
                        f.write("\n".join(last_100_lines))

                    print(
                        f"Downloaded logs for failed step '{step_name}' to {filepath}"
                    )
                else:
                    # Search string found but no error pattern - fall back to URL
                    run_url = f"https://github.com/{repo_path}/actions/runs/{run_id}"
                    print(
                        f"Found search string '{search_string}' but no error pattern for step '{step_name}'. Run URL: {run_url}"
                    )
            else:
                # Search string not found - fall back to URL
                run_url = f"https://github.com/{repo_path}/actions/runs/{run_id}"
                print(
                    f"Could not find search string '{search_string}' for step '{step_name}'. Run URL: {run_url}"
                )
        else:
            print(
                f"Failed to download logs for step '{step_name}': HTTP {response.status_code}"
            )

    except Exception as e:
        print(f"Error downloading logs for step '{step_name}': {e}")


def analyze_workflow_runs(
    github_client: Github, repo_path: str, days: int, show_progress: bool = True
) -> Dict:
    """Analyze GitHub Actions workflow runs for the specified time period."""
    try:
        # Clean up any existing log files
        clean_logs_directory()

        repo = github_client.get_repo(repo_path)
        start_date, end_date = get_date_range(days)

        print(
            f"Analyzing workflow runs from {start_date.isoformat()} to {end_date.isoformat()}"
        )

        # Get workflow runs with pagination limit - filter by name starting with "Building on"
        all_workflow_runs = repo.get_workflow_runs(
            status="completed",
            created=f"{start_date.isoformat()}..{end_date.isoformat()}",
        )

        # Filter for workflows starting with "Building on"
        workflow_runs = [
            run
            for run in all_workflow_runs
            if run.name and run.name.startswith("Building on")
        ]

        # Get steps from local YAML file
        print("Reading steps from list_of_steps.yaml...")
        workflow_steps, log_download_steps, search_strings, show_url_steps = (
            load_steps_from_file()
        )

        step_stats = OrderedDict()

        # Initialize step_stats with all steps from the YAML file in order
        for step_name in workflow_steps:
            step_stats[step_name] = {"success": 0, "failure": 0, "total": 0}
        processed_jobs = 0

        total_runs = len(workflow_runs)
        print(f"{total_runs} workflow runs found.")

        for run_index, run in enumerate(workflow_runs, 1):
            if show_progress:
                print(
                    f"\rAnalyzing run {run_index}/{total_runs}...", end="", flush=True
                )
            if run.status != "completed":
                continue

            jobs = run.jobs()

            # Process only first job per run
            try:
                job = jobs[0]
            except IndexError:
                continue
            if job.completed_at and start_date <= job.completed_at <= end_date:
                processed_jobs += 1

                # Analyze job steps
                for step in job.steps:
                    # Only include steps that are in our list of interest
                    if step.name not in step_stats:
                        continue
                    if step.conclusion is None or step.conclusion == "skipped":
                        continue
                    step_stats[step.name]["total"] += 1
                    if step.conclusion == "success":
                        step_stats[step.name]["success"] += 1
                    elif step.conclusion == "failure":
                        step_stats[step.name]["failure"] += 1
                        # Show URL if configured for this step
                        if show_url_steps.get(step.name, False):
                            step_url = f"https://github.com/{repo_path}/actions/runs/{run.id}/job/{job.id}#step:{step.number}:1"
                            print(
                                f"Failed step '{step.name}' in job '{job.name}': {step_url}"
                            )
                        # Download logs if configured for this step
                        if log_download_steps.get(step.name, False):
                            search_string = search_strings.get(step.name, "")
                            download_step_logs(
                                github_client,
                                repo_path,
                                job,
                                step.name,
                                run.id,
                                job.id,
                                search_string,
                            )

        if show_progress:
            print()  # New line after progress indicator
        return {
            "step_stats": step_stats,
            "workflow_steps": workflow_steps,
            "processed_jobs": processed_jobs,
            "date_range": (start_date, end_date),
        }

    except GithubException as e:
        if e.status == 401:
            raise ValueError("Authentication failed. Check your GitHub token.")
        elif e.status == 403:
            raise ValueError("API rate limit exceeded or insufficient permissions.")
        else:
            raise ValueError(f"GitHub API error: {e.data}")


def print_summary(analysis_result: Dict):
    """Print the summary of step execution statistics."""
    step_stats = analysis_result["step_stats"]
    processed_jobs = analysis_result["processed_jobs"]
    start_date, end_date = analysis_result["date_range"]

    print("\n" + "=" * 60)
    print("GITHUB ACTIONS WORKFLOW ANALYSIS SUMMARY")
    print("=" * 60)
    print(
        f"Analysis Period: {start_date.strftime('%Y-%m-%d')} to {end_date.strftime('%Y-%m-%d')}"
    )
    print(f"Processed Jobs: {processed_jobs}")
    print()

    if not step_stats:
        print("No steps found in the specified time period.")
        return

    print("STEP EXECUTION STATISTICS:")
    print("-" * 60)
    print(
        f"{'Step Name':<40} {'Total':<8} {'Success':<8} {'Failure':<8} {'Success %':<10}"
    )
    print("-" * 60)

    # Iterate through OrderedDict which maintains the order from YAML file
    for step_name, stats in step_stats.items():
        total = stats["total"]
        success = stats["success"]
        failure = stats["failure"]
        success_rate = (success / total * 100) if total > 0 else 0

        print(
            f"{step_name[:39]:<40} {total:<8} {success:<8} {failure:<8} {success_rate:<10.1f}"
        )


def main():
    """Main function."""
    try:
        args = parse_arguments()
        github_token, repo_path = load_environment()

        github_client = Github(github_token)
        analysis_result = analyze_workflow_runs(
            github_client, repo_path, args.days, show_progress=not args.noprogress
        )
        print_summary(analysis_result)

    except ValueError as e:
        print(f"Error: {e}", file=sys.stderr)
        sys.exit(1)
    except Exception as e:
        print(f"Unexpected error: {e}", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
