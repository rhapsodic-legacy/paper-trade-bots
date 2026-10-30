import warnings

# Python 3.13 warns about SQLite connections left for the garbage collector; harmless in tests.
warnings.filterwarnings("ignore", category=ResourceWarning)
