# host-upload
file host upload cli commands

## API Protection
To prevent accidental pushing of personal API keys, you should set up the local git hooks. Run this command once on each new machine:

```bash
python pre_commit.py
```

This will automatically strip secrets before each commit and restore them to your local files afterward.

### Verification
To confirm that keys are not being committed (even if they are visible in your editor), check the last commit content:
```bash
git show HEAD:uc.py
```
This shows what is actually stored in the repository, while `type uc.py` shows what is on your disk.
