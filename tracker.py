#!/usr/bin/env python3
"""
GitHub Organization Issue Tracker & Email Notifier

A serverless Python tool that monitors GitHub organizations for newly created issues
and dispatches email notifications via Gmail SMTP.

Environment Variables:
    TARGET_ORGS         : Comma-separated list of GitHub organizations (e.g. "google,apache,pallets")
    GITHUB_TOKEN        : GitHub Personal Access Token or Actions GITHUB_TOKEN (optional, but recommended)
    EMAIL_SENDER        : Sender Gmail address (e.g. "yourname@gmail.com")
    GMAIL_APP_PASSWORD  : 16-character Google App Password
    EMAIL_RECIPIENT     : Recipient email address (defaults to EMAIL_SENDER if not specified)
    SEEN_ISSUES_FILE    : Path to deduplication JSON file (defaults to "seen_issues.json")
"""

import argparse
from datetime import datetime, timezone
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
import json
import logging
import os
from pathlib import Path
import smtplib
import sys
import tempfile
from typing import Any, Dict, List, Optional, Set, Tuple

# Prefer 'requests' as specified in pixi.toml, with fallback to standard library urllib
try:
    import requests
    HAS_REQUESTS = True
except ImportError:
    import urllib.request
    import urllib.error
    import urllib.parse
    HAS_REQUESTS = False

# ---------------------------------------------------------------------------
# Logging Configuration
# ---------------------------------------------------------------------------
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
logger = logging.getLogger("IssueTracker")

# ---------------------------------------------------------------------------
# Constants & Defaults
# ---------------------------------------------------------------------------
DEFAULT_ORGS = ["google",
            "pallets",
            "cBioPortal",
            "google-deepmind",
            "kornia",
            "openclimatefix",
            "huggingface",
            "pytorch",
            "kubeflow",
            "freecodecamp",
            "cncf",
            "kubernetes",
            "KDE",
            "apache",
            "nodejs",
            "linuxfoundation"]
DEFAULT_SEEN_FILE = "seen_issues.json"
GMAIL_SMTP_HOST = "smtp.gmail.com"
GMAIL_SMTP_PORT = 587
GITHUB_API_BASE = "https://api.github.com"


