import os
import subprocess
import tempfile
from pathlib import Path
from typing import Tuple, List

def merge_text(
    base_text: str, 
    a_text: str, 
    b_text: str, 
    label_a: str = "HEAD", 
    label_b: str = "specialist_b"
) -> Tuple[str, bool]:
    """Performs a 3-way differential merge on strings.
    
    Returns:
        Tuple[str, bool]: The merged text and a boolean indicating if a conflict was detected.
    """
    # 1. Try pure Python merge first to resolve identical changes and avoid subprocess calls
    py_merged, py_conflict = merge_pure_python(base_text, a_text, b_text, label_a, label_b)
    if not py_conflict:
        return py_merged, False

    # 2. If there is a conflict, try system diff3 command for standard formatting
    try:
        with tempfile.TemporaryDirectory() as tmpdir:
            tmp_path = Path(tmpdir)
            base_file = tmp_path / "base"
            a_file = tmp_path / "a"
            b_file = tmp_path / "b"
            
            base_file.write_text(base_text, encoding="utf-8")
            a_file.write_text(a_text, encoding="utf-8")
            b_file.write_text(b_text, encoding="utf-8")
            
            # diff3 -m MYFILE OLDFILE YOURFILE -> -m A BASE B
            cmd = [
                "diff3", 
                "-m", 
                "-L", label_a, 
                "-L", "BASE", 
                "-L", label_b, 
                str(a_file), 
                str(base_file), 
                str(b_file)
            ]
            proc = subprocess.run(cmd, capture_output=True, text=True)
            if proc.returncode in (0, 1):
                conflict = (proc.returncode == 1)
                return proc.stdout, conflict
    except Exception:
        pass

    # 3. Fallback to pure Python merge result if diff3 fails
    return py_merged, py_conflict


def merge_files(
    base_path: Path, 
    a_path: Path, 
    b_path: Path, 
    label_a: str = "HEAD", 
    label_b: str = "specialist_b"
) -> Tuple[str, bool]:
    """Performs a 3-way differential merge on files.
    
    Returns:
        Tuple[str, bool]: The merged text and a boolean indicating if a conflict was detected.
    """
    base_text = base_path.read_text(encoding="utf-8") if base_path.exists() else ""
    a_text = a_path.read_text(encoding="utf-8") if a_path.exists() else ""
    b_text = b_path.read_text(encoding="utf-8") if b_path.exists() else ""
    
    return merge_text(base_text, a_text, b_text, label_a, label_b)


def merge_pure_python(
    base_text: str, 
    a_text: str, 
    b_text: str, 
    label_a: str, 
    label_b: str
) -> Tuple[str, bool]:
    """A robust pure-Python 3-way line-level merge fallback."""
    import difflib

    base_lines = base_text.splitlines(keepends=True)
    a_lines = a_text.splitlines(keepends=True)
    b_lines = b_text.splitlines(keepends=True)

    # Find matching blocks between base and a
    matcher_a = difflib.SequenceMatcher(None, base_lines, a_lines)
    opcodes_a = matcher_a.get_opcodes()

    # Find matching blocks between base and b
    matcher_b = difflib.SequenceMatcher(None, base_lines, b_lines)
    opcodes_b = matcher_b.get_opcodes()

    status_a = ['equal'] * (len(base_lines) + 1)
    replace_a = [None] * (len(base_lines) + 1)
    insert_a = [[] for _ in range(len(base_lines) + 1)]

    for tag, i1, i2, j1, j2 in opcodes_a:
        if tag == 'equal':
            continue
        elif tag == 'insert':
            insert_a[i1].extend(a_lines[j1:j2])
        elif tag == 'delete':
            for i in range(i1, i2):
                status_a[i] = 'delete'
        elif tag == 'replace':
            status_a[i1] = 'replace'
            replace_a[i1] = a_lines[j1:j2]
            for i in range(i1 + 1, i2):
                status_a[i] = 'delete'

    status_b = ['equal'] * (len(base_lines) + 1)
    replace_b = [None] * (len(base_lines) + 1)
    insert_b = [[] for _ in range(len(base_lines) + 1)]

    for tag, i1, i2, j1, j2 in opcodes_b:
        if tag == 'equal':
            continue
        elif tag == 'insert':
            insert_b[i1].extend(b_lines[j1:j2])
        elif tag == 'delete':
            for i in range(i1, i2):
                status_b[i] = 'delete'
        elif tag == 'replace':
            status_b[i1] = 'replace'
            replace_b[i1] = b_lines[j1:j2]
            for i in range(i1 + 1, i2):
                status_b[i] = 'delete'

    merged_lines = []
    conflict_detected = False

    for i in range(len(base_lines) + 1):
        # 1. Merge insertions before line i
        ins_a = insert_a[i]
        ins_b = insert_b[i]
        if ins_a and ins_b:
            if ins_a == ins_b:
                merged_lines.extend(ins_a)
            else:
                conflict_detected = True
                merged_lines.append(f"<<<<<<< {label_a}\n")
                merged_lines.extend(ins_a)
                merged_lines.append("=======\n")
                merged_lines.extend(ins_b)
                merged_lines.append(f">>>>>>> {label_b}\n")
        elif ins_a:
            merged_lines.extend(ins_a)
        elif ins_b:
            merged_lines.extend(ins_b)

        if i >= len(base_lines):
            break

        # 2. Merge line i
        sa = status_a[i]
        sb = status_b[i]

        if sa == 'equal' and sb == 'equal':
            merged_lines.append(base_lines[i])
        elif sa == 'delete' and sb == 'delete':
            pass
        elif sa == 'equal' and sb == 'delete':
            pass
        elif sa == 'delete' and sb == 'equal':
            pass
        elif sa == 'equal' and sb == 'replace':
            merged_lines.extend(replace_b[i])
        elif sa == 'replace' and sb == 'equal':
            merged_lines.extend(replace_a[i])
        else:
            # Conflict: both replaced or delete vs replace
            # Check if both replacements are identical
            if sa == 'replace' and sb == 'replace' and replace_a[i] == replace_b[i]:
                merged_lines.extend(replace_a[i])
            else:
                conflict_detected = True
                merged_lines.append(f"<<<<<<< {label_a}\n")
                if sa == 'replace':
                    merged_lines.extend(replace_a[i])
                elif sa == 'equal':
                    merged_lines.append(base_lines[i])
                merged_lines.append("=======\n")
                if sb == 'replace':
                    merged_lines.extend(replace_b[i])
                elif sb == 'equal':
                    merged_lines.append(base_lines[i])
                merged_lines.append(f">>>>>>> {label_b}\n")

    return "".join(merged_lines), conflict_detected
