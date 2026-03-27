import os
import subprocess
import time

DUMMY_FILE = "dummy_1mb_test.bin"
MB_SIZE = 1 * 1024 * 1024


def create_dummy_file():
    print(f"Creating dummy test file ({DUMMY_FILE})...")
    with open(DUMMY_FILE, "wb") as f:
        f.write(os.urandom(MB_SIZE))


def run_test_host(name, script):
    print(f"\n[{name}] Testing upload...")
    start_time = time.time()
    try:
        # Run the script with the dummy file
        result = subprocess.run(
            ["python", script, DUMMY_FILE], capture_output=True, text=True, timeout=120
        )
        elapsed = time.time() - start_time

        if result.returncode == 0:
            print(f"✅ {name} SUCCESS ({elapsed:.2f}s)")
            # Print the last few lines of output which usually contain the link
            lines = [line.strip() for line in result.stdout.split("\n") if line.strip()]
            for line in lines[-3:]:
                print(f"   > {line}")
            return True
        else:
            print(f"❌ {name} FAILED (Exit Code {result.returncode})")
            print("--- STDOUT ---")
            print(result.stdout)
            print("--- STDERR ---")
            print(result.stderr)
            return False

    except subprocess.TimeoutExpired:
        print(f"❌ {name} TIMEOUT after 120s")
        return False
    except Exception as e:
        print(f"❌ {name} ERROR: {e}")
        return False


def main():
    try:
        create_dummy_file()

        hosts = [
            ("UC Files (uc.py)", "uc.py"),
            ("Viking (viking.py)", "viking.py"),
            ("GoFile (gofile.py)", "gofile.py"),
        ]

        results = {}
        for name, script in hosts:
            if not os.path.exists(script):
                print(f"⚠️ Skipping {name} - file {script} not found.")
                continue

            results[name] = run_test_host(name, script)
            time.sleep(2)  # brief pause between tests to not hit ratelimits

        print("\n=== SMOKE TEST SUMMARY ===")
        all_passed = True
        for name, passed in results.items():
            status = "✅ PASSED" if passed else "❌ FAILED"
            print(f"{name:20s}: {status}")
            if not passed:
                all_passed = False

        if all_passed:
            print("\nAll host tests passed! 🎉")
        else:
            print("\nSome tests failed. Check logs above. ⚠️")

    finally:
        if os.path.exists(DUMMY_FILE):
            os.remove(DUMMY_FILE)
            print(f"\nCleaned up {DUMMY_FILE}")


if __name__ == "__main__":
    main()
