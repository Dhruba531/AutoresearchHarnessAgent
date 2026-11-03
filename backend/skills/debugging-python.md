---
name: debugging-python
description: Recover quickly from a failed sandbox run — read the traceback, shrink the input, fix one thing, re-run.
---
# Debugging a failed run

1. **Read the last frame of the traceback first** — the exception type and the line in *your*
   file. Library frames above it are usually noise.
2. **Classify the failure:**
   - `ModuleNotFoundError` → the package is not installed; there are no installs. Guard the
     import and fall back to the standard library.
   - `FileNotFoundError` → list files; uploads sit in the working directory root.
   - shape / dtype errors → print `.shape` and `.dtype` right before the failing line.
   - `MemoryError` / killed → subsample the data or process it in chunks.
   - timeout → time a 1% slice, extrapolate, cut epochs/estimators/data accordingly.
3. **Shrink before you fix:** reproduce on the first 200 rows so each attempt is fast.
4. **Change one thing per attempt** and say what you changed in the `reason` field.
5. **After two failed fixes of the same error, change approach** (simpler model, different
   library, pure Python) rather than retrying the same idea.
