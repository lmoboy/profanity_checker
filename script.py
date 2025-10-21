from dotenv import load_dotenv
import os
import tempfile
import subprocess
import logging
from datetime import datetime
import requests
import re
import sys
import time

# Combine profanity and severe slurs into one set for convenience in regex creation
ALL_TERMS = set()

profanity = {
    "arsehole", 
    "arsewad", 
    "asshole", 
    "asswipe", 
    "bastard", 
    "bitch", 
    "bitcher", 
    "bitchers", 
    "bitches", 
    "bitchy", 
    "cock", 
    "cock-sucker", 
    "cock-suckers", 
    "cock-sucking", 
    "cocksuck", 
    "cocksucked", 
    "cocksucker", 
    "cocksuckers", 
    "cocksucking", 
    "cocksucks", 
    "cunt", 
    "cunts", 
    "dick", 
    "dildo", 
    "dildos", 
    "shit", 
    "shitty", 
    "retarded"
}
ALL_TERMS.update(profanity)

# Severe slurs list (lowercased)
SEVERE_SLURS = {
    "nigger", 
    "nigga",
    "nig",
    "nigah",
    "nigg",
    "nigg3r",
    "nigg4h",
    "niggah",
    "niggas",
    "niggaz",
    "niggers",
    "niggle",
    "niglet",
    "chink", 
    "kike", 
    "wetback", 
    "spic", 
    "gook", 
    "faggot", 
    "retard", 
    "sex", 
    "hittler", 
    'hitler', 
    
}
ALL_TERMS.update(SEVERE_SLURS)

# --- REGEX COMPILATION ---
# Create a single, case-insensitive regex pattern for all terms
def create_profanity_regex(terms):
    # Escape special regex characters in the terms (like '-') and join with '|'
    pattern = '|'.join(re.escape(term) for term in terms)
    # The pattern will be used with re.IGNORECASE later.
    return re.compile(f"({pattern})")

# Compile the regex pattern once when the script starts
PROFANITY_REGEX = create_profanity_regex(ALL_TERMS)

# Also create a set of the *severe* terms for easy lookup
# when a match is found by the combined regex.
SEVERE_SLURS_LOWER_SET = {slur.lower() for slur in SEVERE_SLURS}
# -------------------------

GITHUB_API = "https://api.github.com"
load_dotenv()

GITHUB_TOKEN = os.environ.get("GITHUB_TOKEN")
















def github_headers():
    h = {"Accept": "application/vnd.github.v3+json"}
    if GITHUB_TOKEN:
        h["Authorization"] = f"token {GITHUB_TOKEN}"
    return h

def list_user_repos(username):
    repos = []
    page = 1
    per_page = 50
    while True:
        url = f"{GITHUB_API}/users/{username}/repos"
        resp = requests.get(url, headers=github_headers(), params={"page": page, "per_page": per_page})
        resp.raise_for_status()
        batch = resp.json()
        if not batch:
            break
        repos.extend(batch)
        page += 1
    return repos