class IssueTracker:
    def __init__(
        self,
        orgs: List[str],
        seen_file: Path,
        github_token: Optional[str] = None,
        email_sender: Optional[str] = None,
        gmail_app_password: Optional[str] = None,
        email_recipient: Optional[str] = None,
        dry_run: bool = False,
        limit_per_org: int = 20,
    ):
        self.orgs = [org.strip() for org in orgs if org.strip()]
        self.seen_file = seen_file
        self.github_token = github_token
        self.email_sender = email_sender
        self.gmail_app_password = gmail_app_password
        self.email_recipient = email_recipient or email_sender
        self.dry_run = dry_run
        self.limit_per_org = limit_per_org
        self.session = self._create_requests_session()
        self.seen_data: Dict[str, Any] = self._load_seen_issues()

    def _create_requests_session(self) -> Any:
        """Create an HTTP session with GitHub API headers and authentication."""
        if HAS_REQUESTS:
            session = requests.Session()
            session.headers.update(
                {
                    "Accept": "application/vnd.github+json",
                    "X-GitHub-Api-Version": "2022-11-28",
                    "User-Agent": "Serverless-GitHub-Issue-Tracker/1.0",
                }
            )
            if self.github_token:
                session.headers.update({"Authorization": "Bearer " + self.github_token})
                logger.info("GitHub API authentication enabled.")
            else:
                logger.warning(
                    "GITHUB_TOKEN is not set. Requests are unauthenticated and subject to strict rate limits."
                )
            return session
        else:
            logger.info("Running in standard library mode (urllib).")
            return None

    def _load_seen_issues(self) -> Dict[str, Any]:
        """Load previously processed issue IDs from the local JSON file."""
        if not self.seen_file.exists():
            logger.info(f"No existing record found at '{self.seen_file}'. Starting fresh.")
            return {}

        try:
            with open(self.seen_file, "r", encoding="utf-8") as f:
                content = f.read().strip()
                if not content:
                    return {}
                data = json.loads(content)

                # Support backward compatibility if stored as a list of IDs
                if isinstance(data, list):
                    logger.info(f"Loaded {len(data)} seen issues from list format.")
                    return {str(item_id): {} for item_id in data}
                elif isinstance(data, dict):
                    logger.info(f"Loaded {len(data)} seen issues from '{self.seen_file}'.")
                    return {str(k): v for k, v in data.items()}
                else:
                    logger.warning("Unexpected data structure in seen_issues.json. Resetting.")
                    return {}
        except (json.JSONDecodeError, OSError) as e:
            logger.error(f"Error reading '{self.seen_file}': {e}. Treating as empty.")
            return {}

    def _save_seen_issues(self) -> None:
        """Save the updated seen issues dictionary atomically to disk."""
        if self.dry_run:
            logger.info("[Dry Run] Skipping write to seen_issues.json.")
            return

        self.seen_file.parent.mkdir(parents=True, exist_ok=True)
        # Write to temporary file first for atomic replacement
        temp_dir = self.seen_file.parent
        with tempfile.NamedTemporaryFile("w", dir=temp_dir, delete=False, encoding="utf-8") as tf:
            json.dump(self.seen_data, tf, indent=2, sort_keys=True)
            temp_path = Path(tf.name)

        temp_path.replace(self.seen_file)
        logger.info(f"Saved {len(self.seen_data)} total issues to '{self.seen_file}'.")

    def fetch_org_issues(self, org: str) -> List[Dict[str, Any]]:
        """
        Fetch newly created open issues for a given organization using GitHub Search API.
        This queries across all public repositories in the org in a single request.
        """
        logger.info(f"Fetching open issues for organization: '{org}'...")
        # Note: 'is:issue' ensures Pull Requests are excluded
        query = f"org:{org} is:issue state:open"
        url = f"{GITHUB_API_BASE}/search/issues"
        params = {
            "q": query,
            "sort": "created",
            "order": "desc",
            "per_page": min(self.limit_per_org, 100),
        }

        if HAS_REQUESTS and self.session:
            try:
                response = self.session.get(url, params=params, timeout=15)
                response.raise_for_status()
                data = response.json()
                items = data.get("items", [])
                logger.info(f"Retrieved {len(items)} issues for '{org}'.")
                return items
            except requests.exceptions.HTTPError as e:
                if response.status_code == 403:
                    logger.error(f"GitHub API rate limit exceeded or access forbidden: {e}")
                elif response.status_code == 422:
                    logger.error(f"Unprocessable entity for org '{org}' (check if org name is valid): {e}")
                else:
                    logger.error(f"HTTP error fetching issues for org '{org}': {e}")
                return []
            except requests.exceptions.RequestException as e:
                logger.error(f"Network error communicating with GitHub API for org '{org}': {e}")
                return []
        else:
            # Standard library urllib fallback
            encoded_query = urllib.parse.urlencode(params)
            full_url = f"{url}?{encoded_query}"
            req = urllib.request.Request(full_url)
            req.add_header("Accept", "application/vnd.github+json")
            req.add_header("X-GitHub-Api-Version", "2022-11-28")
            req.add_header("User-Agent", "Serverless-GitHub-Issue-Tracker/1.0")
            if self.github_token:
                req.add_header("Authorization", "Bearer " + self.github_token)

            try:
                with urllib.request.urlopen(req, timeout=15) as resp:
                    data = json.loads(resp.read().decode("utf-8"))
                    items = data.get("items", [])
                    logger.info(f"Retrieved {len(items)} issues for '{org}'.")
                    return items
            except urllib.error.HTTPError as e:
                logger.error(f"HTTP error fetching issues for org '{org}': {e.code} {e.reason}")
                return []
            except urllib.error.URLError as e:
                logger.error(f"Network error communicating with GitHub API for org '{org}': {e.reason}")
                return []

    def extract_issue_details(self, issue: Dict[str, Any], org: str) -> Dict[str, Any]:
        """
        Extract required issue fields:
        - Direct link (HTML URL)
        - Issue Title
        - Tags / Labels (comma-separated)
        - Assignment Status (whether it is assigned to someone or not)
        """
        issue_id = str(issue["id"])
        html_url = issue.get("html_url", "")
        title = issue.get("title", "Untitled Issue")

        # 1. Tags / Labels (comma-separated)
        raw_labels = issue.get("labels", [])
        label_names = [label["name"] for label in raw_labels if isinstance(label, dict) and "name" in label]
        labels_str = ", ".join(label_names) if label_names else "None"

        # 2. Assignment Status
        assignees = issue.get("assignees", [])
        single_assignee = issue.get("assignee")
        if assignees:
            assignee_logins = [a["login"] for a in assignees if isinstance(a, dict) and "login" in a]
            assignment_status = f"Assigned to {', '.join(assignee_logins)}"
            is_assigned = True
        elif single_assignee and isinstance(single_assignee, dict):
            assignment_status = f"Assigned to {single_assignee.get('login', 'unknown')}"
            is_assigned = True
        else:
            assignment_status = "Unassigned"
            is_assigned = False

        # Additional useful metadata
        repo_url = issue.get("repository_url", "")
        repo_name = repo_url.split("/")[-1] if repo_url else org
        author = issue.get("user", {}).get("login", "Unknown")
        created_at = issue.get("created_at", "")

        return {
            "id": issue_id,
            "org": org,
            "repo": repo_name,
            "html_url": html_url,
            "title": title,
            "labels": labels_str,
            "raw_labels": label_names,
            "assignment_status": assignment_status,
            "is_assigned": is_assigned,
            "author": author,
            "created_at": created_at,
        }

    def _build_email_content(self, issue: Dict[str, Any]) -> Tuple[str, str]:
        """Generate both plain-text and HTML email representations for an issue."""
        status_color = "#2da44e" if not issue["is_assigned"] else "#0969da"
        
        # Labels HTML tags
        labels_html = ""
        if issue["raw_labels"]:
            for lbl in issue["raw_labels"]:
                labels_html += f'<span style="background: #ddf4ff; color: #0969da; padding: 3px 8px; border-radius: 12px; font-size: 12px; margin-right: 5px; display: inline-block;">{lbl}</span>'
        else:
            labels_html = '<span style="color: #6e7781; font-style: italic;">No labels</span>'

        # Plain text representation
        plain_text = (
            f"New GitHub Issue Alert!\n\n"
            f"Title: {issue['title']}\n"
            f"Repository: {issue['org']}/{issue['repo']}\n"
            f"Direct Link: {issue['html_url']}\n"
            f"Assignment Status: {issue['assignment_status']}\n"
            f"Labels / Tags: {issue['labels']}\n"
            f"Created By: @{issue['author']}\n"
            f"Created At: {issue['created_at']}\n\n"
            f"--\nSent by GitHub Serverless Tracker"
        )

        # Responsive modern HTML card
        html = f"""<!DOCTYPE html>
<html>
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1.0">
  <title>New Issue Alert</title>
</head>
<body style="font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Helvetica, Arial, sans-serif; background-color: #f6f8fa; margin: 0; padding: 24px; color: #24292f;">
  <table width="100%" border="0" cellspacing="0" cellpadding="0">
    <tr>
      <td align="center">
        <table width="600" border="0" cellspacing="0" cellpadding="0" style="background-color: #ffffff; border-radius: 8px; border: 1px solid #d0d7de; overflow: hidden; box-shadow: 0 1px 3px rgba(0,0,0,0.05);">
          <!-- Header -->
          <tr>
            <td style="background-color: #24292f; padding: 18px 24px;">
              <h2 style="color: #ffffff; margin: 0; font-size: 18px; font-weight: 600;">
                🔔 GitHub Issue Alert: <span style="color: #58a6ff;">{issue['org']}/{issue['repo']}</span>
              </h2>
            </td>
          </tr>
          <!-- Body -->
          <tr>
            <td style="padding: 24px;">
              <h3 style="margin-top: 0; margin-bottom: 12px; font-size: 20px; line-height: 1.4;">
                <a href="{issue['html_url']}" style="color: #0969da; text-decoration: none;">
                  {issue['title']}
                </a>
              </h3>

              <div style="margin-bottom: 20px;">
                <span style="background-color: {status_color}; color: #ffffff; padding: 4px 10px; border-radius: 20px; font-size: 13px; font-weight: 600; display: inline-block;">
                  {issue['assignment_status']}
                </span>
              </div>

              <table width="100%" border="0" cellspacing="0" cellpadding="8" style="background-color: #f6f8fa; border-radius: 6px; font-size: 14px; margin-bottom: 24px;">
                <tr>
                  <td width="130" style="font-weight: 600; color: #57609a;">Direct Link:</td>
                  <td><a href="{issue['html_url']}" style="color: #0969da; word-break: break-all;">{issue['html_url']}</a></td>
                </tr>
                <tr>
                  <td style="font-weight: 600; color: #57609a;">Tags / Labels:</td>
                  <td>{labels_html}</td>
                </tr>
                <tr>
                  <td style="font-weight: 600; color: #57609a;">Created By:</td>
                  <td>@{issue['author']}</td>
                </tr>
                <tr>
                  <td style="font-weight: 600; color: #57609a;">Created At:</td>
                  <td style="color: #57609a;">{issue['created_at']}</td>
                </tr>
              </table>

              <!-- CTA Button -->
              <table border="0" cellspacing="0" cellpadding="0">
                <tr>
                  <td align="center" style="border-radius: 6px;" bgcolor="#2da44e">
                    <a href="{issue['html_url']}" target="_blank" style="font-size: 14px; font-weight: 600; color: #ffffff; text-decoration: none; padding: 10px 20px; border-radius: 6px; display: inline-block;">
                      Open Issue on GitHub &rarr;
                    </a>
                  </td>
                </tr>
              </table>
            </td>
          </tr>
          <!-- Footer -->
          <tr>
            <td style="background-color: #f6f8fa; border-top: 1px solid #d0d7de; padding: 14px 24px; font-size: 12px; color: #6e7781; text-align: center;">
              Automated serverless alert via GitHub Actions &amp; Python Tracker
            </td>
          </tr>
        </table>
      </td>
    </tr>
  </table>
</body>
</html>
"""
        return plain_text, html

    def send_email_alerts(self, new_issues: List[Dict[str, Any]]) -> None:
        """Send email alerts for each new issue using Gmail SMTP with STARTTLS."""
        if not new_issues:
            return

        if self.dry_run:
            logger.info(f"[Dry Run] Skipping email delivery for {len(new_issues)} new issues.")
            for issue in new_issues:
                logger.info(f"[Dry Run Notification] {issue['title']} ({issue['html_url']})")
            return

        if not self.email_sender or not self.gmail_app_password:
            logger.warning(
                "EMAIL_SENDER or GMAIL_APP_PASSWORD is not configured. "
                "Skipping email delivery. Check your GitHub Secrets!"
            )
            return

        logger.info(
            f"Connecting to Gmail SMTP ({GMAIL_SMTP_HOST}:{GMAIL_SMTP_PORT}) "
            f"to dispatch {len(new_issues)} notification(s)..."
        )

        try:
            # Re-use a single authenticated SMTP session to send all alerts efficiently
            with smtplib.SMTP(GMAIL_SMTP_HOST, GMAIL_SMTP_PORT, timeout=30) as server:
                server.ehlo()
                server.starttls()
                server.ehlo()
                server.login(self.email_sender, self.gmail_app_password)

                for issue in new_issues:
                    subject = f"[{issue['org']}/{issue['repo']}] New Issue: {issue['title']}"
                    plain_text, html_content = self._build_email_content(issue)

                    msg = MIMEMultipart("alternative")
                    msg["Subject"] = subject
                    msg["From"] = f"GitHub Tracker <{self.email_sender}>"
                    msg["To"] = self.email_recipient

                    # Attach text/plain first, then text/html (standard MIME preference)
                    msg.attach(MIMEText(plain_text, "plain", "utf-8"))
                    msg.attach(MIMEText(html_content, "html", "utf-8"))

                    server.send_message(msg)
                    logger.info(f"Dispatched email alert for issue #{issue['id']} to {self.email_recipient}")

            logger.info("All email alerts sent successfully.")
        except smtplib.SMTPAuthenticationError as e:
            logger.error(f"Gmail SMTP authentication failed. Please verify your App Password: {e}")
        except smtplib.SMTPException as e:
            logger.error(f"SMTP error while sending email notifications: {e}")
        except Exception as e:
            logger.error(f"Unexpected error sending email notifications: {e}")

    def run(self) -> None:
        """Main execution flow: fetch -> deduplicate -> alert -> persist."""
        logger.info(f"Beginning issue tracking run for organizations: {self.orgs}")
        newly_discovered_issues: List[Dict[str, Any]] = []

        for org in self.orgs:
            issues = self.fetch_org_issues(org)
            for item in issues:
                # Exclude pull requests (in case GitHub API returned any)
                if "pull_request" in item:
                    continue

                issue_id = str(item["id"])
                if issue_id in self.seen_data:
                    # Already processed
                    continue

                # Process new issue
                details = self.extract_issue_details(item, org)
                newly_discovered_issues.append(details)

                # Record in local dictionary
                self.seen_data[issue_id] = {
                    "title": details["title"],
                    "html_url": details["html_url"],
                    "org": details["org"],
                    "repo": details["repo"],
                    "seen_at": datetime.now(timezone.utc).isoformat(),
                }

        if newly_discovered_issues:
            logger.info(
                f"Found {len(newly_discovered_issues)} new issue(s) across monitored organizations."
            )
            # Dispatch email notifications
            self.send_email_alerts(newly_discovered_issues)
            # Persist state
            self._save_seen_issues()
        else:
            logger.info("No new issues found since the previous run. State is up to date.")


