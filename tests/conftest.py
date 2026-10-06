"""测试夹具：在导入应用前把 SQLite 指到临时目录，避免污染工作区。"""

import os
import tempfile

_TMP = tempfile.mkdtemp(prefix="tempo-test-")
os.environ.setdefault("TEMPO_DB_PATH", os.path.join(_TMP, "test.db"))
