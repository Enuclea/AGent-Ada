#!/usr/bin/env python3
import sys
import time
from pathlib import Path

# Add the directory of sequential_roundtable.py to path
sys.path.append(str(Path(__file__).parent.resolve()))
import sequential_roundtable

def main():
    for i in range(1, 16):
        print(f"\n==========================================")
        print(f"ROUNDTABLE LOOP {i} OF 15 STARTING")
        print(f"==========================================\n")
        try:
            sequential_roundtable.main()
        except Exception as e:
            print(f"Error in loop {i}: {e}", file=sys.stderr)
        print(f"\n==========================================")
        print(f"ROUNDTABLE LOOP {i} OF 15 COMPLETED")
        print(f"==========================================\n")
        if i < 15:
            print("Waiting 10 seconds before next loop...")
            time.sleep(10)

if __name__ == "__main__":
    main()
