# Used by run.bat: prints this Python's full path if it is version 3.10 or newer.
import sys

if sys.version_info >= (3, 10):
    print(sys.executable)
