import os
import tempfile

# 必须在导入 app.main 之前设置，避免模块级应用在仓库里建库文件。
os.environ.setdefault(
    "NEWSPAPER_DB", os.path.join(tempfile.mkdtemp(prefix="newspaper-tests-"), "checker.db")
)
