"""lifecollect —— 数字生命档案采集器。

第一版目标：把散落的材料收拢成一套不依赖任何软件、二十年后仍可读的档案。

对应标准：`00-标准/档案标准-v0.1.md`（在仓库根目录下）
"""

from .archive import LIFECOL_VERSION, STANDARD_VERSION  # noqa: F401

__version__ = LIFECOL_VERSION
__all__ = ["LIFECOL_VERSION", "STANDARD_VERSION", "__version__"]
