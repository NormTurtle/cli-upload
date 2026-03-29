import contextlib
import os
from pathlib import Path

# Configuration: Files and the variable names to protect
PROTECTED_FILES = {"gofile.py": "TOKEN", "uc.py": "API_KEY", "viking.py": "USER_HASH"}

HOOKS_DIR = Path(".git/hooks")
STASH_FILE = HOOKS_DIR / "secret_stash.json"

PRE_COMMIT_PY = HOOKS_DIR / "pre-commit-logic.py"
POST_COMMIT_PY = HOOKS_DIR / "post-commit-logic.py"

PRE_COMMIT_HOOK = HOOKS_DIR / "pre-commit"
POST_COMMIT_HOOK = HOOKS_DIR / "post-commit"

PRE_COMMIT_SCRIPT = r"""import os
import re
import json
from pathlib import Path

PROTECTED = {
    "gofile.py": "TOKEN",
    "uc.py": "API_KEY",
    "viking.py": "USER_HASH"
}
STASH_PATH = Path(".git/hooks/secret_stash.json")

def main():
    stash = {}
    staged_files = os.popen("git diff --cached --name-only").read().splitlines()
    
    modified_any = False
    for filename in staged_files:
        if filename in PROTECTED:
            var_name = PROTECTED[filename]
            path = Path(filename)
            if not path.exists(): continue
            
            content = path.read_text(encoding="utf-8")
            # Regex to find variable = "value"
            pattern = rf'({var_name}\s*=\s*")([^"]+)(")'
            
            if re.search(pattern, content):
                # Save original for post-commit restoration
                stash[filename] = content
                
                # Replace with placeholder using \g<1> for robust backreferencing
                placeholder = rf'\g<1>PASTE_{var_name}_HERE\g<3>'
                new_content = re.sub(pattern, placeholder, content)
                path.write_text(new_content, encoding="utf-8")
                
                # Re-stage the cleaned file
                os.system(f'git add "{filename}"')
                modified_any = True
                print(f"[Hook] Stripped {var_name} from {filename} for commit (placeholder used).")

    if modified_any:
        existing = {}
        if STASH_PATH.exists():
            try:
                existing = json.loads(STASH_PATH.read_text(encoding="utf-8"))
            except:
                pass
        existing.update(stash)
        STASH_PATH.write_text(json.dumps(existing), encoding="utf-8")

if __name__ == "__main__":
    main()
"""

POST_COMMIT_SCRIPT = r"""import json
from pathlib import Path

STASH_PATH = Path(".git/hooks/secret_stash.json")

def main():
    if not STASH_PATH.exists():
        return
    
    try:
        stash = json.loads(STASH_PATH.read_text(encoding="utf-8"))
        for filename, content in stash.items():
            Path(filename).write_text(content, encoding="utf-8")
            print(f"[Hook] Restored {filename} with original keys.")
    finally:
        STASH_PATH.unlink(missing_ok=True)

if __name__ == "__main__":
    main()
"""

# Shell wrappers for Git to execute the Python logic
SHELL_WRAPPER = """#!/bin/sh
python3 .git/hooks/{script_name}
"""


def setup():
    if not Path(".git").exists():
        print("Error: Not a git repository.")
        sys.exit(1)

    if not HOOKS_DIR.exists():
        HOOKS_DIR.mkdir(parents=True)

    # Write Python logic files
    PRE_COMMIT_PY.write_text(PRE_COMMIT_SCRIPT, encoding="utf-8")
    POST_COMMIT_PY.write_text(POST_COMMIT_SCRIPT, encoding="utf-8")

    # Write shell hooks
    PRE_COMMIT_HOOK.write_text(
        SHELL_WRAPPER.format(script_name="pre-commit-logic.py"), encoding="utf-8"
    )
    POST_COMMIT_HOOK.write_text(
        SHELL_WRAPPER.format(script_name="post-commit-logic.py"), encoding="utf-8"
    )

    # Make executable (friendly for Linux/WSL/Git Bash)
    for hook in [PRE_COMMIT_HOOK, POST_COMMIT_HOOK]:
        with contextlib.suppress(BaseException):
            os.chmod(hook, 0o755)

    print("✅ Git hooks installed successfully!")
    print("Locked files: gofile.py, uc.py, viking.py")


if __name__ == "__main__":
    setup()
