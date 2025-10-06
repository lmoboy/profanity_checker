# 🔍 GitHub Profanity & Slur Checker

A Python script designed to scan public and private GitHub repositories for offensive language, including profanities and racial/derogatory slurs, across commit history. This tool utilizes **Regular Expressions (Regex)** for highly precise detection of embedded or unseparated terms within long strings (e.g., detecting "shit" within "thisisashitstring").

## ✨ Features

  * **Deep Scan:** Scans commit messages and code differences across the entire repository history.
  * **Regex-Powered Detection:** Uses a compiled, case-insensitive regex pattern to ensure precision, detecting terms even when they are part of a longer, unseparated string.
  * **Tiered Reporting:** Distinguishes between general profanity and severe slurs (racial/derogatory), logging severe hits as `[SEVERE]`.
  * **Comprehensive Output:** Provides a summary table with profanity level, an "acceptability score," and total counts per repository.
  * **Full Logging:** Generates a detailed log file with absolute path for easy inspection of every detected instance (word, repository, and commit SHA).
  * **GitHub API Integration:** Authenticates using a GitHub Personal Access Token to handle rate limits and access private repositories (if permissions allow).

## 🚀 Setup and Installation

### 1\. Prerequisites

You need **Python 3.x** and `git` installed on your system.

### 2\. Dependencies

Install the required Python libraries using pip:

```bash
pip install python-dotenv requests
```

### 3\. GitHub Token

To scan repositories (especially private ones or to avoid low public rate limits), you must set up a **GitHub Personal Access Token (PAT)**.

1.  Go to **GitHub Settings** $\rightarrow$ **Developer settings** $\rightarrow$ **Personal access tokens** $\rightarrow$ **Tokens (classic)**.
2.  Generate a new token.
3.  For permissions, you will need at least the **`repo`** scope (for all repository-related actions) to ensure full functionality.
4.  Copy the generated token.

### 4\. Environment File (`.env`)

Create a file named **`.env`** in the same directory as the Python script and add your token:

```
# .env file
GITHUB_TOKEN="YOUR_PERSONAL_ACCESS_TOKEN_HERE"
```

## 💻 How to Run

1.  Save the provided script as a Python file (e.g., `checker.py`).
2.  Make sure your `.env` file is in the same directory.
3.  Run the script from your terminal:

<!-- end list -->

```bash
python checker.py
```

4.  The script will prompt you for the **GitHub username** you wish to scan.

### Example Output

```
GitHub username to scan: octocat
... (log output as scan runs) ...

--- Scan Results ---
repository | profanity_level_% | acceptability_level | total_profanity_count | repo_link
----------------------------------------------------------------------------------------------------
my-repo    | 12.50% | 8.75% | 3 | https://github.com/octocat/my-repo
test-code  | 0.00%  | 0.00% | 0 | https://github.com/octocat/test-code
----------------------------------------------------------------------------------------------------
Total profanities detected: 3
Total repositories checked: 2
Total commits checked: 24

🔗 **Full Log File Path:**
/var/folders/2h/l4j20/T/ghscan2_octocat_pvt2/logfile_20251006.log
Done.
```

## ⚙️ Configuration & Customization

The core logic resides in the word lists and the regex compilation.

### Word Lists

You can easily modify the built-in sets at the top of the script:

  * **`profanity`**: General expletives and non-severe terms.
  * **`SEVERE_SLURS`**: Terms classified as racial or derogatory slurs.

<!-- end list -->

```python
# In checker.py

# Add or remove terms as needed (must be lowercase)
profanity = {
    "ass", 
    # ... existing terms
}

SEVERE_SLURS = {
    "nigger", 
    "chink", 
    # ... existing slurs
}
```

### Regex Pattern

The `create_profanity_regex` function automatically handles the combined list and ensures special characters (like hyphens) are correctly escaped, making it robust against changes in your word lists.

```python
# The pattern ensures terms are matched case-insensitively, 
# even when embedded in other text, thanks to re.findall()
def create_profanity_regex(terms):
    pattern = '|'.join(re.escape(term) for term in terms)
    return re.compile(f"({pattern})")
```

-----

## ⚠️ Disclaimer on "Acceptability"

The script calculates an `acceptability_level` based on your original logic: `acceptability = profanity_level * 0.7`.

**Note:** This calculation is **inversely proportional** to what "acceptability" usually implies (a higher profanity level results in a higher acceptability score). You may wish to adjust this metric based on your specific grading or policy requirements. A common alternative is a simple deduction: `acceptability = 100.0 - (profanity_level * weight)`.
