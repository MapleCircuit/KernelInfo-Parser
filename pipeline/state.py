"""Process-wide parser state: the DB/table-engine globals, master file, GreatProcessor and logger.

Importing this module runs the import-time setup that `main.py` used to run itself."""
import sys
from core.globalstuff import (
    G,
    T_DIR,
    T_RAW,
    T_MAINTAINERS,
    T_CREDITS,
    configure_logging,
)

import logging
import multiprocessing
from core.FileHandler import MasterFile
from core.GreatProcessor import GreatProcessor
from db_engine import (
    get_db_engine,
)
from table_engine import TECachedDB
from core.DBLayout import (
    init_db_layout,
)



sys.setrecursionlimit(50000)

G.DB = get_db_engine()  # default engine class; replaced by the configured engine in main()
G.TE = TECachedDB()
MF = MasterFile()
G.MF = MF
gp = GreatProcessor()
configure_logging(level=logging.INFO, fmt="%(asctime)s - %(levelname)s - %(message)s")
logger = logging.getLogger("main")  # the name `main.py` logged under, kept for assertLogs("main")

# Workers rely on fork inheritance (gp, MF, TableEngine caches, shared memory symbols), so pin the start
# method instead of following the platform default (forkserver from Python 3.14).
_MP = multiprocessing.get_context("fork")

init_db_layout(gp)

T_NO_FOREIGN = frozenset({T_RAW, T_DIR, T_MAINTAINERS, T_CREDITS})