# ---------------------------------------------------------------------------
# CLI & Entry Point
# ---------------------------------------------------------------------------
def parse_arguments() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Monitor GitHub organizations for new issues and alert via Gmail SMTP."
    )
    parser.add_argument(
        "--orgs",
        type=str,
        default=os.environ.get("TARGET_ORGS", ",".join(DEFAULT_ORGS)),
        help="Comma-separated list of GitHub organizations (e.g. 'google,pallets')",
    )
    parser.add_argument(
        "--file",
        type=str,
        default=os.environ.get("SEEN_ISSUES_FILE", DEFAULT_SEEN_FILE),
        help=f"Path to seen_issues.json file (default: {DEFAULT_SEEN_FILE})",
    )
    parser.add_argument(
        "--limit",
        type=int,
        default=20,
        help="Maximum number of issues to fetch per organization (default: 20)",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Execute without sending emails or updating the seen_issues.json file",
    )
    return parser.parse_args()


def main():
    args = parse_arguments()
    org_list = [org.strip() for org in args.orgs.split(",") if org.strip()]

    tracker = IssueTracker(
        orgs=org_list,
        seen_file=Path(args.file),
        github_token=os.environ.get("GITHUB_TOKEN"),
        email_sender=os.environ.get("EMAIL_SENDER"),
        gmail_app_password=os.environ.get("GMAIL_APP_PASSWORD"),
        email_recipient=os.environ.get("EMAIL_RECIPIENT"),
        dry_run=args.dry_run,
        limit_per_org=args.limit,
    )
    tracker.run()


if __name__ == "__main__":
    main()