def clone_repo(repo_clone_url, dest_path):
    # If the destination already exists (clone was done before), just do a fetch / pull
    if os.path.isdir(dest_path) and os.path.isdir(os.path.join(dest_path, ".git")):
        # existing repo, update it
        try:
            # fetch latest
            subprocess.run(["git", "fetch"], cwd=dest_path, check=False, 
                          stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        except Exception as e:
            logging.warning(f"Failed to git fetch in existing repo {dest_path}: {e}")
    else:
        # fresh clone
        subprocess.run(["git", "clone", "--no-checkout", repo_clone_url, dest_path], 
                      check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

def run_git_in_repo(repo_path, args):
    try:
        proc = subprocess.run(
            ["git"] + args,
            cwd=repo_path,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            check=False
        )
    except Exception as e:
        logging.error(f"Error running git in {repo_path} args {args}: {e}")
        return ""

    try:
        out = proc.stdout.decode("utf-8", errors="replace")
    except Exception:
        out = proc.stdout.decode("latin1", errors="replace")
    return out

def scan_text_for_profanities(text, repo_name, commit_sha):
    """
    Scan the diff text using the compiled regex. Return (count, hit_severe_flag).
    Also log each offending word / context into the log.
    """
    count = 0
    hit_severe = False
    
    # Use re.findall to get all matches, case-insensitive
    matches = PROFANITY_REGEX.findall(text, re.IGNORECASE)
    
    for match in matches:
        # Convert the matched string to lowercase for checking against the sets
        matched_word_lower = match.lower()
        
        if matched_word_lower in SEVERE_SLURS_LOWER_SET:
            hit_severe = True
            logging.warning(f"[SEVERE] Repo {repo_name} commit {commit_sha}: found severe slur '{match}' in diff")
        
        if matched_word_lower in ALL_TERMS:
            count += 1
            logging.info(f"Repo {repo_name} commit {commit_sha}: profanity word '{match}'")
            
    return count, hit_severe

def analyze_repo(repo_path, repo_api_url, github_user, current_repo_index, total_repos):
    resp = requests.get(repo_api_url + "/commits", headers=github_headers(), params={"per_page": 100})
    try:
        commits_json = resp.json()
    except ValueError:
        commits_json = []
    if not isinstance(commits_json, list):
        commits_json = []
    
    if len(commits_json) == 0:
        return {
            "repo": os.path.basename(repo_path),
            "profanity_level_percent": 0.0,
            "acceptability_level": 0.0,
            "total_profanity_count": 0,
            "repo_url": repo_api_url.replace("api.github.com/repos", "github.com"),
            "total_commits": 0 # Ensure this is present for print_results
        }
    
    commit_shas = [c.get("sha") for c in commits_json if isinstance(c, dict) and c.get("sha")]
    total_commits_in_repo = len(commit_shas)

    total_commits = 0
    commits_with_profanity = 0
    total_profanity_count = 0
    any_severe = False

    repo_name = os.path.basename(repo_path)

    for commit_index, sha in enumerate(commit_shas):
        total_commits += 1
        # Update progress display
        display_progress(
            github_user, 
            repo_name, 
            sha[:8],  # Show first 8 characters of commit hash
            commit_index + 1, 
            total_commits_in_repo, 
            current_repo_index, 
            total_repos
        )
        
        diff = run_git_in_repo(repo_path, ["show", sha, "--unified=0"])
        if not diff:
            continue
        ccount, hit_sev = scan_text_for_profanities(diff, repo_name, sha)
        if ccount > 0:
            commits_with_profanity += 1
            total_profanity_count += ccount
        if hit_sev:
            any_severe = True

    if total_commits == 0:
        profanity_level = 0.0
    else:
        profanity_level = commits_with_profanity / total_commits * 100.0

    if any_severe:
        acceptability = 0.0
    else:
        acceptability = profanity_level * 0.7 

    return {
        "repo": repo_name,
        "profanity_level_percent": profanity_level,
        "acceptability_level": acceptability,
        "total_profanity_count": total_profanity_count,
        "repo_url": repo_api_url.replace("api.github.com/repos", "github.com"),
        "total_commits": total_commits
    }

def full_scan_user(username, work_dir=None, log_dir=None):
    if work_dir is None:
        work_dir = tempfile.mkdtemp(prefix=f"ghscan2_{username}_")
    os.makedirs(work_dir, exist_ok=True)

    if log_dir is None:
        log_dir = work_dir
    os.makedirs(log_dir, exist_ok=True)
    date_str = datetime.utcnow().strftime("%Y%m%d")
    log_filename = os.path.join(log_dir, f"logfile_{date_str}.log")
    
    log_file_path = os.path.abspath(log_filename)

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s | %(levelname)s | %(message)s",
        handlers=[
            logging.FileHandler(log_filename, mode="a", encoding="utf-8"),
            logging.StreamHandler()
        ]
    )
    
    logging.info(f"Log file created at: {log_file_path}")

    repos = list_user_repos(username)
    total_repos = len(repos)
    results = []
    
    for repo_index, repo in enumerate(repos):
        name = repo["name"]
        api_url = repo["url"]
        clone_url = repo["clone_url"]
        logging.info(f"--- Processing repo {name} ---")
        local_path = os.path.join(work_dir, name)
        try:
            clone_repo(clone_url, local_path)
        except Exception as e:
            logging.error(f"Error cloning repo {name}: {e}")
            continue
        logging.info(f"Analyzing repo {name} …")
        r = analyze_repo(local_path, api_url, username, repo_index + 1, total_repos)
        results.append(r)
        
    return results, log_file_path

def clear_screen():
    """Clear the terminal screen"""
    os.system('cls' if os.name == 'nt' else 'clear')

def display_progress(github_user, repository_name, repository_commit, repository_commit_index, repository_commit_total, current_repository_index, total_repositories):
    """Display real-time progress in an ASCII box"""
    # Calculate progress percentages
    commit_progress = (repository_commit_index / repository_commit_total) * 100 if repository_commit_total > 0 else 0
    total_progress = (current_repository_index / total_repositories) * 100 if total_repositories > 0 else 0
    
    # Create the progress display
    lines = [
        "Profanity checker v1.0.1",
        f"Current user: {github_user}",
        "",
        f"Current repository: {repository_name}",
        f"Current commit : {repository_commit}",
        "",
        f"Repository progress : {commit_progress:.1f}%",
        f"User progress : {total_progress:.1f}%"
    ]
    
    # Find the longest line to determine box width
    max_width = max(len(line) for line in lines)
    box_width = max_width + 4  # Add padding
    
    # Create the ASCII box
    top_bottom = "┌" + "─" * (box_width - 2) + "┐"
    middle = "│" + " " * (box_width - 2) + "│"
    
    # Clear screen and display
    clear_screen()
    print(top_bottom)
    print(middle)
    
    for line in lines:
        # Center the text within the box
        padding = (box_width - 2 - len(line)) // 2
        left_padding = padding
        right_padding = box_width - 2 - len(line) - left_padding
        centered_line = "│" + " " * left_padding + line + " " * right_padding + "│"
        print(centered_line)
    
    print(middle)
    print("└" + "─" * (box_width - 2) + "┘")
    sys.stdout.flush()

def print_results(results, log_file_path):
    print("\n--- Scan Results ---")
    print("repository | profanity_level_% | acceptability_level | total_profanity_count | repo_link")
    print("-" * 100)
    for r in results:
        # Check for presence of 'total_commits' to handle potential empty list from analyze_repo
        if 'total_commits' not in r:
            r['total_commits'] = 0 
            
        print(f"{r['repo']} | {r['profanity_level_percent']:.2f}% | {r['acceptability_level']:.2f}% | {r['total_profanity_count']} | {r['repo_url']}")
    print("-" * 100)
    total_profanities = sum(r["total_profanity_count"] for r in results)
    total_repos = len(results)
    total_commits = sum(r["total_commits"] for r in results)
    print(f"Total profanities detected: {total_profanities}")
    print(f"Total repositories checked: {total_repos}")
    print(f"Total commits checked: {total_commits}")
    
    print("\n🔗 **Full Log File Path:**")
    print(f"{log_file_path}")
    # ---------------------------------------------


if __name__ == "__main__":

    username = input("GitHub username to scan: ").strip()
    results, log_path = full_scan_user(username)
    print_results(results, log_path)
    print("Done.")
