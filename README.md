# GitHub Actions Summary

Analyze GitHub Actions workflow runs for a specified number of days and generate step execution statistics.

## Features

- Analyzes GitHub Actions workflow runs for the last N days (excluding current day, using UTC timezone)
- Filters workflows by name pattern ("Building on" prefix)
- Generates step execution success/failure statistics
- Supports custom step ordering via YAML configuration
- Provides detailed success rate reporting
- Optional progress indicator (can be disabled with `--noprogress`)

## Installation

This project uses `uv` for package management. Install dependencies:

```bash
uv sync
```

## Configuration

1. Create a `.env` file with your GitHub credentials:
   ```
   GITHUB_TOKEN=your_github_token_here
   GITHUB_REPOSITORY=owner/repo-name
   ```

2. Create a `list_of_steps.yaml` file to define which steps to analyze and their order:
   ```yaml
   steps:
     - name: "Step Name 1"
     - name: "Step Name 2"
       download_logs_on_failure: true
       search_string: "search-pattern-in-logs"
       show_url: true
     - name: "Another Step"
   ```

   **Optional step parameters:**
   - `download_logs_on_failure`: When `true`, automatically downloads the last 100 lines of logs before failure for this step
   - `search_string`: When `download_logs_on_failure` is enabled, looks for this string in logs to find the relevant section before downloading
   - `show_url`: When `true`, displays the direct URL to failed step execution in GitHub Actions UI

## Usage

### Main Analysis Script

Run the analysis for the last N days:

```bash
uv run github_actions_summary.py <days> [--noprogress]
```

Examples:
```bash
# Analyze last 7 days with progress indicator
uv run github_actions_summary.py 7

# Analyze last 7 days without progress indicator
uv run github_actions_summary.py 7 --noprogress
```

This will analyze GitHub Actions workflow runs from the last 7 days (excluding today, using UTC timezone) and display:
- Total number of processed runs and jobs
- Success/failure statistics for each step
- Success rate percentages
- Results sorted by the order defined in `list_of_steps.yaml`

### Collect Versions Analyzer

Analyze collect-versions retry successes in Setup Project Dir logs:

```bash
uv run collect_versions_analyzer.py <days>
```

Examples:
```bash
# Analyze last 7 days for collect-versions retries
uv run collect_versions_analyzer.py 7

# Analyze today only (days=0)
uv run collect_versions_analyzer.py 0
```

This specialized tool:
- Extracts logs from Setup Project Dir steps in workflow runs
- Searches for collect-versions success messages that occurred after retry attempts (attempt > 1)
- Shows both successful retry attempts and any failed attempts for context
- Filters workflows by name pattern ("Building on" prefix)
- Processes only executed (not skipped) Setup Project Dir steps

## Output

The script generates a summary report showing:
- Analysis period and scope
- Step execution statistics table
- Success rates for each monitored step

**Additional output features:**
- **Failed step URLs**: When `show_url: true` is configured for a step, direct links to failed step executions are displayed during analysis
- **Log downloads**: When `download_logs_on_failure: true` is configured, failed step logs are automatically downloaded to the `failed_step_logs/` directory with filenames like: `{step_name}_run{run_id}_job{job_id}_{timestamp}.log`
- **Log filtering**: Downloaded logs show the last 100 lines before failure, filtered to exclude lines containing `***`, and positioned relative to the specified `search_string`

## Requirements

- Python 3.8+
- GitHub Personal Access Token with repository access
- Repository with GitHub Actions workflows
